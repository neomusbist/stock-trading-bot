"""DEV/PAPER-only: price AND recovery gated entry candidates, a daily-risk admission gate and a
limit-buy order lifecycle that never invents fills.

Nothing here is wired into the LIVE engine. Fill rules are deliberately conservative:
* A buy limit fills only on a real trade print strictly below the limit; a print equal to the
  limit fills only the printed size (queue position is unknowable -> no size, no fill).
* Quote observations never fill an order.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math

from core.util import floor_tick

# ---------------------------------------------------------------- daily risk admission

def daily_risk_admission(daily_budget, realized_loss, open_risk, order_risk):
    """Approve a new order only if realized loss + open risk + THIS order's risk <= budget.

    The observed overrun (-3,442.7 won vs 2,833.88 won) came from a gate that tested only
    ``realized_loss >= budget`` before entry, so an order whose own risk exceeded the remainder
    was still admitted. Remaining risk must be netted against the expected order risk first.
    """
    vals = (daily_budget, realized_loss, open_risk, order_risk)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in vals):
        return dict(approved=False, reason='INVALID_RISK_INPUT', remaining=0.0)
    remaining = daily_budget - realized_loss - open_risk
    if order_risk > remaining + 1e-9:
        return dict(approved=False, reason='DAILY_RISK_EXCEEDED', remaining=max(0.0, remaining),
                    order_risk=order_risk)
    return dict(approved=True, reason='WITHIN_DAILY_RISK', remaining=remaining, order_risk=order_risk)


def max_quantity_for_remaining(remaining, risk_per_share):
    if (type(remaining) not in (int, float) or type(risk_per_share) not in (int, float)
            or not math.isfinite(remaining) or not math.isfinite(risk_per_share)
            or remaining <= 0 or risk_per_share <= 0):
        return 0
    return int(remaining // risk_per_share)

# ---------------------------------------------------------------- price + recovery candidate

def price_recovery_candidate(*, trigger, structural_stop, p_min, p_max, confirmation_closed_at, trades, quotes,
                             search_seconds=300):
    """Order candidate only when ALL hold, in order:
    1. recovery confirmed by a real trade print >= trigger after the confirmation bar closed,
    2. no print/bid at or below the structural stop before that print (support still held),
    3. the ask at that moment is within [p_min, p_max] (never chase above p_max).

    trades/quotes: lists of dicts with ``time`` (+``price`` / ``bid``,``ask``). Missing tape -> UNKNOWN.
    """
    end = confirmation_closed_at + timedelta(seconds=search_seconds)
    tr = sorted((t for t in trades if confirmation_closed_at <= t['time'] <= end), key=lambda x: x['time'])
    if not tr:
        return dict(status='UNKNOWN', reason='NO_TRADE_TAPE_AFTER_CONFIRMATION')
    for t in tr:
        if t['price'] <= structural_stop:
            return dict(status='KNOWN', candidate=False, reason='SUPPORT_LOST_BEFORE_RECOVERY', at=t['time'])
        if t['price'] >= trigger:
            q = [x for x in quotes if x['time'] <= t['time'] and x['time'] >= confirmation_closed_at
                 and 0 < x['bid'] <= x['ask']]
            if not q:
                return dict(status='UNKNOWN', reason='NO_QUOTE_AT_RECOVERY', at=t['time'])
            ask = max(q, key=lambda x: x['time'])['ask']
            if ask > p_max:
                return dict(status='KNOWN', candidate=False, reason='ASK_ABOVE_MAX_ENTRY_NO_CHASE',
                            at=t['time'], ask=ask, p_max=p_max)
            if ask < p_min:
                return dict(status='KNOWN', candidate=False, reason='ASK_BELOW_MIN_SUPPORT', at=t['time'], ask=ask)
            return dict(status='KNOWN', candidate=True, reason='PRICE_AND_RECOVERY_CONFIRMED', at=t['time'],
                        limit=floor_tick(ask), p_max=p_max)
    return dict(status='KNOWN', candidate=False, reason='RECOVERY_NOT_CONFIRMED_BY_TRADES')

# ---------------------------------------------------------------- limit buy lifecycle

@dataclass
class LimitBuyOrder:
    symbol: str
    limit: float
    qty: int
    submitted_at: datetime
    timeout_seconds: int = 120
    structural_stop: float = 0.0
    state: str = 'PENDING'
    filled: int = 0
    notional: float = 0.0
    history: list = field(default_factory=list)

    @property
    def remaining(self):
        return self.qty - self.filled

    @property
    def avg_price(self):
        return self.notional / self.filled if self.filled else None

    def _log(self, at, event, **kw):
        self.history.append(dict(at=at, event=event, **kw))

    def on_trade(self, at, price, size=None):
        """Apply a real trade print. Returns filled quantity (0 if none)."""
        if self.state not in ('PENDING', 'PARTIAL'):
            return 0
        if at < self.submitted_at:
            return 0
        if at > self.submitted_at + timedelta(seconds=self.timeout_seconds):
            return 0
        if price < self.limit:
            q = self.remaining if size is None else min(self.remaining, int(size))
        elif price == self.limit and size is not None:
            q = min(self.remaining, int(size))
        else:
            return 0
        if q <= 0:
            return 0
        self.filled += q
        self.notional += q * min(price, self.limit)
        self.state = 'FILLED' if self.remaining == 0 else 'PARTIAL'
        self._log(at, self.state, qty=q, price=price)
        return q

    def on_quote(self, at, bid, ask):
        """Re-evaluate on a quote: support loss cancels the unfilled remainder; timeout expires it."""
        if self.state not in ('PENDING', 'PARTIAL'):
            return self.state
        if bid and self.structural_stop and bid <= self.structural_stop:
            return self._cancel(at, 'INVALIDATED_SUPPORT_LOST')
        if at > self.submitted_at + timedelta(seconds=self.timeout_seconds):
            return self._cancel(at, 'TIMEOUT')
        return self.state

    def cancel(self, at, reason='USER_CANCEL'):
        return self._cancel(at, reason)

    def _cancel(self, at, reason):
        if self.state not in ('PENDING', 'PARTIAL'):
            return self.state
        self.state = ('PARTIAL_' if self.filled else 'CANCELLED_') + reason
        self._log(at, self.state, filled=self.filled, unfilled=self.remaining)
        return self.state

    def expire(self, at):
        return self.on_quote(at, 0, 0) if at > self.submitted_at + timedelta(seconds=self.timeout_seconds) else self.state
