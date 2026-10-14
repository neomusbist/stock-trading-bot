"""
Fast and Early Momentum Entry Strategy (R6)
Captures early momentum entries without requiring 2-5 completed 1-minute bars.

Design & Architectural Boundaries:
- Works directly on streaming real-time ticks, order books, and micro-windows (10s ~ 60s).
- Evaluates 4 distinct market regimes:
    A: EARLY_MOMENTUM (Aggressive early buying flow, tight spread, Net RR >= 1.5) -> ACCEPT
    B: EXTENDED_IMPULSE_CHASE (Late-stage exhaustion, over-extended, chase > 0.3%) -> REJECT
    C: FALSE_BREAKOUT_QUOTE_SPOOF (Low volume micro-spike, wide spread, quote trap) -> REJECT
    D: DOWNTREND_COUNTER_BOUNCE (Dead-cat bounce within intraday bear trend) -> REJECT
- Dedicated opening phases:
    09:00~09:02 (OPENING_EARLY): Tick/depth flow, no 1-min completed bar requirement.
    09:02~09:05 (OPENING_BUILD): Micro-range + 1st bar low protection.
    09:05~09:15 (OPENING_STABILIZE): Smooth transition to regular tape.
    09:15+      (REGULAR): Standard intraday early momentum.
- Signal TTL (15s) and strict chase protection (MAX_CHASE_EXCEEDED).
- 3-tier exit structure:
    1. Invalidation Stop (micro-swing base)
    2. Account Risk Limit (max 2% per trade)
    3. Emergency Plunge Stop (instant market exit)
    + Momentum Stall Exit (early scratch if momentum stalls within 60~120s)
    + Break-even lock at +1R.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from collections import deque
import math

from core.util import floor_tick, tick_size
from automation.session import supported, after_supported, KST

@dataclass
class FastSignal:
    symbol: str
    name: str
    limit: float
    stop: float
    target: float
    emergency: float
    quantity: int
    net_reward_risk: float
    gross_reward_risk: float
    reward_net_per_share: float
    risk_net_per_share: float
    regime: str
    phase: str
    created_at: datetime
    ttl_seconds: float = 15.0
    max_chase_fraction: float = 0.003
    max_chase_price: float = 0.0
    reason: str = "EARLY_MOMENTUM_ENTRY"
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.max_chase_price:
            self.max_chase_price = self.limit * (1.0 + self.max_chase_fraction)

    def is_expired(self, current_time: datetime, current_ask: float) -> Tuple[bool, str]:
        elapsed = (current_time - self.created_at).total_seconds()
        if elapsed > self.ttl_seconds:
            return True, f"SIGNAL_TTL_EXPIRED ({elapsed:.1f}s > {self.ttl_seconds}s)"
        if current_ask > self.max_chase_price:
            return True, f"MAX_CHASE_EXCEEDED (ask {current_ask:.0f} > max {self.max_chase_price:.0f})"
        return False, ""


class MicroTape:
    """Tracks a rolling window of ticks (up to 120 seconds) for a single symbol."""
    def __init__(self, max_seconds: float = 120.0):
        self.max_seconds = max_seconds
        self.ticks = deque()  # (timestamp, price, volume, is_uptick, bid, ask)

    def ingest(self, t) -> None:
        last_price = self.ticks[-1][1] if self.ticks else t.price
        is_uptick = t.price >= last_price
        bid = getattr(t, 'bid', 0.0) or t.price
        ask = getattr(t, 'ask', 0.0) or t.price
        self.ticks.append((t.time, t.price, t.volume, is_uptick, bid, ask))
        self._prune(t.time)

    def _prune(self, current_time: datetime) -> None:
        cutoff = current_time - timedelta(seconds=self.max_seconds)
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft()

    def metrics(self, current_time: datetime) -> Dict[str, Any]:
        self._prune(current_time)
        if not self.ticks:
            return {}

        now_t = current_time
        t10 = now_t - timedelta(seconds=10)
        t30 = now_t - timedelta(seconds=30)
        t60 = now_t - timedelta(seconds=60)

        vol_10, to_10, ticks_10 = 0, 0.0, 0
        vol_30, to_30, ticks_30 = 0, 0.0, 0
        vol_60, to_60, ticks_60 = 0, 0.0, 0
        uptick_vol_30 = 0
        downtick_vol_30 = 0
        uptick_vol_60 = 0
        downtick_vol_60 = 0

        p_min_30 = float('inf')
        p_max_30 = float('-inf')
        p_min_60 = float('inf')
        p_max_60 = float('-inf')

        vwap_num_30 = 0.0
        last_bid, last_ask = self.ticks[-1][4], self.ticks[-1][5]

        for stamp, p, v, is_up, b, a in self.ticks:
            to = p * v
            if stamp >= t10:
                vol_10 += v
                to_10 += to
                ticks_10 += 1
            if stamp >= t30:
                vol_30 += v
                to_30 += to
                ticks_30 += 1
                vwap_num_30 += to
                if is_up:
                    uptick_vol_30 += v
                else:
                    downtick_vol_30 += v
                if p < p_min_30: p_min_30 = p
                if p > p_max_30: p_max_30 = p
            if stamp >= t60:
                vol_60 += v
                to_60 += to
                ticks_60 += 1
                if is_up:
                    uptick_vol_60 += v
                else:
                    downtick_vol_60 += v
                if p < p_min_60: p_min_60 = p
                if p > p_max_60: p_max_60 = p

        tot_vol_30 = uptick_vol_30 + downtick_vol_30
        uptick_ratio_30 = uptick_vol_30 / tot_vol_30 if tot_vol_30 > 0 else 0.5

        tot_vol_60 = uptick_vol_60 + downtick_vol_60
        uptick_ratio_60 = uptick_vol_60 / tot_vol_60 if tot_vol_60 > 0 else 0.5

        vwap_30 = vwap_num_30 / vol_30 if vol_30 > 0 else self.ticks[-1][1]
        spread = (last_ask - last_bid) / last_bid if (last_bid > 0 and last_ask >= last_bid) else 0.0

        return {
            'ticks_count': len(self.ticks),
            'ticks_10': ticks_10,
            'turnover_10': to_10,
            'volume_10': vol_10,
            'ticks_30': ticks_30,
            'turnover_30': to_30,
            'volume_30': vol_30,
            'ticks_60': ticks_60,
            'turnover_60': to_60,
            'volume_60': vol_60,
            'uptick_ratio_30': uptick_ratio_30,
            'uptick_ratio_60': uptick_ratio_60,
            'downtick_ratio_30': 1.0 - uptick_ratio_30,
            'p_min_30': p_min_30 if p_min_30 != float('inf') else self.ticks[-1][1],
            'p_max_30': p_max_30 if p_max_30 != float('-inf') else self.ticks[-1][1],
            'p_min_60': p_min_60 if p_min_60 != float('inf') else self.ticks[-1][1],
            'p_max_60': p_max_60 if p_max_60 != float('-inf') else self.ticks[-1][1],
            'vwap_30': vwap_30,
            'spread': spread,
            'last_bid': last_bid,
            'last_ask': last_ask,
            'last_price': self.ticks[-1][1],
            'latest_time': self.ticks[-1][0],
        }


class FastMomentumStrategy:
    """
    R6 Fast & Early Momentum Entry Strategy.
    Integrates micro-tape dynamics, 4-regime classification, opening phase gates,
    strict Net RR >= 1.5, TTL management, and 3-tier exit logic.
    """
    def __init__(self, cfg=None, auto=None, costs=None):
        self.cfg = cfg
        self.auto = auto
        if costs is None:
            self.costs = {'buy': 0.00015, 'sell': 0.00015, 'tax': 0.002, 'entry_slippage': 0.002, 'exit_slippage': 0.003}
        elif isinstance(costs, dict):
            self.costs = dict(costs)
        else:
            self.costs = {
                'buy': getattr(costs, 'buy', 0.00015),
                'sell': getattr(costs, 'sell', 0.00015),
                'tax': getattr(costs, 'tax', 0.0020),
                'entry_slippage': getattr(costs, 'entry_slippage', 0.0020),
                'exit_slippage': getattr(costs, 'exit_slippage', 0.0030),
            }
        self.tapes: Dict[str, MicroTape] = {}
        self.active_signals: Dict[str, FastSignal] = {}
        self.last_exit_by_symbol: Dict[str, datetime] = {}
        self.cooldown_seconds: float = 900.0  # 15 minutes cooldown after exit

    def get_tape(self, symbol: str) -> MicroTape:
        if symbol not in self.tapes:
            self.tapes[symbol] = MicroTape(max_seconds=120.0)
        return self.tapes[symbol]

    def ingest_tick(self, t) -> None:
        self.get_tape(t.symbol).ingest(t)

    def determine_phase(self, stamp: datetime) -> str:
        s = stamp.strftime('%H:%M:%S')
        if '09:00:00' <= s < '09:02:00':
            return 'OPENING_EARLY'
        elif '09:02:00' <= s < '09:05:00':
            return 'OPENING_BUILD'
        elif '09:05:00' <= s < '09:15:00':
            return 'OPENING_STABILIZE'
        elif '09:15:00' <= s < '15:10:00':
            return 'REGULAR'
        elif '15:40:00' <= s < '20:00:00':
            return 'AFTER'
        return 'OFF_SESSION'

    def classify_regime(
        self,
        t,
        m: Dict[str, Any],
        phase: str,
        watch: Dict[str, Any],
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Classifies current micro-price-action into Regimes A, B, C, or D.
        Returns: (regime_code, reason_code, details)
        """
        p = t.price
        bid = m.get('last_bid', p)
        ask = m.get('last_ask', p)
        spread = m.get('spread', 0.0)

        # 1. Regime C Check: False breakout / Quote spoof / Illiquid spike
        max_allowed_spread = 0.0040 if phase == 'OPENING_EARLY' else 0.0035
        if spread > max_allowed_spread:
            return 'C', 'FALSE_BREAKOUT_WIDE_SPREAD', {'spread': spread, 'max_spread': max_allowed_spread}

        min_ticks = 4 if phase == 'OPENING_EARLY' else 6
        if m.get('ticks_10', 0) < min_ticks:
            return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TICK_VELOCITY', {'ticks_10': m.get('ticks_10', 0), 'min_ticks': min_ticks}

        min_turnover_10s = 3_000_000.0 if phase == 'OPENING_EARLY' else 5_000_000.0
        if m.get('turnover_10', 0.0) < min_turnover_10s:
            return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER', {'turnover_10': m.get('turnover_10', 0.0), 'min_turnover': min_turnover_10s}

        # 2. Regime D Check: Downtrend counter-bounce
        ref_price = watch.get('reference_price') or watch.get('first_raw_price')
        if ref_price and ref_price > 0:
            if p < ref_price * 0.985 and m.get('p_max_60', p) < ref_price * 0.990:
                # Stock is trading significantly below opening/reference price with weak bounces
                return 'D', 'DOWNTREND_COUNTER_BOUNCE', {'price': p, 'ref_price': ref_price}

        # 3. Regime B Check: Extended impulse chase (e.g. Pan Ocean late chase)
        # If price has already run up too far from micro-low within 60s without consolidation:
        p_min_60 = m.get('p_min_60', p)
        if p_min_60 > 0:
            impulse_extension = (p - p_min_60) / p_min_60
            max_extension = 0.022 if phase == 'OPENING_EARLY' else 0.020  # Max 2.0% - 2.2% from micro-low
            if impulse_extension > max_extension:
                return 'B', 'EXTENDED_IMPULSE_CHASE_OVEREXTENDED', {
                    'extension': impulse_extension,
                    'max_extension': max_extension,
                    'p_min_60': p_min_60,
                    'price': p
                }

        # If buying pressure is already exhausted (uptick volume ratio falling below 55% at high):
        if m.get('uptick_ratio_30', 0.5) < 0.55:
            return 'B', 'EXTENDED_IMPULSE_MOMENTUM_EXHAUSTION', {
                'uptick_ratio_30': m.get('uptick_ratio_30', 0.5),
                'required': 0.55
            }

        # 4. Regime A Check: Early Momentum
        min_uptick_ratio = 0.62 if phase == 'OPENING_EARLY' else 0.65
        if m.get('uptick_ratio_30', 0.5) < min_uptick_ratio:
            return 'C', 'INSUFFICIENT_UPTICK_DOMINANCE', {
                'uptick_ratio_30': m.get('uptick_ratio_30', 0.5),
                'required': min_uptick_ratio
            }

        # Micro-breakout: price must be at or within 1 tick of the 30s high
        p_max_30 = m.get('p_max_30', p)
        ts = tick_size(p)
        if p < (p_max_30 - ts):
            return 'C', 'MICRO_BREAKOUT_NOT_AT_PEAK', {'price': p, 'p_max_30': p_max_30}

        return 'A', 'EARLY_MOMENTUM_CONFIRMED', {}

    def calculate_invalidation_and_target(
        self,
        t,
        m: Dict[str, Any],
        watch: Dict[str, Any],
        capital_limit: float = 81300.0,
        risk_budget: float = 1626.0  # 2% of 81,300
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates stop (invalidation), emergency line, realistic target, Net RR, and position quantity.
        Enforces strict Net RR >= 1.5 including commissions, taxes, and slippage.
        """
        p = t.price
        entry_price = getattr(t, 'ask', 0.0) or p
        ts = tick_size(p)

        # 1. Invalidation Stop: Just below the 30s micro-low (or VWAP)
        micro_low = m.get('p_min_30', p)
        structural_stop = floor_tick(micro_low - ts)

        # Bounds on risk distance: minimum 0.7%, maximum 2.0%
        risk_dist = entry_price - structural_stop
        if risk_dist <= 0 or (risk_dist / entry_price) < 0.007:
            structural_stop = floor_tick(entry_price * (1.0 - 0.009))  # Default 0.9% risk
            risk_dist = entry_price - structural_stop

        if (risk_dist / entry_price) > 0.022:
            structural_stop = floor_tick(entry_price * (1.0 - 0.018))  # Cap risk at 1.8%
            risk_dist = entry_price - structural_stop

        emergency_stop = floor_tick(structural_stop - max(2 * ts, structural_stop * 0.005))

        # 2. Cost parameters and net risk
        comm_buy = self.costs.get('buy', 0.00015)
        comm_sell = self.costs.get('sell', 0.00015)
        tax = self.costs.get('tax', 0.0020)
        entry_slip = self.costs.get('entry_slippage', 0.0020)
        exit_slip = self.costs.get('exit_slippage', 0.0030)
        cost_buy = comm_buy + entry_slip
        cost_sell = comm_sell + tax + exit_slip

        risk_net_per_share = (
            entry_price - structural_stop
            + entry_price * comm_buy
            + structural_stop * cost_sell
        )
        if risk_net_per_share <= 0:
            return None

        min_net_rr = getattr(self.cfg, 'min_reward_risk', 1.5) if self.cfg else 1.5

        # Minimum target price required to achieve Net RR >= min_net_rr
        target_for_min_net_rr = floor_tick(
            (min_net_rr * risk_net_per_share + entry_price * (1.0 + cost_buy)) / (1.0 - cost_sell) + ts
        )
        projected_target = max(floor_tick(entry_price + 2.5 * risk_dist), target_for_min_net_rr)

        # Check resistance ceiling from watch / intraday peak
        resistance = watch.get('resistance')
        if resistance and resistance > entry_price:
            # If resistance is overhead, target cannot exceed resistance
            target_price = min(float(resistance), projected_target)
        else:
            target_price = projected_target

        if target_price <= entry_price:
            return None

        # 3. Cost-inclusive Net RR calculation
        reward_net_per_share = (
            target_price * (1.0 - cost_sell)
            - entry_price * (1.0 + cost_buy)
        )
        net_rr = reward_net_per_share / risk_net_per_share
        gross_rr = (target_price - entry_price) / risk_dist

        if net_rr < min_net_rr:
            return {
                'pass': False,
                'reason': 'NET_REWARD_RISK_LOW',
                'net_rr': net_rr,
                'min_net_rr': min_net_rr,
                'gross_rr': gross_rr,
                'entry': entry_price,
                'stop': structural_stop,
                'target': target_price
            }

        # 4. Sizing
        # Risk per share
        max_shares_by_risk = math.floor(risk_budget / risk_net_per_share) if risk_net_per_share > 0 else 0
        max_shares_by_cap = math.floor(capital_limit / (entry_price * (1.0 + comm_buy + entry_slip)))
        qty = min(max_shares_by_risk, max_shares_by_cap)

        if qty <= 0:
            return {
                'pass': False,
                'reason': 'RISK_ZERO_QUANTITY',
                'max_shares_by_risk': max_shares_by_risk,
                'max_shares_by_cap': max_shares_by_cap,
                'entry': entry_price
            }

        return {
            'pass': True,
            'entry': entry_price,
            'stop': structural_stop,
            'emergency': emergency_stop,
            'target': target_price,
            'quantity': qty,
            'net_rr': net_rr,
            'gross_rr': gross_rr,
            'reward_net_per_share': reward_net_per_share,
            'risk_net_per_share': risk_net_per_share,
        }

    def evaluate(
        self,
        t,
        watch: Dict[str, Any],
        capital_limit: float = 81300.0,
        risk_budget: float = 1626.0,
        ingest: bool = True
    ) -> Optional[FastSignal]:
        """
        Main entry evaluation for FAST momentum.
        Returns a FastSignal if Regime A conditions and risk-reward checks pass; otherwise None.

        ingest=False means the caller already fed EVERY received tick through
        ingest_tick() (ShadowObserver does this before its evaluation throttle).
        Evaluating on a throttled subset of ticks starves the MicroTape and makes the
        tick-velocity / turnover regime gates structurally unreachable.
        """
        if ingest:
            self.ingest_tick(t)
        tape = self.get_tape(t.symbol)
        m = tape.metrics(t.time)
        if not m or m.get('ticks_count', 0) < 4:
            return None

        # Cooldown check: prevent immediate re-entry into same symbol after exit
        last_exit = self.last_exit_by_symbol.get(t.symbol)
        if last_exit and (t.time - last_exit).total_seconds() < self.cooldown_seconds:
            return None

        phase = self.determine_phase(t.time)
        if phase == 'OFF_SESSION':
            return None

        # Regime classification
        regime, reason, details = self.classify_regime(t, m, phase, watch)
        if regime != 'A':
            # Not an early momentum setup (could be B, C, or D)
            return None

        # Calculate stops, targets, net RR and quantity
        geom = self.calculate_invalidation_and_target(
            t, m, watch,
            capital_limit=capital_limit,
            risk_budget=risk_budget
        )
        if not geom or not geom.get('pass'):
            return None

        sig = FastSignal(
            symbol=t.symbol,
            name=watch.get('name', t.symbol),
            limit=geom['entry'],
            stop=geom['stop'],
            target=geom['target'],
            emergency=geom['emergency'],
            quantity=geom['quantity'],
            net_reward_risk=geom['net_rr'],
            gross_reward_risk=geom['gross_rr'],
            reward_net_per_share=geom['reward_net_per_share'],
            risk_net_per_share=geom['risk_net_per_share'],
            regime='A',
            phase=phase,
            created_at=t.time,
            ttl_seconds=15.0,
            max_chase_fraction=0.003,
            reason='EARLY_MOMENTUM_ENTRY',
            extra={'metrics': m, 'geometry': geom}
        )
        self.active_signals[t.symbol] = sig
        return sig

    def evaluate_exit(
        self,
        position: Dict[str, Any],
        t,
        current_time: datetime
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Tailored Exit Logic for FAST Momentum:
        1. Tier 1: Invalidation line (micro-swing stop)
        2. Tier 2: Account risk limit (unrealized loss >= 2%)
        3. Tier 3: Emergency plunge stop
        4. Momentum Stall Exit (early scratch if momentum stalls within 60~120s)
        5. Profit Protection: Lock break-even at +1R, trail at +1.5R.
        """
        p = t.price
        sym = position.get('symbol', t.symbol)
        entry = position.get('avg', position.get('entry', p))
        qty = position.get('qty', 1)
        stop = position.get('stop', entry * 0.985)
        emergency = position.get('emergency', stop * 0.995)
        opened_at_str = position.get('opened')
        opened_at = datetime.fromisoformat(opened_at_str) if isinstance(opened_at_str, str) else current_time
        holding_seconds = (current_time - opened_at).total_seconds()

        # Track MFE (Maximum Favorable Excursion)
        highest = position.setdefault('highest_price', p)
        if p > highest:
            position['highest_price'] = p
            highest = p

        # Dynamic Profit Lock & Trailing Stop:
        # If gain reached >= 1.8%, lock profit at minimum +1.8% or trail 0.8% below highest peak
        mfe_pct = (highest - entry) / entry if entry > 0 else 0.0
        if mfe_pct >= 0.018:
            lock_stop = floor_tick(max(entry * 1.018, highest * 0.992))
            if lock_stop > position.get('stop', 0):
                position['stop'] = lock_stop
                position['profit_locked'] = True

        # Check if price hit the locked profit stop
        if position.get('profit_locked') and p <= position['stop']:
            self.last_exit_by_symbol[sym] = current_time
            return True, 'PROFIT_LOCK_EXIT', {'price': p, 'stop': position['stop'], 'highest': highest}

        # Tier 3: Emergency plunge
        if p <= emergency:
            self.last_exit_by_symbol[sym] = current_time
            return True, 'EMERGENCY_PLUNGE_EXIT', {'price': p, 'emergency': emergency}

        # Tier 1: Invalidation Stop
        if p <= stop:
            self.last_exit_by_symbol[sym] = current_time
            return True, 'INVALIDATION_STOP_EXIT', {'price': p, 'stop': stop}

        # Tier 2: Account Risk Limit (max 2% of capital)
        unrealized_loss = (entry - p) * qty
        max_allowed_loss = 1626.0  # 2% of 81,300 KRW
        if unrealized_loss >= max_allowed_loss:
            self.last_exit_by_symbol[sym] = current_time
            return True, 'ACCOUNT_RISK_LIMIT_EXIT', {'loss': unrealized_loss, 'limit': max_allowed_loss}

        # Micro-metrics from tape
        tape = self.get_tape(sym)
        m = tape.metrics(current_time)

        # Early Momentum Stall Exit:
        # If holding for >= 60 seconds, price hasn't moved into profit (p < entry + 0.3*(entry-stop)),
        # and selling pressure is dominant (downtick ratio >= 65%), exit early to minimize loss!
        if holding_seconds >= 60.0:
            r_gain = (p - entry) / max(1.0, entry - stop)
            if r_gain < 0.3 and m.get('downtick_ratio_30', 0.0) >= 0.65:
                self.last_exit_by_symbol[sym] = current_time
                return True, 'MOMENTUM_STALL_SCRATCH_EXIT', {
                    'holding_seconds': holding_seconds,
                    'r_gain': r_gain,
                    'downtick_ratio_30': m.get('downtick_ratio_30', 0.0)
                }

        # Profit Protection:
        # If price reached >= 1.0R, active stop should be moved to break-even + cost
        r_dist = entry - stop
        if r_dist > 0 and (p - entry) >= r_dist:
            comm_tax_buf = entry * 0.003  # Cover roundtrip costs
            breakeven_stop = floor_tick(entry + comm_tax_buf)
            if breakeven_stop > stop:
                position['stop'] = breakeven_stop
                position['profit_locked'] = True

        return False, 'HOLD', {'holding_seconds': holding_seconds, 'price': p}
