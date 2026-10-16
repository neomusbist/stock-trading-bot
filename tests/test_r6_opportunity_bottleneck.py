"""R6 Opportunity Bottleneck and Observation Continuity Regression Tests."""
import unittest
from collections import deque
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.price_context import PriceContext
from automation.runner import prewarm_handoff, SubscriptionState
from automation.engine import AutonomousEngine
from tests.test_automation import FakeBroker


START = datetime(2026, 10, 16, 9, 30, tzinfo=KST)
SYM = '452280'


class OpportunityBottleneckTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(self.auto.commission_buy, self.auto.commission_sell,
                           self.auto.sell_tax, self.cfg.entry_slippage_fraction,
                           self.cfg.exit_slippage_fraction)
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)
        self.broker = FakeBroker()
        self.policy = Policy(Path('tests/test_policy.json'))
        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, Path('tests/book'), self.broker, lambda: START)
        self.engine.s['positions'] = {}

    def feed_bars(self, detector, num_minutes, start_time):
        cumulative = 100
        for m in range(num_minutes):
            minute_dt = start_time + timedelta(minutes=m)
            for s in range(0, 60, 5):
                cumulative += 10
                tick_time = minute_dt + timedelta(seconds=s)
                t = Tick(SYM, tick_time, 17000 + m * 10, 10, cumulative, 17000 + m * 10, 17000 + m * 10 - 10, True)
                detector.ingest(t, tick_time)
        # Advance 1 tick to close the final minute
        cumulative += 10
        final_time = start_time + timedelta(minutes=num_minutes, seconds=0)
        t = Tick(SYM, final_time, 17000 + num_minutes * 10, 10, cumulative, 17000, 16990, True)
        detector.ingest(t, final_time)

    def test_prewarm_handoff_transfers_shadow_detector_bars(self):
        """Promotion from SHADOW to WATCH transfers completed bars and state via handoff_from."""
        shadow_detector = PatternDetector(self.cfg, self.auto, self.costs)
        self.feed_bars(shadow_detector, 3, START)
        self.assertEqual(len(shadow_detector.history.get(SYM, [])), 3)

        # Main detector has 0 bars
        self.assertEqual(len(self.detector.history.get(SYM, [])), 0)

        # Mock feed without queue
        class DummyFeed:
            observation_drops = 0
            def configure_observation(self, *a, **k): pass

        feed = DummyFeed()
        drops, priority = prewarm_handoff(
            self.engine, self.detector, feed,
            observed={SYM}, real_symbols=[SYM], prior_priority=set(),
            drops=0, sink=None, shadow_detector=shadow_detector
        )

        # Main detector now has the transferred bars
        self.assertEqual(len(self.detector.history.get(SYM, [])), 3)
        self.assertIn(SYM, priority)

    def test_subscription_state_preserves_bars_within_grace_period(self):
        """Detached symbol preserves bars beyond stale_seconds (15s) when within observation_grace (180s)."""
        sub = SubscriptionState([SYM])
        self.feed_bars(self.detector, 4, START)
        self.assertEqual(len(self.detector.history.get(SYM, [])), 4)

        class DummyFeed:
            def update_symbols(self, *a): pass
            def refresh_exchange(self): return True

        feed = DummyFeed()
        # Detach SYM at 09:35:00
        detach_time = START + timedelta(minutes=5)
        sub.refresh(self.engine, self.detector, feed, desired=[], stamp=detach_time, grace_seconds=180)
        self.assertIn(SYM, sub.detached)
        self.assertEqual(len(self.detector.history.get(SYM, [])), 4)

        # 30 seconds later (exceeds stale_seconds=15, but within grace=180)
        t_rejoin = detach_time + timedelta(seconds=30)
        sub.refresh(self.engine, self.detector, feed, desired=[SYM], stamp=t_rejoin, grace_seconds=180)
        # History must NOT be forgotten
        self.assertEqual(len(self.detector.history.get(SYM, [])), 4)

        # After grace expires (181 seconds detachment)
        sub.refresh(self.engine, self.detector, feed, desired=[], stamp=t_rejoin, grace_seconds=180)
        t_expired = t_rejoin + timedelta(seconds=181)
        sub.refresh(self.engine, self.detector, feed, desired=[SYM], stamp=t_expired, grace_seconds=180)
        # Now it is expired and forgotten
        self.assertEqual(len(self.detector.history.get(SYM, [])), 0)

    def test_minute_gap_does_not_wipe_out_stored_completed_bars(self):
        """A quiet minute or missing tick appends the completed bar and marks gap instead of purging history."""
        self.detector.continuous_observation = True
        self.feed_bars(self.detector, 2, START)
        self.assertEqual(len(self.detector.history.get(SYM, [])), 2)

        # Now skip minute 3 completely, tick arrives at minute 4, second 10
        t_gap = Tick(SYM, START + timedelta(minutes=4, seconds=10), 17100, 20, 5000, 17100, 17090, True)
        self.detector.ingest(t_gap, t_gap.time)

        # Completed bars must still be in history (minute 0, 1, and 2), NOT wiped out
        self.assertEqual(len(self.detector.history.get(SYM, [])), 3)
        self.assertEqual(self.detector.last_gap_reason.get(SYM), 'MISSING_MINUTE')
        # Current bar has MISSING_MINUTE gap reason
        self.assertEqual(self.detector.current[SYM]['gap_reason'], 'MISSING_MINUTE')

    def test_preload_from_context_restores_bars_for_empty_history(self):
        """PriceContext bars are safely preloaded into history on entry when history is empty."""
        asof = START + timedelta(minutes=10)
        # Add 3 confirmed bars to PriceContext
        for m in range(3):
            b_time = START + timedelta(minutes=m)
            bar = {
                'time': b_time, 'open': 17000, 'high': 17100, 'low': 16950,
                'close': 17050, 'volume': 500, 'first_second': 0, 'last_second': 59,
                'tick_count': 10, 'gap_reason': None
            }
            self.detector.price_context.record(SYM, bar, asof)

        self.assertEqual(len(self.detector.history.get(SYM, [])), 0)
        loaded = self.detector.preload_from_context(SYM, asof)
        self.assertEqual(loaded, 3)
        self.assertEqual(len(self.detector.history.get(SYM, [])), 3)


if __name__ == '__main__':
    unittest.main()
