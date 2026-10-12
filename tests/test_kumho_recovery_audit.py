import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from unittest.mock import Mock, patch
from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import AutomationConfig, Policy
from automation.runner import SubscriptionState, evaluate_actual_entry
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from core.realtime import Feed

STAMP = datetime(2026, 10, 12, 9, 56, 5, tzinfo=KST)
KUMHO_SYM = "002990"

class KumhoRecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = replace(Config(), live_mode=True, max_watchlist=12, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True, scan_seconds=40)
        from core.util import atomic_json
        atomic_json(self.root / 'policy', {'mode': 'LIVE', 'live_authorized': True, 'releases': {}, 'revision': 0, 'commands': []})
        self.policy = Policy(self.root / 'policy')
        self.rest = Mock()
        self.rest.identity = 'TEST-ACCOUNT'
        self.rest.cfg = self.cfg
        self.rest.profile = {}
        self.rest.stock_status.return_value = {
            'pass': True,
            'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG',
            'raw': {'code': KUMHO_SYM, 'name': '금호건설', 'state': '신규100%'},
            'checked_at': STAMP.isoformat()
        }
        self.detector = PatternDetector(self.cfg, self.auto, None)
        self.detector.continuous_observation = True

    def test_kumho_0956_priority_status_refreshed_enters_final_order_validation(self):
        """금호건설 09:56 사례: 상태 조회가 적시에 갱신되면 MARKET_STATUS_STALE 없이 주문 생성 진입"""
        engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.root / 'runtime', self.rest, lambda: STAMP)
        engine.available_cash = 80181.0
        engine.s['cash'] = 80181.0
        engine.account_ok = True
        engine.connected = True
        engine.last_account = 1000.0
        engine.enter = Mock(return_value={'local': '2026-10-12-1', 'status': 'ACCEPTED'})

        # Old status (239s old, checked at 09:52:06)
        stale_status = {
            'pass': True,
            'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG',
            'raw': {'code': KUMHO_SYM, 'name': '금호건설', 'state': '신규100%'},
            'checked_at': (STAMP - timedelta(seconds=239)).isoformat()
        }
        watch = {'code': KUMHO_SYM, 'name': '금호건설', 'market_status': stale_status, 'source_count': 3, 'fallback_source1': False}
        t = Tick(KUMHO_SYM, STAMP, 13990.0, 100, 1000, 13990.0, 13980.0, True, 1000, 1000)

        # Mock signal to return valid Setup B signal
        mock_sig = {
            'pattern': 'B', 'symbol': KUMHO_SYM, 'limit': 13990, 'stop': 13850, 'target': 14450,
            'efficiency': {'score': 0.67, 'inputs': {'confidence': 0.7, 'planned_minutes': 30}}
        }
        mock_detector = Mock()
        mock_detector.signal.return_value = mock_sig

        # Radar status_cache has fresh status or rest client provides it
        radar = Mock()
        radar.status_cache = {
            KUMHO_SYM: {
                'pass': True,
                'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG',
                'raw': {'code': KUMHO_SYM, 'name': '금호건설', 'state': '신규100%'},
                'checked_at': STAMP.isoformat()
            }
        }
        radar.cache = {(STAMP.date(), KUMHO_SYM): [{'datetime': (STAMP.date() - timedelta(days=1)).isoformat(), 'open': 13000, 'high': 14000, 'low': 12900, 'close': 13500, 'volume': 500000}]}

        with patch('automation.runner.structure', return_value={'status': 'ELIGIBLE', 'reason': '통과', 'resistance': 14450}):
            o = evaluate_actual_entry(engine, mock_detector, t, watch, radar, {KUMHO_SYM: 13990.0}, Mock())

        self.assertIsNotNone(o)
        self.assertEqual(engine.enter.call_count, 1)
        # engine.market_status must now be fresh
        self.assertEqual(engine.market_status[KUMHO_SYM]['checked_at'], STAMP.isoformat())

    def test_kumho_0956_status_failure_or_rate_limit_safely_blocks_live_order(self):
        """상태조회 실패나 API 제한 초과 시 실주문이 안전하게 차단됨"""
        engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.root / 'runtime', self.rest, lambda: STAMP)
        engine.available_cash = 80181.0
        engine.s['cash'] = 80181.0
        engine.account_ok = True
        engine.connected = True

        # Old status (239s old)
        stale_status = {
            'pass': True,
            'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG',
            'raw': {'code': KUMHO_SYM, 'name': '금호건설', 'state': '신규100%'},
            'checked_at': (STAMP - timedelta(seconds=239)).isoformat()
        }
        watch = {'code': KUMHO_SYM, 'name': '금호건설', 'market_status': stale_status, 'source_count': 3, 'fallback_source1': False}
        t = Tick(KUMHO_SYM, STAMP, 13990.0, 100, 1000, 13990.0, 13980.0, True, 1000, 1000)

        mock_sig = {
            'pattern': 'B', 'symbol': KUMHO_SYM, 'limit': 13990, 'stop': 13850, 'target': 14450,
            'efficiency': {'score': 0.67, 'inputs': {'confidence': 0.7, 'planned_minutes': 30}}
        }
        mock_detector = Mock()
        mock_detector.signal.return_value = mock_sig

        # Radar cache has NO fresh status, and rest client stock_status raises exception / quota limit
        radar = Mock()
        radar.status_cache = {}
        radar.cache = {(STAMP.date(), KUMHO_SYM): [{'datetime': (STAMP.date() - timedelta(days=1)).isoformat(), 'open': 13000, 'high': 14000, 'low': 12900, 'close': 13500, 'volume': 500000}]}
        self.rest.stock_status.side_effect = RuntimeError("STATUS_SCAN_LIMIT_EXCEEDED")

        with patch('automation.runner.structure', return_value={'status': 'ELIGIBLE', 'reason': '통과', 'resistance': 14450}):
            o = evaluate_actual_entry(engine, mock_detector, t, watch, radar, {KUMHO_SYM: 13990.0}, Mock())

        # Order must be blocked because status remains STALE
        self.assertIsNone(o)
        self.assertEqual(self.rest.submit.call_count, 0)
        self.assertEqual(engine.new_entry_reason(KUMHO_SYM), 'MARKET_STATUS_STALE')

    def test_websocket_send_failure_in_update_symbols_does_not_crash_worker(self):
        """WebSocket 연결 끊김 중 update_symbols() 호출 시 예외를 던지지 않고 안전하게 에러 이벤트 방출"""
        feed = Feed(self.rest, [KUMHO_SYM])
        feed.logged_in = True
        mock_socket = Mock()
        mock_socket.send.side_effect = Exception("WebSocketConnectionClosedException: socket is already closed")
        feed.socket = mock_socket

        # Should not raise exception
        success = feed.update_symbols([KUMHO_SYM, '091590'])
        self.assertTrue(success)
        self.assertFalse(feed.logged_in)
        # Should have emitted ERROR event
        self.assertFalse(feed.q.empty())
        item = feed.q.get_nowait()
        self.assertEqual(item[1].get('trnm'), 'ERROR')

    def test_watch_demotion_grace_period_preserves_ticks_and_real_gap_resets(self):
        """WATCH 교체 시 60초 유예기간 동안 구독 유지되어 연속 봉 보존, 실제 누락(gap_expired) 시 리셋 구분"""
        sub = SubscriptionState([KUMHO_SYM])
        # Case 1: Within gap limit, retained
        sub.detached.add(KUMHO_SYM)
        sub.detached_at[KUMHO_SYM] = STAMP - timedelta(seconds=2)
        # Not expired yet (2s <= 5s)
        self.assertFalse(sub.gap_expired(self.detector, KUMHO_SYM, STAMP))

        # Case 2: Past stale_seconds (10s > 5s), gap_expired is True
        sub.detached_at[KUMHO_SYM] = STAMP - timedelta(seconds=10)
        self.assertTrue(sub.gap_expired(self.detector, KUMHO_SYM, STAMP))

    def test_radar_evaluate_proactively_refreshes_watch_candidates(self):
        """레이더 평가 시 WATCH 상위 후보는 120초가 아니라 60초 경과 시 사전 갱신되어 STALE 방지"""
        from automation.radar import RadarWorker
        worker = RadarWorker(self.cfg, self.auto, self.policy, self.rest, self.root, fallback_source1=True)
        worker.status_priority_symbols = frozenset([KUMHO_SYM])
        worker.status_cache[KUMHO_SYM] = {
            'pass': True,
            'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG',
            'checked_at': (STAMP - timedelta(seconds=70)).isoformat()
        }
        worker.cache[(STAMP.date(), KUMHO_SYM)] = [
            {'datetime': (STAMP.date() - timedelta(days=i)).isoformat(), 'open': 13000, 'high': 14000, 'low': 12900, 'close': 13500, 'volume': 500000}
            for i in range(30, 0, -1)
        ]

        with patch('automation.radar.now', return_value=STAMP):
            res = worker.evaluate({'code': KUMHO_SYM, 'reference_price': 13990})

        # At 70s (> 60s for priority symbol), stock_status MUST be refreshed
        self.assertEqual(self.rest.stock_status.call_count, 1)
        self.assertEqual(worker.status_cache[KUMHO_SYM]['checked_at'], STAMP.isoformat())

if __name__ == '__main__':
    unittest.main()
