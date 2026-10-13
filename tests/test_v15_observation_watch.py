"""Unit tests for V1.5 WATCH stability, real-time observation continuity, and SHADOW isolation."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from dataclasses import replace
import time
import queue

from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.runner import (
    resolve_radar_focus,
    evaluate_actual_entry,
    process_message,
    SubscriptionState,
)
from automation.shadow_observation import ShadowObserver
from automation.observation_worker import ObservationWorker
from core.strategy import Tick
from core.realtime import Feed
from core.settings import Config, KST
from tests import test_setup_acceleration as entry_fixture


def make_bar(t, o, h, l, c, v=100):
    return {'time': t, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v, 'first_second': 0, 'gap_reason': None}


class V15ObservationWatchTests(unittest.TestCase):
    setUp = entry_fixture.SetupAccelerationTests.setUp

    def test_unverified_account_preserves_watch_observation_and_bars(self):
        """11:39:26 scenario: Unverified account capital retains WATCH collection as observation_only without wiping WATCH to 0."""
        sym = '042510'
        # 1. Establish initial WATCH with completed bars
        lookup = {
            sym: {'code': sym, 'name': '라온시큐어', 'status': 'WATCH', 'source_count': 2,
                  'reference_price': 18500, 'affordability': {'entry_eligible': True, 'reason_code': 'ONE_SHARE_AFFORDABLE'}}
        }
        # Ingest 4 bars into detector
        for i in range(4):
            bar_time = self.clock + timedelta(minutes=i)
            b = make_bar(bar_time, 18400, 18500, 18350, 18450)
            self.d.history.setdefault(sym, []).append(b)
        self.assertEqual(len(self.d.usable_bars(sym)), 4)

        # 2. Simulate 11:39:26: radar reports ACCOUNT_CAPITAL_UNAVAILABLE
        rows = [
            {'code': sym, 'name': '라온시큐어', 'status': 'UNKNOWN', 'source_count': 2,
             'reference_price': 18500, 'affordability': {'entry_eligible': None, 'reason_code': 'ACCOUNT_CAPITAL_UNAVAILABLE'},
             'discovery_score': 30}
        ]
        # Resolve focus with ACCOUNT_CAPITAL_UNAVAILABLE
        sel = resolve_radar_focus(self.e, lookup, rows, rank_watch=[], readiness={},
                                  cfg=self.cfg, auto=self.auto, stamp=self.clock)

        # Assert WATCH is NOT emptied to 0, but preserved with observation_only=True
        self.assertIn(sym, lookup)
        self.assertEqual(lookup[sym]['status'], 'WATCH')
        self.assertTrue(lookup[sym].get('observation_only'))
        self.assertEqual(len(lookup), 1)

        # 3. Verify evaluate_actual_entry blocks purchase and emits NO_BUY / ACCOUNT_CAPITAL_UNAVAILABLE
        t = Tick(sym, self.clock, 18540, 10, 1000, 18540, 18530)
        self.b.orders.clear()
        res = evaluate_actual_entry(self.e, self.d, t, lookup[sym], Mock(), {}, None)
        self.assertIsNone(res)
        self.assertEqual(self.b.orders, [])
        # Check emitted decision in events.jsonl or decisions.last
        last_eval = self.e.decisions.last.get(('ENTRY_EVAL', sym))
        self.assertIsNotNone(last_eval)
        events_file = self.e.folder / 'events.jsonl'
        if events_file.exists():
            import json
            lines = [json.loads(line) for line in events_file.read_text(encoding='utf-8').strip().split('\n') if line]
            eval_events = [ev for ev in lines if ev.get('event') == 'ENTRY_EVAL' and ev.get('symbol') == sym]
            self.assertTrue(any(ev.get('decision') == 'NO_BUY' and ev.get('primary_reason_code') == 'ACCOUNT_CAPITAL_UNAVAILABLE'
                                for ev in eval_events))

        # 4. Verify completed bars in detector remain intact
        self.assertEqual(len(self.d.usable_bars(sym)), 4)

    def test_realtime_failure_disconnects_and_prevents_stale_tick_execution(self):
        """11:40:07 scenario: Realtime socket disconnection disconnects engine, resets detector, and blocks stale execution."""
        subscriptions = SubscriptionState(['042510'])
        self.e.connected = True
        self.e.account_ok = True

        # Ingest a bar
        self.d.history['042510'] = [make_bar(self.clock, 18400, 18500, 18350, 18450)]

        # 1. Dispatch DISCONNECTED message
        disconnect_msg = {'trnm': 'DISCONNECTED', 'reason': 'SOCKET_EOF', 'stage': 'READ'}
        process_message(self.e, self.d, disconnect_msg, self.clock, {'042510'}, {}, Mock(), {}, Mock(), True, subscriptions)

        self.assertFalse(self.e.connected)
        self.assertFalse(self.e.account_ok)
        self.assertEqual(self.d.history, {})  # Reset detector
        self.assertIn('042510', subscriptions.waiting)

        # 2. Simulate stale tick arriving after disconnection with old timestamp
        stale_tick_time = self.clock - timedelta(seconds=10)
        stale_tick_item = {
            'type': '0B', 'item': '042510',
            'values': {'10': 18540, '15': 10, '13': 1000, '27': 18540, '28': 18530, '20': stale_tick_time.strftime('%H%M%S')}
        }
        msg = {'trnm': 'REAL', 'data': [stale_tick_item]}
        prices = {}
        with patch('automation.runner.now', return_value=self.clock):
            n = process_message(self.e, self.d, msg, self.clock, {'042510'}, {}, Mock(), prices, Mock(), True, subscriptions)
        # Stale tick should not update current prices
        self.assertNotIn('042510', prices)

    def test_shadow_quote_burst_does_not_congest_main_queue(self):
        """SHADOW 0B and 0D quote traffic is diverted from feed.q, keeping order/execution queue clear."""
        feed = Feed(Mock(), ['042510', '999990'])
        feed.configure_observation(priority=['042510'], symbols=['999990'], sink=Mock())

        # Emit 50 SHADOW quote items (0B and 0D) for non-priority symbol 999990
        shadow_items = []
        for i in range(25):
            shadow_items.append({'type': '0B', 'item': '999990', 'values': {'10': 1000 + i, '15': 10, '13': 1000, '20': '100000'}})
            shadow_items.append({'type': '0D', 'item': '999990', 'values': {'27': 1001, '28': 999, '20': '100000'}})

        feed.emit({'trnm': 'REAL', 'data': shadow_items})
        # Priority queue feed.q must remain completely empty!
        self.assertTrue(feed.q.empty())

        # Now emit 1 execution/priority event for 042510
        priority_items = [{'type': '0B', 'item': '042510', 'values': {'10': 18540, '15': 4, '13': 5000, '20': '100001'}}]
        feed.emit({'trnm': 'REAL', 'data': priority_items})
        self.assertEqual(feed.q.qsize(), 1)
        received, evt = feed.q.get_nowait()
        self.assertEqual(evt['data'][0]['item'], '042510')

    def test_small_score_fluctuation_does_not_churn_shadow(self):
        """Minor score fluctuations during warmup do not displace incumbent SHADOW candidates (+2.0 hysteresis)."""
        observer = ShadowObserver(self.cfg, self.auto, self.e.costs, Mock(), limit=2)
        r1 = {'code': '000010', 'name': '종목1', 'source_count': 1, 'discovery_score': 10.0,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}
        r2 = {'code': '000020', 'name': '종목2', 'source_count': 1, 'discovery_score': 10.0,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}

        observer.update([r1, r2], self.clock)
        self.assertEqual(set(observer.rows), {'000010', '000020'})

        # At +30 seconds, candidate 3 arrives with score 11.5 (+1.5 points higher than 10.0)
        r3 = {'code': '000030', 'name': '종목3', 'source_count': 1, 'discovery_score': 11.5,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}
        observer.update([r1, r2, r3], self.clock + timedelta(seconds=30))

        # Because incumbents have +2.0 hysteresis during warmup (effective 12.0 > 11.5), r3 is rejected
        self.assertNotIn('000030', observer.rows)
        self.assertEqual(set(observer.rows), {'000010', '000020'})

    def test_superior_candidate_displaces_incumbent(self):
        """Significantly superior candidate (+5.0 points or ready tier) displaces incumbent in SHADOW."""
        observer = ShadowObserver(self.cfg, self.auto, self.e.costs, Mock(), limit=2)
        r1 = {'code': '000010', 'name': '종목1', 'source_count': 1, 'discovery_score': 10.0,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}
        r2 = {'code': '000020', 'name': '종목2', 'source_count': 1, 'discovery_score': 10.0,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}

        observer.update([r1, r2], self.clock)

        # Candidate 4 arrives with source_count=2, score=20.0 (+10 points higher, beating 10.0 + 2.0 = 12.0)
        r4 = {'code': '000040', 'name': '우수후보', 'source_count': 2, 'discovery_score': 20.0,
              'affordability': {'entry_eligible': True}, 'shadow_view': {'market_status': {'pass': True}}}
        observer.update([r1, r2, r4], self.clock + timedelta(seconds=30))

        # r4 displaces the weakest incumbent
        self.assertIn('000040', observer.rows)
        self.assertEqual(len(observer.rows), 2)

    def test_watch_retention_and_gap_expiry(self):
        """Short subscription gap retains observation, while gap exceeding stale_seconds resets and marks gaps."""
        sub = SubscriptionState(['042510'])
        feed = Mock(); feed.refresh_exchange.return_value = True; feed.update_symbols.return_value = True
        t0 = self.clock

        # Ingest tick at t0
        tick0 = Tick('042510', t0, 18540, 10, 1000, 18540, 18530)
        self.d.ingest(tick0, t0)
        self.assertIn('042510', self.d.last)

        # 1. Temporarily remove 042510 for 2 seconds (within stale_seconds = 5s)
        sub.refresh(self.e, self.d, feed, desired=[], stamp=t0 + timedelta(seconds=1))
        self.assertIn('042510', sub.detached)

        # Re-add at t0 + 2s: observation is retained
        sub.refresh(self.e, self.d, feed, desired=['042510'], stamp=t0 + timedelta(seconds=2))
        self.assertIn('042510', self.d.last)

        # 2. Remove 042510 for 10 seconds (> stale_seconds = 5s)
        sub.refresh(self.e, self.d, feed, desired=[], stamp=t0 + timedelta(seconds=3))
        # Re-add at t0 + 15s (gap = 12s > 5s): gap expired -> detector.forget called
        sub.refresh(self.e, self.d, feed, desired=['042510'], stamp=t0 + timedelta(seconds=15))
        self.assertNotIn('042510', self.d.last)


if __name__ == '__main__':
    unittest.main()
