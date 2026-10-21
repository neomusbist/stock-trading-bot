"""Early Impulse Pullback Strategy (Setup D).

Identifies the first major volume impulse candle, monitors the subsequent
normal retracement for support above the impulse base, and generates an early
entry when re-acceleration breaks the immediate local pullback high BEFORE
the prior session/day high is retested.

Strict Causal and Market Verification Rules:
1. Completed bar confirmation: only bars whose full minute has elapsed (known_signal_bars).
2. Executable quote integrity: bid, ask, and price must all be valid; no fallback to current_price.
3. Orderbook depth and freshness: depth_available, fresh book age, and positive ask_qty.
4. Resistance ceiling: target projection capped by confirmed daily/intraday resistance.
5. Engine alignment: integrates with Costs, build_plan reward guard, and risk/capital sizing.
6. SHADOW research isolation: actual_order_permission=False.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import math

from core.util import floor_tick, tick_size
from core.market_time import tick_freshness, book_freshness
from core.market_evidence import known_signal_bars, finite
from automation.exit_plan import build_plan


def is_valid_number(val) -> bool:
    if val is None:
        return False
    try:
        n = float(val)
        return math.isfinite(n) and n > 0
    except (ValueError, TypeError):
        return False


@dataclass
class EarlyImpulseSignal:
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
    pattern: str = 'EARLY_IMPULSE_PULLBACK'
    created_at: Optional[datetime] = None
    ttl_seconds: float = 30.0
    max_chase_fraction: float = 0.005
    max_chase_price: float = 0.0
    reason: str = 'EARLY_IMPULSE_PULLBACK_REBOUND'
    extra: Dict[str, Any] = field(default_factory=dict)


class EarlyImpulsePullbackStrategy:
    """Evaluates early impulse pullback setups on strictly completed bars and fresh quotes."""

    def __init__(self, cfg=None, auto=None, costs=None):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs
        self.min_support_bars = 6
        self.lookback_bars = 15
        self.min_volume_ratio = 2.5
        self.min_impulse_gain = 0.008
        self.min_pullback_bars = 2
        self.max_pullback_bars = 8
        self.min_stop_fraction = 0.006
        self.max_stop_fraction = 0.030
        self.max_chase_fraction = 0.005
        self.min_gross_rr = 1.3
        self.min_net_rr = 0.95
        self.max_spread = 0.004
        self.last_signal = {}
        self.rejection_counts = {}

    def _reject(self, symbol: str, reason: str, **details) -> Tuple[None, Dict[str, Any]]:
        self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
        return None, {'pass': False, 'reason': reason, **details}

    def find_impulse_bar(self, bars: List[dict]) -> Optional[Tuple[int, dict]]:
        """Finds the most recent volume impulse bar in completed history."""
        if len(bars) < self.min_support_bars:
            return None
        lookback = min(len(bars), self.lookback_bars)
        recent = bars[-lookback:]
        # Scan from oldest eligible position to newer bars, leaving room for pullback
        for i in range(5, len(recent) - self.min_pullback_bars + 1):
            prior_5 = recent[i - 5:i]
            bar = recent[i]
            avg_vol = sum(b.get('volume', 0) for b in prior_5) / 5.0
            if avg_vol <= 0:
                continue
            ratio = bar.get('volume', 0) / avg_vol
            gain = (bar['close'] - bar['open']) / bar['open'] if bar.get('open') else 0.0
            if ratio >= self.min_volume_ratio and gain >= self.min_impulse_gain and bar['close'] > bar['open']:
                global_idx = len(bars) - lookback + i
                return global_idx, bar
        return None

    def evaluate(self, t, bars: List[dict], watch: Any,
                 capital_limit: float = 71822.0,
                 risk_budget: float = 1429.0,
                 asof: Optional[datetime] = None) -> Tuple[Optional[EarlyImpulseSignal], Dict[str, Any]]:
        now = asof or getattr(t, 'time', None) or datetime.now()
        symbol = getattr(t, 'symbol', 'UNKNOWN')
        name = watch.get('name', symbol) if isinstance(watch, dict) else symbol

        if not bars or len(bars) < self.min_support_bars:
            return self._reject(symbol, 'INSUFFICIENT_COMPLETED_BARS', count=len(bars) if bars else 0)

        # 1. Enforce completed bar causality (no lookahead / uncompleted current minute bar)
        causal_bars, reason = known_signal_bars(bars[-self.lookback_bars:], now, self.min_support_bars)
        if reason:
            return self._reject(symbol, reason, data_status='UNKNOWN')
        if any(not finite(b.get('volume')) for b in causal_bars):
            return self._reject(symbol, 'BAR_VOLUME_EVIDENCE_MISSING', data_status='UNKNOWN')

        # 2. Strict executable quote verification: no falling back to current_price!
        price = getattr(t, 'price', None)
        bid = getattr(t, 'bid', None)
        ask = getattr(t, 'ask', None)
        if not is_valid_number(ask):
            return self._reject(symbol, 'NO_VALID_ASK', price=price, ask=ask, data_status='UNKNOWN')
        if not (is_valid_number(bid) and is_valid_number(price)) or bid > ask or ask < price:
            return self._reject(symbol, 'INVALID_EXECUTABLE_QUOTE', bid=bid, ask=ask, price=price)

        spread = (ask - bid) / price
        if spread > self.max_spread:
            return self._reject(symbol, 'SPREAD_TOO_WIDE', spread=spread, max_spread=self.max_spread)

        # 3. Orderbook depth and freshness verification
        depth_avail = getattr(t, 'depth_available', None) is True
        depth_src = getattr(t, 'depth_source', 'UNKNOWN') in {'0D', '0B_FIELDS'}
        book_fresh = book_freshness(t, now, getattr(self.cfg, 'stale_seconds', 2.0))['fresh'] if hasattr(t, 'book_time') else False
        has_qty = finite(getattr(t, 'ask_qty', None)) and getattr(t, 'ask_qty', 0) > 0

        if not (depth_avail and depth_src and book_fresh and has_qty):
            # If fresh depth is missing, execution feasibility is strictly UNKNOWN
            return self._reject(symbol, 'MEASURED_FRESH_BOOK_REQUIRED',
                                data_status='UNKNOWN', fill_feasibility='UNKNOWN',
                                depth_available=depth_avail, depth_source=getattr(t, 'depth_source', None),
                                book_fresh=book_fresh, ask_qty=getattr(t, 'ask_qty', None))

        # 4. Identify Volume Impulse Bar
        impulse_match = self.find_impulse_bar(causal_bars)
        if not impulse_match:
            return self._reject(symbol, 'NO_QUALIFYING_VOLUME_IMPULSE')
        impulse_idx, impulse_bar = impulse_match

        impulse_high = impulse_bar['high']
        impulse_low = impulse_bar['low']
        pullback_bars = causal_bars[impulse_idx + 1:]

        if len(pullback_bars) < self.min_pullback_bars:
            return self._reject(symbol, 'PULLBACK_TOO_SHORT', count=len(pullback_bars), min_bars=self.min_pullback_bars)
        if len(pullback_bars) > self.max_pullback_bars:
            return self._reject(symbol, 'PULLBACK_EXPIRED', count=len(pullback_bars), max_bars=self.max_pullback_bars)

        # Retracement support must remain above impulse low (no breakdown)
        support_low = min(b['low'] for b in pullback_bars)
        if support_low < impulse_low:
            return self._reject(symbol, 'BREAKDOWN_BELOW_IMPULSE_BASE', support_low=support_low, impulse_low=impulse_low)

        # Rebound condition: price and ask must break above previous bar high
        prev_bar = pullback_bars[-1]
        if price < prev_bar['high'] or ask < prev_bar['high']:
            return self._reject(symbol, 'REBOUND_NOT_CONFIRMED', prev_high=prev_bar['high'], price=price, ask=ask)

        # Early entry condition: must enter BEFORE breaking above impulse high
        if ask > impulse_high:
            return self._reject(symbol, 'PRICE_ALREADY_ABOVE_IMPULSE_HIGH', ask=ask, impulse_high=impulse_high)

        # 5. Invalidation (stop) and Target Calculation
        structural_buffer = getattr(self.cfg, 'structural_buffer', 0.002) if self.cfg else 0.002
        structural_stop = floor_tick(support_low * (1.0 - structural_buffer))
        emergency_stop = floor_tick(structural_stop * 0.995)

        entry = ask
        stop_dist = (entry - structural_stop) / entry
        if stop_dist < self.min_stop_fraction:
            return self._reject(symbol, 'STOP_TOO_TIGHT', stop_dist=stop_dist, min_stop=self.min_stop_fraction)
        if stop_dist > self.max_stop_fraction:
            return self._reject(symbol, 'STOP_TOO_WIDE', stop_dist=stop_dist, max_stop=self.max_stop_fraction)

        # Measured projection: measured move of impulse range (not arbitrary 2x inflation)
        projected_gain = impulse_high - support_low
        target = floor_tick(entry + projected_gain)

        # Check resistance ceiling if available from watch / market structure
        overhead = watch.get('resistance') if isinstance(watch, dict) else None
        if is_valid_number(overhead):
            if overhead <= entry:
                return self._reject(symbol, 'KNOWN_RESISTANCE_AT_OR_BELOW_ENTRY', entry=entry, resistance=overhead)
            # Capped strictly by resistance ceiling!
            target = min(target, floor_tick(overhead))

        # 6. Rigorous Friction & Risk Accounting
        if self.costs:
            fee = self.costs.estimate(entry, target)
            roundtrip_cost = self.costs.roundtrip(entry, emergency_stop) + emergency_stop * self.costs.exit_slippage
            buy_rate = self.costs.buy + self.costs.entry_slippage
        else:
            fee = entry * 0.00015 + target * (0.00015 + 0.0020 + 0.003)
            roundtrip_cost = entry * 0.00215 + emergency_stop * 0.00515
            buy_rate = 0.00215

        reward_net = (target - entry) - fee
        risk_net = (entry - emergency_stop) + roundtrip_cost

        if risk_net <= 0:
            return self._reject(symbol, 'INVALID_RISK_NET')

        net_rr = reward_net / risk_net
        gross_rr = (target - entry) / (entry - emergency_stop) if entry > emergency_stop else 0.0

        if gross_rr < self.min_gross_rr or net_rr < self.min_net_rr:
            return self._reject(symbol, 'REWARD_RISK_INSUFFICIENT',
                                net_rr=net_rr, min_net_rr=self.min_net_rr,
                                gross_rr=gross_rr, min_gross_rr=self.min_gross_rr,
                                reward_net=reward_net, risk_net=risk_net,
                                target=target, entry=entry, resistance=overhead)

        # 7. Common Plan Integration
        if self.cfg and self.auto:
            common_plan = build_plan(self.cfg, self.auto, {
                'pattern': 'EARLY_IMPULSE_PULLBACK',
                'limit': entry,
                'stop': emergency_stop,
                'emergency': emergency_stop,
                'target': target,
                'invalidation_policy': {
                    'enabled': True,
                    'structural_stop': structural_stop,
                    'emergency_stop': emergency_stop
                }
            })
            if not common_plan['reward_guard_pass']:
                return self._reject(symbol, 'COMMON_ENGINE_REWARD_GUARD',
                                    net_rr=common_plan['net_reward_risk'],
                                    minimum_net_rr=common_plan['minimum_net_reward_risk'])

        # 8. Quantity Sizing
        cap_qty = int(capital_limit // (entry * (1.0 + buy_rate)))
        risk_qty = int(risk_budget // risk_net) if risk_net > 0 else 0
        quantity = min(cap_qty, risk_qty)

        if quantity < 1:
            return self._reject(symbol, 'ZERO_QUANTITY_RISK_OR_CAPITAL',
                                cap_qty=cap_qty, risk_qty=risk_qty,
                                capital_limit=capital_limit, risk_budget=risk_budget)

        signal = EarlyImpulseSignal(
            symbol=symbol,
            name=name,
            limit=entry,
            stop=structural_stop,
            target=target,
            emergency=emergency_stop,
            quantity=quantity,
            net_reward_risk=net_rr,
            gross_reward_risk=gross_rr,
            reward_net_per_share=reward_net,
            risk_net_per_share=risk_net,
            pattern='EARLY_IMPULSE_PULLBACK',
            created_at=now,
            max_chase_fraction=self.max_chase_fraction,
            max_chase_price=impulse_high,
            reason='EARLY_IMPULSE_PULLBACK_REBOUND',
            extra={
                'impulse_high': impulse_high,
                'impulse_low': impulse_low,
                'support_low': support_low,
                'pullback_bars_count': len(pullback_bars),
                'rebound_trigger_high': prev_bar['high'],
                'resistance_ceiling': overhead,
                'actual_order_permission': False,
                'SHADOW_ONLY': True,
                'profitability_proven': False,
                'strategy_policy': 'EARLY_IMPULSE_PULLBACK_SHADOW_RESEARCH',
                'fill_feasibility': 'CONFIRMED' if has_qty else 'UNKNOWN',
                'book_time': t.book_time.isoformat() if hasattr(t, 'book_time') else None
            }
        )
        self.last_signal[symbol] = signal
        return signal, {'pass': True, 'signal': signal}
