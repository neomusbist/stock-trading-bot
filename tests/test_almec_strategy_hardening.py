import unittest
import time
from datetime import datetime, timedelta
from collections import deque
from core.settings import Config, KST
from core.strategy import Tick
from core.engine import Engine
from automation.capital import Costs
from automation.policy import AutomationConfig, Policy
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from tests.test_automation import FakeBroker

START = datetime(2026, 10, 7, 9, 30, tzinfo=KST)
SYM = '053350'  # Almec (알멕)

class AlmecStrategyHardeningTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=False, stale_seconds=5)
        self.auto = AutomationConfig(costs_confirmed=True)
        self.costs = Costs(buy=0.00015, sell=0.00015, tax=0.002, entry_slippage=0.002, exit_slippage=0.003)

    def test_priority4_stop_loss_bifurcation(self):
        """Top bid touching stop while traded price is above stop must NOT trigger exit."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = Engine(self.cfg, tmpdir, FakeBroker())
            engine.s['positions'][SYM] = {
                'qty': 10, 'avg': 57200, 'high': 57200, 'low': 56300, 'stop': 56200, 'initial_r': 1000, 'initial_stop': 56200,
                'exit_requested': False, 'exit_attempts': 0, 'exit_reason': None, 'qty_blocked': False,
                'opened': START.isoformat(), 'last_tick': START.isoformat()
            }
            engine.connected = True
            engine.broker = FakeBroker()

            # Tick 1: Traded price 56,300, top bid 56,200 (Almec case: bid touched 56,200, trade at 56,300)
            t_touch = Tick(SYM, START + timedelta(seconds=1), 56300, 50, 1000, 56300, 56200)
            engine.on_tick(t_touch)
            # MUST NOT EXIT on mere bid touch!
            self.assertFalse(engine.s['positions'][SYM]['exit_requested'])
            self.assertEqual(len(engine.active_orders(SYM, side='SELL')), 0)

            # Tick 2: Traded price actually reaches stop 56,200 -> Structural exit!
            t_structural = Tick(SYM, START + timedelta(seconds=2), 56200, 50, 1050, 56300, 56200)
            engine.on_tick(t_structural)
            self.assertNotIn(SYM, engine.s['positions'], "Position must be liquidated on structural stop")
            self.assertEqual(engine.s['trades'][-1]['reason'], '구조/이익보호선 이탈')

    def test_priority4_emergency_bid_collapse(self):
        """When bid book drops below stop (vacuum/collapse), emergency exit triggers."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = Engine(self.cfg, tmpdir, FakeBroker())
            engine.s['positions'][SYM] = {
                'qty': 10, 'avg': 57200, 'high': 57200, 'low': 56500, 'stop': 56200, 'initial_r': 1000, 'initial_stop': 56200,
                'exit_requested': False, 'exit_attempts': 0, 'exit_reason': None, 'qty_blocked': False,
                'opened': START.isoformat(), 'last_tick': START.isoformat()
            }
            engine.connected = True
            engine.broker = FakeBroker()

            # Bid collapses to 56,000 (well below 56,100 emergency threshold), trade price lagging at 56,400
            t_collapse = Tick(SYM, START + timedelta(seconds=1), 56400, 10, 1000, 56400, 56000)
            engine.on_tick(t_collapse)
            self.assertNotIn(SYM, engine.s['positions'], "Position must be liquidated on emergency bid collapse")
            self.assertEqual(engine.s['trades'][-1]['reason'], '호가 급락/유동성 붕괴')

    def test_priority1_observation_continuity_across_minutes(self):
        """Continuous observation preserves completed bars even if a minute's first trade is after second 5."""
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        detector.continuous_observation = True
        cum = 0

        # Minute 1: 09:30:00 to 09:30:59
        for s in (0, 15, 30, 45, 59):
            cum += 100
            t = Tick(SYM, START + timedelta(seconds=s), 53000, 100, cum, 53050, 52950)
            detector.ingest(t, t.time)

        # Minute 2: First trade arrives at 09:31:08 (second = 8 > stale_seconds 5)
        # Because detector was ALREADY observing since 09:30, minute 2 coverage is 100%!
        cum += 200
        t_min2 = Tick(SYM, START + timedelta(minutes=1, seconds=8), 53200, 200, cum, 53250, 53150)
        detector.ingest(t_min2, t_min2.time)

        # Minute 1 was completed and moved to history
        self.assertEqual(len(detector.history[SYM]), 1)
        self.assertEqual(len(detector.usable_bars(SYM)), 1, "Completed minute 1 must be usable")

        # Complete minute 2 and advance to minute 3 (first trade at 09:32:12)
        for s in (20, 40, 58):
            cum += 100
            t = Tick(SYM, START + timedelta(minutes=1, seconds=s), 53300, 100, cum, 53350, 53250)
            detector.ingest(t, t.time)
        cum += 200
        t_min3 = Tick(SYM, START + timedelta(minutes=2, seconds=12), 53500, 200, cum, 53550, 53450)
        detector.ingest(t_min3, t_min3.time)

        # Both minute 1 and minute 2 must be fully usable!
        usable = detector.usable_bars(SYM)
        self.assertEqual(len(usable), 2, "Both completed bars must be usable despite trade timing")
        self.assertIsNone(usable[1].get('gap_reason'), "Bar 2 must not have gap_reason under continuous observation")

    def test_priority1_detector_handoff(self):
        """Handoff between shadow and main detector transfers completed bars and observation start."""
        shadow_det = PatternDetector(self.cfg, self.auto, self.costs)
        main_det = PatternDetector(self.cfg, self.auto, self.costs)
        main_det.continuous_observation = True
        shadow_det.continuous_observation = True
        cum = 0

        # Shadow detector observes 3 completed bars
        for m in range(3):
            for s in (0, 30):
                cum += 100
                t = Tick(SYM, START + timedelta(minutes=m, seconds=s), 53000 + m * 200, 100, cum, 53050, 52950)
                shadow_det.ingest(t, t.time)
        cum += 100
        t_roll = Tick(SYM, START + timedelta(minutes=3, seconds=1), 53700, 100, cum, 53750, 53650)
        shadow_det.ingest(t_roll, t_roll.time)
        self.assertEqual(len(shadow_det.usable_bars(SYM)), 3)

        # Main detector has 0 bars before handoff
        self.assertEqual(len(main_det.usable_bars(SYM)), 0)

        # Promotion handoff
        self.assertTrue(main_det.handoff_from(shadow_det, SYM))
        self.assertEqual(len(main_det.usable_bars(SYM)), 3)
        self.assertEqual(main_det.observation_start[SYM], shadow_det.observation_start[SYM])

    def test_priority2_setup_b_pullback_early_rebreakout(self):
        """Setup B allows early entry on confirmed pullback low + local swing re-breakout."""
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        # Simulate Almec:
        # Impulse: 52,500 -> 54,000 (peak = 54,000)
        # Pullback: dips to 53,400 (bar 2), bounces to high 53,700 with close 53,600 (bar 3)
        bars = [
            {'time': START, 'low': 52500, 'high': 53200, 'close': 53100, 'volume': 1000, 'first_second': 0},
            {'time': START + timedelta(minutes=1), 'low': 53000, 'high': 54000, 'close': 53900, 'volume': 3000, 'first_second': 0},
            {'time': START + timedelta(minutes=2), 'low': 53400, 'high': 53800, 'close': 53500, 'volume': 400, 'first_second': 0},
            {'time': START + timedelta(minutes=3), 'low': 53450, 'high': 53700, 'close': 53650, 'volume': 300, 'first_second': 0},
        ]
        geom = detector.pullback(bars)
        self.assertIsNotNone(geom)
        self.assertEqual(geom['pattern'], 'B')
        self.assertEqual(geom['support'], 53400, "Support is the confirmed pullback low")
        self.assertEqual(geom['peak'], 54000, "Peak is the prior impulse high")
        # Breakout level should be the local pullback high (53,800), NOT 54,000!
        self.assertEqual(geom['breakout'], 53800, "Rebreakout trigger is local pullback resistance")

        # Price ticks up across 53,800 to 53,850 (before reaching peak 54,000)
        t_early = Tick(SYM, START + timedelta(minutes=4, seconds=10), 53850, 500, 5000, 53900, 53800)
        watch = {'status': 'WATCH', 'source_details': {'s1': {}, 's2': {}}}
        gates = detector.setup_gates(t_early, watch, geom, bars=bars)
        self.assertEqual(gates['breakout']['result'], 'PASS', "Early rebreakout must PASS at 53,850")
        self.assertEqual(gates['invalidation']['result'], 'PASS', "Invalidation from 53,400 support must PASS")

    def test_priority3_setup_a_extended_trend_evaluation(self):
        """Setup A evaluates swing extension and prevents fragile noise stops on extended trends."""
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        # Stock ran from 51,700 up to 57,200 (+10.6%), with 1-minute candle ranges of ~1,000 KRW
        bars = [
            {'time': START + timedelta(minutes=0), 'low': 51700, 'high': 52500, 'close': 52400, 'volume': 2000, 'first_second': 0},
            {'time': START + timedelta(minutes=1), 'low': 52300, 'high': 54000, 'close': 53800, 'volume': 2500, 'first_second': 0},
            {'time': START + timedelta(minutes=2), 'low': 53600, 'high': 55500, 'close': 55300, 'volume': 2000, 'first_second': 0},
            {'time': START + timedelta(minutes=3), 'low': 55200, 'high': 56500, 'close': 56300, 'volume': 1500, 'first_second': 0},
            {'time': START + timedelta(minutes=4), 'low': 56200, 'high': 57100, 'close': 57000, 'volume': 1000, 'first_second': 0},
        ]
        detector.prior_high[SYM] = 57100
        t_extended = Tick(SYM, START + timedelta(minutes=5, seconds=5), 57200, 500, 10000, 57300, 57200)

        # Setup A generated
        candidates = detector.candidates(t_extended, bars)
        setup_a = next((s for s in candidates if s['pattern'] == 'A'), None)
        self.assertIsNotNone(setup_a)
        # Lookback uses swing support across recent bars (55,200), not just 56,200!
        self.assertLessEqual(setup_a['support'], 56200)

if __name__ == '__main__':
    unittest.main()
