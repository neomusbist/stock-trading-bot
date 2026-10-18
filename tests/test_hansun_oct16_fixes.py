"""Tests for 2026-10-16 한선엔지니어링 incident fixes and verifications.

1. 14:42 FAST entry into overhead resistance (16,880 KRW) rejected:
   - Target capped by confirmed intraday peak -> Net RR -0.29 < 1.5
   - Rejection code: KNOWN_CEILING_NET_REWARD_RISK_LOW
2. Genuine breakout above resistance:
   - Entry above peak (16,890 > 16,880) is not capped by 16,880
3. 13:25 Setup B trade rejected:
   - Entry 16,700, target 16,930, stop 16,610 / emergency 16,580
   - Gross RR 2.55, but Net RR 0.67 < 1.5
   - Rejection in setup_gates (RISK_REWARD_FAIL) and engine (EXIT_PLAN_REWARD_UNKNOWN_OR_LOW)
4. PriceContext cache persistence across restarts:
   - dump_cache() and load_cache() preserve confirmed peaks for same-day recovery
"""
from dataclasses import replace
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import unittest

from core.settings import Config, KST
from core.strategy import Tick
from automation.capital import Costs
from automation.fast_momentum import FastMomentumStrategy
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy
from automation.price_context import PriceContext
from automation.engine import AutonomousEngine
from automation.exit_plan import build_plan


class HansunOct16FixesTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 10, 16, 14, 42, 34, tzinfo=KST)
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(
            self.auto.commission_buy, self.auto.commission_sell, self.auto.sell_tax,
            self.cfg.entry_slippage_fraction, self.cfg.exit_slippage_fraction
        )

    def test_hansun_1442_fast_entry_capped_by_confirmed_peak_is_rejected(self):
        """14:42 FAST entry into overhead resistance (16,880 KRW) must be rejected with KNOWN_CEILING_NET_REWARD_RISK_LOW."""
        strategy = FastMomentumStrategy(self.cfg, self.auto, self.costs)
        symbol = '452280'
        tape = strategy.get_tape(symbol)

        # Ingest micro-tape replicating 14:41:14 ~ 14:42:34 price action
        t0 = self.time - timedelta(seconds=80)
        # Pullback / support at 16,730
        tape.ingest(Tick(symbol, t0, 16730, 50, 1000, 16730, 16720, True), t0)
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=10), 16740, 50, 1050, 16740, 16730, True), t0 + timedelta(seconds=10))
        # Reaction high / breakout at 16,790
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=30), 16790, 100, 1150, 16790, 16780, True), t0 + timedelta(seconds=30))
        # Retrace to 16,780
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=45), 16780, 50, 1200, 16780, 16770, True), t0 + timedelta(seconds=45))
        # Rebound confirmation at 16,800
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=55), 16800, 100, 1300, 16800, 16790, True), t0 + timedelta(seconds=55))
        # Rebound breakout to 16,810 (decision tick)
        t_now = self.time
        current_tick = Tick(symbol, t_now, 16810, 500, 5000, 16810, 16800, True,
                            ask_qty=100, bid_qty=100, depth_available=True, book_time=t_now)
        tape.ingest(current_tick, t_now)

        # Watch without peak: daily resistance 17,850
        watch_without_peak = {
            'code': symbol, 'name': '한선엔지니어링',
            'resistance': 17850, 'resistance_asof': self.time.replace(hour=9, minute=0).isoformat(),
            'observed_at': self.time.replace(hour=9, minute=0).isoformat()
        }

        # Watch with confirmed peak at 16,880 from 14:27
        watch_with_peak = dict(watch_without_peak)
        watch_with_peak['nearest_confirmed_peak'] = {
            'price': 16880,
            'time': self.time.replace(minute=27, second=0).isoformat(),
            'confirmed_at': self.time.replace(minute=29, second=0).isoformat(),
        }

        # Evaluation with peak: target should be capped to 16,880, net RR becomes negative (-0.29)
        geom = strategy.calculate_invalidation_and_target(
            current_tick, tape.metrics(t_now), watch_with_peak,
            capital_limit=81300, risk_budget=1626, asof=t_now
        )
        self.assertFalse(geom['pass'])
        self.assertEqual(geom['reason'], 'KNOWN_CEILING_NET_REWARD_RISK_LOW')
        self.assertEqual(geom['target'], 16880)
        self.assertLess(geom['net_rr'], 0)

    def test_unconfirmed_one_tick_poke_above_peak_is_still_capped_and_rejected(self):
        """A 1-tick poke above a confirmed peak (16,890 > 16,880) without confirmed support is NOT a confirmed breakout."""
        strategy = FastMomentumStrategy(self.cfg, self.auto, self.costs)
        symbol = '452280'
        tape = strategy.get_tape(symbol)

        t0 = self.time - timedelta(seconds=80)
        tape.ingest(Tick(symbol, t0, 16830, 50, 1000, 16830, 16820, True), t0)
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=20), 16880, 100, 1150, 16880, 16870, True), t0 + timedelta(seconds=20))
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=40), 16860, 50, 1200, 16860, 16850, True), t0 + timedelta(seconds=40))
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=55), 16880, 100, 1300, 16880, 16870, True), t0 + timedelta(seconds=55))

        t_now = self.time
        # 1-tick poke to 16,890 while support is still 16,860 (< 16,880) and peak is un-cleared
        current_tick = Tick(symbol, t_now, 16890, 500, 5000, 16890, 16880, True,
                            ask_qty=100, bid_qty=100, depth_available=True, book_time=t_now)
        tape.ingest(current_tick, t_now)

        watch_with_unbroken_peak = {
            'code': symbol, 'name': '한선엔지니어링',
            'resistance': 17850, 'resistance_asof': self.time.replace(hour=9, minute=0).isoformat(),
            'observed_at': self.time.replace(hour=9, minute=0).isoformat(),
            'nearest_confirmed_peak': {
                'price': 16880,
                'time': self.time.replace(minute=27, second=0).isoformat(),
                'confirmed_at': self.time.replace(minute=29, second=0).isoformat(),
            }
        }

        geom = strategy.calculate_invalidation_and_target(
            current_tick, tape.metrics(t_now), watch_with_unbroken_peak,
            capital_limit=81300, risk_budget=1626, asof=t_now
        )
        self.assertFalse(geom['pass'])
        self.assertEqual(geom['reason'], 'KNOWN_CEILING_NET_REWARD_RISK_LOW')
        self.assertEqual(geom['target'], 16880)

    def test_genuine_breakout_above_peak_is_not_capped_by_lower_peak(self):
        """When breakout above 16,880 holds support >= 16,880 and re-breaks higher, 16,880 no longer caps target."""
        strategy = FastMomentumStrategy(self.cfg, self.auto, self.costs)
        symbol = '452280'
        tape = strategy.get_tape(symbol)

        t0 = self.time - timedelta(seconds=80)
        tape.ingest(Tick(symbol, t0, 16830, 50, 1000, 16830, 16820, True), t0)
        # Breakout above 16,880 to 16,910
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=25), 16910, 100, 1150, 16910, 16900, True), t0 + timedelta(seconds=25))
        # Pullback holds support at 16,890 (>= 16,880)
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=45), 16890, 50, 1200, 16890, 16880, True), t0 + timedelta(seconds=45))
        # Rebound confirmation at 16,900
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=60), 16900, 100, 1300, 16900, 16890, True), t0 + timedelta(seconds=60))

        t_now = self.time
        current_tick = Tick(symbol, t_now, 16920, 500, 5000, 16920, 16910, True,
                            ask_qty=100, bid_qty=100, depth_available=True, book_time=t_now)
        tape.ingest(current_tick, t_now)

        watch_with_old_peak = {
            'code': symbol, 'name': '한선엔지니어링',
            'resistance': 17850, 'resistance_asof': self.time.replace(hour=9, minute=0).isoformat(),
            'observed_at': self.time.replace(hour=9, minute=0).isoformat(),
            'nearest_confirmed_peak': {
                'price': 16880,
                'time': self.time.replace(minute=27, second=0).isoformat(),
                'confirmed_at': self.time.replace(minute=29, second=0).isoformat(),
            }
        }

        geom = strategy.calculate_invalidation_and_target(
            current_tick, tape.metrics(t_now), watch_with_old_peak,
            capital_limit=81300, risk_budget=1626, asof=t_now
        )
        self.assertTrue(geom['pass'])
        self.assertEqual(geom['target'], 17850)
        self.assertGreaterEqual(geom['net_rr'], 1.5)

    def test_entry_at_exact_peak_price_is_capped_and_rejected(self):
        """When entry price is exactly at the confirmed peak (16,880 == 16,880), the peak caps the target and rejects."""
        strategy = FastMomentumStrategy(self.cfg, self.auto, self.costs)
        symbol = '452280'
        tape = strategy.get_tape(symbol)

        t0 = self.time - timedelta(seconds=80)
        tape.ingest(Tick(symbol, t0, 16730, 50, 1000, 16730, 16720, True), t0)
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=30), 16860, 100, 1150, 16860, 16850, True), t0 + timedelta(seconds=30))
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=45), 16850, 50, 1200, 16850, 16840, True), t0 + timedelta(seconds=45))
        tape.ingest(Tick(symbol, t0 + timedelta(seconds=55), 16870, 100, 1300, 16870, 16860, True), t0 + timedelta(seconds=55))

        t_now = self.time
        current_tick = Tick(symbol, t_now, 16880, 500, 5000, 16880, 16870, True,
                            ask_qty=100, bid_qty=100, depth_available=True, book_time=t_now)
        tape.ingest(current_tick, t_now)

        watch_with_peak = {
            'code': symbol, 'name': '한선엔지니어링',
            'resistance': 17850, 'resistance_asof': self.time.replace(hour=9, minute=0).isoformat(),
            'observed_at': self.time.replace(hour=9, minute=0).isoformat(),
            'nearest_confirmed_peak': {
                'price': 16880,
                'time': self.time.replace(minute=27, second=0).isoformat(),
                'confirmed_at': self.time.replace(minute=29, second=0).isoformat(),
            }
        }

        geom = strategy.calculate_invalidation_and_target(
            current_tick, tape.metrics(t_now), watch_with_peak,
            capital_limit=81300, risk_budget=1626, asof=t_now
        )
        self.assertFalse(geom['pass'])
        self.assertEqual(geom['reason'], 'KNOWN_CEILING_NET_REWARD_RISK_LOW')
        self.assertEqual(geom['target'], 16880)

    def test_hansun_1325_setup_b_low_net_rr_rejected_at_engine_and_detector(self):
        """13:25 Setup B with gross RR 2.55 but Net RR 0.67 must be rejected."""
        # 1. Test build_plan calculation
        sig_b = {
            'symbol': '452280', 'pattern': 'B', 'limit': 16700,
            'stop': 16610, 'emergency': 16580, 'target': 16930,
            'invalidation_policy': {'enabled': True, 'structural_stop': 16610, 'emergency_stop': 16580}
        }
        plan = build_plan(self.cfg, self.auto, sig_b)
        self.assertFalse(plan['reward_guard_pass'])
        # Net RR is approximately 0.51~0.67, far below 1.5
        self.assertLess(plan['net_reward_risk'], 1.0)
        self.assertGreater(plan['net_reward_risk'], 0.4)

        # 2. Test engine.entry_market_reason blocks it
        class DummyBroker:
            def __init__(self):
                self.orders = []
                self.identity = 'paper'
            def submit(self, *a, **k):
                return {'status': 'ACCEPTED'}

        with tempfile.TemporaryDirectory() as tmpdir:
            policy = Policy(Path(tmpdir) / 'policy.json')
            engine = AutonomousEngine(self.cfg, self.auto, policy, Path(tmpdir) / 'book', DummyBroker(), lambda: self.time)
            reason = engine.entry_market_reason(sig_b, 4)
            self.assertEqual(reason, 'EXIT_PLAN_REWARD_UNKNOWN_OR_LOW')

    def test_price_context_dump_and_load_cache_preserves_confirmed_peaks(self):
        """PriceContext dump_cache and load_cache preserve intraday confirmed peaks across restarts."""
        pc1 = PriceContext()
        symbol = '452280'
        asof = self.time

        # Record 3 completed 1-minute bars forming a peak at 16,880
        t_base = asof.replace(minute=25, second=0, microsecond=0)
        bars = [
            {'time': t_base, 'open': 16800, 'high': 16820, 'low': 16790, 'close': 16810, 'gap_reason': None, 'first_second': 0},
            {'time': t_base + timedelta(minutes=1), 'open': 16810, 'high': 16880, 'low': 16800, 'close': 16820, 'gap_reason': None, 'first_second': 0},
            {'time': t_base + timedelta(minutes=2), 'open': 16820, 'high': 16840, 'low': 16800, 'close': 16810, 'gap_reason': None, 'first_second': 0},
        ]
        for b in bars:
            pc1.record(symbol, b, asof)

        snap1 = pc1.snapshot(symbol, Tick(symbol, asof, 16810, 1, 1, 16810, 16800, True), 16810)
        self.assertIsNotNone(snap1['nearest_confirmed_peak'])
        self.assertEqual(snap1['nearest_confirmed_peak']['price'], 16880)

        # Dump cache
        cached_data = pc1.dump_cache()
        self.assertIn('rows', cached_data)
        self.assertIn(symbol, cached_data['rows'])

        # Create a new PriceContext (simulating process restart)
        pc2 = PriceContext()
        recovered = pc2.load_cache(cached_data, asof)
        self.assertEqual(recovered, 3)

        snap2 = pc2.snapshot(symbol, Tick(symbol, asof, 16810, 1, 1, 16810, 16800, True), 16810)
        self.assertIsNotNone(snap2['nearest_confirmed_peak'])
        self.assertEqual(snap2['nearest_confirmed_peak']['price'], 16880)
        self.assertEqual(snap1['nearest_confirmed_peak'], snap2['nearest_confirmed_peak'])

    def test_fast_stop_four_scenarios_and_no_1tick_micro_support_hijack(self):
        """Verify MicroTape uses the true pullback floor (not a 1-tick micro dip) and passes all 4 stop scenarios."""
        from automation.exit_plan import invalidation_lines
        strategy = FastMomentumStrategy(self.cfg, self.auto, self.costs)
        symbol = '452280'
        tape = strategy.get_tape(symbol)

        t0 = datetime(2026, 10, 16, 10, 14, 0, tzinfo=KST)
        # Origin 17,070 -> Breakout 17,150 -> Real pullback floor 17,110 -> Bounce 17,140 -> 1-tick dip 17,130 -> 17,140 -> 17,160
        seq = [
            (0, 17070), (10, 17110), (20, 17150), (30, 17110),
            (40, 17140), (45, 17130), (50, 17140), (55, 17160),
        ]
        for sec, prc in seq:
            ts = t0 + timedelta(seconds=sec)
            tape.ingest(Tick(symbol, ts, prc, 100, (sec + 1) * 100, prc, prc - 10, True,
                             ask_qty=100, bid_qty=100, depth_available=True, book_time=ts), ts)

        t_now = t0 + timedelta(seconds=55)
        decision_tick = Tick(symbol, t_now, 17160, 100, 5600, 17160, 17150, True,
                             ask_qty=100, bid_qty=100, depth_available=True, book_time=t_now)
        watch = {
            'code': symbol, 'name': '한선엔지니어링',
            'resistance': 17850, 'resistance_asof': t0.replace(hour=9, minute=0).isoformat(),
            'observed_at': t0.replace(hour=9, minute=0).isoformat()
        }
        geom = strategy.calculate_invalidation_and_target(
            decision_tick, tape.metrics(t_now), watch, capital_limit=81300, risk_budget=1626, asof=t_now
        )
        self.assertTrue(geom['pass'])
        # Support MUST be the true pullback floor 17,110, NOT the later 1-tick dip 17,130
        self.assertEqual(geom['structural_evidence']['support'], 17110)
        self.assertEqual(geom['stop'], 17090)
        self.assertLessEqual(geom['emergency'], 17070)

        pos = {
            'symbol': symbol, 'ownership': 'BOT', 'pattern': 'FAST',
            'avg': 17160.0, 'qty': geom['quantity'],
            'initial_stop': geom['stop'], 'stop': geom['stop'], 'emergency': geom['emergency'],
            'opened': t_now.isoformat(),
            'signal': {
                'pattern': 'FAST', 'stop': geom['stop'], 'emergency': geom['emergency'],
                'invalidation_policy': {
                    'enabled': True, 'structural_stop': geom['stop'], 'emergency_stop': geom['emergency'],
                    'noise_band': geom['noise_band'], 'breakout_level': 17150, 'support_level': 17110, 'origin_level': 17070
                }
            }
        }
        # Scenario 1: 1~2 tick shakeout (17,140 -> 17,130) holds
        t_shake = t_now + timedelta(seconds=10)
        tick_shake = Tick(symbol, t_shake, 17130, 50, 5700, 17130, 17120, True,
                          ask_qty=100, bid_qty=100, depth_available=True, book_time=t_shake)
        strategy.ingest_tick(tick_shake, t_shake)
        should_exit, reason, _ = strategy.evaluate_exit(pos, tick_shake, t_shake)
        self.assertFalse(should_exit)
        self.assertEqual(reason, 'HOLD')

        # Scenario 2: Single trade print at structural stop (17,090) with bid=17,100 > emergency holds (requires 2 ticks)
        t_pull = t_now + timedelta(seconds=20)
        tick_pull = Tick(symbol, t_pull, 17090, 50, 5800, 17100, 17090, True,
                         ask_qty=100, bid_qty=100, depth_available=True, book_time=t_pull)
        strategy.ingest_tick(tick_pull, t_pull)
        should_exit, reason, _ = strategy.evaluate_exit(pos, tick_pull, t_pull)
        self.assertFalse(should_exit)

        # Scenario 3: Second consecutive independent trade print at <= structural stop (17,090) triggers structural exit
        t_break2 = t_now + timedelta(seconds=22)
        tick_break2 = Tick(symbol, t_break2, 17090, 50, 5850, 17090, 17080, True,
                           ask_qty=100, bid_qty=100, depth_available=True, book_time=t_break2)
        strategy.ingest_tick(tick_break2, t_break2)
        should_exit, reason, _ = strategy.evaluate_exit(pos, tick_break2, t_break2)
        self.assertTrue(should_exit)
        self.assertEqual(reason, 'INVALIDATION_STOP_EXIT')

        # Scenario 4: Emergency stop breach (<= 17,070) triggers immediate emergency stop even on 1 tick
        lines = invalidation_lines(self.cfg, self.auto, pos)
        self.assertEqual(lines['active_structure'], 17090)
        self.assertEqual(lines['emergency'], geom['emergency'])
        t_emerg = t_now + timedelta(seconds=25)
        tick_emerg = Tick(symbol, t_emerg, geom['emergency'], 50, 5900, geom['emergency'] + 10, geom['emergency'], True,
                          ask_qty=100, bid_qty=100, depth_available=True, book_time=t_emerg)
        strategy.ingest_tick(tick_emerg, t_emerg)
        should_exit_em, reason_em, _ = strategy.evaluate_exit(pos, tick_emerg, t_emerg)
        self.assertTrue(should_exit_em)
        self.assertEqual(reason_em, 'EMERGENCY_PLUNGE_EXIT')

    def test_preload_from_context_no_keyerror_and_missing_minute_blocks_false_signal(self):
        """preload_from_context must not cause KeyError: 'first_second', and MISSING_MINUTE must block usable_bars."""
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        symbol = '092870'
        t_base = datetime(2026, 10, 19, 9, 1, 0, tzinfo=KST)
        for m in range(4):
            bm = t_base + timedelta(minutes=m)
            det.price_context.record(symbol, {
                'time': bm, 'open': 10000 + m * 50, 'high': 10100 + m * 50,
                'low': 9990 + m * 50, 'close': 10050 + m * 50, 'venue': 'KRX'
            }, bm + timedelta(minutes=1))

        asof = t_base + timedelta(minutes=4, seconds=2)
        loaded = det.preload_from_context(symbol, asof)
        self.assertEqual(loaded, 4)
        usable = det.usable_bars(symbol)
        self.assertEqual(len(usable), 4)

        # Now ingest a tick with a missing minute (jumping from 09:04 completed bar to 09:06)
        t_gap = t_base + timedelta(minutes=5, seconds=1)
        det.ingest(Tick(symbol, t_gap, 10300, 100, 1000, 10300, 10290, True), t_gap)
        self.assertEqual(det.usable_bars(symbol), [])
        q = det.quality_summary(symbol)
        self.assertEqual(q['usable_completed_bars'], 0)
        self.assertEqual(q['difference_reason'], 'MISSING_MINUTE')

    def test_new_trading_day_startup_resets_consecutive_losses_and_daily_loss_preserving_prior_trades(self):
        """Starting AutonomousEngine on 2026-10-19 resets consecutive_losses and daily_loss while keeping 2026-10-16 trades."""
        class DummyBroker:
            def __init__(self):
                self.orders = []
                self.identity = 'paper'
            def submit(self, *a, **k):
                return {'status': 'ACCEPTED'}

        with tempfile.TemporaryDirectory() as tmpdir:
            book = Path(tmpdir) / 'book'
            book.mkdir(parents=True, exist_ok=True)
            policy = Policy(Path(tmpdir) / 'policy.json')
            friday = datetime(2026, 10, 16, 15, 20, 0, tzinfo=KST)
            engine_fri = AutonomousEngine(self.cfg, self.auto, policy, book, DummyBroker(), lambda: friday)
            engine_fri.s['daily_loss'] = 767.0
            engine_fri.s['automation']['consecutive_losses'] = 2
            engine_fri.s['trades'].append({'symbol': '092870', 'net_pnl': 1102.0, 'closed': '2026-10-16T09:30:38+09:00', 'completed': True})
            engine_fri.save()

            monday = datetime(2026, 10, 19, 8, 50, 0, tzinfo=KST)
            engine_mon = AutonomousEngine(self.cfg, self.auto, policy, book, DummyBroker(), lambda: monday)
            self.assertEqual(engine_mon.s['day'], '2026-10-19')
            self.assertEqual(engine_mon.s['daily_loss'], 0.0)
            self.assertEqual(engine_mon.s['automation']['consecutive_losses'], 0)
            self.assertEqual(len(engine_mon.s['trades']), 1)
            self.assertEqual(engine_mon.s['trades'][0]['symbol'], '092870')


if __name__ == '__main__':
    unittest.main()
