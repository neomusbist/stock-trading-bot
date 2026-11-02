import unittest
from datetime import datetime, timedelta

from core.settings import KST
from automation.entry_lifecycle import (
    daily_risk_admission, max_quantity_for_remaining, price_recovery_candidate, LimitBuyOrder)

T0 = datetime(2026, 11, 2, 9, 30, tzinfo=KST)
S = lambda n: T0 + timedelta(seconds=n)
BUDGET = 70847.0 * 0.04


class DailyRisk(unittest.TestCase):
    def test_observed_overrun_is_blocked(self):
        # replay: losses 1299.9 + 1137.3 = 2437.2 realized; third order risk ~1,416 must be refused
        r = daily_risk_admission(BUDGET, 2437.2, 0.0, 1416.94)
        self.assertFalse(r['approved']); self.assertEqual(r['reason'], 'DAILY_RISK_EXCEEDED')
        self.assertAlmostEqual(r['remaining'], BUDGET - 2437.2, places=2)

    def test_open_risk_counts(self):
        self.assertFalse(daily_risk_admission(BUDGET, 0, 2000, 1000)['approved'])

    def test_boundary_exact_remaining_ok(self):
        self.assertTrue(daily_risk_admission(1000, 400, 100, 500)['approved'])
        self.assertFalse(daily_risk_admission(1000, 400, 100, 500.01)['approved'])

    def test_invalid_inputs_fail_closed(self):
        self.assertFalse(daily_risk_admission(1000, float('nan'), 0, 1)['approved'])
        self.assertFalse(daily_risk_admission(1000, -1, 0, 1)['approved'])

    def test_quantity_cap_from_remaining(self):
        self.assertEqual(max_quantity_for_remaining(396.68, 270.0), 1)
        self.assertEqual(max_quantity_for_remaining(0, 270.0), 0)


class Candidate(unittest.TestCase):
    base = dict(trigger=100, structural_stop=95, p_min=96, p_max=102, confirmation_closed_at=S(0))

    def q(self, ask, t=30):
        return [dict(time=S(t), bid=ask - 1, ask=ask)]

    def test_positive(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(30), price=100)], quotes=self.q(101))
        self.assertTrue(r['candidate']); self.assertEqual(r['limit'], 101)

    def test_no_chase_above_pmax(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(30), price=100)], quotes=self.q(103))
        self.assertFalse(r['candidate']); self.assertEqual(r['reason'], 'ASK_ABOVE_MAX_ENTRY_NO_CHASE')

    def test_boundary_ask_equals_pmax(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(30), price=100)], quotes=self.q(102))
        self.assertTrue(r['candidate'])

    def test_price_only_without_recovery_is_not_candidate(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(30), price=98)], quotes=self.q(98))
        self.assertFalse(r['candidate']); self.assertEqual(r['reason'], 'RECOVERY_NOT_CONFIRMED_BY_TRADES')

    def test_support_lost_first(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(10), price=95), dict(time=S(30), price=100)],
                                     quotes=self.q(101))
        self.assertEqual(r['reason'], 'SUPPORT_LOST_BEFORE_RECOVERY')

    def test_no_tape_unknown(self):
        r = price_recovery_candidate(**self.base, trades=[], quotes=self.q(101))
        self.assertEqual(r['status'], 'UNKNOWN')

    def test_no_quote_unknown(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(30), price=100)], quotes=[])
        self.assertEqual(r['status'], 'UNKNOWN')

    def test_trade_before_confirmation_ignored(self):
        r = price_recovery_candidate(**self.base, trades=[dict(time=S(-5), price=101)], quotes=self.q(101))
        self.assertEqual(r['status'], 'UNKNOWN')


class Lifecycle(unittest.TestCase):
    def order(self):
        return LimitBuyOrder('X', 100, 10, S(0), 120, 95)

    def test_unfilled_when_tape_above_limit(self):
        o = self.order(); self.assertEqual(o.on_trade(S(5), 101, 50), 0)
        self.assertEqual(o.state, 'PENDING'); self.assertEqual(o.filled, 0)

    def test_quote_never_fills(self):
        o = self.order(); o.on_quote(S(5), 98, 99)
        self.assertEqual(o.filled, 0)

    def test_equal_price_needs_size(self):
        o = self.order(); self.assertEqual(o.on_trade(S(5), 100), 0)
        self.assertEqual(o.on_trade(S(6), 100, 3), 3); self.assertEqual(o.state, 'PARTIAL')

    def test_partial_then_full(self):
        o = self.order(); o.on_trade(S(5), 99, 4); self.assertEqual(o.state, 'PARTIAL')
        o.on_trade(S(9), 99, 20); self.assertEqual(o.state, 'FILLED'); self.assertEqual(o.filled, 10)
        self.assertEqual(o.avg_price, 99)

    def test_fill_price_never_worse_than_limit(self):
        o = self.order(); o.on_trade(S(5), 99, 10); self.assertLessEqual(o.avg_price, 100)

    def test_timeout_cancels_remainder_keeps_partial(self):
        o = self.order(); o.on_trade(S(5), 99, 4)
        o.on_quote(S(130), 99, 100)
        self.assertEqual(o.state, 'PARTIAL_TIMEOUT'); self.assertEqual(o.filled, 4); self.assertEqual(o.remaining, 6)

    def test_no_fill_after_timeout(self):
        o = self.order(); self.assertEqual(o.on_trade(S(130), 90, 10), 0)

    def test_support_loss_cancels(self):
        o = self.order(); o.on_quote(S(10), 95, 96)
        self.assertEqual(o.state, 'CANCELLED_INVALIDATED_SUPPORT_LOST')

    def test_explicit_cancel_and_idempotent(self):
        o = self.order(); o.cancel(S(3)); self.assertEqual(o.state, 'CANCELLED_USER_CANCEL')
        self.assertEqual(o.on_trade(S(4), 90, 10), 0); o.cancel(S(5)); self.assertEqual(o.state, 'CANCELLED_USER_CANCEL')

    def test_expire(self):
        o = self.order(); o.expire(S(121)); self.assertEqual(o.state, 'CANCELLED_TIMEOUT')


if __name__ == '__main__':
    unittest.main()
