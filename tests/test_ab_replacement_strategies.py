"""Targeted regression tests for P0 performance fixes and P1-P3 replacement strategies."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.policy import load_automation
from automation.release_integrity import _INTEGRITY_CACHE, check_runtime_integrity
from automation.trade_review_capture import TradeReviewCapture
from core.settings import load_config
from core.strategy import Tick
from research.ab_replacement_strategies import (
    ACTUAL_ORDER_PERMISSION,
    LIVE_ENABLED,
    SupportRecoveryHoldResearch,
    TrendBreakoutHoldResearch,
    classify_legacy_ab_baseline,
    evaluate_post_entry_path,
    observe_replacement_ab_on_completed_bar,
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

    def test_trend_breakout_hold_never_inflates_target_from_required_1_5R(self):
        trend = TrendBreakoutHoldResearch(self.cfg, self.auto, self.costs)
        # 1) Tight pivot with 2.0% base height (base_low=10,000, b1.low=10,130, b2.low=10,150, level=10,200):
        #    measured_target = 10,200 + 2.5 * 200 = 10,700 > required_1_5R_target, passes WITHOUT target inflation!
        bars = [
            make_bar(0, 10000, 10150, 9980, 10120),
            make_bar(1, 10120, 10300, 10100, 10250),
            make_bar(2, 10250, 10260, 10020, 10050),
            make_bar(3, 10050, 10120, 10000, 10100),
            make_bar(4, 10135, 10160, 10130, 10150),
            make_bar(5, 10155, 10200, 10150, 10190),
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
        self.assertFalse(res['target_reverse_engineered'])
        self.assertEqual(res['viable_target'], res['measured_target'])
        self.assertGreater(res['measured_target'], res['required_1_5R_target'])
        self.assertTrue(res['economics']['rr_pass'])
        self.assertEqual(res['orderability']['orderability_status'], 'ORDERABLE_SCENARIO')
        self.assertFalse(res['actual_order_permission'])

        # 2) Wide 3-bar range noise where measured_target < required_1_5R_target:
        #    MUST reject under standard evaluation (no reverse-engineered target inflation!)
        wide_bars = [
            make_bar(3, 9690, 9760, 9690, 9750),
            make_bar(4, 9750, 9800, 9710, 9780),
            make_bar(5, 9780, 9810, 9730, 9800),
        ]
        wide_res = trend.evaluate_entry(
            bars=wide_bars,
            asof=asof,
            price=9830,
            bid=9820,
            ask=9830,
            bid_qty=500,
            ask_qty=500,
            depth_available=True,
            quote_fresh=True,
            watch=watch,
            cash_budget=50000.0,
            risk_budget=1600.0,
        )
        self.assertEqual(wide_res['status'], 'REJECT')
        self.assertFalse(wide_res['measured_target_rr_pass'])
        self.assertLess(wide_res['measured_target'], wide_res['required_1_5R_target'])

        # 3) If allow_unbounded_trend_hold=True is explicitly requested for pure trend-exit research,
        #    it marks trend_hold_only_candidate=True while keeping orderability_status='REJECT'.
        hold_only = trend.evaluate_entry(
            bars=wide_bars,
            asof=asof,
            price=9830,
            bid=9820,
            ask=9830,
            watch=watch,
            allow_unbounded_trend_hold=True,
        )
        self.assertEqual(hold_only['status'], 'PASS')
        self.assertTrue(hold_only['trend_hold_only_candidate'])
        self.assertFalse(hold_only['measured_target_rr_pass'])
        self.assertEqual(hold_only['orderability']['orderability_status'], 'REJECT')

        # 4) Major resistance at or below breakout is rejected immediately
        blocked = trend.evaluate_entry(
            bars=bars,
            asof=asof,
            price=10210,
            bid=10200,
            ask=10210,
            watch={**watch, 'resistance': 10200},
            causal_noise=20.0,
        )
        self.assertEqual(blocked['status'], 'REJECT')
        self.assertEqual(blocked['reason'], 'MAJOR_RESISTANCE_AT_OR_BELOW_BREAKOUT')

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
        bars = [
            make_bar(0, 2000, 2060, 1995, 2050),
            make_bar(1, 2050, 2140, 2045, 2120),
            make_bar(2, 2110, 2115, 2040, 2045),
            make_bar(3, 2045, 2055, 2030, 2035),
            make_bar(4, 2035, 2050, 2030, 2040),
        ]
        asof = datetime(2026, 10, 26, 9, 5, 2, tzinfo=KST)
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
            noise_verified=False,
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

    def test_p3_completed_bar_non_ordering_hook(self):
        bars = [
            {**make_bar(i, 10000 + i * 20, 10100 + i * 20, 9990 + i * 20, 10050 + i * 20),
             'time': datetime(2026, 10, 26, 9, i, 0, tzinfo=KST)}
            for i in range(4)
        ]
        t = Tick('047770', datetime(2026, 10, 26, 9, 4, 2, tzinfo=KST), 10200, 100, 5000, 10200, 10190)
        hook_res = observe_replacement_ab_on_completed_bar(
            symbol='047770',
            bars=bars,
            latest_tick=t,
            watch={'status': 'WATCH', 'resistance': 12000, 'source_count': 2, 'market_status': {'pass': True}},
            cfg=self.cfg,
            auto=self.auto,
            cash_budget=35423.5,
            risk_budget=1416.94,
        )
        self.assertTrue(hook_res['evaluated'])
        self.assertFalse(hook_res['actual_order_permission'])
        self.assertFalse(hook_res['live_enabled'])

    def test_p0_counterexample_intrabar_breakout_and_quote_arrival_never_missed_by_cache(self):
        """Counterexample test for P0 Item 2:
        When completed-bar structure cache (_bar_structure_cache) and _setup_gates_cache
        are warm from non-breakout or wide-spread ticks inside the same minute,
        (a) an intra-bar price breakout tick immediately triggers signal emission, and
        (b) a new quote tightening ask/spread into the viable zone immediately triggers signal emission.
        """
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        sym = '999990'
        base_time = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        watch = {'status': 'WATCH', 'resistance': 12000, 'source_details': {'a': {}, 'b': {}}, 'market_status': {'pass': True}}

        # Build 5 completed bars forming a tight C box [9860, 10000]
        cum = 0
        for m in range(5):
            for s in range(60):
                px = 9860 if s < 10 else 10000 if s < 25 else 9970
                cum += 200
                t = Tick(sym, base_time + timedelta(minutes=m, seconds=s), px, 200, cum, px + 10, px - 10)
                det.ingest(t, t.time)

        # In minute 5, send non-breakout ticks at 9970 (warming _bar_structure_cache and _setup_gates_cache)
        for s in range(4):
            cum += 200
            t = Tick(sym, base_time + timedelta(minutes=5, seconds=s), 9970, 200, cum, 9980, 9970)
            det.ingest(t, t.time)
            self.assertIsNone(det.signal(t, watch))

        self.assertIn(sym, det._bar_structure_cache)
        self.assertIn(sym, det._setup_gates_cache)

        # Counterexample (a): Intra-bar breakout tick at second=4 with wide spread (ask=10060, bid=10000 -> spread=0.599% > 0.4%)
        cum += 4000
        t_wide_quote = Tick(sym, base_time + timedelta(minutes=5, seconds=4), 10010, 4000, cum, 10060, 10000)
        det.ingest(t_wide_quote, t_wide_quote.time)
        self.assertIsNone(det.signal(t_wide_quote, watch))

        # Counterexample (b): Same minute, quote improves to ask=10010, bid=10000 -> signal MUST fire immediately!
        t_good_quote = replace(t_wide_quote, ask=10010, bid=10000)
        sig = det.signal(t_good_quote, watch)
        self.assertIsNotNone(sig, "Signal must not be missed when quote improves intra-bar")
        self.assertEqual(sig['pattern'], 'C')

    def test_p0_trade_review_capture_survives_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = TradeReviewCapture(Path(tmp), start=False)
            import os
            real_replace = os.replace
            calls = {'count': 0}

            def flaky_replace(self_path, target):
                calls['count'] += 1
                if calls['count'] <= 2:
                    err = PermissionError(5, "Access is denied")
                    err.winerror = 5
                    raise err
                return real_replace(self_path, target)

            with patch('os.replace', side_effect=flaky_replace):
                cap.write_status(force=True)
            self.assertTrue((cap.folder / 'capture-status.json').exists())
            self.assertGreaterEqual(calls['count'], 3)

    def test_p0_release_integrity_stat_fingerprint_cache(self):
        _INTEGRITY_CACHE.clear()
        with patch('automation.release_integrity.sha256_file', wraps=lambda p: '0' * 64) as mocked_sha:
            check_runtime_integrity()
            first_calls = mocked_sha.call_count
            self.assertGreater(first_calls, 0)
            # Second call with unchanged file stats must hit cache without re-reading/hashing files
            check_runtime_integrity()
            self.assertEqual(mocked_sha.call_count, first_calls)


    def test_support_recovery_fires_in_engine_detector(self):
        """Verify new SupportRecovery in PatternDetector fires on held support with net_RR >= 1.5."""
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        sym = '393210'
        base_time = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        bars = [
            {'time': base_time, 'open': 3660, 'high': 3800, 'low': 3660, 'close': 3780, 'volume': 50000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=1), 'open': 3780, 'high': 4000, 'low': 3770, 'close': 3990, 'volume': 60000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=2), 'open': 3990, 'high': 4305, 'low': 3980, 'close': 4280, 'volume': 80000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=3), 'open': 4280, 'high': 4290, 'low': 4100, 'close': 4120, 'volume': 30000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=4), 'open': 4120, 'high': 4150, 'low': 4005, 'close': 4015, 'volume': 25000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=5), 'open': 4015, 'high': 4050, 'low': 4010, 'close': 4035, 'volume': 20000, 'first_second': 0},
        ]
        for b in bars:
            det.history.setdefault(sym, []).append(b)

        eval_time = base_time + timedelta(minutes=6, seconds=5)
        det.observation_start[sym] = base_time
        for s in range(10, 70):
            det.buffers.setdefault(sym, []).append(Tick(sym, eval_time - timedelta(seconds=s), 4020, 200, 2000000, 4025, 4020))
        for s in range(1, 9):
            det.buffers.setdefault(sym, []).append(Tick(sym, eval_time - timedelta(seconds=s), 4030, 800, 2000000, 4030, 4025))

        t = Tick(sym, eval_time, 4035, 1000, 2010000, 4035, 4030)
        watch = {
            'status': 'WATCH',
            'source_details': {'s1': {}, 's2': {}},
            'resistance': 4305,
            'resistance_room_pass': True,
            'market_status': {'pass': True},
        }

        sig = det.signal(t, watch)
        self.assertIsNotNone(sig, "SupportRecovery must fire on support defense")
        self.assertEqual(sig['pattern'], 'B')
        self.assertEqual(sig['limit'], 4035)
        self.assertEqual(sig['target'], 4305)
        self.assertGreaterEqual(sig['evaluation_gates']['risk_reward']['values']['net_rr'], 1.5)

    def test_trend_breakout_fires_without_nearby_peak_cap(self):
        """Verify new TrendBreakout in PatternDetector fires above tight base without false cap."""
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.continuous_observation = True
        sym = '047770'
        base_time = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        bars = [
            {'time': base_time, 'open': 10000, 'high': 10150, 'low': 9980, 'close': 10120, 'volume': 50000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=1), 'open': 10120, 'high': 10300, 'low': 10100, 'close': 10250, 'volume': 60000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=2), 'open': 10250, 'high': 10260, 'low': 10020, 'close': 10050, 'volume': 80000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=3), 'open': 10050, 'high': 10120, 'low': 10000, 'close': 10100, 'volume': 30000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=4), 'open': 10135, 'high': 10160, 'low': 10130, 'close': 10150, 'volume': 25000, 'first_second': 0},
            {'time': base_time + timedelta(minutes=5), 'open': 10155, 'high': 10200, 'low': 10150, 'close': 10190, 'volume': 20000, 'first_second': 0},
        ]
        for b in bars:
            det.history.setdefault(sym, []).append(b)

        eval_time = base_time + timedelta(minutes=6, seconds=5)
        det.observation_start[sym] = base_time
        for s in range(10, 70):
            det.buffers.setdefault(sym, []).append(Tick(sym, eval_time - timedelta(seconds=s), 10150, 100, 2000000, 10160, 10150))
        det.buffers.setdefault(sym, []).append(Tick(sym, eval_time - timedelta(seconds=9), 10160, 500, 2000000, 10170, 10160))
        for s in range(1, 9):
            det.buffers.setdefault(sym, []).append(Tick(sym, eval_time - timedelta(seconds=s), 10200, 500, 2000000, 10210, 10200))

        t = Tick(sym, eval_time, 10210, 500, 2010000, 10210, 10200)
        watch = {
            'status': 'WATCH',
            'source_details': {'s1': {}, 's2': {}},
            'resistance': 11500,
            'resistance_room_pass': True,
            'market_status': {'pass': True},
        }

        sig = det.signal(t, watch)
        self.assertIsNotNone(sig, "TrendBreakout must fire on breakout above 10200")
        self.assertEqual(sig['pattern'], 'A')
        self.assertEqual(sig['limit'], 10210)
        self.assertEqual(sig['target'], 10700)
        self.assertGreaterEqual(sig['evaluation_gates']['risk_reward']['values']['net_rr'], 1.5)


if __name__ == '__main__':
    unittest.main()

