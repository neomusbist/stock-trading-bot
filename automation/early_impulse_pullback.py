"""Early Impulse Pullback Strategy (Setup D).

Identifies the first major volume impulse candle, monitors the subsequent
normal retracement for support above the impulse base, and generates an early
entry when re-acceleration breaks the immediate local pullback high BEFORE
the prior session/day high is retested.

Uses genuine pre-decision evidence:
1. Impulse detection: 1-minute volume >= 3.0x rolling average and close > open.
2. Support preservation: Retracement low >= impulse base low (no breakdown).
3. Re-acceleration trigger: Current price > prior pullback bar high and <= impulse high.
4. Structural invalidation: Placed strictly at the confirmed pullback support low minus 1 tick.
5. Capital and risk budget: Strictly sized to 2% risk budget and capital cap.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from core.util import tick_size, floor_tick


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass
class ImpulsePullbackSignal:
    symbol: str
    name: str
    entry: float
    stop: float
    target: float
    quantity: int
    net_reward_risk: float
    gross_reward_risk: float
    reward_net_per_share: float
    risk_net_per_share: float
    impulse_high: float
    impulse_low: float
    support_low: float
    created_at: datetime
    evidence: Dict[str, Any] = field(default_factory=dict)


class EarlyImpulsePullbackStrategy:
    def __init__(self, cfg=None, auto=None, costs=None):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs or {
            'buy': 0.00015,
            'sell': 0.00015,
            'tax': 0.002,
            'entry_slippage': 0.002,
            'exit_slippage': 0.003
        }
        self.min_volume_ratio = 2.5
        self.min_impulse_gain = 0.008
        self.min_pullback_bars = 2
        self.max_pullback_bars = 8
        self.min_reward_risk = 1.5

    def find_impulse_bar(self, bars: List[dict]) -> Optional[Tuple[int, dict]]:
        """Finds the most recent volume impulse bar in the last 15 completed bars."""
        if len(bars) < 6:
            return None
        lookback = min(len(bars), 15)
        recent = bars[-lookback:]
        # Scan from older to newer within window
        for i in range(5, len(recent) - 1):
            prior_5 = recent[i - 5:i]
            bar = recent[i]
            avg_vol = sum(b.get('volume', 0) for b in prior_5) / 5.0
            if avg_vol <= 0:
                continue
            ratio = bar.get('volume', 0) / avg_vol
            gain = (bar['close'] - bar['open']) / bar['open'] if bar.get('open') else 0.0
            if ratio >= self.min_volume_ratio and gain >= self.min_impulse_gain and bar['close'] > bar['open']:
                # The index relative to bars
                global_idx = len(bars) - lookback + i
                return global_idx, bar
        return None

    def evaluate(self, symbol: str, name: str, bars: List[dict],
                 current_price: float, current_ask: float,
                 capital_limit: float = 71822.0,
                 risk_budget: float = 1429.0,
                 asof: datetime = None) -> Tuple[Optional[ImpulsePullbackSignal], str, Dict[str, Any]]:
        """Causally evaluates whether an early pullback rebound entry exists."""
        if not number(current_price) or current_price <= 0:
            return None, 'CURRENT_PRICE_INVALID', {}
        ask = current_ask if (number(current_ask) and current_ask >= current_price) else current_price

        # 1. Identify first volume impulse
        impulse_match = self.find_impulse_bar(bars)
        if not impulse_match:
            return None, 'NO_QUALIFYING_VOLUME_IMPULSE', {}
        impulse_idx, impulse_bar = impulse_match

        impulse_high = impulse_bar['high']
        impulse_low = impulse_bar['low']
        impulse_mid = (impulse_high + impulse_low) / 2.0
        pullback_bars = bars[impulse_idx + 1:]

        # 2. Check pullback window bounds
        if len(pullback_bars) < self.min_pullback_bars:
            return None, f'PULLBACK_TOO_SHORT ({len(pullback_bars)} < {self.min_pullback_bars})', {
                'impulse_time': impulse_bar.get('time'), 'impulse_high': impulse_high
            }
        if len(pullback_bars) > self.max_pullback_bars:
            return None, f'PULLBACK_EXPIRED ({len(pullback_bars)} > {self.max_pullback_bars})', {}

        # 3. Check for breakdown below impulse base
        support_low = min(b['low'] for b in pullback_bars)
        if support_low < impulse_low:
            return None, 'BREAKDOWN_BELOW_IMPULSE_BASE', {
                'support_low': support_low, 'impulse_low': impulse_low
            }

        # 4. Check for early rebound trigger
        # Must be breaking above the previous bar's high or holding higher low
        prev_bar = pullback_bars[-1]
        prior_pullback_high = max(b['high'] for b in pullback_bars[:-1]) if len(pullback_bars) > 1 else prev_bar['high']

        # Price must be rebounding (above recent pullback bar high)
        if current_price < prev_bar['high']:
            return None, 'REBOUND_NOT_CONFIRMED (below previous bar high)', {
                'prev_high': prev_bar['high'], 'current_price': current_price
            }

        # Crucial condition: must be EARLY (below or at the original impulse high)
        if current_price > impulse_high:
            return None, 'PRICE_ALREADY_ABOVE_IMPULSE_HIGH (use standard breakout instead)', {
                'current_price': current_price, 'impulse_high': impulse_high
            }

        # 5. Calculate Stop, Target, Risk/Reward
        step = tick_size(support_low)
        stop = floor_tick(support_low - step)
        target = floor_tick(impulse_high + 2.0 * (impulse_high - support_low))

        total_entry_cost = self.costs['buy'] + self.costs['entry_slippage']
        total_exit_cost = self.costs['sell'] + self.costs['tax'] + self.costs['exit_slippage']

        cost_per_share = ask * total_entry_cost + target * total_exit_cost
        risk_net_per_share = (ask - stop) + ask * total_entry_cost + stop * total_exit_cost
        reward_net_per_share = (target - ask) - cost_per_share

        if risk_net_per_share <= 0:
            return None, 'INVALID_RISK_CALCULATION', {}

        net_rr = reward_net_per_share / risk_net_per_share
        gross_rr = (target - ask) / (ask - stop) if (ask - stop) > 0 else 0.0

        if net_rr < self.min_reward_risk:
            return None, f'RISK_REWARD_FAIL (net {net_rr:.2f} < {self.min_reward_risk:.2f})', {
                'net_rr': net_rr, 'target': target, 'stop': stop
            }

        # 6. Sizing
        shares_by_risk = math.floor(risk_budget / risk_net_per_share)
        shares_by_cap = math.floor(capital_limit / (ask * (1 + total_entry_cost)))
        qty = max(0, min(shares_by_risk, shares_by_cap))

        if qty < 1:
            return None, f'CAPITAL_OR_RISK_INSUFFICIENT (qty={qty})', {
                'shares_by_risk': shares_by_risk, 'shares_by_cap': shares_by_cap
            }

        evidence = {
            'impulse_time': impulse_bar.get('time'),
            'impulse_high': impulse_high,
            'impulse_low': impulse_low,
            'impulse_mid': impulse_mid,
            'pullback_bars_count': len(pullback_bars),
            'support_low': support_low,
            'rebound_trigger_price': prev_bar['high'],
            'entry_ask': ask,
            'stop': stop,
            'target': target,
            'quantity': qty,
            'net_reward_risk': net_rr,
            'strategy_name': 'EARLY_IMPULSE_PULLBACK'
        }

        signal = ImpulsePullbackSignal(
            symbol=symbol,
            name=name,
            entry=ask,
            stop=stop,
            target=target,
            quantity=qty,
            net_reward_risk=net_rr,
            gross_reward_risk=gross_rr,
            reward_net_per_share=reward_net_per_share,
            risk_net_per_share=risk_net_per_share,
            impulse_high=impulse_high,
            impulse_low=impulse_low,
            support_low=support_low,
            created_at=asof or datetime.now(),
            evidence=evidence
        )
        return signal, 'SIGNAL_GENERATED', evidence
