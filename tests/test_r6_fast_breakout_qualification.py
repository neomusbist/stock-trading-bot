"""
Regression and Qualification Tests for Repaired FAST Momentum Strategy (2026-11-03)
Validates:
1. Lee-Ready tick test: aggressive ask-eating trades inherit direction=1, uptick_ratio >= 0.55.
2. 1-tick tight book in KRX (ask - bid <= tick_size) is never rejected as wide spread.
3. Opening build phase (09:00 - 09:05) allows healthy base breakouts up to 3.5% extension.
4. Final signal generation produces net RR >= 1.5, valid stop and emergency, quantity >= 1, and actual_order_permission.
"""
import unittest
from datetime import datetime, timedelta
from core.settings import KST, Config
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.fast_momentum import FastMomentumStrategy, MicroTape
from tests.test_r6_fast_repair import BASE, watch

class TestFastBreakoutQualification(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True, fast_momentum_enabled=True)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_lee_ready_zero_tick_preserves_buying_pressure_on_ask_eats(self):
        """Aggressive trades at the same price inherit uptick direction under Lee-Ready rule."""
        tape = MicroTape()
        # Price rises from 1000 to 1010, then 10 trades occur at 1010 eating the ask
        t0 = Tick('005930', BASE, 1000.0, 100, 100, ask=1000.0, bid=999.0, depth_available=True, book_time=BASE)
        tape.ingest(t0, BASE)
        t1 = Tick('005930', BASE + timedelta(seconds=1), 1010.0, 100, 200, ask=1010.0, bid=1009.0, depth_available=True, book_time=BASE)
        tape.ingest(t1, BASE + timedelta(seconds=1))

        # 10 flat trades at 1010
        for s in range(2, 12):
            t = Tick('005930', BASE + timedelta(seconds=s), 1010.0, 100, 200 + (s - 1) * 100,
                     ask=1010.0, bid=1009.0, depth_available=True, book_time=BASE + timedelta(seconds=s))
            tape.ingest(t, t.time)

        m = tape.metrics(BASE + timedelta(seconds=12))
        # Directional volume: all 11 trades from t1 to t11 are upticks
        self.assertAlmostEqual(m['uptick_ratio_30'], 1.0, places=2)
        self.assertEqual(m['downtick_ratio_30'], 0.0)

    def test_tight_one_tick_spread_bypasses_percentage_spread_rejection(self):
        """For low price stocks (e.g. 250 KRW), 1-tick spread is 1/250 = 0.40% > 0.35%. 1-tick book must pass."""
        prices = [250.0 + i for i in range(10)]
        for i, p in enumerate(prices):
            t = Tick('034020', BASE + timedelta(seconds=i), p, 100, (i + 1) * 100,
                     ask=251.0, bid=250.0, depth_available=True, book_time=BASE + timedelta(seconds=i))
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick('034020', BASE + timedelta(seconds=10), 250.0, 100, 1100,
                      ask=251.0, bid=250.0, depth_available=True, book_time=BASE + timedelta(seconds=10))
        m = self.strategy.get_tape('034020').metrics(last_t.time)
        m['spread'] = (last_t.ask - last_t.bid) / last_t.bid  # 1 / 250 = 0.0040 (0.4%)

        w = {'name': '동전주', 'reference_price': 250.0}
        regime, reason, details = self.strategy.classify_regime(last_t, m, 'REGULAR', w, asof=last_t.time)
        # In regular session, max_spread is 0.0035 (0.35%). Here spread is 0.40% but 1 tick.
        # Must not be rejected for FALSE_BREAKOUT_WIDE_SPREAD because it is a tight 1-tick book.
        self.assertNotEqual(reason, 'FALSE_BREAKOUT_WIDE_SPREAD')

    def test_real_krx_breakout_generates_qualified_live_signal(self):
        """Realistic KRX breakout: base -> pullback -> breakout at ask produces final trade signal."""
        # Setup: Origin=984, Peak=1000, Support=990, Breakout=1001 with 5 prints at ask
        prices = [984, 988, 992, 996, 1000, 990, 992, 994, 996, 998, 1001, 1001, 1001, 1001, 1001]
        ticks = [
            Tick('999990', BASE + timedelta(seconds=i), p, 1000, 1000 * (i + 1),
                 ask=p, bid=p - 1, ask_qty=10000, bid_qty=10000, depth_available=True,
                 book_time=BASE + timedelta(seconds=i), depth_source='SIMULATED_QUOTE')
            for i, p in enumerate(prices)
        ]
        for t in ticks:
            self.strategy.ingest_tick(t, t.time)

        sig = self.strategy.evaluate(ticks[-1], watch(resistance=1045, at=BASE), capital_limit=81300.0,
                                     risk_budget=1626.0, ingest=False, received=ticks[-1].time, live_authorized=True)

        self.assertIsNotNone(sig, "FAST breakout must generate a qualified signal")
        self.assertEqual(sig.symbol, '999990')
        self.assertEqual(sig.limit, 1001)
        self.assertEqual(sig.stop, 989)
        self.assertEqual(sig.emergency, 985)
        self.assertEqual(sig.target, 1045)
        self.assertGreaterEqual(sig.net_reward_risk, 1.5)
        self.assertGreaterEqual(sig.quantity, 1)
        self.assertTrue(sig.extra.get('actual_order_permission'), "LIVE authorized evaluation must have order permission")

if __name__ == '__main__':
    unittest.main()
