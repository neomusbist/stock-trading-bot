"""Local Breakout and Hold Strategy for confirmed resistance breaches."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional
import math

from core.util import floor_tick, tick_size
from automation.decisions import gate

def is_valid_number(val):
    if val is None:
        return False
    try:
        n = float(val)
        return math.isfinite(n) and n > 0
    except (ValueError, TypeError):
        return False


@dataclass
class LocalBreakoutSignal:
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
    pattern: str = 'LOCAL_BREAKOUT'
    created_at: Optional[datetime] = None
    ttl_seconds: float = 30.0
    max_chase_fraction: float = 0.005
    max_chase_price: float = 0.0
    reason: str = 'LOCAL_RESISTANCE_BREAKOUT_AND_HOLD'
    extra: Dict[str, Any] = field(default_factory=dict)


class LocalBreakoutStrategy:
    """Evaluates short-term local resistance breakout & hold on completed bars & ticks."""

    def __init__(self, cfg, auto, costs):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs
        self.lookback_bars = 10
        self.min_support_bars = 5
        self.min_stop_fraction = 0.008  # 0.8%
        self.max_stop_fraction = 0.025  # 2.5%
        self.max_chase_fraction = 0.005  # 0.5%
        self.target_multiplier = 2.0  # 2.0x risk projection
        self.min_gross_rr = 1.5  # minimum gross reward/risk
        self.min_net_rr = 0.95  # minimum net reward/risk after full round-trip friction
        self.max_spread = 0.004  # 0.4% max bid-ask spread
        self.last_signal = {}
        self.rejection_counts = {}

    def _reject(self, symbol, reason, **details):
        self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
        return None, {'pass': False, 'reason': reason, **details}

    def evaluate(self, t, bars, watch, capital_limit=81300., risk_budget=1626., asof=None):
        now = asof or getattr(t, 'time', None)
        symbol = t.symbol
        name = watch.get('name', symbol) if isinstance(watch, dict) else symbol

        if not bars or len(bars) < self.min_support_bars:
            return self._reject(symbol, 'INSUFFICIENT_LOCAL_BARS', count=len(bars) if bars else 0)

        # Quotes validation
        bid = getattr(t, 'bid', None)
        ask = getattr(t, 'ask', None)
        price = getattr(t, 'price', None)
        if not (is_valid_number(bid) and is_valid_number(ask) and is_valid_number(price)) or bid > ask:
            return self._reject(symbol, 'INVALID_EXECUTABLE_QUOTE')

        spread = (ask - bid) / price
        if spread > self.max_spread:
            return self._reject(symbol, 'SPREAD_TOO_WIDE', spread=spread, max_spread=self.max_spread)

        # Local resistance from recent completed bars
        lookback = min(len(bars), self.lookback_bars)
        ref_bars = bars[-lookback:]
        local_high = max(b['high'] for b in ref_bars)
        
        # Local support from recent base
        support_lookback = min(len(bars), self.min_support_bars)
        support = min(b['low'] for b in bars[-support_lookback:])

        # Breakout condition: price and ask must be at or above local resistance
        if price < local_high or ask < local_high:
            return self._reject(symbol, 'LOCAL_RESISTANCE_NOT_BROKEN',
                                price=price, ask=ask, local_high=local_high)

        # Chase control: cannot chase more than 0.5% above the breakout level
        chase = (ask - local_high) / local_high
        if chase > self.max_chase_fraction:
            return self._reject(symbol, 'MAX_CHASE_EXCEEDED',
                                chase=chase, max_chase=self.max_chase_fraction,
                                ask=ask, local_high=local_high)

        # Structural stop: buffer below support
        structural_buffer = getattr(self.cfg, 'structural_buffer', 0.002)
        structural_stop = floor_tick(support * (1.0 - structural_buffer))
        emergency_stop = floor_tick(structural_stop * 0.995)

        entry = ask
        stop_dist = (entry - structural_stop) / entry
        if stop_dist < self.min_stop_fraction:
            return self._reject(symbol, 'STOP_TOO_TIGHT',
                                stop_dist=stop_dist, min_stop=self.min_stop_fraction)
        if stop_dist > self.max_stop_fraction:
            return self._reject(symbol, 'STOP_TOO_WIDE',
                                stop_dist=stop_dist, max_stop=self.max_stop_fraction)

        # Target projection
        projected_gain = self.target_multiplier * (entry - structural_stop)
        target = floor_tick(entry + projected_gain)

        # Check resistance ceiling if available
        overhead = watch.get('resistance') if isinstance(watch, dict) else None
        if is_valid_number(overhead) and overhead > entry:
            if overhead >= entry + 1.2 * (entry - structural_stop):
                target = min(target, floor_tick(overhead))

        # Full cost accounting
        fee = self.costs.estimate(entry, target)
        roundtrip_cost = self.costs.roundtrip(entry, structural_stop) + structural_stop * self.costs.exit_slippage
        reward_net = (target - entry) - fee
        risk_net = (entry - structural_stop) + roundtrip_cost

        if risk_net <= 0:
            return self._reject(symbol, 'INVALID_RISK_NET')

        net_rr = reward_net / risk_net
        gross_rr = (target - entry) / (entry - structural_stop) if entry > structural_stop else 0.0

        if gross_rr < self.min_gross_rr or net_rr < self.min_net_rr:
            return self._reject(symbol, 'REWARD_RISK_INSUFFICIENT',
                                net_rr=net_rr, min_net_rr=self.min_net_rr,
                                gross_rr=gross_rr, min_gross_rr=self.min_gross_rr,
                                reward_net=reward_net, risk_net=risk_net)

        # Risk-based quantity sizing
        cap_qty = int(capital_limit // (entry * (1.0 + self.costs.buy + self.costs.entry_slippage)))
        risk_qty = int(risk_budget // risk_net) if risk_net > 0 else 0
        quantity = min(cap_qty, risk_qty)

        if quantity < 1:
            return self._reject(symbol, 'ZERO_QUANTITY_RISK_OR_CAPITAL',
                                cap_qty=cap_qty, risk_qty=risk_qty, capital_limit=capital_limit, risk_budget=risk_budget)

        signal = LocalBreakoutSignal(
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
            pattern='LOCAL_BREAKOUT',
            created_at=now,
            max_chase_fraction=self.max_chase_fraction,
            max_chase_price=local_high * (1.0 + self.max_chase_fraction),
            reason='LOCAL_RESISTANCE_BREAKOUT_AND_HOLD',
            extra={
                'local_high': local_high,
                'support': support,
                'stop_distance_pct': stop_dist,
                'chase_pct': chase,
                'spread_pct': spread,
                'actual_order_permission': False,  # STRICT SHADOW ONLY
                'SHADOW_ONLY': True,
                'profitability_proven': False,
                'strategy_policy': 'LOCAL_BREAKOUT_SHADOW_RESEARCH'
            }
        )
        self.last_signal[symbol] = signal
        return signal, {'pass': True, 'signal': signal}
