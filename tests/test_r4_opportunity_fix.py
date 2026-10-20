"""Regressions and verifications for R4 trading opportunity and churn bottlenecks."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automation.capital import Costs
from automation.focus_selection import calculate_tier, select_focus, TIER_RANK_ONLY, TIER_FRESH_WEAK
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.radar import DynamicRadar, RadarWorker
from automation.structural_box import structural_box
from core.settings import Config, KST
from core.strategy import Tick

AT = datetime(2026, 10, 14, 9, 24, 7, tzinfo=KST)
SYM = '999990'
AUTO = replace(AutomationConfig(), fresh_box_basis_enabled=True, confirmed_box_direction=True, confirmed_invalidation_enabled=True)
CFG = Config()
COSTS = Costs(.00015, .00015, .002, .002, .003)

def candles(values):
    return [dict(time=AT.replace(second=0)-timedelta(minutes=len(values)-i), open=o, high=h, low=l, close=c,
                 volume=v, first_second=0, gap_reason=None) for i, (o, h, l, c, v) in enumerate(values)]

PAN_OCEAN_BARS = candles([(6920, 6920, 6890, 6910, 58506), (6910, 6920, 6870, 6890, 54669),
                          (6890, 6900, 6870, 6880, 19936), (6880, 6890, 6870, 6890, 23851),
                          (6890, 6960, 6880, 6960, 273891)])

class R4OpportunityFixTests(unittest.TestCase):
    def test_accumulating_bc_retains_rank_only_tier_despite_acceleration_fail(self):
        gates = {
            'warmup': {'result': 'PASS'},
            'acceleration': {'result': 'FAIL', 'values': {'ratio': 0.42}},
            'liquidity': {'result': 'PASS', 'values': {'window_turnover': 30000000}},
            'spread': {'result': 'PASS'},
            'structure': {'result': 'PASS'}
        }
        # Setup B and C are accumulating usable bars (candidate_generated = False).
        trials = [
            {'setup': 'B', 'candidate_generated': False, 'acceleration_required': False},
            {'setup': 'C', 'candidate_generated': False, 'acceleration_required': False}
        ]
        tier = calculate_tier(gates, trials)
        self.assertEqual(tier, TIER_RANK_ONLY)

    def test_legacy_empty_trials_still_yields_fresh_weak_on_accel_fail(self):
        gates = {
            'warmup': {'result': 'PASS'},
            'acceleration': {'result': 'FAIL'},
            'liquidity': {'result': 'PASS'}
        }
        self.assertEqual(calculate_tier(gates, []), TIER_FRESH_WEAK)

    def test_discovery_score_floors_negative_rank_improvement(self):
        radar = DynamicRadar(CFG, AUTO, Mock(locked=lambda s: False))
        items = [{
            'code': '042370',
            'reference_price': 10000,
            'source_details': {'a': {'observed_rank': 12, 'values': {'flu_rt': 2.5}}}
        }]
        radar.previous['042370'] = {
            'source_details': {'a': {'observed_rank': 10, 'values': {'flu_rt': 2.5}}},
            'stamp': AT - timedelta(seconds=60)
        }
        rows, _, _ = radar.update(items, lambda r: {'status': 'ELIGIBLE'}, AT)
        self.assertEqual(len(rows), 1)
        # Even with rank dropping 10 -> 12 (-2 positions), discovery_score must not drop below 10 + 1/12
        expected_base = 10 + 1/12
        self.assertAlmostEqual(rows[0]['discovery_score'], expected_base, places=4)

    def test_scheduled_items_admits_multi_source_candidates_to_hot_slots(self):
        worker = object.__new__(RadarWorker)
        worker.a = AUTO
        worker.radar = SimpleNamespace(watch={})
        worker.status_waiting = {}
        worker.status_sequence = 0
        worker.status_cache = {}
        worker.status_priority_symbols = frozenset(str(i) for i in range(12))
        rows = [{'code': str(i), 'source_details': {'s': {}}, 'discovery_context': {'last_cycle_return': 0}} for i in range(85)]
        # Symbol 55 has 2 sources (multi-source surge).
        rows[55]['source_details'] = {'s1': {}, 's2': {}}
        with patch('automation.radar.now', return_value=AT):
            order = worker.scheduled_items(rows)
        # Active WATCH (0-11) first, then the multi-source hot candidate (55).
        self.assertEqual([x['code'] for x in order[:12]], list(map(str, range(12))))
        self.assertEqual(order[12]['code'], '55')

    def test_prior_high_remains_active_across_breakout_minute(self):
        d = PatternDetector(CFG, AUTO, COSTS)
        # Feed ticks to establish prior session high at 10000 across minute 0 and 1.
        for sec in range(125):
            t = Tick(SYM, AT.replace(minute=0, second=sec % 60), 10000, 100, 100 * (sec + 1), 10000, 9990)
            d.ingest(t, t.time)
        # New minute 2: tick 1 breaks out to 10050.
        t_break1 = Tick(SYM, AT.replace(minute=2, second=5), 10050, 100, 100 * 126, 10050, 10040)
        d.ingest(t_break1, t_break1.time)
        self.assertEqual(d.prior_high.get(SYM), 10000)
        self.assertEqual(d.day_high.get(SYM), 10050)
        # 10 seconds later in the same minute, price is still at 10050.
        t_break2 = Tick(SYM, AT.replace(minute=2, second=15), 10050, 100, 100 * 127, 10050, 10040)
        d.ingest(t_break2, t_break2.time)
        # prior_high must still be 10000 so breakout condition (price > prior_high) stays true!
        self.assertEqual(d.prior_high.get(SYM), 10000)
        self.assertTrue(t_break2.price > d.prior_high.get(SYM))

    def test_pan_ocean_rejected_by_expanding_impulse_and_reward_guard(self):
        d = PatternDetector(CFG, AUTO, COSTS)
        setup = d.box(PAN_OCEAN_BARS)
        self.assertIsNotNone(setup)
        # 1. Structural box rejects Pan Ocean's 5th bar spike as expanding impulse.
        passed, evidence = structural_box(PAN_OCEAN_BARS, AT, setup, CFG, AUTO, 6970, 6960, 6970)
        self.assertFalse(passed)
        self.assertEqual(evidence['reason'], 'BOX_CONTAINS_EXPANDING_IMPULSE')

        # 2. Exit plan net reward/risk guard rejects Pan Ocean's 0.8078 net RR.
        from automation.exit_plan import build_plan
        signal = {
            'pattern': 'C', 'session': 'REGULAR', 'venue': 'KRX',
            'limit': 6970, 'stop': 6860, 'target': 7140, 'projection': 7160
        }
        plan = build_plan(CFG, replace(AUTO, exit_plan_reward_guard_enabled=True),
                          signal, 6970, 10, False)
        self.assertFalse(plan['reward_guard_pass'])
        self.assertLess(plan['net_reward_risk'], 1.0)
        self.assertAlmostEqual(plan['net_reward_risk'], 0.8078, places=3)

    def test_scheduled_items_admits_shadow_readiness_priority_to_hot_slots(self):
        worker = object.__new__(RadarWorker)
        worker.a = replace(AUTO, structural_breakout_enabled=True)
        worker.radar = SimpleNamespace(watch={}, momentum={'77': {'fast_signal_ready': True, 'tier': 0}},
            policy=SimpleNamespace(locked=lambda symbol: False), weak={}, affordable=lambda row: True)
        worker.status_waiting = {}
        worker.status_sequence = 0
        worker.status_cache = {}
        worker.status_priority_symbols = frozenset(str(i) for i in range(12))
        rows = [{'code': str(i), 'source_details': {'s': {}}, 'discovery_context': {'last_cycle_return': 0}} for i in range(85)]
        with patch('automation.radar.now', return_value=AT):
            order = worker.scheduled_items(rows)
        # Active WATCH (0-11) first, then candidate with SHADOW fast readiness ('77').
        self.assertEqual([x['code'] for x in order[:12]], list(map(str, range(12))))
        self.assertEqual(order[12]['code'], '77')

    def test_scheduled_items_expands_hot_slots_when_active_incumbents_are_cached(self):
        worker = object.__new__(RadarWorker)
        worker.a = AUTO
        worker.radar = SimpleNamespace(watch={})
        worker.status_waiting = {}
        worker.status_sequence = 0
        worker.status_priority_symbols = frozenset(str(i) for i in range(12))
        # All 12 active incumbents have fresh cached status checked 5 seconds ago
        worker.status_cache = {str(i): {'checked_at': (AT - timedelta(seconds=5)).isoformat()} for i in range(12)}
        # 16 candidates have gains
        rows = [{'code': str(i), 'source_details': {'s': {}}, 'discovery_context': {'last_cycle_return': 0.05 if i >= 12 and i < 28 else 0}} for i in range(85)]
        with patch('automation.radar.now', return_value=AT):
            order = worker.scheduled_items(rows)
        # Active WATCH (0-11) first, then all 16 hot candidates admitted (slots expanded to 20 instead of 8)
        self.assertEqual([x['code'] for x in order[:12]], list(map(str, range(12))))
        hot_admitted = [x['code'] for x in order[12:28]]
        self.assertEqual(set(hot_admitted), set(str(i) for i in range(12, 28)))

