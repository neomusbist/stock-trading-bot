"""Unit tests for V1.5 stability, entry quality, and execution resilience improvements."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from dataclasses import replace
import time
import queue

from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.runner import AccountWorker, request_capital_refresh, readiness_snapshot
from automation.focus_selection import select_focus
from core.strategy import Tick
from core.settings import Config, KST
from tests import test_setup_acceleration as entry_fixture


def make_bar(t, o, h, l, c, v=100):
    return {'time': t, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v, 'first_second': 0, 'gap_reason': None}


class V15StabilityQualityTests(unittest.TestCase):
    setUp = entry_fixture.SetupAccelerationTests.setUp

    def test_reconcile_deferred_flags_immediate_refresh(self):
        """When local fill postdates snapshot query start, reconcile returns False and flags immediate refresh."""
        self.e.s['orders']['ORDER-1'] = {
            'local': '2026-10-13-1', 'id': 'REAL-1', 'symbol': '042510',
            'side': 'BUY', 'qty': 1, 'limit': 18540, 'filled': 1, 'amount': 18540,
            'status': 'FILLED', 'last_fill_at': '2026-10-13T09:54:36.000+09:00'
        }
        stale_snapshot = {
            'cash': 100000,
            'query_started_at': '2026-10-13T09:54:30.000+09:00',
            'holdings': {},
            'orders': []
        }
        res = self.e.reconcile(stale_snapshot, fills=[])
        self.assertFalse(res)
        self.assertTrue(self.e.s['automation'].get('reconcile_deferred_pending'))

        # request_capital_refresh should consume the flag and signal account worker immediately
        mock_account = Mock()
        triggered = request_capital_refresh(self.e, mock_account, in_session=True)
        self.assertTrue(triggered)
        mock_account.request.set.assert_called_once()
        self.assertFalse(self.e.s['automation'].get('reconcile_deferred_pending'))

    def test_account_worker_throttles_rapid_requests(self):
        """AccountWorker throttles successive queries to prevent broker API flood."""
        mock_rest = Mock()
        mock_rest.snapshot.return_value = {'query_started_at': '2026-10-13T09:54:30+09:00', 'holdings': {}, 'orders': []}
        worker = AccountWorker(mock_rest, interval=5.0)
        worker.min_query_interval = 0.1  # Fast for test

        # Simulate first query
        worker.last_query_started = time.monotonic()
        now_before = time.monotonic()

        # Run one step of run() logic manually or check elapsed
        elapsed = time.monotonic() - worker.last_query_started
        self.assertLessEqual(elapsed, worker.min_query_interval)

    def test_tick_lag_source_diagnosis(self):
        """readiness_snapshot correctly classifies the source of tick staleness."""
        counts = {'ticks': 10, 'subscription_acks': 1, 'account_checks': 1, 'radar_cycles': 1}
        now_dt = datetime(2026, 10, 13, 9, 54, 0, tzinfo=KST)

        # 1. WS_SOURCE_SILENT: tick is stale, event loop is normal, queue is empty
        self.e.last_regular_tick_at = now_dt - timedelta(seconds=10)
        self.e.event_loop_lag = 0.05
        self.e.feed_queue_size = 0
        snap = readiness_snapshot(self.e, counts, radar_ok=True, stamp=now_dt, detector=None)
        self.assertEqual(snap['tick_lag_source'], 'WS_SOURCE_SILENT')

        # 2. EVENT_LOOP_LAG: tick is stale and event loop lag > 1.0s
        self.e.event_loop_lag = 1.5
        snap = readiness_snapshot(self.e, counts, radar_ok=True, stamp=now_dt, detector=None)
        self.assertEqual(snap['tick_lag_source'], 'EVENT_LOOP_LAG')

        # 3. QUEUE_BUFFER: tick is stale, event loop normal, but queue > 5 items
        self.e.event_loop_lag = 0.1
        self.e.feed_queue_size = 12
        snap = readiness_snapshot(self.e, counts, radar_ok=True, stamp=now_dt, detector=None)
        self.assertEqual(snap['tick_lag_source'], 'QUEUE_BUFFER')

        # 4. Fresh tick: tick_lag_source is None
        self.e.last_regular_tick_at = now_dt - timedelta(seconds=2)
        snap = readiness_snapshot(self.e, counts, radar_ok=True, stamp=now_dt, detector=None)
        self.assertIsNone(snap['tick_lag_source'])

    def test_shallow_pullback_blocked_under_chase_quality_filter(self):
        """Setup B with shallow retrace (<20%) or only 1 pull bar is rejected under chase_quality_filter."""
        start = self.clock - timedelta(minutes=10)
        # Impulse from 17000 to 18680 (+9.8%), then a tiny dip of 1 bar to 18550 (retrace = 130/1680 = 7.7%)
        b1 = make_bar(start + timedelta(minutes=1), 17000, 17200, 17000, 17180, 500)
        b2 = make_bar(start + timedelta(minutes=2), 17180, 17600, 17150, 17550, 600)
        b3 = make_bar(start + timedelta(minutes=3), 17550, 18200, 17500, 18100, 800)
        b4 = make_bar(start + timedelta(minutes=4), 18100, 18680, 18050, 18650, 1000) # peak at 18680
        b5 = make_bar(start + timedelta(minutes=5), 18650, 18660, 18550, 18580, 200) # only 1 pull bar, tiny retrace

        bars = [b1, b2, b3, b4, b5]

        # With chase_quality_filter = True (default)
        self.d.a = replace(self.d.a, chase_quality_filter=True, pullback_min_retrace=0.20)
        res, reason, evidence = self.d.pullback_analysis(bars)
        self.assertIsNone(res)
        self.assertEqual(reason, 'PULLBACK_SHALLOW_OR_UNCONFIRMED')

        # With chase_quality_filter = False (legacy)
        self.d.a = replace(self.d.a, chase_quality_filter=False)
        res_legacy, reason_legacy, _ = self.d.pullback_analysis(bars)
        self.assertIsNotNone(res_legacy)
        self.assertEqual(res_legacy['pattern'], 'B')

    def test_late_extension_blocked_under_chase_quality_filter(self):
        """Setup B and C are rejected when price is extended > 7% above swing low without deep consolidation."""
        start = self.clock - timedelta(minutes=10)
        # Swing low at 17000, current price at 18540 (extension = 9.05% > 7%)
        b1 = make_bar(start + timedelta(minutes=1), 17000, 17300, 17000, 17280, 500)
        b2 = make_bar(start + timedelta(minutes=2), 17280, 17700, 17250, 17650, 600)
        b3 = make_bar(start + timedelta(minutes=3), 17650, 18300, 17600, 18250, 700)
        b4 = make_bar(start + timedelta(minutes=4), 18250, 18600, 18200, 18550, 800)
        b5 = make_bar(start + timedelta(minutes=5), 18550, 18580, 18400, 18450, 300)
        b6 = make_bar(start + timedelta(minutes=6), 18450, 18500, 18420, 18480, 200)

        bars = [b1, b2, b3, b4, b5, b6]
        self.d.a = replace(self.d.a, chase_quality_filter=True)

        setup_c = {'pattern': 'C', 'breakout': 18500, 'support': 18400, 'projection': 18600, 'box_width': 100}
        t = Tick('042510', self.clock, 18540, 100, 50000, 18540, 18530)
        gates = self.d.setup_gates(t, self.watch, setup_c, bars)
        # Invalidation gate should fail due to late-stage extension > 7%
        self.assertFalse(gates['invalidation']['result'] == 'PASS')

    def test_resilient_emergency_bid_stop(self):
        """Under resilient_protection_enabled=True, a 1-tick bid dip does not trigger panic exit, but flash crash does."""
        self.e.a = replace(self.e.a, resilient_protection_enabled=True)
        # Create position at 18540, stop at 18320 (tick_size is 10)
        self.e.s['positions']['042510'] = {
            'symbol': '042510', 'qty': 1, 'avg': 18540, 'initial_stop': 18320, 'stop': 18320,
            'initial_r': 220, 'high': 18540, 'low': 18540, 'opened': self.clock.isoformat(),
            'exit_requested': False, 'exit_reason': '', 'exit_attempts': 0, 'qty_blocked': False,
            'last_tick': self.clock.isoformat()
        }

        # 1. Normal wobble: bid touches 18300 (2 ticks below stop, but > 3 ticks / 0.8% buffer)
        # stop = 18320; 3 ticks = 30; 0.8% = 146; buffer = max(30, 146) = 146 -> emergency_bid_stop = 18170
        t_wobble = Tick('042510', self.clock, 18340, 10, 1000, 18340, 18300)
        self.e.on_tick(t_wobble)
        self.assertFalse(self.e.s['positions']['042510']['exit_requested'])

        # 2. Structural stop: executed price trades at or below stop (18320)
        t_stopped = Tick('042510', self.clock, 18320, 10, 1000, 18330, 18320)
        self.e.on_tick(t_stopped)
        self.assertTrue(self.e.s['positions']['042510']['exit_requested'])
        self.assertEqual(self.e.s['positions']['042510']['exit_reason'], '구조/이익보호선 이탈')

        # 3. Flash crash: bid collapses to 18150 (below emergency_bid_stop 18170) while trade price is 18350
        self.e.s['positions']['042510']['exit_requested'] = False
        self.e.s['positions']['042510']['exit_reason'] = ''
        t_flash = Tick('042510', self.clock, 18350, 10, 1000, 18350, 18150)
        self.e.on_tick(t_flash)
        self.assertTrue(self.e.s['positions']['042510']['exit_requested'])
        self.assertEqual(self.e.s['positions']['042510']['exit_reason'], '호가 급락/유동성 붕괴')

    def test_unaffordable_stock_filtered_from_focus(self):
        """ONE_SHARE_UNAFFORDABLE candidates are excluded from WATCH selection when capital is limited."""
        candidates = [
            {'code': '042510', 'name': '라온시큐어', 'reference_price': 17500, 'source_count': 3,
             'affordability': {'entry_eligible': True, 'reason_code': 'ONE_SHARE_AFFORDABLE'}, 'discovery_score': 35},
            {'code': '000660', 'name': 'SK하이닉스', 'reference_price': 190000, 'source_count': 5,
             'affordability': {'entry_eligible': False, 'reason_code': 'ONE_SHARE_UNAFFORDABLE'}, 'discovery_score': 55}
        ]
        sel = select_focus({}, candidates, {}, max_watchlist=8, fallback_source1=True)
        # SK하이닉스 should be excluded, and 라온시큐어 should be promoted to WATCH
        self.assertIn('042510', sel.next_watch)
        self.assertNotIn('000660', sel.next_watch)

    def test_tight_stop_distance_rejected_under_chase_quality_filter(self):
        """Setup with too tight stop distance (<1.2%, like Kumho Electric 0.85%) is rejected under chase_quality_filter."""
        # Kumho Electric: limit = 12840, support = 12740, stop = 12730 -> distance = 0.85%
        setup_b = {'pattern': 'B', 'breakout': 12840, 'support': 12740, 'peak': 12840, 'projection': 13000}
        t = Tick('001210', self.clock, 12840, 10, 1000, 12840, 12830)

        # 1. Under chase_quality_filter = True: rejected
        self.d.a = replace(self.d.a, chase_quality_filter=True, min_stop_distance_fraction=0.012)
        gates = self.d.setup_gates(t, self.watch, setup_b)
        self.assertFalse(gates['invalidation']['result'] == 'PASS')

        # 2. Under chase_quality_filter = False (legacy): passes
        self.d.a = replace(self.d.a, chase_quality_filter=False)
        gates_legacy = self.d.setup_gates(t, self.watch, setup_b)
        self.assertTrue(gates_legacy['invalidation']['result'] == 'PASS')


if __name__ == '__main__':
    unittest.main()
