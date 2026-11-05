"""Classics Strategy Engine (투자 원전 9권 기반 신규 매매전략 엔진).

Incorporates core principles from:
1. John J. Murphy (Technical Analysis): Role Reversal (Broken Resistance -> Support), Volume Confirmation
2. Alexander Elder (Trading for a Living): Triple Screen, 2% Account Equity Sizing Rule
3. William J. O'Neil (How to Make Money in Stocks): Pivot Point Breakout, Bar-Level Volume Expansion (>= +50%)
4. Curtis Faith (Way of the Turtle): N (ATR) Volatility Scaling, Expectancy, Noise Buffering
5. Jack D. Schwager (Market Wizards): Asymmetric Risk-Reward (Net RR >= 1.5), Defense First
6. 기술적 분석은 처음이지 (Korean Technical Analysis Guide): 수평 매물대, 캔들 중심값 지지, 종가 안착
7. Robert Pardo (Evaluation & Optimization): Deterministic Rules, Robust Parameter Cliffs
8. David Aronson (Evidence-Based Technical Analysis): Objective Hypotheses, Zero Data-Mining Overfitting
9. Larry Harris (Market Microstructure): Limit Order Book Spreads, Adverse Selection, Realistic Cost PnL
"""

import math
from typing import Dict, Any, List, Optional, Tuple
from core.util import tick_size, floor_tick

HARD_LOCK_SYMBOLS = {'005930', '017900', '052260'}

def is_valid_number(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    try:
        n = float(val)
        return math.isfinite(n) and n > 0
    except (TypeError, ValueError):
        return False

DEFAULT_COSTS = {
    'buy': 0.00015,
    'sell': 0.00015,
    'tax': 0.0018,
    'entry_slippage': 0.0005,
    'exit_slippage': 0.0005
}


class ClassicsMicrostructureGate:
    """Larry Harris: Market microstructure, bid-ask spread, and real cost-adjusted net economics."""

    def __init__(self, costs: Optional[Dict[str, float]] = None, max_spread: float = 0.0040, min_net_rr: float = 1.5):
        self.costs = dict(costs or DEFAULT_COSTS)
        self.max_spread = max_spread
        self.min_net_rr = min_net_rr

    def evaluate_quote(self, symbol: str, bid: float, ask: float, price: float) -> Tuple[bool, str, Dict[str, Any]]:
        if symbol in HARD_LOCK_SYMBOLS:
            return False, 'HARD_LOCK_PROTECTED_SYMBOL', {'symbol': symbol}

        # Filter out non-standard symbols (ETFs, ETNs, warrants ending in letters) and penny stocks < 2,000 KRW
        if not (len(symbol) == 6 and symbol.isdigit()):
            return False, 'NON_STANDARD_EQUITY_SYMBOL', {'symbol': symbol}

        if price < 2000.0:
            return False, 'PENNY_STOCK_EXCLUDED', {'price': price, 'min_price': 2000.0}

        if not is_valid_number(bid) or not is_valid_number(ask) or not is_valid_number(price) or not (0 < bid <= ask):
            return False, 'EXECUTABLE_QUOTE_INVALID', {'bid': bid, 'ask': ask, 'price': price}

        spread = (ask - bid) / bid
        step = tick_size(price)
        # Allow multi-tick spread only if within maximum percentage
        if spread > self.max_spread and (ask - bid) > step:
            return False, 'SPREAD_EXCEEDS_MAXIMUM', {'spread': spread, 'max_spread': self.max_spread, 'bid': bid, 'ask': ask}

        return True, 'PASS', {'spread': spread, 'tick_size': step}

    def evaluate_economics(self, entry: float, stop: float, target: float, emergency_stop: Optional[float] = None) -> Tuple[bool, str, Dict[str, Any]]:
        emergency = emergency_stop if emergency_stop is not None else stop
        if not (0 < emergency <= stop < entry < target):
            return False, 'INVALID_GEOMETRY', {'entry': entry, 'stop': stop, 'emergency': emergency, 'target': target}

        entry_slip = self.costs.get('entry_slippage', 0.0005)
        exit_slip = self.costs.get('exit_slippage', 0.0005)
        buy_fee = self.costs.get('buy', 0.00015)
        sell_fee = self.costs.get('sell', 0.00015)
        tax = self.costs.get('tax', 0.0018)

        modeled_entry = entry * (1 + entry_slip)
        buy_outlay = modeled_entry * (1 + buy_fee)

        sell_mult = (1 - exit_slip) * (1 - sell_fee - tax)
        reward_net_per_share = target * sell_mult - buy_outlay
        risk_net_per_share = buy_outlay - emergency * sell_mult

        if risk_net_per_share <= 0:
            return False, 'ZERO_OR_NEGATIVE_RISK', {'risk_net_per_share': risk_net_per_share}

        net_rr = reward_net_per_share / risk_net_per_share
        gross_rr = (target - entry) / (entry - stop)

        economics = {
            'entry': entry,
            'stop': stop,
            'emergency': emergency,
            'target': target,
            'modeled_entry': modeled_entry,
            'buy_outlay': buy_outlay,
            'reward_net_per_share': reward_net_per_share,
            'risk_net_per_share': risk_net_per_share,
            'net_rr': net_rr,
            'gross_rr': gross_rr,
            'min_net_rr': self.min_net_rr
        }

        if net_rr < self.min_net_rr:
            return False, 'NET_REWARD_RISK_LOW', economics

        return True, 'PASS', economics


class ClassicsRiskSizer:
    """Alexander Elder 2% Rule & Curtis Faith Turtle Volatility Management."""

    def __init__(self, risk_fraction: float = 0.02, min_stop_distance_fraction: float = 0.015):
        self.risk_fraction = risk_fraction  # 2% max account equity risk per trade
        self.min_stop_distance_fraction = min_stop_distance_fraction

    @staticmethod
    def calculate_trailing_stop(entry: float, current_stop: float, current_high: float, costs: Dict[str, float]) -> float:
        """Alexander Elder Ch. 10 & Curtis Faith Trailing Lock.

        1. If price reaches +1.0R (gain >= entry - initial_stop) or +1.5%, move stop to Breakeven (+costs).
        2. If price reaches >= +2.0%, trail stop 1.0% below peak high.
        """
        step = tick_size(entry)
        total_cost_pct = (
            costs.get('buy', 0.00015) +
            costs.get('sell', 0.00015) +
            costs.get('tax', 0.0018) +
            costs.get('entry_slippage', 0.0005) +
            costs.get('exit_slippage', 0.0005)
        )
        breakeven_price = floor_tick(entry * (1.0 + total_cost_pct + 0.001))  # Cover cost + 0.1% buffer
        new_stop = current_stop

        # Breakeven lock when price reaches +1.5%
        if current_high >= entry * 1.015:
            new_stop = max(new_stop, breakeven_price)

        # Trailing profit lock when price reaches +2.0%
        if current_high >= entry * 1.020:
            trailing_level = floor_tick(current_high * 0.990)
            new_stop = max(new_stop, trailing_level)

        return new_stop

    @staticmethod
    def calculate_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
        if len(bars) < 2:
            return 0.0
        trs = []
        for i in range(1, len(bars)):
            h = bars[i]['high']
            l = bars[i]['low']
            prev_c = bars[i-1]['close']
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
        if not trs:
            return 0.0
        window = trs[-period:]
        return sum(window) / len(window)

    def evaluate_stop_and_size(
        self,
        entry: float,
        structural_stop: float,
        bars: List[Dict[str, Any]],
        equity: float,
        capital_limit: float,
        economics: Dict[str, Any],
        remaining_daily_risk: Optional[float] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        step = tick_size(entry)
        atr = self.calculate_atr(bars)

        # Noise floor: minimum distance to survive tick/spread noise (Elder & Faith)
        noise_buffer = max(step * 2, atr * 0.8, entry * self.min_stop_distance_fraction)
        actual_stop_distance = entry - structural_stop

        if actual_stop_distance < noise_buffer:
            return False, 'STOP_TOO_TIGHT_NOISE_RISK', {
                'entry': entry,
                'structural_stop': structural_stop,
                'actual_stop_distance': actual_stop_distance,
                'required_noise_buffer': noise_buffer,
                'atr': atr,
                'step': step
            }

        risk_net = economics['risk_net_per_share']
        buy_outlay = economics['buy_outlay']

        if risk_net <= 0 or buy_outlay <= 0:
            return False, 'INVALID_COST_VALUATION', {'risk_net': risk_net, 'buy_outlay': buy_outlay}

        # 2% Risk Budget, strictly capped by remaining daily risk
        risk_budget = equity * self.risk_fraction
        if remaining_daily_risk is not None:
            if remaining_daily_risk <= 0:
                return False, 'DAILY_RISK_BUDGET_EXHAUSTED', {
                    'quantity': 0,
                    'remaining_daily_risk': remaining_daily_risk,
                    'risk_budget': 0.0,
                    'capital_limit': capital_limit
                }
            risk_budget = min(risk_budget, remaining_daily_risk)

        shares_by_risk = math.floor(risk_budget / risk_net)
        shares_by_cap = math.floor(capital_limit / buy_outlay)

        quantity = min(shares_by_risk, shares_by_cap)

        if quantity <= 0:
            return False, 'RISK_ZERO_QUANTITY', {
                'quantity': 0,
                'shares_by_risk': shares_by_risk,
                'shares_by_cap': shares_by_cap,
                'risk_budget': risk_budget,
                'capital_limit': capital_limit,
                'remaining_daily_risk': remaining_daily_risk
            }

        return True, 'PASS', {
            'quantity': quantity,
            'shares_by_risk': shares_by_risk,
            'shares_by_cap': shares_by_cap,
            'risk_budget': risk_budget,
            'capital_limit': capital_limit,
            'atr': atr,
            'noise_buffer': noise_buffer
        }


class ClassicsBreakoutStrategy:
    """William J. O'Neil & John J. Murphy: Volume-confirmed pivot breakout."""

    def __init__(self, min_vol_ratio: float = 1.5, max_chase_pct: float = 0.025, lookback_bars: int = 15):
        self.min_vol_ratio = min_vol_ratio  # O'Neil: volume >= 150% of 5-bar average
        self.max_chase_pct = max_chase_pct
        self.lookback_bars = lookback_bars

    def evaluate(self, bars: List[Dict[str, Any]], price: float, ask: float, watch: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Dict[str, Any]]:
        if len(bars) < max(self.lookback_bars, 6):
            return False, 'INSUFFICIENT_BARS', {'bars_count': len(bars)}

        seq = bars[-self.lookback_bars:]
        # Consolidation pivot is the highest high of prior completed bars (excluding the very latest active bar if forming)
        prior_base = seq[:-1]
        pivot = max(b['high'] for b in prior_base)
        base_low = min(b['low'] for b in prior_base)

        if pivot <= 0 or base_low <= 0 or pivot <= base_low:
            return False, 'INVALID_BASE_GEOMETRY', {'pivot': pivot, 'base_low': base_low}

        # Check volume surge on the breakout candle (or recent 2 candles)
        recent_vols = [b['volume'] for b in seq[-6:-1]]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        latest_vol = seq[-1]['volume']

        vol_ratio = (latest_vol / avg_vol) if avg_vol > 0 else 0.0

        if vol_ratio < self.min_vol_ratio:
            return False, 'BREAKOUT_VOLUME_INSUFFICIENT', {
                'latest_vol': latest_vol,
                'avg_vol': avg_vol,
                'vol_ratio': vol_ratio,
                'min_vol_ratio': self.min_vol_ratio
            }

        # Breakout price condition
        if price <= pivot:
            return False, 'PRICE_NOT_ABOVE_PIVOT', {'price': price, 'pivot': pivot}

        # Anti-chase filter (O'Neil: do not chase > 2.5% above pivot)
        chase_ratio = (price - pivot) / pivot
        if chase_ratio > self.max_chase_pct:
            return False, 'CHASING_EXTENDED_BREAKOUT', {'price': price, 'pivot': pivot, 'chase_ratio': chase_ratio, 'max_chase': self.max_chase_pct}

        # Structural Support: higher swing low of base or last 3 bars low
        support = min(b['low'] for b in seq[-3:])
        step = tick_size(price)
        stop = floor_tick(support - step)

        # Projection: Murphy Ch. 4 / Dow Theory target
        target = floor_tick(pivot + 2.0 * (pivot - support))
        if watch and is_valid_number(watch.get('resistance')) and watch['resistance'] > price:
            target = min(target, floor_tick(watch['resistance']))

        return True, 'PASS', {
            'strategy': 'CLASSICS_BREAKOUT',
            'pivot': pivot,
            'base_low': base_low,
            'support': support,
            'stop': stop,
            'emergency': floor_tick(stop - step),
            'target': target,
            'vol_ratio': vol_ratio,
            'entry': ask
        }


class ClassicsRoleReversalStrategy:
    """John J. Murphy Ch. 4 (Role Reversal) & Alexander Elder (Screen 2) & Korean Guide Ch. 3.

    Broken resistance level R becomes support upon pullback/retest.
    """

    def __init__(self, tolerance_pct: float = 0.005, min_impulse_rise: float = 0.008, max_pullback_vol_ratio: float = 0.85):
        self.tolerance_pct = tolerance_pct  # Retest touches within R +- 0.5%
        self.min_impulse_rise = min_impulse_rise  # At least 0.8% initial impulse
        self.max_pullback_vol_ratio = max_pullback_vol_ratio  # Pullback volume contracts

    def evaluate(self, bars: List[Dict[str, Any]], price: float, ask: float, watch: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Dict[str, Any]]:
        if len(bars) < 8:
            return False, 'INSUFFICIENT_BARS', {'bars_count': len(bars)}

        seq = bars[-20:] if len(bars) >= 20 else bars
        # Find local resistance pivot R in early half
        half = len(seq) // 2
        r_level = max(b['high'] for b in seq[:half])

        # Find impulse peak after R breakout
        post_breakout = seq[half:]
        peak_bar = max(post_breakout, key=lambda b: b['high'])
        peak_idx = seq.index(peak_bar)
        peak_price = peak_bar['high']

        # Peak must be an actual breakout above R
        impulse_rise = (peak_price - r_level) / r_level if r_level > 0 else 0
        if impulse_rise < self.min_impulse_rise:
            return False, 'NO_CONFIRMED_PRIOR_BREAKOUT', {'r_level': r_level, 'peak_price': peak_price, 'impulse_rise': impulse_rise}

        # Pullback phase: bars after peak
        pullback = seq[peak_idx:]
        if len(pullback) < 2:
            return False, 'PULLBACK_NOT_DEVELOPED', {'pullback_bars': len(pullback)}

        pullback_low = min(b['low'] for b in pullback)

        # Role Reversal Test: Did pullback low test the broken resistance R?
        # Low must touch near R (within tolerance) and not collapse deeply below R
        lower_bound = r_level * (1.0 - self.tolerance_pct)
        upper_bound = r_level * (1.0 + self.tolerance_pct)

        if pullback_low < lower_bound or pullback_low > upper_bound * 1.01:
            return False, 'ROLE_REVERSAL_ZONE_MISSED', {
                'r_level': r_level,
                'pullback_low': pullback_low,
                'lower_bound': lower_bound,
                'upper_bound': upper_bound
            }

        # Volume contraction test (Murphy & Elder): Pullback volume should dry up
        impulse_vol = sum(b['volume'] for b in seq[half:peak_idx+1]) / max(1, len(seq[half:peak_idx+1]))
        pull_vol = sum(b['volume'] for b in pullback) / max(1, len(pullback))

        if impulse_vol > 0 and (pull_vol / impulse_vol) > self.max_pullback_vol_ratio:
            return False, 'PULLBACK_VOLUME_NOT_CONTRACTING', {'pull_vol': pull_vol, 'impulse_vol': impulse_vol, 'ratio': pull_vol / impulse_vol}

        # Bounce confirmation: Current price is rebounding above pullback low and holding R
        if price <= pullback_low or price < r_level:
            return False, 'SUPPORT_BOUNCE_NOT_CONFIRMED', {'price': price, 'pullback_low': pullback_low, 'r_level': r_level}

        # Elder Triple Screen Screen 1: Trend alignment (Price above moving average)
        ma_window = min(len(seq), 10)
        ma = sum(b['close'] for b in seq[-ma_window:]) / ma_window
        if price < ma:
            return False, 'BELOW_TREND_MA', {'price': price, 'ma': ma}

        step = tick_size(price)
        base_low = min(b['low'] for b in seq[:half])
        # Structural stop: placed below the broken resistance R and pullback low with noise buffer (Elder & Murphy)
        stop = floor_tick(min(pullback_low - step, r_level * (1.0 - 0.015), base_low))
        target = floor_tick(max(peak_price + 2.0 * (peak_price - r_level), price + 2.0 * (price - stop)))

        if watch and is_valid_number(watch.get('resistance')) and watch['resistance'] > price:
            target = min(target, floor_tick(watch['resistance']))

        return True, 'PASS', {
            'strategy': 'CLASSICS_ROLE_REVERSAL',
            'broken_resistance_r': r_level,
            'peak_price': peak_price,
            'pullback_low': pullback_low,
            'support': pullback_low,
            'stop': stop,
            'emergency': floor_tick(stop - step),
            'target': target,
            'entry': ask
        }


class ClassicsBoxExpansionStrategy:
    """Curtis Faith (Way of the Turtle) dynamic N-scaled Box Consolidation & Breakout."""

    def __init__(self, min_bars: int = 5, atr_multiplier: float = 2.0):
        self.min_bars = min_bars
        self.atr_multiplier = atr_multiplier

    def evaluate(self, bars: List[Dict[str, Any]], price: float, ask: float, watch: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Dict[str, Any]]:
        if len(bars) < self.min_bars + 5:
            return False, 'INSUFFICIENT_BARS', {'bars_count': len(bars)}

        seq = bars[-self.min_bars:]
        box_top = max(b['high'] for b in seq)
        box_low = min(b['low'] for b in seq)

        if box_low <= 0:
            return False, 'INVALID_BOX_LOW', {'box_low': box_low}

        # Dynamic box range scaled by recent ATR
        atr = ClassicsRiskSizer.calculate_atr(bars[:-self.min_bars] or bars)
        allowed_range = max(0.025, (atr * self.atr_multiplier) / box_low)
        actual_range = (box_top - box_low) / box_low

        if actual_range > allowed_range:
            return False, 'BOX_RANGE_TOO_WIDE', {'actual_range': actual_range, 'allowed_range': allowed_range, 'atr': atr}

        # Breakout above box top
        if price <= box_top:
            return False, 'PRICE_NOT_ABOVE_BOX_TOP', {'price': price, 'box_top': box_top}

        step = tick_size(price)
        # Midpoint or box low acts as structural stop
        box_mid = (box_top + box_low) / 2.0
        stop = floor_tick(box_mid - step)
        target = floor_tick(box_top + 2.0 * (box_top - box_low))

        if watch and is_valid_number(watch.get('resistance')) and watch['resistance'] > price:
            target = min(target, floor_tick(watch['resistance']))

        return True, 'PASS', {
            'strategy': 'CLASSICS_BOX_EXPANSION',
            'box_top': box_top,
            'box_low': box_low,
            'box_mid': box_mid,
            'support': box_mid,
            'stop': stop,
            'emergency': floor_tick(box_low - step),
            'target': target,
            'entry': ask
        }


class ClassicsStrategyEngine:
    """Unified Orchestrator: Multi-strategy evaluation, Harris microstructure gate, Elder risk sizing."""

    def __init__(self, costs: Optional[Dict[str, float]] = None, equity: float = 10000000.0, capital_limit: float = 81300.0):
        self.costs = dict(costs or DEFAULT_COSTS)
        self.equity = equity
        self.capital_limit = capital_limit

        self.micro_gate = ClassicsMicrostructureGate(self.costs)
        self.risk_sizer = ClassicsRiskSizer(risk_fraction=0.02)

        self.strat_role_reversal = ClassicsRoleReversalStrategy()
        self.strat_box_expansion = ClassicsBoxExpansionStrategy()
        self.strat_breakout = ClassicsBreakoutStrategy()

    def evaluate_candidate(
        self,
        symbol: str,
        price: float,
        bid: float,
        ask: float,
        bars: List[Dict[str, Any]],
        watch: Optional[Dict[str, Any]] = None,
        equity: Optional[float] = None,
        capital_limit: Optional[float] = None,
        remaining_daily_risk: Optional[float] = None
    ) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
        eq = equity if equity is not None else self.equity
        cap = capital_limit if capital_limit is not None else self.capital_limit

        # Step 1: Microstructure Quote Check
        q_pass, q_reason, q_data = self.micro_gate.evaluate_quote(symbol, bid, ask, price)
        if not q_pass:
            return None, q_reason, q_data

        # Step 2: Evaluate Strategies in High-to-Low Quality Hierarchy
        # Hierarchy: 1. Role Reversal (Highest Win-rate Retest)
        #            2. Box Expansion (Turtle Squeezes)
        #            3. Pivot Breakout (Momentum Expansion)
        chosen_setup = None
        reasons = {}

        rr_pass, rr_reason, rr_setup = self.strat_role_reversal.evaluate(bars, price, ask, watch)
        if rr_pass:
            chosen_setup = rr_setup
        else:
            reasons['ROLE_REVERSAL'] = rr_reason

            box_pass, box_reason, box_setup = self.strat_box_expansion.evaluate(bars, price, ask, watch)
            if box_pass:
                chosen_setup = box_setup
            else:
                reasons['BOX_EXPANSION'] = box_reason

                bo_pass, bo_reason, bo_setup = self.strat_breakout.evaluate(bars, price, ask, watch)
                if bo_pass:
                    chosen_setup = bo_setup
                else:
                    reasons['BREAKOUT'] = bo_reason

        if chosen_setup is None:
            return None, 'NO_QUALIFIED_CLASSICS_SETUP', {'reasons': reasons}

        # Step 3: Economic Viability Check (Harris Net RR >= 1.5)
        entry = chosen_setup['entry']
        stop = chosen_setup['stop']
        emergency = chosen_setup.get('emergency', stop)
        target = chosen_setup['target']

        econ_pass, econ_reason, econ_data = self.micro_gate.evaluate_economics(entry, stop, target, emergency)
        if not econ_pass:
            return None, econ_reason, {**chosen_setup, **econ_data}

        # Step 4: Volatility Noise Floor & Elder 2% Position Sizing
        size_pass, size_reason, size_data = self.risk_sizer.evaluate_stop_and_size(
            entry=entry,
            structural_stop=stop,
            bars=bars,
            equity=eq,
            capital_limit=cap,
            economics=econ_data,
            remaining_daily_risk=remaining_daily_risk
        )
        if not size_pass:
            return None, size_reason, {**chosen_setup, **econ_data, **size_data}

        # Form Final Approved Signal
        signal = {
            'symbol': symbol,
            'name': watch.get('name', symbol) if watch else symbol,
            'strategy': chosen_setup['strategy'],
            'entry': entry,
            'stop': stop,
            'emergency': emergency,
            'target': target,
            'quantity': size_data['quantity'],
            'net_rr': econ_data['net_rr'],
            'gross_rr': econ_data['gross_rr'],
            'reward_net_per_share': econ_data['reward_net_per_share'],
            'risk_net_per_share': econ_data['risk_net_per_share'],
            'risk_budget': size_data['risk_budget'],
            'noise_buffer': size_data['noise_buffer'],
            'setup_details': chosen_setup,
            'economics': econ_data,
            'sizing': size_data
        }

        return signal, 'PASS', signal
