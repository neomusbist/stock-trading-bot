"""Local Breakout and Hold Strategy for confirmed resistance breaches."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional
import math

from core.util import floor_tick, tick_size
from automation.decisions import gate
from core.market_time import tick_freshness, book_freshness
from core.market_evidence import known_signal_bars, finite
from automation.exit_plan import build_plan

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

        # Source correctness, not an additional momentum/volume multiplier.
        # A cache's minute label or later OHLC does not prove live availability.
        bars,reason=known_signal_bars(bars[-self.lookback_bars:],now,self.min_support_bars)
        if reason:return self._reject(symbol,reason,data_status='UNKNOWN')
        if any(not finite(b.get('volume')) for b in bars):
            return self._reject(symbol,'BAR_VOLUME_EVIDENCE_MISSING',data_status='UNKNOWN')
        if not getattr(t,'regular',False) or not tick_freshness(t,now,self.cfg.stale_seconds)['fresh']:
            return self._reject(symbol,'TRADE_STALE_OR_FUTURE',data_status='UNKNOWN')

        # Quotes validation
        bid = getattr(t, 'bid', None)
        ask = getattr(t, 'ask', None)
        price = getattr(t, 'price', None)
        if not (is_valid_number(bid) and is_valid_number(ask) and is_valid_number(price)) or bid > ask:
            return self._reject(symbol, 'INVALID_EXECUTABLE_QUOTE')

        spread = (ask - bid) / price
        if spread > self.max_spread:
            return self._reject(symbol, 'SPREAD_TOO_WIDE', spread=spread, max_spread=self.max_spread)
        if (getattr(t,'depth_available',None) is not True
                or getattr(t,'depth_source','UNKNOWN') not in {'0D','0B_FIELDS'}
                or not book_freshness(t,now,self.cfg.stale_seconds)['fresh']
                or not finite(getattr(t,'ask_qty',None)) or not finite(getattr(t,'bid_qty',None))):
            return self._reject(symbol,'MEASURED_FRESH_BOOK_REQUIRED',data_status='UNKNOWN')

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
        if is_valid_number(overhead):
            # A nearby known ceiling must not disappear merely because it is
            # too close to satisfy this strategy's desired reward/risk.
            if overhead<=entry:return self._reject(symbol,'KNOWN_RESISTANCE_AT_OR_BELOW_ENTRY')
            target=min(target,floor_tick(overhead))

        # Full cost accounting
        fee = self.costs.estimate(entry, target)
        roundtrip_cost = self.costs.roundtrip(entry, emergency_stop) + emergency_stop * self.costs.exit_slippage
        reward_net = (target - entry) - fee
        # Quantity and advertised risk must use the final executable emergency
        # line, just as AutonomousEngine.quantity_details does.
        risk_net = (entry - emergency_stop) + roundtrip_cost

        if risk_net <= 0:
            return self._reject(symbol, 'INVALID_RISK_NET')

        net_rr = reward_net / risk_net
        gross_rr = (target - entry) / (entry - emergency_stop) if entry > emergency_stop else 0.0

        if gross_rr < self.min_gross_rr or net_rr < self.min_net_rr:
            return self._reject(symbol, 'REWARD_RISK_INSUFFICIENT',
                                net_rr=net_rr, min_net_rr=self.min_net_rr,
                                gross_rr=gross_rr, min_gross_rr=self.min_gross_rr,
                                reward_net=reward_net, risk_net=risk_net)

        common_plan=build_plan(self.cfg,self.auto,{'pattern':'LOCAL_BREAKOUT',
            'limit':entry,'stop':emergency_stop,'emergency':emergency_stop,'target':target,
            'invalidation_policy':{'enabled':True,'structural_stop':structural_stop,
                                   'emergency_stop':emergency_stop}})
        if not common_plan['reward_guard_pass']:
            return self._reject(symbol,'COMMON_ENGINE_REWARD_GUARD',
                net_rr=common_plan['net_reward_risk'],minimum_net_rr=common_plan['minimum_net_reward_risk'],
                target=target,structural_stop=structural_stop,emergency_stop=emergency_stop,
                basis='SAME_FINAL_EMERGENCY_AND_COSTS_AS_R6_ENGINE')

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
                ,'risk_basis':'EMERGENCY_PLUS_ROUNDTRIP_AND_EXIT_RESERVE'
                ,'bar_available_at':[b['available_at'] for b in bars]
                ,'book_time':t.book_time.isoformat(),'book_received_at':getattr(t,'book_received_at',None)
                ,'bar_volume_sum':sum(b['volume'] for b in bars)
                ,'R6_common_engine_eligibility':'REWARD_CHECK_ONLY_OTHER_ORDER_GATES_STILL_REQUIRED'
            }
        )
        self.last_signal[symbol] = signal
        return signal, {'pass': True, 'signal': signal}
