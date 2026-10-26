"""Targeted regression tests for inactive A/B replacement strategies (P0-P2)."""
from datetime import datetime, timedelta, timezone
import unittest

from automation.capital import Costs
from automation.policy import load_automation
from core.settings import load_config
from research.ab_replacement_strategies import (
    ACTUAL_ORDER_PERMISSION,
    LIVE_ENABLED,
    SupportRecoveryHoldResearch,
    TrendBreakoutHoldResearch,
    classify_legacy_ab_baseline,
    evaluate_post_entry_path,
)


KST = timezone(timedelta(hours=9))


def make_bar(minute_offset: int, open_p: float, high_p: float, low_p: float, close_p: float, *, gap_reason=None, source='ACTUAL_RECEIVED'):
    start = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST) + timedelta(minutes=minute_offset)
    avail = start + timedelta(minutes=1, seconds=1)
    return {
        'time': start.isoformat(),
        'available_at': avail.isoformat(),
        'open': open_p,
        'high': high_p,
        'low': low_p,
        'close': close_p,
        'volume': 15000,
        'first_second': 0,
        'gap_reason': gap_reason,
        'observation_source': source,
    }


class TestABReplacementStrategies(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config('config.example.json')
        self.auto = load_automation('automation.example.json')
        self.costs = Costs(
            self.auto.commission_buy,
            self.auto.commission_sell,
            self.auto.sell_tax,
            self.cfg.entry_slippage_fraction,
            self.cfg.exit_slippage_fraction,
        )

    def test_module_is_strictly_inactive(self):
        self.assertFalse(LIVE_ENABLED)
        self.assertFalse(ACTUAL_ORDER_PERMISSION)

    def test_p0_baseline_classification(self):
        oct23_mock = {
            'rows': [
                {'setup': 'A', 'structure_confirmed': True, 'zone': {'maximum_entry': 1967, 'minimum_trigger_entry': 2010}},
                {'setup': 'B', 'structure_confirmed': True, 'target': 4315, 'zone': {'maximum_entry': 4170, 'minimum_trigger_entry': 4320}},
            ]
        }
        oct26_mock = {
            'setups': {
                'A': {'independent_structure_PASS': 22, 'price_zones': 21, 'trigger_intersections': 0, 'RR_PASS': 0},
                'B': {'independent_structure_PASS': 2, 'price_zones': 0, 'trigger_intersections': 0, 'RR_PASS': 0},
            }
        }
        res = classify_legacy_ab_baseline(oct23_mock, oct26_mock)
        self.assertEqual(res['status'], 'CONFIRMED_STRUCTURAL_FAILURE_BASELINE')
        self.assertEqual(res['oct23_regular']['A_max_entry_below_trigger'], 1)
        self.assertEqual(res['oct23_regular']['B_target_at_or_below_trigger'], 1)
        self.assertEqual(res['oct26_opening_0930']['A_structure_pass'], 22)
        self.assertEqual(res['oct26_opening_0930']['B_structure_pass'], 2)
        self.assertEqual(res['oct26_opening_0930']['A_rr_pass'], 0)
        self.assertEqual(res['oct26_opening_0930']['B_rr_pass'], 0)

    def test_trend_breakout_hold_does_not_cap_at_nearby_minor_peak(self):
        trend = TrendBreakoutHoldResearch(self.cfg, self.auto, self.costs)
        # Early minor peak at 10,300 (bars 0-2), then rising 3-bar base at 10,000 -> 10,200 (bars 3-5)
        bars = [
            make_bar(0, 10000, 10150, 9980, 10120),
            make_bar(1, 10120, 10300, 10100, 10250),
            make_bar(2, 10250, 10260, 10020, 10050),
            make_bar(3, 10050, 10120, 10040, 10100),
            make_bar(4, 10100, 10160, 10090, 10150),
            make_bar(5, 10150, 10200, 10140, 10190),
        ]
        asof = datetime(2026, 10, 26, 9, 6, 5, tzinfo=KST)
        watch = {
            'status': 'WATCH',
            'source_count': 3,
            'fallback_source1': False,
            'resistance': 11500,
            'market_status': {'pass': True},
        }
        res = trend.evaluate_entry(
            bars=bars,
            asof=asof,
            price=10210,
            bid=10200,
            ask=10210,
            bid_qty=500,
            ask_qty=500,
            depth_available=True,
            quote_fresh=True,
            watch=watch,
            cash_budget=50000.0,
            risk_budget=1600.0,
            causal_noise=20.0,
            noise_verified=True,
            pretrigger_jump=10.0,
            continuous_tape=True,
        )
        self.assertEqual(res['status'], 'PASS')
        self.assertGreater(res['viable_target'], 10300)
        self.assertTrue(res['economics']['rr_pass'])
        self.assertTrue(res['entry_zone']['current_order_price_in_zone'])
        self.assertEqual(res['orderability']['orderability_status'], 'ORDERABLE_SCENARIO')
        self.assertFalse(res['actual_order_permission'])

        # If a major daily resistance sits right above breakout (e.g. 10,250), reject!
        blocked = trend.evaluate_entry(
            bars=bars,
            asof=asof,
            price=10210,
            bid=10200,
            ask=10210,
            bid_qty=500,
            ask_qty=500,
            depth_available=True,
            quote_fresh=True,
            watch={**watch, 'resistance': 10250},
            cash_budget=50000.0,
            risk_budget=1600.0,
            causal_noise=20.0,
            noise_verified=True,
        )
        self.assertEqual(blocked['status'], 'REJECT')
        self.assertEqual(blocked['reason'], 'MAJOR_RESISTANCE_BLOCKS_MIN_VIABLE_TREND_ROOM')

    def test_trend_breakout_trailing_and_breakeven_exits(self):
        entry_at = datetime(2026, 10, 26, 9, 6, 5, tzinfo=KST)
        future_bars = [
            make_bar(6, 10210, 10450, 10320, 10420),
            make_bar(7, 10420, 10650, 10380, 10600),
            make_bar(8, 10600, 10750, 10520, 10700),
            make_bar(9, 10700, 10720, 10150, 10295),  # Closes below trailing 3-bar low (floor_tick(10320*0.999)=10300)
        ]
        path = evaluate_post_entry_path(
            strategy_type='TREND_BREAKOUT_HOLD',
            entry_at=entry_at,
            entry_ask=10210,
            structural_stop=10080,
            emergency_stop=10040,
            target=10800,
            risk_per_share=250.0,
            costs=self.costs,
            future_bars=future_bars,
            continuous_coverage=True,
        )
        self.assertTrue(path['breakeven_armed'])
        self.assertTrue(path['trailing_armed'])
        self.assertEqual(path['exit_rule'], 'TRAILING_3BAR_LOW_EXIT')
        self.assertEqual(path['mfe_krw'], 540.0)

    def test_support_recovery_separates_structural_close_from_emergency_stop(self):
        rec = SupportRecoveryHoldResearch(self.cfg, self.auto, self.costs)
        # Confirmed peak at bar 1 (2140), higher-low support at 2030 (bars 3-4)
        bars = [
            make_bar(0, 2000, 2060, 1995, 2050),
            make_bar(1, 2050, 2140, 2045, 2120),
            make_bar(2, 2110, 2115, 2040, 2045),
            make_bar(3, 2045, 2055, 2030, 2035),
            make_bar(4, 2035, 2050, 2030, 2040),
        ]
        asof = datetime(2026, 10, 26, 9, 5, 2, tzinfo=KST)
        # Under wide 3-bar range noise (2110-2040 = 70), net RR at 2045 is < 1.5 -> REJECT
        wide = rec.evaluate_entry(
            bars=bars,
            asof=asof,
            price=2045,
            bid=2040,
            ask=2045,
            bid_qty=200,
            ask_qty=600,
            depth_available=True,
            quote_fresh=True,
            micro_reclaimed=True,
            cash_budget=35000.0,
            risk_budget=1400.0,
            causal_noise=None,
            noise_verified=False,
        )
        self.assertEqual(wide['status'], 'REJECT')

        # Under causal 60s prefix noise (15 KRW), emergency=2010, net RR >= 1.5 -> PASS
        tight = rec.evaluate_entry(
            bars=bars,
            asof=asof,
            price=2045,
            bid=2040,
            ask=2045,
            bid_qty=200,
            ask_qty=600,
            depth_available=True,
            quote_fresh=True,
            micro_reclaimed=True,
            cash_budget=35000.0,
            risk_budget=1400.0,
            causal_noise=15.0,
            noise_verified=False,  # Missing continuous quote envelope -> UNKNOWN orderability
            pretrigger_jump=15.0,
            continuous_tape=True,
        )
        self.assertEqual(tight['status'], 'PASS')
        self.assertEqual(tight['structural_stop'], 2025)
        self.assertEqual(tight['emergency_stop'], 2010)
        self.assertEqual(tight['target'], 2140)
        self.assertGreaterEqual(tight['economics']['net_rr'], 1.5)
        self.assertLessEqual(tight['stress_sizing']['quantity'], tight['sizing']['quantity'])
        self.assertEqual(tight['orderability']['orderability_status'], 'UNKNOWN')
        self.assertEqual(tight['orderability']['reason'], 'UNKNOWN_PREFIX_NOISE_COVERAGE')

        # Intra-bar wick to 2015 (> emergency 2010, < structural 2025) that closes at 2035 does NOT stop out;
        # next bar reaches 2140 -> TARGET_PEAK_EXIT
        path = evaluate_post_entry_path(
            strategy_type='SUPPORT_RECOVERY',
            entry_at=asof,
            entry_ask=2045,
            structural_stop=2025,
            emergency_stop=2010,
            target=2140,
            risk_per_share=tight['economics']['risk_per_share'],
            costs=self.costs,
            future_bars=[
                make_bar(5, 2040, 2060, 2015, 2035),
                make_bar(6, 2035, 2145, 2035, 2140),
            ],
            continuous_coverage=True,
        )
        self.assertEqual(path['exit_rule'], 'TARGET_PEAK_EXIT')
        self.assertEqual(path['exit_price'], 2140.0)


if __name__ == '__main__':
    unittest.main()
