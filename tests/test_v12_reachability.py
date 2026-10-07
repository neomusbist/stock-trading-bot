"""V1.2 종합 도달성 및 안전성 회귀 시험 스위트.
- 합성 A/B/C 파이프라인 실제 도달성 (전략 임계값 우회 없이 정규 게이트 통과)
- SHADOW 격리 보장 (SHADOW_ONLY=True, actual_order_permission=False, broker 호출 0건)
- Fast promotion & Focus selection (Tier 기반 즉시 승격, 동일 Tier churn 방지, source>=2 우선 및 source=1 빈 슬롯 한정)
- SessionPlan 단일 기준 및 불변식 (14:55 신규매수 종료, 15:10 강제 청산, overnight 금지)
- Reconciliation freshness race condition 방지 보존
- 기존 포지션 정책(HARD_LOCK, BREAKEVEN_EXIT, EXTERNAL_HOLD) 보존
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from core.settings import Config, KST, ROOT
from core.strategy import Tick
from automation.policy import AutomationConfig, Policy, EXISTING_POLICIES
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.session import SessionPlan, DEFAULT_SESSION_PLAN, assessment
from automation.focus_selection import (
    select_focus, calculate_tier, FocusSelection,
    candidate_priority_tuple, FocusNotification, TIER_NAMES,
    TIER_SIGNAL_READY, TIER_GEOMETRY_BASE_READY, TIER_MOMENTUM_BASE_READY,
    TIER_RANK_ONLY, TIER_FRESH_WEAK
)
from automation.shadow_observation import ShadowObserver, make_evaluation_view
from automation.observation_worker import ObservationWorker
from tests.test_automation import FakeBroker
from tests.test_pipeline import replay_prices

START = datetime(2026, 9, 30, 14, 40, tzinfo=KST)
SYMBOL = '999990'


class V12ReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.clock = [START]
        self.broker = FakeBroker()
        self.policy = Policy(self.folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=1000000,
            costs_confirmed=True
        )
        self.cfg = replace(Config(), live_mode=True)
        self.out = contextlib.redirect_stdout(io.StringIO())
        self.out.__enter__()
        self.addCleanup(self.out.__exit__, None, None, None)
        self.engine = AutonomousEngine(
            self.cfg, self.auto, self.policy, self.folder / 'book', self.broker, lambda: self.clock[0]
        )
        self.engine.market_status = {SYMBOL: {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'}}
        self.engine.reconcile({'cash': 1000000, 'equity': 1000000, 'orders': [], 'holdings': {}})
        self.engine.connected = True
        self.engine.market = '3'

    # -------------------------------------------------------------------------
    # 1. 합성 A/B/C 파이프라인 실제 도달성
    # -------------------------------------------------------------------------
    def _run_synthetic_pattern_to_order(self, pattern):
        detector = PatternDetector(self.cfg, self.auto, self.engine.costs)
        cumulative = 0
        signal = None
        watch = {
            'status': 'WATCH',
            'resistance': 11000,
            'previous_close': 10000,
            'source_details': {'ka10032': {}, 'ka10027': {}}
        }
        last_tick = None
        for second, price, volume in replay_prices(pattern):
            self.clock[0] = START + timedelta(seconds=second)
            cumulative += volume
            last_tick = Tick(SYMBOL, self.clock[0], price, volume, cumulative, price + 10, price - 10)
            self.assertTrue(detector.ingest(last_tick, self.clock[0]))
            signal = detector.signal(last_tick, watch)

        self.assertIsNotNone(signal, f"Synthetic setup {pattern} did not generate signal")
        self.assertEqual(signal['pattern'], pattern)

        # 게이트 검증: warmup, spread, structure, acceleration, liquidity 통과 확인
        gates = signal['evaluation_gates']
        self.assertEqual(gates['warmup']['result'], 'PASS')
        self.assertEqual(gates['spread']['result'], 'PASS')
        self.assertEqual(gates['structure']['result'], 'PASS')
        self.assertEqual(gates['acceleration']['result'], 'PASS')
        self.assertEqual(gates['liquidity']['result'], 'PASS')

        # 전략 불변식 검증
        self.assertGreaterEqual(signal['turnover_ratio'], 3.0)
        self.assertGreaterEqual(signal['turnover'], 30000000)
        self.assertGreaterEqual(signal['gain'], 0.003)

        # 엔진 진입 평가 및 브로커 주문 접수
        self.engine.last_account = __import__('time').monotonic()
        order = self.engine.enter(signal, last_tick)
        self.assertIsNotNone(order, f"Engine enter failed for {pattern}")
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.broker.orders[-1][0], 'BUY')
        self.assertEqual(self.broker.orders[-1][1], SYMBOL)
        self.assertGreater(self.broker.orders[-1][2], 0)

    def test_synthetic_patterns_a_b_c_reachability(self):
        for pattern in ('A', 'B', 'C'):
            self.setUp()
            self._run_synthetic_pattern_to_order(pattern)

    def test_strategy_parameter_boundaries_enforced(self):
        detector = PatternDetector(self.cfg, self.auto, self.engine.costs)
        # acceleration < 3.0 실패 검증
        watch = {'status': 'WATCH', 'resistance': 11000, 'previous_close': 10000}
        t = Tick(SYMBOL, START, 10000, 10, 10, 10010, 9990)
        gates = detector.base_gates(t, watch)
        self.assertEqual(gates['acceleration']['result'], 'UNKNOWN')
        self.assertIsNone(detector.signal(t, watch))

    # -------------------------------------------------------------------------
    # 2. SHADOW 엄격 격리
    # -------------------------------------------------------------------------
    def test_shadow_observer_strict_isolation_no_broker_calls(self):
        events = []
        observer = ShadowObserver(
            self.cfg, self.auto, self.engine.costs,
            lambda kind, **data: events.append((kind, data))
        )
        row = {'code': SYMBOL, 'status': 'SHADOW', 'source_count': 2, 'resistance': 11000, 'previous_close': 10000}
        observer.update([row], START)

        cumulative = 0
        for second, price, volume in replay_prices('A'):
            stamp = START + timedelta(seconds=second)
            cumulative += volume
            t = Tick(SYMBOL, stamp, price, volume, cumulative, price + 10, price - 10)
            observer.tick(t, stamp)

        # 브로커 주문 0건
        self.assertEqual(len(self.broker.orders), 0)
        # 모든 이벤트에 SHADOW_ONLY=True, actual_order_permission=False 포함
        for kind, data in events:
            self.assertTrue(data.get('SHADOW_ONLY', False), f"{kind} missing SHADOW_ONLY")
            self.assertFalse(data.get('actual_order_permission', False), f"{kind} has actual order permission")

    # -------------------------------------------------------------------------
    # 3. Focus Selection 및 Fast Promotion
    # -------------------------------------------------------------------------
    def test_calculate_tier_mapping(self):
        row = {'source_count': 2}
        all_pass_gates = {
            'warmup': {'result': 'PASS'},
            'spread': {'result': 'PASS'},
            'structure': {'result': 'PASS'},
            'rr': {'result': 'PASS'},
            'resistance': {'result': 'PASS'},
            'economic': {'result': 'PASS'},
            'breakout': {'result': 'PASS'},
            'liquidity': {'result': 'PASS'},
            'acceleration': {'result': 'PASS'}
        }
        trials = [{'setup': 'A', 'candidate_generated': True, 'gates': {'breakout': {'result': 'PASS'}}}]
        # Tier 0: all gates PASS, full signal ready
        self.assertEqual(calculate_tier(all_pass_gates, trials, row), TIER_SIGNAL_READY)

        # Tier 1: all gates PASS, geometry ready but breakout waiting
        trials_geom = [{'setup': 'A', 'candidate_generated': True, 'gates': {'breakout': {'result': 'FAIL'}}}]
        self.assertEqual(calculate_tier(all_pass_gates, trials_geom, row), TIER_GEOMETRY_BASE_READY)

        # Tier 2: all common gates PASS, no geometry
        trials_none = [{'setup': 'A', 'candidate_generated': False}]
        self.assertEqual(calculate_tier(all_pass_gates, trials_none, row), TIER_MOMENTUM_BASE_READY)

        # Tier 3: rank only / no fresh readiness
        self.assertEqual(calculate_tier({}, trials_none, row), TIER_RANK_ONLY)

        # Tier 4: acceleration or liquidity FAIL
        accel_fail = dict(all_pass_gates, acceleration={'result': 'FAIL'})
        self.assertEqual(calculate_tier(accel_fail, trials_none, row), TIER_FRESH_WEAK)

    def test_higher_tier_promotes_immediately_and_zero_same_tier_churn(self):
        current_watch = ['001001', '001002']
        all_candidates = [
            {'code': '001001', 'status': 'WATCH', 'source_count': 2, 'score': 100},
            {'code': '001002', 'status': 'WATCH', 'source_count': 2, 'score': 90},
            {'code': '001003', 'status': 'SHADOW', 'source_count': 2, 'score': 80}
        ]
        # 둘 다 Tier 2일 때 동일 Tier 후보(001003)가 있어도 Churn 없이 기존 유지
        eval_views = {
            '001001': {'tier': 2, 'gates': {}, 'setup_trials': []},
            '001002': {'tier': 2, 'gates': {}, 'setup_trials': []},
            '001003': {'tier': 2, 'gates': {}, 'setup_trials': []}
        }
        res = select_focus(current_watch, all_candidates, eval_views, max_watchlist=2)
        self.assertEqual(res.next_watch, ['001001', '001002'])
        self.assertEqual(res.promoted, [])

        # 001003이 Tier 0으로 승격되면 즉시 Tier 2(001002)를 대체하여 승격
        eval_views['001003']['tier'] = 0
        eval_views['001003']['setup_trials'] = [{'candidate_generated': True, 'gates': {'breakout': {'result': 'PASS'}}}]
        res2 = select_focus(current_watch, all_candidates, eval_views, max_watchlist=2)
        self.assertIn('001003', res2.next_watch)
        self.assertEqual(res2.promoted, ['001003'])
        self.assertEqual(res2.demoted, ['001002'])

    def test_source_primary_priority_and_fallback_in_empty_slots_only(self):
        current_watch = []
        all_candidates = [
            {'code': 'PRIMARY1', 'status': 'SHADOW', 'source_count': 2, 'score': 100},
            {'code': 'FALLBACK1', 'status': 'SHADOW', 'source_count': 1, 'score': 999},  # 점수가 높아도 source_count=1
            {'code': 'PRIMARY2', 'status': 'SHADOW', 'source_count': 3, 'score': 50}
        ]
        eval_views = {
            'PRIMARY1': {'tier': 2, 'gates': {}, 'setup_trials': []},
            'FALLBACK1': {'tier': 0, 'gates': {}, 'setup_trials': []},  # fallback이지만 tier 0
            'PRIMARY2': {'tier': 2, 'gates': {}, 'setup_trials': []}
        }
        # max_watchlist = 2: PRIMARY1과 PRIMARY2가 우선 채워지고, 빈 슬롯이 없으면 FALLBACK1은 진입 불가
        res = select_focus(current_watch, all_candidates, eval_views, max_watchlist=2)
        self.assertEqual(set(res.next_watch), {'PRIMARY1', 'PRIMARY2'})
        self.assertNotIn('FALLBACK1', res.next_watch)

        # max_watchlist = 3으로 빈 슬롯이 있으면 FALLBACK1이 빈 슬롯에 진입
        res_3 = select_focus(current_watch, all_candidates, eval_views, max_watchlist=3)
        self.assertEqual(set(res_3.next_watch), {'PRIMARY1', 'PRIMARY2', 'FALLBACK1'})

    # -------------------------------------------------------------------------
    # 4. SessionPlan 단일 기준 및 불변식
    # -------------------------------------------------------------------------
    def test_session_plan_invariants_and_windows(self):
        plan = SessionPlan(
            observe_start='09:00:00',
            new_entry_start='09:02:00',
            hard_new_entry_end='15:05:00',
            force_flatten_start='15:10:00',
            minimum_remaining_minutes=15,
            support_end='15:20:00',
            allow_overnight=False
        ).validate()
        self.assertEqual(plan.effective_new_entry_end, '14:55:00')
        self.assertTrue(plan.observe_start <= plan.new_entry_start < plan.effective_new_entry_end < plan.force_flatten_start < plan.support_end)
        dt_entry = datetime(2026, 10, 6, 14, 54, 59, tzinfo=KST)
        self.assertTrue(plan.is_entry_window(dt_entry))
        dt_cutoff = datetime(2026, 10, 6, 14, 55, 0, tzinfo=KST)
        self.assertFalse(plan.is_entry_window(dt_cutoff))
        dt_flatten = datetime(2026, 10, 6, 15, 10, 0, tzinfo=KST)
        self.assertTrue(plan.is_flatten_window(dt_flatten))
        with self.assertRaises(AssertionError):
            SessionPlan(allow_overnight=True).validate()

    # -------------------------------------------------------------------------
    # 5. Reconciliation Freshness Race Condition 방지 보존
    # -------------------------------------------------------------------------
    def test_reconciliation_freshness_patch_preserved(self):
        fill_time = START + timedelta(minutes=5)
        # 포지션 생성
        self.engine.s['positions'][SYMBOL] = {
            'qty': 2,
            'avg': 5000.0,
            'last_entry_fill_at': fill_time.isoformat(),
            'stop': 4900,
            'exit_requested': False,
            'exit_reason': None
        }
        self.engine.actual = {SYMBOL: {'qty': 2, 'avg': 5000.0}}

        # 진입 체결 시각 이전에 시작된 오래된 스냅샷(qty=0) 도착
        stale_snapshot = {
            'query_started_at': (fill_time - timedelta(seconds=1)).isoformat(),
            'cash': 1000000,
            'equity': 1000000,
            'orders': [],
            'holdings': {}  # SYMBOL qty=0
        }
        # stale snapshot으로 인해 false-positive HALT가 발생하지 않아야 함
        self.engine.reconcile(stale_snapshot)
        self.assertEqual(self.engine.s['halt'], '')
        self.assertEqual(self.engine.s['positions'][SYMBOL]['qty'], 2)

    # -------------------------------------------------------------------------
    # 6. 기존 포지션 정책 보존
    # -------------------------------------------------------------------------
    def test_existing_policies_preserved(self):
        self.assertEqual(EXISTING_POLICIES['005930'], 'MANUAL_HOLD')
        self.assertEqual(EXISTING_POLICIES['017900'], 'HARD_LOCK')
        self.assertEqual(EXISTING_POLICIES['052260'], 'BREAKEVEN_EXIT')
        self.assertEqual(EXISTING_POLICIES['267320'], 'EXTERNAL_HOLD')

        # HARD_LOCK 종목 주문 차단 검증
        with self.assertRaises(Exception):
            self.engine.order_guard('kt10000', {'stk_cd': '017900', 'ord_qty': '1', 'ord_uv': '10000'})


if __name__ == '__main__':
    unittest.main()
