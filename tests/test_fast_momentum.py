"""
Unit and Integration Tests for R6 Fast Momentum Strategy (automation/fast_momentum.py)
"""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from automation.fast_momentum import FastMomentumStrategy, FastSignal, MicroTape
from automation.session import KST

class TestFastMomentumStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = FastMomentumStrategy()
        self.base_time = datetime(2026, 10, 14, 9, 1, 0, tzinfo=KST)

    def _tick(self, symbol="005930", price=70000.0, volume=100, delta_seconds=0, bid=None, ask=None):
        t = SimpleNamespace(
            symbol=symbol,
            price=float(price),
            volume=int(volume),
            time=self.base_time + timedelta(seconds=delta_seconds),
            bid=float(bid or price - 50),
            ask=float(ask or price + 50)
        )
        return t

    def test_micro_tape_metrics(self):
        tape = MicroTape(max_seconds=120.0)
        for s in range(20):
            t = self._tick(price=70000.0 + s * 50, volume=100, delta_seconds=s)
            tape.ingest(t)

        m = tape.metrics(self.base_time + timedelta(seconds=19))
        self.assertEqual(m['ticks_count'], 20)
        self.assertGreater(m['turnover_10'], 0)
        self.assertEqual(m['uptick_ratio_30'], 1.0)
        self.assertEqual(m['p_min_30'], 70000.0)
        self.assertEqual(m['p_max_30'], 70950.0)

    def test_regime_a_early_momentum_entry(self):
        """Regime A: Aggressive early buying with tight spread -> emits valid signal."""
        # Feed 10 ticks of building volume and upward ticks
        for s in range(10):
            t = self._tick(
                price=50000.0 + s * 50,
                volume=300,
                delta_seconds=s * 2,
                bid=50000.0 + s * 50,
                ask=50050.0 + s * 50
            )
            self.strategy.ingest_tick(t)

        watch = {'name': '유효후보', 'reference_price': 49800.0, 'resistance': 52500.0}
        last_t = self._tick(
            price=50450.0,
            volume=500,
            delta_seconds=19,
            bid=50450.0,
            ask=50500.0
        )
        sig = self.strategy.evaluate(last_t, watch, capital_limit=81300.0, risk_budget=1626.0)

        self.assertIsNotNone(sig)
        self.assertEqual(sig.regime, 'A')
        self.assertGreaterEqual(sig.net_reward_risk, 1.5)
        self.assertLess(sig.stop, sig.limit)
        self.assertGreater(sig.target, sig.limit)
        self.assertGreaterEqual(sig.quantity, 1)

    def test_regime_b_pan_ocean_late_chase_rejected(self):
        """Regime B: Price already extended > 2.5% in 60s -> REJECTED (pan ocean protection)."""
        # Price starts at 6550, rapidly surges to 6970 (> 6% up)
        for s in range(15):
            p = 6550.0 + s * 30
            t = self._tick(symbol="028670", price=p, volume=200, delta_seconds=s * 3)
            self.strategy.ingest_tick(t)

        watch = {'name': '팬오션', 'reference_price': 6550.0}
        top_t = self._tick(
            symbol="028670",
            price=6970.0,
            volume=100,
            delta_seconds=50,
            bid=6960.0,
            ask=6970.0
        )
        sig = self.strategy.evaluate(top_t, watch, capital_limit=81300.0, risk_budget=1626.0)
        self.assertIsNone(sig, "Overextended chase at 6,970 must be rejected as Regime B")

    def test_regime_c_quote_spoof_wide_spread_rejected(self):
        """Regime C: Micro spike on wide spread -> REJECTED."""
        for s in range(5):
            t = self._tick(
                price=10000.0,
                volume=10,
                delta_seconds=s * 2,
                bid=9900.0,
                ask=10050.0  # spread > 1.5%
            )
            self.strategy.ingest_tick(t)

        watch = {'name': '허위돌파', 'reference_price': 10000.0}
        last_t = self._tick(price=10050.0, volume=10, delta_seconds=10, bid=9900.0, ask=10050.0)
        sig = self.strategy.evaluate(last_t, watch)
        self.assertIsNone(sig, "Wide spread must be rejected as Regime C")

    def test_regime_d_downtrend_bounce_rejected(self):
        """Regime D: Dead-cat bounce in downtrend -> REJECTED."""
        # Reference price was 20,000, currently trading at 19,200 (-4%)
        for s in range(8):
            t = self._tick(
                price=19200.0 + s * 20,
                volume=100,
                delta_seconds=s * 2,
                bid=19200.0 + s * 20,
                ask=19220.0 + s * 20
            )
            self.strategy.ingest_tick(t)

        watch = {'name': '하락주', 'reference_price': 20000.0}
        last_t = self._tick(price=19340.0, volume=100, delta_seconds=16)
        sig = self.strategy.evaluate(last_t, watch)
        self.assertIsNone(sig, "Downtrend bounce must be rejected as Regime D")

    def test_signal_ttl_and_chase_expiration(self):
        """Signal expires if TTL elapsed > 15s or price chased > 0.3%."""
        created_at = self.base_time
        sig = FastSignal(
            symbol="005930",
            name="삼성전자",
            limit=70000.0,
            stop=69300.0,
            target=71500.0,
            emergency=69000.0,
            quantity=1,
            net_reward_risk=1.8,
            gross_reward_risk=2.1,
            reward_net_per_share=1300.0,
            risk_net_per_share=720.0,
            regime='A',
            phase='OPENING_EARLY',
            created_at=created_at,
            ttl_seconds=15.0,
            max_chase_fraction=0.003
        )

        # Within TTL and price: NOT expired
        exp, reason = sig.is_expired(created_at + timedelta(seconds=10), 70100.0)
        self.assertFalse(exp)

        # TTL expired (> 15s)
        exp_ttl, reason_ttl = sig.is_expired(created_at + timedelta(seconds=16), 70100.0)
        self.assertTrue(exp_ttl)
        self.assertIn("SIGNAL_TTL_EXPIRED", reason_ttl)

        # Max chase exceeded (70,000 * 1.003 = 70,210; ask is 70,300)
        exp_chase, reason_chase = sig.is_expired(created_at + timedelta(seconds=5), 70300.0)
        self.assertTrue(exp_chase)
        self.assertIn("MAX_CHASE_EXCEEDED", reason_chase)

    def test_opening_early_phase_0900_to_0902(self):
        """09:00~09:02 phase evaluates early momentum without 1-min completed bars."""
        early_time = datetime(2026, 10, 14, 9, 0, 30, tzinfo=KST)
        phase = self.strategy.determine_phase(early_time)
        self.assertEqual(phase, 'OPENING_EARLY')

        for s in range(6):
            t = SimpleNamespace(
                symbol="000660",
                price=180000.0 + s * 200,
                volume=50,
                time=early_time + timedelta(seconds=s * 3),
                bid=180000.0 + s * 200,
                ask=180100.0 + s * 200
            )
            self.strategy.ingest_tick(t)

        watch = {'name': 'SK하이닉스', 'reference_price': 179500.0}
        last_t = SimpleNamespace(
            symbol="000660",
            price=181000.0,
            volume=80,
            time=early_time + timedelta(seconds=16),
            bid=181000.0,
            ask=181200.0
        )
        # Sizing with larger capital to allow 1 share of 180k
        sig = self.strategy.evaluate(last_t, watch, capital_limit=250000.0, risk_budget=5000.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.phase, 'OPENING_EARLY')

    def test_exit_plan_invalidation_and_scratch(self):
        """3-tier exit: Stop loss, emergency plunge, and momentum stall scratch."""
        pos = {
            'symbol': '005930',
            'entry': 70000.0,
            'qty': 1,
            'stop': 69300.0,
            'emergency': 69000.0,
            'opened': self.base_time.isoformat()
        }

        # 1. Normal hold
        t_hold = self._tick(price=70100.0, delta_seconds=30)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_hold, self.base_time + timedelta(seconds=30))
        self.assertFalse(should_exit)

        # 2. Invalidation stop exit
        t_stop = self._tick(price=69200.0, delta_seconds=40)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_stop, self.base_time + timedelta(seconds=40))
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'INVALIDATION_STOP_EXIT')

        # 3. Emergency plunge exit
        t_emg = self._tick(price=68900.0, delta_seconds=40)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_emg, self.base_time + timedelta(seconds=40))
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'EMERGENCY_PLUNGE_EXIT')

        # 4. Momentum stall scratch exit after 60s
        # Simulate heavy downticks in the tape
        for s in range(10):
            t_down = self._tick(price=70000.0 - s * 20, volume=150, delta_seconds=60 + s)
            self.strategy.ingest_tick(t_down)

        t_stall = self._tick(price=69850.0, delta_seconds=75)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_stall, self.base_time + timedelta(seconds=75))
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'MOMENTUM_STALL_SCRATCH_EXIT')

    def test_profit_protection_lock_at_one_r(self):
        """Profit protection moves stop to break-even at +1R."""
        pos = {
            'symbol': '005930',
            'entry': 70000.0,
            'qty': 1,
            'stop': 69300.0,  # 1R = 700 KRW
            'emergency': 69000.0,
            'opened': self.base_time.isoformat()
        }
        # Price hits 70,800 (+800 KRW > 1R)
        t_profit = self._tick(price=70800.0, delta_seconds=30)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_profit, self.base_time + timedelta(seconds=30))
        self.assertFalse(should_exit)
        self.assertTrue(pos.get('profit_locked'))
        self.assertGreater(pos['stop'], 70000.0, "Stop must be moved above entry price to lock profit")


if __name__ == '__main__':
    unittest.main()
