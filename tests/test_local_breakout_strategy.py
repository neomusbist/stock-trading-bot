"""Unit tests for LocalBreakoutStrategy and quote shift depth handling."""
import unittest
from datetime import datetime, timedelta
from dataclasses import replace

from core.settings import Config, KST
from core.strategy import Tick
from core.realtime import parse_tick, parse_book
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.local_breakout import LocalBreakoutStrategy, LocalBreakoutSignal

BASE_TIME = datetime(2026, 10, 20, 14, 30, tzinfo=KST)
SYM = '396300'


class LocalBreakoutStrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(
            buy=self.auto.commission_buy,
            sell=self.auto.commission_sell,
            tax=self.auto.sell_tax,
            entry_slippage=self.cfg.entry_slippage_fraction,
            exit_slippage=self.cfg.exit_slippage_fraction
        )
        self.strategy = LocalBreakoutStrategy(self.cfg, self.auto, self.costs)
        self.watch = {'code': SYM, 'name': 'HT로보틱스', 'resistance': 10500.0}

    def _make_bars(self, count=10, base_price=9600.0, step=10.0):
        bars = []
        for i in range(count):
            t = BASE_TIME + timedelta(minutes=i)
            o = base_price + i * step
            h = o + 20.0
            l = o - 10.0
            c = o + 10.0
            bars.append({
                'time': t,
                'open': o,
                'high': h,
                'low': l,
                'close': c,
                'volume': 10000 + i * 1000
            })
        return bars

    def test_insufficient_bars_rejected(self):
        bars = self._make_bars(count=3)
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=3), 9700.0, 100, 1000, 9700.0, 9690.0)
        sig, details = self.strategy.evaluate(tick, bars, self.watch)
        self.assertIsNone(sig)
        self.assertEqual(details['reason'], 'INSUFFICIENT_LOCAL_BARS')

    def test_invalid_quotes_rejected(self):
        bars = self._make_bars(count=10)
        # Inverted quotes: bid > ask
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=10), 9700.0, 100, 1000, 9690.0, 9710.0)
        sig, details = self.strategy.evaluate(tick, bars, self.watch)
        self.assertIsNone(sig)
        self.assertEqual(details['reason'], 'INVALID_EXECUTABLE_QUOTE')

    def test_spread_too_wide_rejected(self):
        bars = self._make_bars(count=10)
        # Spread > 0.4% (e.g. 50 on 9700 = 0.51%)
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=10), 9700.0, 100, 1000, 9730.0, 9680.0)
        sig, details = self.strategy.evaluate(tick, bars, self.watch)
        self.assertIsNone(sig)
        self.assertEqual(details['reason'], 'SPREAD_TOO_WIDE')

    def test_resistance_not_broken_rejected(self):
        bars = self._make_bars(count=10, base_price=9600.0)
        local_high = max(b['high'] for b in bars)
        # Price below local high
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=10), local_high - 10.0, 100, 1000, local_high - 5.0, local_high - 10.0)
        sig, details = self.strategy.evaluate(tick, bars, self.watch)
        self.assertIsNone(sig)
        self.assertEqual(details['reason'], 'LOCAL_RESISTANCE_NOT_BROKEN')

    def test_max_chase_exceeded_rejected(self):
        bars = self._make_bars(count=10, base_price=9600.0)
        local_high = max(b['high'] for b in bars)
        # Price chased > 0.5% (e.g. +1.0%)
        chased_price = local_high * 1.010
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=10), chased_price, 100, 1000, chased_price, chased_price - 10.0)
        sig, details = self.strategy.evaluate(tick, bars, self.watch)
        self.assertIsNone(sig)
        self.assertEqual(details['reason'], 'MAX_CHASE_EXCEEDED')

    def test_valid_breakout_signal_generated(self):
        bars = []
        for i in range(10):
            t = BASE_TIME + timedelta(minutes=i)
            bars.append({
                'time': t,
                'open': 9500.0,
                'high': 9600.0,
                'low': 9480.0,
                'close': 9580.0,
                'volume': 15000
            })
        local_high = max(b['high'] for b in bars)
        breakout_entry = local_high + 10.0  # +0.1% above local high (within 0.5% chase)
        bid = breakout_entry - 5.0  # spread = 5 / 9600 = 0.05% < 0.4%
        tick = Tick(SYM, BASE_TIME + timedelta(minutes=10), breakout_entry, 1000, 50000, breakout_entry, bid)

        sig, details = self.strategy.evaluate(tick, bars, self.watch, capital_limit=81300., risk_budget=1626.)
        self.assertIsNotNone(sig)
        self.assertTrue(details['pass'])
        self.assertEqual(sig.pattern, 'LOCAL_BREAKOUT')
        self.assertEqual(sig.limit, breakout_entry)
        self.assertLess(sig.stop, sig.limit)
        self.assertGreater(sig.target, sig.limit)
        self.assertGreater(sig.quantity, 0)
        self.assertGreaterEqual(sig.gross_reward_risk, 1.5)
        self.assertGreaterEqual(sig.net_reward_risk, 0.95)
        self.assertFalse(sig.extra['actual_order_permission'])
        self.assertTrue(sig.extra['SHADOW_ONLY'])


if __name__ == '__main__':
    unittest.main()
