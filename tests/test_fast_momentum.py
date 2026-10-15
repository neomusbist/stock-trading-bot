"""
Unit and Integration Tests for R6 Fast Momentum Strategy (automation/fast_momentum.py)
"""
import unittest
from datetime import datetime, timedelta
from core.strategy import Tick
from tests.test_r6_fast_repair import ticks, watch

from automation.fast_momentum import FastMomentumStrategy, FastSignal, MicroTape
from automation.session import KST

class TestFastMomentumStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = FastMomentumStrategy()
        self.base_time = datetime(2026, 10, 14, 9, 1, 0, tzinfo=KST)

    def _tick(self, symbol="005930", price=70000.0, volume=100, delta_seconds=0, bid=None, ask=None):
        stamp = self.base_time + timedelta(seconds=delta_seconds)
        t = Tick(
            symbol=symbol,
            price=float(price),
            volume=int(volume),
            cumulative=int((delta_seconds+1)*volume),
            time=stamp,
            bid=float(bid or price - 50),
            ask=float(ask or price + 50),
            bid_qty=10000, ask_qty=10000, depth_available=True,
            book_time=stamp, depth_source='SIMULATED_QUOTE'
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
        """Independent causal support and a timestamped ceiling permit an early candidate."""
        data = ticks(start=self.base_time)
        for t in data:
            self.strategy.ingest_tick(t, t.time)
        sig = self.strategy.evaluate(data[-1], watch(at=self.base_time), capital_limit=81300.0,
                                     risk_budget=1626.0, ingest=False, received=data[-1].time)

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

        data = ticks(start=early_time)
        for t in data:
            self.strategy.ingest_tick(t, t.time)
        sig = self.strategy.evaluate(data[-1], watch(at=early_time), capital_limit=81300.0,
                                     risk_budget=1626.0, ingest=False, received=data[-1].time)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.phase, 'OPENING_EARLY')

    def test_exit_plan_invalidation_and_scratch(self):
        """Confirmed soft breach, immediate executable hard breach, and failed rebound."""
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

        # A single touch holds; a second independent print confirms the breach.
        t_stop = self._tick(price=69200.0, delta_seconds=40)
        self.strategy.ingest_tick(t_stop, t_stop.time)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_stop, self.base_time + timedelta(seconds=40))
        self.assertFalse(should_exit)
        t_stop2 = self._tick(price=69150.0, delta_seconds=41)
        self.strategy.ingest_tick(t_stop2, t_stop2.time)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_stop2, t_stop2.time)
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'INVALIDATION_STOP_EXIT')

        # 3. Emergency plunge exit
        t_emg = self._tick(price=68900.0, delta_seconds=42)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_emg, t_emg.time)
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'EMERGENCY_PLUNGE_EXIT')

        # A lower rebound high and two prints below its floor are causal evidence.
        self.strategy = FastMomentumStrategy()
        for s, price in enumerate([70000, 70200, 70050, 70150, 70040, 70030]):
            t_down = self._tick(price=price, volume=150, delta_seconds=60+s)
            self.strategy.ingest_tick(t_down, t_down.time)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_down, t_down.time)
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'MOMENTUM_FAILURE_EXIT')

    def test_profit_protection_requires_confirmed_post_fill_structure(self):
        """An excursion alone holds; a later confirmed higher low can protect profit."""
        pos = {
            'symbol': '005930',
            'entry': 70000.0,
            'qty': 1,
            'stop': 69300.0,  # 1R = 700 KRW
            'emergency': 69000.0,
            'opened': self.base_time.isoformat()
        }
        t_profit = self._tick(price=71100.0, delta_seconds=30)
        self.strategy.ingest_tick(t_profit, t_profit.time)
        should_exit, reason, _ = self.strategy.evaluate_exit(pos, t_profit, self.base_time + timedelta(seconds=30))
        self.assertFalse(should_exit)
        self.assertFalse(pos.get('profit_locked', False))
        self.assertEqual(pos['stop'], 69300)
        for s, price in enumerate([71500, 71200, 71600], 31):
            t_profit = self._tick(price=price, delta_seconds=s)
            self.strategy.ingest_tick(t_profit, t_profit.time)
            outcome = self.strategy.evaluate_exit(pos, t_profit, t_profit.time)
        self.assertFalse(outcome[0])
        self.assertTrue(pos.get('profit_locked'))
        self.assertGreater(pos['protection_stop'], 70000)
        self.assertEqual(pos['stop'], 69300)

    def test_sm_bexel_incident_reproduction_holds_noise(self):
        """2026-10-15 17:43 SM Bexel incident reproduction:
        Entry 3,305 KRW, stop 3,290 KRW, emergency 3,285 KRW.
        Within 21 seconds, price fluctuates between 3,300 and 3,315 (5 KRW 1-tick noise).
        With 45s settling window and multi-step swing filtering, the bot MUST HOLD and NOT exit.
        """
        pos = {
            'symbol': '010580',
            'entry': 3305.0,
            'qty': 21,
            'stop': 3290.0,
            'emergency': 3285.0,
            'opened': self.base_time.isoformat()
        }
        bexel_ticks = [
            (0, 3305.0, 3300.0, 3305.0),
            (3, 3310.0, 3305.0, 3310.0),
            (8, 3315.0, 3310.0, 3315.0),  # Peak excursion +10 KRW (+0.3%)
            (12, 3310.0, 3305.0, 3310.0), # 1-tick dip
            (15, 3315.0, 3310.0, 3315.0), # 1-tick bounce
            (18, 3310.0, 3305.0, 3310.0), # 1-tick dip
            (20, 3305.0, 3300.0, 3305.0), # Back to entry
            (21, 3300.0, 3300.0, 3305.0), # Retest breakout level at 3,300 KRW
        ]
        strat = FastMomentumStrategy()
        for idx, (sec, price, bid, ask) in enumerate(bexel_ticks):
            t_stamp = self.base_time + timedelta(seconds=sec)
            t = Tick(symbol='010580', price=price, volume=100, cumulative=(idx + 1) * 100,
                     time=t_stamp, bid=bid, ask=ask, bid_qty=500, ask_qty=500,
                     depth_available=True, book_time=t_stamp)
            strat.ingest_tick(t, t.time)

        # Evaluate at 21 seconds when bid hit 3,300 KRW (above structural stop 3,290 KRW)
        should_exit, reason, evidence = strat.evaluate_exit(pos, t, t.time)
        self.assertFalse(should_exit, "1-tick noise pullback to 3,300 must NOT trigger premature momentum failure exit")
        self.assertEqual(reason, 'HOLD')
        self.assertLess(evidence['holding_seconds'], 45.0)

        # Even if evaluated after 50 seconds with the same 5 KRW tick noise,
        # multi-step swing filtering (>= 2 * min_step) must prevent false momentum failure exit!
        future_time = self.base_time + timedelta(seconds=50)
        t_50 = Tick(symbol='010580', price=3300.0, volume=100, cumulative=1000,
                    time=future_time, bid=3300.0, ask=3305.0, bid_qty=500, ask_qty=500,
                    depth_available=True, book_time=future_time)
        strat.ingest_tick(t_50, t_50.time)
        should_exit_50, reason_50, _ = strat.evaluate_exit(pos, t_50, future_time)
        self.assertFalse(should_exit_50, "1-tick (5 KRW) micro-fluctuations must be filtered as noise even after settling window")
        self.assertEqual(reason_50, 'HOLD')

    def test_sm_bexel_real_structural_invalidation_counter_example(self):
        """Counter-example: True breakdown to structural stop (3,290 KRW) or emergency (3,285 KRW)
        must exit immediately and protect capital, regardless of holding time.
        """
        pos = {
            'symbol': '010580',
            'entry': 3305.0,
            'qty': 21,
            'stop': 3290.0,
            'emergency': 3285.0,
            'opened': self.base_time.isoformat()
        }
        strat = FastMomentumStrategy()

        # 1. Emergency plunge immediately at second 5: bid drops to 3,285 KRW
        t_emg = Tick(symbol='010580', price=3285.0, volume=100, cumulative=100,
                     time=self.base_time + timedelta(seconds=5), bid=3285.0, ask=3290.0,
                     bid_qty=500, ask_qty=500,
                     depth_available=True, book_time=self.base_time + timedelta(seconds=5))
        strat.ingest_tick(t_emg, t_emg.time)
        should_exit, reason, _ = strat.evaluate_exit(pos, t_emg, t_emg.time)
        self.assertTrue(should_exit, "Emergency plunge must exit immediately regardless of settling window")
        self.assertEqual(reason, 'EMERGENCY_PLUNGE_EXIT')

        # 2. Structural stop breach: two prints at 3,290 KRW
        strat2 = FastMomentumStrategy()
        t_stop1 = Tick(symbol='010580', price=3290.0, volume=100, cumulative=100,
                       time=self.base_time + timedelta(seconds=10), bid=3290.0, ask=3295.0,
                       bid_qty=500, ask_qty=500,
                       depth_available=True, book_time=self.base_time + timedelta(seconds=10))
        strat2.ingest_tick(t_stop1, t_stop1.time)
        # First print: holds pending confirmation
        should_exit1, _, _ = strat2.evaluate_exit(pos, t_stop1, t_stop1.time)
        self.assertFalse(should_exit1)

        t_stop2 = Tick(symbol='010580', price=3290.0, volume=100, cumulative=200,
                       time=self.base_time + timedelta(seconds=11), bid=3290.0, ask=3295.0,
                       bid_qty=500, ask_qty=500,
                       depth_available=True, book_time=self.base_time + timedelta(seconds=11))
        strat2.ingest_tick(t_stop2, t_stop2.time)
        # Second print: confirms invalidation stop
        should_exit2, reason2, _ = strat2.evaluate_exit(pos, t_stop2, t_stop2.time)
        self.assertTrue(should_exit2, "Confirmed breach of 3,290 stop must trigger INVALIDATION_STOP_EXIT")
        self.assertEqual(reason2, 'INVALIDATION_STOP_EXIT')

    def test_sm_bexel_genuine_momentum_failure_counter_example(self):
        """Counter-example: Genuine momentum failure with real multi-step swings after 45s:
        - Advance +30 KRW (3,305 -> 3,335, 6 ticks)
        - Pullback -20 KRW (3,335 -> 3,315, 4 ticks floor)
        - Rebound +10 KRW to lower high (3,315 -> 3,325, 2 ticks < 3,335)
        - Breakdown below floor 3,315 to 3,310 / 3,305 at 60s
        Must trigger MOMENTUM_FAILURE_EXIT to scratch before stop loss.
        """
        pos = {
            'symbol': '010580',
            'entry': 3305.0,
            'qty': 21,
            'stop': 3290.0,
            'emergency': 3285.0,
            'opened': self.base_time.isoformat()
        }
        strat = FastMomentumStrategy()
        true_failure_ticks = [
            (0, 3305.0, 3300.0, 3305.0),
            (15, 3335.0, 3330.0, 3335.0), # Swing High 1: +30 KRW (6 ticks up)
            (30, 3315.0, 3315.0, 3320.0), # Swing Low 1: -20 KRW pullback (floor)
            (46, 3325.0, 3320.0, 3325.0), # Swing High 2: +10 KRW rebound (lower high < 3335)
            (55, 3310.0, 3310.0, 3315.0), # Print 1 below floor 3315
            (60, 3305.0, 3300.0, 3305.0), # Print 2 below floor 3315 (bid 3300)
        ]
        for idx, (sec, price, bid, ask) in enumerate(true_failure_ticks):
            t_stamp = self.base_time + timedelta(seconds=sec)
            t = Tick(symbol='010580', price=price, volume=100, cumulative=(idx + 1) * 100,
                     time=t_stamp, bid=bid, ask=ask, bid_qty=500, ask_qty=500,
                     depth_available=True, book_time=t_stamp)
            strat.ingest_tick(t, t.time)

        should_exit, reason, evidence = strat.evaluate_exit(pos, t, t.time)
        self.assertTrue(should_exit, "Genuine multi-step failed rebound breaking floor after 60s must trigger MOMENTUM_FAILURE_EXIT")
        self.assertEqual(reason, 'MOMENTUM_FAILURE_EXIT')
        self.assertEqual(evidence['failed_rebound_high'], 3325.0)
        self.assertEqual(evidence['floor'], 3315.0)

    def test_sm_bexel_1830_trade_review_reproduction(self):
        """2026-10-15 18:30~18:32 SM Bexel (010580) 22-share trade review:
        Entry 3,235 KRW, stop 3,220 KRW, emergency 3,220 KRW, target 3,435 KRW.
        Excursion reached 3,260 KRW (+1.67R, MFE).
        Pullback to 3,250 KRW (floor), followed by 1-tick bounce to 3,255 KRW (+5 KRW).
        Then dip to 3,245 KRW.
        In unpatched code: 3,255 was treated as a lower high (< 3,260) and exited at 3,245.
        In patched code: The +5 KRW bounce (< 2 * min_step = 10 KRW) is filtered as noise.
        The bot MUST HOLD at 3,245 KRW and not prematurely exit!
        """
        trade_base = datetime(2026, 10, 15, 18, 30, 25, tzinfo=KST)
        pos = {
            'symbol': '010580',
            'entry': 3235.0,
            'qty': 22,
            'stop': 3220.0,
            'emergency': 3220.0,
            'opened': trade_base.isoformat()
        }
        trade_ticks = [
            (0, 3235.0, 3230.0, 3235.0),   # 18:30:25 entry
            (8, 3250.0, 3245.0, 3250.0),   # 18:30:33 1R hit
            (38, 3240.0, 3240.0, 3245.0),  # 18:31:03 dip
            (68, 3245.0, 3245.0, 3250.0),  # 18:31:33 bounce
            (95, 3260.0, 3255.0, 3260.0),  # 18:32:00 peak MFE 3260
            (110, 3250.0, 3250.0, 3255.0), # 18:32:15 pullback to 3250 floor
            (125, 3255.0, 3250.0, 3255.0), # 18:32:30 1-tick bounce to 3255
            (135, 3245.0, 3245.0, 3250.0), # 18:32:40 dip to 3245
            (136, 3245.0, 3245.0, 3250.0), # 18:32:41 second print at 3245 (exit trigger in unpatched)
        ]
        strat = FastMomentumStrategy()
        for idx, (sec, price, bid, ask) in enumerate(trade_ticks):
            t_stamp = trade_base + timedelta(seconds=sec)
            t = Tick(symbol='010580', price=price, volume=100, cumulative=(idx + 1) * 100,
                     time=t_stamp, bid=bid, ask=ask, bid_qty=500, ask_qty=500,
                     depth_available=True, book_time=t_stamp)
            strat.ingest_tick(t, t.time)

        should_exit, reason, evidence = strat.evaluate_exit(pos, t, t.time)
        self.assertFalse(should_exit, "Patched code must filter out 1-tick rebound and hold position!")
        self.assertEqual(reason, 'HOLD')
        self.assertEqual(evidence['holding_seconds'], 136.0)


if __name__ == '__main__':
    unittest.main()

