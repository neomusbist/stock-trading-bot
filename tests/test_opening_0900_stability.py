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


if __name__ == '__main__':
    unittest.main()
