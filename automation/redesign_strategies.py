"""
R6 Strategy Redesign Module (EarlyMomentumBreakout, PullbackReclaimBreakout, FAST v2).
Pure mathematical models and candidate generators with fee/slippage awareness.
Diagnostic/Research/Shadow compatible; never bypasses risk or authorization guards.
"""
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Dict, List, Optional, Tuple, Union

from core.util import floor_tick, tick_size
from automation.decisions import gate


def ceil_to_tick(price: float) -> int:
    ts = tick_size(int(price))
    return int(math.ceil(price / ts) * ts)


def floor_to_tick(price: float) -> int:
    ts = tick_size(int(price))
    return int(math.floor(price / ts) * ts)


def extract_costs(costs: Any) -> Tuple[float, float, float, float, float]:
    """Extracts (buy, sell, tax, entry_slippage, exit_slippage) from Costs dataclass, dict, or None."""
    if costs is None:
        return 0.00015, 0.00015, 0.0018, 0.002, 0.003
    if isinstance(costs, dict):
        return (
            costs.get('buy', 0.00015),
            costs.get('sell', 0.00015),
            costs.get('tax', 0.0018),
            costs.get('entry_slippage', 0.002),
            costs.get('exit_slippage', 0.003),
        )
    return (
        getattr(costs, 'buy', 0.00015),
        getattr(costs, 'sell', 0.00015),
        getattr(costs, 'tax', 0.0018),
        getattr(costs, 'entry_slippage', 0.002),
        getattr(costs, 'exit_slippage', 0.003),
    )


def calculate_net_proceeds_and_cost(entry: float, target: float, stop: float, costs: Any):
    buy_fee, sell_fee, tax, entry_slip, exit_slip = extract_costs(costs)

    cost_in = entry * (1.0 + entry_slip) * (1.0 + buy_fee)
    net_retention = (1.0 - exit_slip) * (1.0 - sell_fee - tax)

    reward_net = (target * net_retention) - cost_in if target else None
    risk_net = cost_in - (stop * net_retention) if stop else None
    net_rr = reward_net / risk_net if reward_net is not None and risk_net and risk_net > 0 else None
    breakeven_price = ceil_to_tick(cost_in / net_retention)
    return {
        'cost_in': cost_in,
        'net_retention': net_retention,
        'reward_net': reward_net,
        'risk_net': risk_net,
        'net_rr': net_rr,
        'breakeven_price': breakeven_price
    }


# =========================================================================
# 1. EarlyMomentumBreakout (Candidate Setup A)
# =========================================================================
def evaluate_early_momentum_breakout(bars: list, t, watch: dict, cfg, auto, costs: dict) -> Tuple[Optional[dict], dict]:
    """
    Early momentum breakout using base high, real-time bid support, volume acceleration,
    and executable entry limit (ask1 + 1 tick) with strict net RR >= 1.40.
    """
    if len(bars) < 2:
        return None, {'reason': 'INSUFFICIENT_USABLE_BARS', 'pass': False}

    recent_bars = bars[-4:]
    h_base = max(b['high'] for b in recent_bars)
    l_base = min(b['low'] for b in recent_bars)
    base_spread = (h_base - l_base) / h_base if h_base > 0 else 1.0

    if base_spread > 0.035:
        return None, {'reason': 'BASE_SPREAD_TOO_WIDE', 'pass': False, 'base_spread': base_spread}

    # Breakout condition: last price > h_base and bid >= h_base
    if t.price <= h_base or t.bid < h_base:
        return None, {'reason': 'BASE_HIGH_NOT_BROKEN', 'pass': False, 'h_base': h_base, 'price': t.price}

    # Chase limit: ask <= h_base + 0.8% (or max 4 ticks)
    max_chase_price = floor_tick(h_base * 1.008)
    if t.ask > max_chase_price:
        return None, {'reason': 'MAX_CHASE_EXCEEDED', 'pass': False, 'ask': t.ask, 'max_chase': max_chase_price}

    # Volume acceleration check
    prior_bars = bars[:-1] if len(bars) > 1 else bars
    avg_vol = sum(b.get('volume', 0) for b in prior_bars[-5:]) / max(1, len(prior_bars[-5:]))
    last_bar_vol = bars[-1].get('volume', 0)
    has_vol_surge = (last_bar_vol >= 1.25 * avg_vol) or (t.volume >= avg_vol * 1.4)
    if not has_vol_surge:
        return None, {'reason': 'VOLUME_ACCELERATION_FAIL', 'pass': False}

    # Executable pricing with 1-tick tolerance
    ts = tick_size(t.ask)
    entry_limit = t.ask + ts
    delta_base = h_base - l_base

    # Invalidation stop: Base structural invalidation at l_base - ts
    struct_stop = floor_tick(l_base - ts)
    emerg_stop = min(struct_stop - ts, floor_tick(l_base - 2 * ts))

    if not (0 < emerg_stop < struct_stop < entry_limit):
        return None, {'reason': 'INVALID_GEOMETRY', 'pass': False}

    stop_dist = (entry_limit - emerg_stop) / entry_limit
    if not (0.0035 <= stop_dist <= 0.05):
        return None, {'reason': 'STOP_DISTANCE_OUT_OF_BOUNDS', 'pass': False, 'stop_dist': stop_dist}

    # Target calculation: realistic projection above h_base, strictly capped by nearest active resistance/VI
    overhead = watch.get('resistance')
    atr = max(ts * 3, statistics_atr(bars, 14))
    wave_target = max(floor_tick(h_base + 1.8 * delta_base), floor_tick(entry_limit + 3.0 * atr), floor_tick(entry_limit * 1.020))

    ceilings = []
    if overhead is not None and overhead > entry_limit:
        ceilings.append(floor_tick(overhead))
    if watch.get('vi_upper') and watch['vi_upper'] > entry_limit + 2 * ts:
        ceilings.append(floor_tick(watch['vi_upper'] - 2 * ts))

    if ceilings:
        physical_ceiling = min(ceilings)
        target = min(wave_target, physical_ceiling)
    else:
        # No explicit overhead or VI ceiling: cap by conservative ATR / horizon, never assume distant target
        conservative_cap = floor_tick(entry_limit + min(3.0 * atr, entry_limit * 0.035))
        target = min(wave_target, conservative_cap)

    calc_struct = calculate_net_proceeds_and_cost(entry_limit, target, struct_stop, costs)
    calc_emerg = calculate_net_proceeds_and_cost(entry_limit, target, emerg_stop, costs)
    min_rr = getattr(cfg, 'min_early_momentum_rr', 1.40)

    # Breakeven check: target must clear breakeven price + 2 ticks
    if target < calc_struct['breakeven_price'] + 2 * ts:
        return None, {'reason': 'TARGET_BELOW_BREAKEVEN', 'pass': False, 'target': target, 'be': calc_struct['breakeven_price']}

    if calc_struct['net_rr'] is None or calc_struct['net_rr'] < min_rr:
        return None, {'reason': 'NET_RR_LOW', 'pass': False, 'net_rr': calc_struct['net_rr'], 'min_rr': min_rr}

    if calc_emerg['net_rr'] is None or calc_emerg['net_rr'] < 1.0:
        return None, {'reason': 'EMERGENCY_RR_SUB_ONE', 'pass': False, 'net_rr_emergency': calc_emerg['net_rr'], 'min_emerg_rr': 1.0}

    signal = {
        'pattern': 'EarlyMomentumBreakout',
        'symbol': t.symbol,
        'limit': entry_limit,
        'stop': struct_stop,
        'emergency': emerg_stop,
        'target': target,
        'structural_stop': struct_stop,
        'emergency_stop': emerg_stop,
        'net_reward_risk': calc_struct['net_rr'],
        'net_rr_structural': calc_struct['net_rr'],
        'net_rr_emergency': calc_emerg['net_rr'],
        'risk_per_share_sized': calc_emerg['risk_net'],
        'time': t.time.isoformat(),
        'h_base': h_base,
        'l_base': l_base,
        'invalidation_policy': {
            'enabled': True,
            'policy_name': 'EARLY_MOMENTUM_BASE',
            'structural_stop': struct_stop,
            'emergency_stop': emerg_stop,
            'noise_band': max(ts, entry_limit - struct_stop)
        }
    }
    return signal, {'pass': True, 'signal': signal}


# =========================================================================
# 2. PullbackReclaimBreakout (Candidate Setup B)
# =========================================================================
def evaluate_pullback_reclaim_breakout(bars: list, t, watch: dict, cfg, auto, costs: dict) -> Tuple[Optional[dict], dict]:
    """
    Pullback reclaim breakout: prior impulse wave (>=2%), controlled pullback (25%-60%),
    volume dry-up (<=55%), and reclaim of prior bar high. Pre-validated net RR >= 1.50.
    """
    if len(bars) < 4:
        return None, {'reason': 'INSUFFICIENT_USABLE_BARS', 'pass': False}

    seq = bars[-12:]
    peak_idx = max(range(1, len(seq) - 1), key=lambda i: seq[i]['high'])
    impulse_bars = seq[:peak_idx + 1]
    pullback_bars = seq[peak_idx + 1:]

    if not pullback_bars:
        return None, {'reason': 'NO_PULLBACK_BARS', 'pass': False}

    h_impulse = seq[peak_idx]['high']
    l_impulse = min(b['low'] for b in impulse_bars)
    delta_impulse = h_impulse - l_impulse

    if delta_impulse < l_impulse * 0.020:
        return None, {'reason': 'IMPULSE_RISE_INSUFFICIENT', 'pass': False, 'rise_pct': delta_impulse / l_impulse}

    l_pullback = min(b['low'] for b in pullback_bars)
    retrace = (h_impulse - l_pullback) / delta_impulse if delta_impulse > 0 else 1.0

    if not (0.25 <= retrace <= 0.60):
        return None, {'reason': 'RETRACE_OUT_OF_BOUNDS', 'pass': False, 'retrace': retrace}

    # Volume dry-up
    peak_vol = seq[peak_idx].get('volume', 1)
    pullback_avg_vol = sum(b.get('volume', 0) for b in pullback_bars) / len(pullback_bars)
    if peak_vol > 0 and pullback_avg_vol > peak_vol * 0.55:
        return None, {'reason': 'PULLBACK_VOLUME_NOT_DRIED', 'pass': False, 'vol_ratio': pullback_avg_vol / peak_vol}

    # Local reclaim: price > prior bar high
    h_reclaim = pullback_bars[-1]['high']
    if t.price <= h_reclaim or t.ask <= h_reclaim:
        return None, {'reason': 'RECLAIM_HIGH_NOT_BROKEN', 'pass': False, 'h_reclaim': h_reclaim, 'price': t.price}

    ts = tick_size(t.ask)
    if t.ask > h_reclaim + 3 * ts:
        return None, {'reason': 'RECLAIM_CHASE_EXCEEDED', 'pass': False, 'ask': t.ask}

    entry_limit = t.ask + ts
    struct_stop = floor_tick(l_pullback - ts)
    emerg_stop = min(struct_stop - 2 * ts, floor_tick(struct_stop * 0.996))

    if not (0 < emerg_stop < struct_stop < entry_limit):
        return None, {'reason': 'INVALID_GEOMETRY', 'pass': False}

    overhead = watch.get('resistance')
    atr = max(ts * 3, statistics_atr(bars, 14))
    target_candidate = max(floor_tick(h_impulse + 0.382 * delta_impulse), floor_tick(entry_limit + 3.0 * atr), floor_tick(entry_limit * 1.020))

    ceilings = []
    if overhead is not None and overhead > entry_limit:
        ceilings.append(floor_tick(overhead))
    if watch.get('vi_upper') and watch['vi_upper'] > entry_limit + 2 * ts:
        ceilings.append(floor_tick(watch['vi_upper'] - 2 * ts))

    if ceilings:
        physical_ceiling = min(ceilings)
        target = min(target_candidate, physical_ceiling)
    else:
        # No explicit overhead or VI ceiling: cap by conservative ATR / horizon, never assume distant target
        conservative_cap = floor_tick(entry_limit + min(3.0 * atr, entry_limit * 0.035))
        target = min(target_candidate, conservative_cap)

    calc_struct = calculate_net_proceeds_and_cost(entry_limit, target, struct_stop, costs)
    calc_emerg = calculate_net_proceeds_and_cost(entry_limit, target, emerg_stop, costs)
    min_rr = getattr(cfg, 'min_pullback_reclaim_rr', 1.50)

    # First milestone: impulse peak must exceed breakeven price
    if h_impulse <= calc_struct['breakeven_price'] + ts:
        return None, {'reason': 'IMPULSE_PEAK_BELOW_BREAKEVEN', 'pass': False, 'h_impulse': h_impulse, 'be': calc_struct['breakeven_price']}

    # Breakeven check: target must clear breakeven price + 2 ticks
    if target < calc_struct['breakeven_price'] + 2 * ts:
        return None, {'reason': 'TARGET_BELOW_BREAKEVEN', 'pass': False, 'target': target, 'be': calc_struct['breakeven_price']}

    if calc_struct['net_rr'] is None or calc_struct['net_rr'] < min_rr:
        return None, {'reason': 'NET_RR_LOW', 'pass': False, 'net_rr': calc_struct['net_rr'], 'min_rr': min_rr}

    if calc_emerg['net_rr'] is None or calc_emerg['net_rr'] < 1.0:
        return None, {'reason': 'EMERGENCY_RR_SUB_ONE', 'pass': False, 'net_rr_emergency': calc_emerg['net_rr'], 'min_emerg_rr': 1.0}

    signal = {
        'pattern': 'PullbackReclaimBreakout',
        'symbol': t.symbol,
        'limit': entry_limit,
        'stop': struct_stop,
        'emergency': emerg_stop,
        'target': target,
        'structural_stop': struct_stop,
        'emergency_stop': emerg_stop,
        'net_reward_risk': calc_struct['net_rr'],
        'net_rr_structural': calc_struct['net_rr'],
        'net_rr_emergency': calc_emerg['net_rr'],
        'risk_per_share_sized': calc_emerg['risk_net'],
        'time': t.time.isoformat(),
        'h_impulse': h_impulse,
        'l_pullback': l_pullback,
        'h_reclaim': h_reclaim,
        'invalidation_policy': {
            'enabled': True,
            'policy_name': 'PULLBACK_RECLAIM_STRUCTURE',
            'structural_stop': struct_stop,
            'emergency_stop': emerg_stop,
            'noise_band': max(2 * ts, 0.25 * (h_reclaim - l_pullback))
        }
    }
    return signal, {'pass': True, 'signal': signal}


# =========================================================================
# 3. FAST v2: compute_fast_viable_horizon
# =========================================================================
@dataclass
class FastHorizonResult:
    entry_limit: int
    structural_stop: int
    emergency_stop: int
    breakeven_price: int
    min_reward_price: int
    realizable_target: int
    net_reward_per_share: float
    net_risk_per_share: float
    net_rr: float
    is_viable: bool
    reject_reason: Optional[str]


def compute_fast_viable_horizon(
    entry_limit: int,
    structural_stop: int,
    emergency_stop: int,
    atr_1m: float,
    micro_impulse_span: int,
    nearest_intraday_res: Optional[int],
    daily_res_cap: Optional[int],
    vi_upper_price: Optional[int],
    mtf_5m_context: str = "BULLISH",
    costs: Optional[dict] = None,
    min_net_rr: float = 1.50,
) -> FastHorizonResult:
    """
    FAST v2 realizable horizon calculation.
    Prevents distant daily resistance (+35% in Lunit) from inflating RR.
    Caps target by micro wave projection and checks breakeven reachability.
    """
    buy_fee, sell_fee, tax, entry_slip, exit_slip = extract_costs(costs)
    ts = tick_size(entry_limit)

    if mtf_5m_context == "BEARISH":
        return FastHorizonResult(entry_limit, structural_stop, emergency_stop, 0, 0, 0, 0.0, 0.0, 0.0, False, "FAST_MTF_5M_BEARISH_BLOCKED")

    effective_min_rr = min_net_rr if mtf_5m_context == "BULLISH" else (min_net_rr + 0.30)

    cost_in = entry_limit * (1.0 + entry_slip) * (1.0 + buy_fee)
    net_retention = (1.0 - exit_slip) * (1.0 - sell_fee - tax)
    breakeven_price = ceil_to_tick(cost_in / net_retention)

    stop_proceeds = emergency_stop * net_retention
    net_risk = max(float(ts), cost_in - stop_proceeds)
    min_reward_price = ceil_to_tick((cost_in + effective_min_rr * net_risk) / net_retention)

    wave_span = max(int(round(3.5 * atr_1m)), int(round(1.8 * micro_impulse_span)))
    wave_target = min(
        floor_to_tick(entry_limit + wave_span),
        floor_to_tick(entry_limit * 1.035)  # Max +3.5% micro wave cap
    )

    ceilings = [c for c in [nearest_intraday_res, daily_res_cap] if c is not None and c > entry_limit]
    if vi_upper_price is not None and vi_upper_price > entry_limit + 2 * ts:
        ceilings.append(vi_upper_price - 2 * ts)

    physical_ceiling = min(ceilings) if ceilings else wave_target
    realizable_target = min(wave_target, physical_ceiling)

    net_reward = (realizable_target * net_retention) - cost_in
    net_rr = net_reward / net_risk if net_risk > 0 else 0.0

    if realizable_target < min_reward_price or (realizable_target - breakeven_price) < 3 * ts:
        return FastHorizonResult(
            entry_limit, structural_stop, emergency_stop, breakeven_price,
            min_reward_price, realizable_target, net_reward, net_risk, net_rr,
            False, "FAST_REALIZABLE_HORIZON_BELOW_MIN_RR"
        )

    return FastHorizonResult(
        entry_limit, structural_stop, emergency_stop, breakeven_price,
        min_reward_price, realizable_target, net_reward, net_risk, net_rr,
        True, None
    )


# =========================================================================
# 4. ConditionalFastExitEvaluator (Policies A, B, C)
# =========================================================================
class ConditionalFastExitEvaluator:
    """
    Unified evaluator comparing:
    - Policy A: Baseline micro-momentum exit (45s, 2-tick swing filter)
    - Policy B: Confirmed-bar structural trailing exit
    - Policy C: Conditional 2-stage hybrid exit (Phase 1 holds noise, Phase 2 locks floor)
    """
    def __init__(self, policy: str = "POLICY_C", time_stop_seconds: float = 300.0):
        self.policy = policy
        self.time_stop_seconds = time_stop_seconds

    def evaluate(
        self,
        position: dict,
        current_tick,
        decision_time: datetime,
        recent_trades: list,
        confirmed_1m_lows: Optional[list] = None,
        vwap_day: Optional[float] = None
    ) -> Tuple[bool, str, dict]:
        entry = position['avg']
        struct_stop = position.get('stop', position['initial_stop'])
        emerg_stop = position['emergency']
        opened = datetime.fromisoformat(position['opened']) if isinstance(position.get('opened'), str) else decision_time
        if opened.tzinfo is not None and decision_time.tzinfo is None:
            decision_time = decision_time.replace(tzinfo=opened.tzinfo)
        elif opened.tzinfo is None and decision_time.tzinfo is not None:
            opened = opened.replace(tzinfo=decision_time.tzinfo)
        holding_seconds = (decision_time - opened).total_seconds()
        peak = position.get('peak_price', entry)
        ts = tick_size(entry)

        # 0. Emergency Stop: Immediate for all policies
        if current_tick.bid <= emerg_stop or (recent_trades and recent_trades[-1] <= emerg_stop):
            return True, 'EMERGENCY_PLUNGE_EXIT', {'price': emerg_stop}

        # Policy A: Baseline
        if self.policy == "POLICY_A":
            # 2 consecutive trades below structural stop
            if len(recent_trades) >= 2 and all(p <= struct_stop for p in recent_trades[-2:]):
                return True, 'INVALIDATION_STOP_EXIT', {'price': struct_stop}
            # Micro-momentum failure (>=45s, lower rebound high >= 2 ticks)
            if holding_seconds >= 45.0 and len(recent_trades) >= 4:
                # Standard micro-momentum failure check
                trough = min(recent_trades[-4:-1])
                rebound = max(recent_trades[-3:-1])
                if rebound >= trough + 2 * ts and recent_trades[-1] <= trough:
                    return True, 'MOMENTUM_FAILURE_EXIT', {'floor': trough, 'failed_rebound_high': rebound}
            return False, 'HOLD', {}

        # Policy B: Confirmed Bar Structural Only
        elif self.policy == "POLICY_B":
            if len(recent_trades) >= 2 and all(p <= struct_stop for p in recent_trades[-2:]):
                return True, 'STRUCTURAL_STOP_2TICK_BREACH', {'price': struct_stop}
            if confirmed_1m_lows and len(confirmed_1m_lows) >= 2:
                bar_trail = min(confirmed_1m_lows[-2:]) - ts
                if current_tick.bid <= bar_trail:
                    return True, 'CONFIRMED_BAR_TRAIL_EXIT', {'trail': bar_trail}
            return False, 'HOLD', {}

        # Policy C: Conditional 2-Stage Hybrid
        else:
            if len(recent_trades) >= 2 and all(p <= struct_stop for p in recent_trades[-2:]):
                return True, 'STRUCTURAL_STOP_2TICK_BREACH', {'price': struct_stop}

            be_price = position.get('breakeven_price', entry + ts)
            arm_threshold = max(be_price + 2 * ts, entry + 2 * ts)
            is_profit_armed = (peak >= arm_threshold)

            # Phase 1: Pre-breakeven
            if not is_profit_armed:
                # Time-stop: Evaluated only after time_stop_seconds (e.g. 300s) without progression
                if holding_seconds >= self.time_stop_seconds and peak <= entry and (vwap_day and current_tick.bid <= vwap_day):
                    return True, 'FAST_TIME_STOP_NO_EXPANSION', {'holding_seconds': holding_seconds}
                return False, 'HOLD', {'phase': 1, 'reason': 'PRE_PROFIT_STRUCTURAL_DEFENSE'}

            # Phase 2: Post-breakeven Profit Protection
            profit_floor = be_price + ts
            if confirmed_1m_lows and len(confirmed_1m_lows) >= 2:
                bar_trail = min(confirmed_1m_lows[-2:]) - ts
                profit_floor = max(profit_floor, bar_trail)

            if len(recent_trades) >= 2 and all(p <= profit_floor for p in recent_trades[-2:]):
                return True, 'FAST_PROFIT_FLOOR_OR_BAR_TRAIL_EXIT', {'floor': profit_floor}

            mfe = peak - entry
            allowed_pullback = max(4 * ts, int(round(0.45 * mfe)))
            dynamic_swing_stop = peak - allowed_pullback
            if len(recent_trades) >= 2 and all(p <= dynamic_swing_stop for p in recent_trades[-2:]):
                return True, 'FAST_SCALED_MFE_DRAWDOWN_EXIT', {'swing_stop': dynamic_swing_stop}

            return False, 'HOLD', {'phase': 2, 'profit_floor': profit_floor}


def true_range_atr(bars: list, period: int = 14) -> float:
    """
    Computes standard Wilder True Range (TR) average over the given bar window:
    TR = max(High - Low, |High - Prior Close|, |Low - Prior Close|)
    """
    if not bars:
        return 10.0
    if len(bars) == 1:
        return max(1.0, float(bars[0]['high'] - bars[0]['low']))

    tr_list = []
    sub = bars[-(period + 1):]
    for i in range(1, len(sub)):
        h = float(sub[i]['high'])
        l = float(sub[i]['low'])
        prev_c = float(sub[i - 1]['close'])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

    if not tr_list:
        return max(1.0, float(bars[-1]['high'] - bars[-1]['low']))
    return sum(tr_list) / len(tr_list)


statistics_atr = true_range_atr
