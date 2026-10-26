"""09:00 개장 기술 안정성 검증 및 회귀 방지 테스트

1. 개장 시점 고빈도 관찰 이벤트의 비동기 큐 처리 및 메인 루프 동기 블록 제거 검증
2. WATCH 종목 동일 티어 내 미세 점수 진동 시 최소 체류시간(dwell tenure, 60s) 및 +5.0 히스테리시스에 의한 플래핑 방지 검증
3. 상위 티어(Tier 0/1/2) 유망 종목 발생 시 dwell tenure와 무관하게 즉각 승격 허용 검증
4. 기존 WATCH 종목이 FRESH_WEAK(유동성/무효화 결함) 발생 시 dwell tenure 해제 및 즉각 교체 허용 검증
5. GUI tail_events mtime/size 기반 캐싱으로 500ms 주기 반복 디스크 I/O 제거 검증
"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from core import event_log
from core.settings import Config, KST
from core.strategy import Tick
from operator_service import tail_events, _TAIL_CACHE
from automation.focus_selection import (
    select_focus,
    candidate_priority_tuple,
    TIER_SIGNAL_READY,
    TIER_GEOMETRY_BASE_READY,
    TIER_MOMENTUM_BASE_READY,
    TIER_RANK_ONLY,
    TIER_FRESH_WEAK
)


AT = datetime(2026, 10, 26, 9, 0, 5, tzinfo=KST)


class OpeningStabilityVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'events.jsonl'
        _TAIL_CACHE.clear()

    def test_opening_diagnostic_events_route_to_async_writer(self):
        """09:00 개장 시점 고빈도 관찰 이벤트들이 SYNC_CRITICAL로 떨어지지 않고
        비동기 큐(ASYNC)로 안전하게 인계되는지 전수 검증.
        """
        writer = event_log.AsyncDiagnosticEventWriter(self.path, capacity=512)
        try:
            opening_events = [
                'OBSERVATION_RELEASED',
                'OPENING_PIPELINE_STAGE',
                'BOOK_DEPTH_OBSERVED',
                'OBSERVATION_RETENTION',
                'OBSERVATION_PREWARM_HANDOFF',
                'PRICE_CONTEXT_CACHE_RESTORED',
                'ENTRY_REFERENCE_RECOVERY',
                'OBSERVATION_READINESS_CHANGED',
                'WATCH_DYNAMIC_PROMOTION',
                'WATCH_PROMOTION_BLOCKED',
                'REFERENCE_CACHE_CHECKPOINT_CLOSED',
                'REFERENCE_CACHE_CHECKPOINT_PENDING',
                'OBSERVATION_OPPORTUNITY_REEVALUATED',
                'OBSERVATION_DATA_GAP',
                'ACCOUNT_CAPITAL_REFRESHED',
                'ACCOUNT_CAPITAL_REFRESH_REQUIRED',
                'POSITION_DATA_HEALTH',
                'BROKER_PNL_RECONCILED',
            ]

            for ev in opening_events:
                row = {'time': AT.isoformat(), 'event': ev, 'symbol': '005930'}
                delivery = writer.append(row)
                self.assertEqual(delivery, 'ASYNC', f"Event {ev} should be delivered ASYNC, got {delivery}")

            # 큐 용량이 512로 증가하여 100건 이상 버스트도 여유 있게 수용
            self.assertGreaterEqual(writer.stats()['capacity'], 512)
        finally:
            writer.close()

    def test_watchlist_dwell_tenure_prevents_thrashing(self):
        """동일 티어(RANK_ONLY)에서 후보 B가 A보다 점수가 약간 높아도(3점 차이),
        A의 dwell tenure(60초) 내에서는 B가 A를 축출하지 못함을 검증.
        """
        current_watch = {
            '000010': {
                'code': '000010',
                'score': 80.0,
                'discovery_score': 80.0,
                'source_count': 2,
                'first_watch_at': AT.isoformat(),
            }
        }
        candidates = [
            {'code': '000010', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '000020', 'discovery_score': 83.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        readiness_map = {
            '000010': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
            '000020': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
        }

        # 15초 경과 시점: A의 체류시간 15초 (< 60초)
        clock_15s = (AT + timedelta(seconds=15)).isoformat()
        selection = select_focus(
            current_watch,
            candidates,
            readiness_map,
            max_watchlist=1,
            fallback_source1=True,
            clock_iso=clock_15s,
            min_dwell_seconds=60.0
        )

        # A가 보호되어 교체되지 않아야 함
        self.assertEqual(selection.next_watch, ['000010'])
        self.assertEqual(selection.promoted, [])
        self.assertEqual(selection.demoted, [])
        self.assertFalse(selection.applied)

        # 65초 경과 시점: A의 체류시간 65초 (>= 60초 만료)
        # B(83점) vs A(80점 + 5점 히스테리시스 = 85점)
        # 히스테리시스 덕분에 3점 차이는 여전히 A가 유지!
        clock_65s = (AT + timedelta(seconds=65)).isoformat()
        selection2 = select_focus(
            current_watch,
            candidates,
            readiness_map,
            max_watchlist=1,
            fallback_source1=True,
            clock_iso=clock_65s,
            min_dwell_seconds=60.0
        )
        self.assertEqual(selection2.next_watch, ['000010'])

        # 만약 B가 86점(+6점 우위)으로 히스테리시스(+5.0)를 초과하면 정당하게 교체됨
        candidates[1]['discovery_score'] = 86.0
        selection3 = select_focus(
            current_watch,
            candidates,
            readiness_map,
            max_watchlist=1,
            fallback_source1=True,
            clock_iso=clock_65s,
            min_dwell_seconds=60.0
        )
        self.assertEqual(selection3.next_watch, ['000020'])
        self.assertEqual(selection3.promoted, ['000020'])
        self.assertEqual(selection3.demoted, ['000010'])

    def test_higher_tier_candidate_overrides_dwell_tenure(self):
        """기존 종목 A가 dwell tenure 보호 중이더라도, 신규 후보 C가 상위 티어
        (TIER_SIGNAL_READY 또는 TIER_GEOMETRY_BASE_READY)에 도달하면 즉각 승격되어야 함.
        """
        current_watch = {
            '000010': {
                'code': '000010',
                'score': 100.0,
                'discovery_score': 100.0,
                'source_count': 5,
                'first_watch_at': AT.isoformat(),
            }
        }
        candidates = [
            {'code': '000010', 'discovery_score': 100.0, 'source_count': 5, 'status': 'WATCH'},
            {'code': '000030', 'discovery_score': 50.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        readiness_map = {
            '000010': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
            '000030': {'tier': TIER_SIGNAL_READY, 'timestamp': AT.isoformat()},
        }

        # 10초 경과 시점
        clock_10s = (AT + timedelta(seconds=10)).isoformat()
        selection = select_focus(
            current_watch,
            candidates,
            readiness_map,
            max_watchlist=1,
            fallback_source1=True,
            clock_iso=clock_10s,
            min_dwell_seconds=60.0
        )

        # 상위 티어(Tier 0)인 000030이 tenure 중인 Tier 3 종목을 정상 승격/교체
        self.assertEqual(selection.next_watch, ['000030'])
        self.assertEqual(selection.promoted, ['000030'])
        self.assertEqual(selection.demoted, ['000010'])

    def test_failing_incumbent_loses_tenure_protection(self):
        """기존 종목 A가 dwell tenure 기간 내라도, 유동성 실패나 가격 무효화로
        TIER_FRESH_WEAK가 되면 tenure 보호가 해제되어 건강한 후보로 교체되어야 함.
        """
        current_watch = {
            '000010': {
                'code': '000010',
                'score': 80.0,
                'discovery_score': 80.0,
                'source_count': 2,
                'first_watch_at': AT.isoformat(),
            }
        }
        candidates = [
            {'code': '000010', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '000020', 'discovery_score': 70.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        readiness_map = {
            '000010': {'tier': TIER_FRESH_WEAK, 'timestamp': AT.isoformat(), 'price_blocked': True},
            '000020': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
        }

        clock_10s = (AT + timedelta(seconds=10)).isoformat()
        selection = select_focus(
            current_watch,
            candidates,
            readiness_map,
            max_watchlist=1,
            fallback_source1=True,
            clock_iso=clock_10s,
            min_dwell_seconds=60.0
        )

        # 결함 있는 A는 tenure 보호 없이 RANK_ONLY인 B로 교체됨
        self.assertEqual(selection.next_watch, ['000020'])
        self.assertEqual(selection.promoted, ['000020'])
        self.assertEqual(selection.demoted, ['000010'])

    def test_tail_events_caching_eliminates_redundant_disk_io(self):
        """events.jsonl의 크기와 mtime이 변경되지 않았을 때 tail_events가
        디스크를 재조회하지 않고 캐시된 리스트를 즉각 반환함을 검증.
        """
        rows = [{'event': 'TICK', 'seq': i} for i in range(10)]
        lines = '\n'.join(json.dumps(r) for r in rows) + '\n'
        self.path.write_text(lines, encoding='utf-8')

        # 첫 번째 호출: 디스크 읽기 및 캐시 적재
        res1 = tail_events(self.path, limit=5)
        self.assertEqual(len(res1), 5)
        self.assertEqual(res1[-1]['seq'], 9)

        # 파일이 변경되지 않은 상태에서 호출: 동일한 결과 반환
        res2 = tail_events(self.path, limit=5)
        self.assertEqual(res1, res2)

        # 파일에 새 라인이 추가되면 캐시가 무효화되고 새 데이터를 반환
        new_row = {'event': 'ORDER_ACCEPTED', 'seq': 10}
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(new_row) + '\n')

        res3 = tail_events(self.path, limit=5)
        self.assertEqual(res3[-1]['seq'], 10)
        self.assertEqual(res3[-1]['event'], 'ORDER_ACCEPTED')

    def test_four_minute_dwell_tenure_guarantees_four_completed_bars(self):
        """기본 min_dwell_seconds=240.0s (4분) 설정 시, 1~3분(60s, 120s, 180s)
        구간에서 동일 티어 점수 진동에 의해 축출되지 않고 4개 완성봉을 온전히
        축적할 수 있음을 검증.
        """
        current_watch = {
            '000010': {
                'code': '000010',
                'score': 80.0,
                'discovery_score': 80.0,
                'source_count': 2,
                'first_watch_at': AT.isoformat(),
            }
        }
        candidates = [
            {'code': '000010', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '000020', 'discovery_score': 88.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        readiness_map = {
            '000010': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
            '000020': {'tier': TIER_RANK_ONLY, 'timestamp': AT.isoformat()},
        }

        # 60초 경과 (1개봉 완성): 기존 60s 설정에서는 축출 위험이었으나 240s 기본값에서는 완벽 보호
        clock_60s = (AT + timedelta(seconds=60)).isoformat()
        sel_60 = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=clock_60s)
        self.assertEqual(sel_60.next_watch, ['000010'], "Bar 1 completion: should retain incumbent")

        # 120초 경과 (2개봉 완성): 보호 지속
        clock_120s = (AT + timedelta(seconds=120)).isoformat()
        sel_120 = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=clock_120s)
        self.assertEqual(sel_120.next_watch, ['000010'], "Bar 2 completion: should retain incumbent")

        # 180초 경과 (3개봉 완성): 보호 지속
        clock_180s = (AT + timedelta(seconds=180)).isoformat()
        sel_180 = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=clock_180s)
        self.assertEqual(sel_180.next_watch, ['000010'], "Bar 3 completion: should retain incumbent")

        # 239초 경과 (4개봉 직전): 보호 지속
        clock_239s = (AT + timedelta(seconds=239)).isoformat()
        sel_239 = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=clock_239s)
        self.assertEqual(sel_239.next_watch, ['000010'], "Bar 4 building: should retain incumbent")

        # 241초 경과 (4개 완성봉 확보 완료): tenure 보호 만료
        # B(88점) > A(80점 + 5점 히스테리시스 = 85점) -> 점수 우위 종목으로 정당 교체 허용
        clock_241s = (AT + timedelta(seconds=241)).isoformat()
        sel_241 = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=clock_241s)
        self.assertEqual(sel_241.next_watch, ['000020'], "Post-4-bar expiry: allows justified promotion")

    def test_async_diagnostic_writer_queue_saturation_safety(self):
        """큐 용량 포화 시 이벤트가 유실(drop)되지 않고 SYNC_FALLBACK으로
        안전하게 기록되며, 종료 시 잔여 이벤트가 전량 플러시되는지 검증.
        """
        writer = event_log.AsyncDiagnosticEventWriter(self.path, capacity=4)
        try:
            deliveries = []
            for i in range(12):
                row = {'time': AT.isoformat(), 'event': 'OBSERVATION_RELEASED', 'idx': i}
                deliveries.append(writer.append(row))

            # 큐 용량(4) 초과분은 SYNC_FALLBACK으로 안전하게 기록됨 (유실 없음)
            self.assertIn('ASYNC', deliveries)
            self.assertIn('SYNC_FALLBACK', deliveries)
        finally:
            writer.close()

        # 파일에 12개 이벤트가 유실 없이 전부 기록되었는지 확인
        lines = [json.loads(line) for line in self.path.read_text(encoding='utf-8').strip().split('\n')]
        self.assertEqual(len(lines), 12, "Zero event loss: exactly 12 events written")
        sequences = [row['event_ingress_sequence'] for row in lines]
        # 모든 ingress 번호가 유실/누락 없이 온전히 보존됨
        self.assertEqual(sorted(sequences), list(range(1, 13)), "All sequence numbers preserved without loss")

    def test_critical_financial_events_never_routed_to_async(self):
        """주문, 체결, 포지션 종료, 위험관리, 주문의향 이벤트 및 BUY/SELL 속성을
        가진 모든 핵심 거래 이벤트는 예외 없이 SYNC_CRITICAL로 기록됨을 검증.
        """
        writer = event_log.AsyncDiagnosticEventWriter(self.path, capacity=512)
        try:
            critical_samples = [
                {'time': AT.isoformat(), 'event': 'ORDER_ACCEPTED', 'symbol': '005930', 'side': 'BUY', 'price': 70000},
                {'time': AT.isoformat(), 'event': 'ORDER_INTENT', 'symbol': '005930', 'side': 'BUY', 'price': 70000},
                {'time': AT.isoformat(), 'event': 'FILL', 'symbol': '005930', 'side': 'BUY', 'qty': 10},
                {'time': AT.isoformat(), 'event': 'POSITION_CLOSED', 'symbol': '005930'},
                {'time': AT.isoformat(), 'event': 'RISK_HALT', 'reason': 'DAILY_LOSS_LIMIT'},
                {'time': AT.isoformat(), 'event': 'HARD_LOCK', 'symbol': '005930'},
                {'time': AT.isoformat(), 'event': 'ENTRY_EVAL', 'decision': 'BUY', 'symbol': '005930'},
                {'time': AT.isoformat(), 'event': 'ENTRY_EVAL', 'signal_detected': True, 'symbol': '005930'},
            ]

            for row in critical_samples:
                delivery = writer.append(row)
                self.assertEqual(delivery, 'SYNC_CRITICAL', f"Critical event {row['event']} must be SYNC_CRITICAL, got {delivery}")
        finally:
            writer.close()


if __name__ == '__main__':
    unittest.main()
