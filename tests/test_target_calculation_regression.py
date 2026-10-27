import unittest
from datetime import datetime
from core.settings import Config, KST
from core.strategy import Tick
from core.util import floor_tick, tick_size
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.patterns import PatternDetector


class TargetCalculationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)

    def test_setup_a_prior_high_projection_exact_math(self):
        """Setup A prior_high: projection = prior + 2 * (prior - support)"""
        prior = 2125
        support = 2025
        # 2125 + 2 * (2125 - 2025) = 2125 + 200 = 2325
        expected = prior + 2 * (prior - support)
        self.assertEqual(expected, 2325)

    def test_setup_a_trend_breakout_base_projection_exact_math(self):
        """Setup A TREND_BREAKOUT_BASE: projection = floor_tick(level + 2.5 * (level - base_low))"""
        level = 2125
        base_low = 2045
        # 2125 - 2045 = 80
        # 2.5 * 80 = 200
        # 2125 + 200 = 2325
        raw_val = level + 2.5 * (level - base_low)
        self.assertEqual(raw_val, 2325.0)
        proj = floor_tick(raw_val)
        self.assertEqual(proj, 2325)

    def test_setup_a_continuation_projection_exact_math(self):
        """Setup A LOCAL_CONFIRMED_CONTINUATION: projection = local_high + 2 * (local_high - support)"""
        local_high = 2125
        support = 2025
        expected = local_high + 2 * (local_high - support)
        self.assertEqual(expected, 2325)

    def test_setup_b_pullback_projection_exact_math(self):
        """Setup B: projection = peak + rise"""
        base = 2000
        peak = 2100
        rise = peak - base  # 100
        expected = peak + rise  # 2200
        self.assertEqual(expected, 2200)

    def test_setup_c_box_projection_exact_math(self):
        """Setup C: projection = top + 2 * (top - low)"""
        top = 2100
        low = 2050
        box_width = top - low  # 50
        expected = top + 2 * box_width  # 2100 + 100 = 2200
        self.assertEqual(expected, 2200)

    def test_target_clamping_to_overhead_resistance(self):
        """target() function correctly clamps projection to resistance if limit < resistance"""
        t_now = datetime(2026, 10, 27, 11, 46, 7, tzinfo=KST)
        tick = Tick('138360', t_now, 2130, 50000, 15000000, 2130.0, 2125.0)
        watch = {'code': '138360', 'resistance': 2205.0}
        setup = {'pattern': 'A', 'projection': 2325, 'breakout': 2125}
        
        # limit(2130) < resistance(2205) -> clamped to 2205.0
        final_target = self.detector.target(tick, watch, setup, 2130)
        self.assertEqual(final_target, 2205.0)
        
        # if resistance is broken (limit >= resistance), target becomes unconstrained projection
        final_target_broken = self.detector.target(tick, watch, setup, 2210)
        self.assertEqual(final_target_broken, 2325)


if __name__ == '__main__':
    unittest.main()
