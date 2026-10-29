"""
Causal verification of usable_bars:
1. Normal consecutive bars are preserved and usable.
2. A claimed no-trade gap without received subscription-specific book evidence remains UNKNOWN.
3. Network / receiver data loss (volume_lost > 0) invalidates usable_bars (returns []).
4. Subscription / coverage gaps invalidate usable_bars (returns []).
5. Unproven gaps (continuous_observation == False or unobserved start) invalidate usable_bars (returns []).
6. Gaps exceeding elapsed time limit (> 900s) are rejected as stale.
"""
import unittest
from datetime import datetime, timedelta
from core.settings import Config, KST
from core.strategy import Tick
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs


class UsableBarsCausalTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(stale_seconds=5)
        self.auto = AutomationConfig(structural_breakout_enabled=True, intraday_context_enabled=True, confirmed_invalidation_enabled=True)
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.sym = "089230"
        self.base_time = datetime(2026, 10, 29, 10, 0, 0, tzinfo=KST)

    def test_case_1_consecutive_bars_are_usable(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        cum = 1000
        for m in range(4):
            t = self.base_time + timedelta(minutes=m, seconds=2)
            cum += 100
            det.ingest(Tick(self.sym, t, 2500, 100, cum, 2500, 2490, True), t)
        t4 = self.base_time + timedelta(minutes=4, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t4, 2500, 100, cum, 2500, 2490, True), t4)
        usable = det.usable_bars(self.sym)
        self.assertEqual(len(usable), 4)

    def test_case_2_gap_without_book_evidence_stays_unknown(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        cum = 1000
        # Bar 1: 10:00 (first tick at second 2)
        t0 = self.base_time + timedelta(minutes=0, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t0, 2500, 100, cum, 2500, 2490, True), t0)

        # Bar 2: 10:01
        t1 = self.base_time + timedelta(minutes=1, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t1, 2500, 100, cum, 2500, 2490, True), t1)

        # Bar 3: 10:02
        t2 = self.base_time + timedelta(minutes=2, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t2, 2500, 100, cum, 2500, 2490, True), t2)

        # The next recorded trade is at 10:04:15 and cumulative volume agrees.
        # No actual 0D or subscription continuity evidence is supplied here.
        t4 = self.base_time + timedelta(minutes=4, seconds=15)
        cum += 50
        det.ingest(Tick(self.sym, t4, 2510, 50, cum, 2510, 2500, True), t4)

        # Volume agreement by itself does not prove an observed no-trade gap.
        usable = det.usable_bars(self.sym)
        self.assertEqual(len(usable), 0)
        self.assertEqual(det.current[self.sym].get('gap_reason'),'NO_TRADE_COVERAGE_UNPROVEN')
        self.assertFalse(det.current[self.sym].get('resumed_from_no_trade_gap',False))

    def test_case_3_data_loss_gap_invalidates_usable_bars(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        cum = 1000
        t0 = self.base_time + timedelta(minutes=0, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t0, 2500, 100, cum, 2500, 2490, True), t0)

        t1 = self.base_time + timedelta(minutes=1, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t1, 2500, 100, cum, 2500, 2490, True), t1)

        # Gap from 10:01 to 10:03, BUT cumulative volume increased by 500 when tick volume is only 50
        t3 = self.base_time + timedelta(minutes=3, seconds=15)
        cum += 500 # missed 450 shares in network!
        det.ingest(Tick(self.sym, t3, 2510, 50, cum, 2510, 2500, True), t3)

        usable = det.usable_bars(self.sym)
        self.assertEqual(len(usable), 0, "Data loss gap must invalidate usable_bars")
        self.assertEqual(det.current[self.sym].get('gap_reason'), 'DATA_LOSS_GAP')

    def test_case_4_subscription_gap_invalidates_usable_bars(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        cum = 1000
        t0 = self.base_time + timedelta(minutes=0, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t0, 2500, 100, cum, 2500, 2490, True), t0)

        det.mark_coverage_gap(self.sym, self.base_time + timedelta(minutes=1), 'SUBSCRIPTION_GAP')
        self.assertEqual(det.usable_bars(self.sym), [])

    def test_case_5_unproven_gap_without_continuous_observation(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = False
        cum = 1000
        t0 = self.base_time + timedelta(minutes=0, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t0, 2500, 100, cum, 2500, 2490, True), t0)

        t2 = self.base_time + timedelta(minutes=2, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t2, 2500, 100, cum, 2500, 2490, True), t2)
        self.assertEqual(det.usable_bars(self.sym), [], "Unproven gap must not yield usable bars")

    def test_case_6_excessive_gap_over_900s_rejected(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        cum = 1000
        t0 = self.base_time + timedelta(minutes=0, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t0, 2500, 100, cum, 2500, 2490, True), t0)

        # Gap of 16 minutes (960s > 900s)
        t16 = self.base_time + timedelta(minutes=16, seconds=2)
        cum += 100
        det.ingest(Tick(self.sym, t16, 2500, 100, cum, 2500, 2490, True), t16)
        self.assertEqual(det.usable_bars(self.sym), [], "Gap > 900s must be rejected as stale")


if __name__ == "__main__":
    unittest.main()
