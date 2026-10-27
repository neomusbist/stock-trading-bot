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
from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.runner import tick_freshness
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

    def test_counterexample_tick_drought_prevents_four_usable_bars_despite_240s_dwell(self):
        """[반례 1: 틱 누락/결측] 240초 동안 체류했으나 중간 1분 동안 틱이 전혀 수신되지
        않은 경우(Drought), usable_bars가 4개에 도달하지 못해 전략 진입이 올바르게 차단됨을 검증.
        (체류시간 240초 단독으로는 완성봉 확보가 불가능함을 입증하는 반례)
        """
        cfg = Config()
        auto = AutomationConfig()
        costs = {'buy': 0.00015, 'sell': 0.00015, 'tax': 0.002, 'entry_slippage': 0.002, 'exit_slippage': 0.003}
        det = PatternDetector(cfg, auto, costs)
        det.continuous_observation = True
        sym = '005930'
        base = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        det.observation_start[sym] = base

        # 09:00: 틱 수신
        det.ingest(Tick(sym, base + timedelta(seconds=1), 70000, 10, 10, 70010, 69990), base)
        det.ingest(Tick(sym, base + timedelta(seconds=59), 70000, 10, 20, 70010, 69990), base)
        # 09:01: 틱 수신
        det.ingest(Tick(sym, base + timedelta(minutes=1, seconds=1), 70050, 10, 30, 70060, 70040), base)
        det.ingest(Tick(sym, base + timedelta(minutes=1, seconds=59), 70050, 10, 40, 70060, 70040), base)
        # 09:02: 틱 결측 (거래 정지/호가 무체결로 1분간 0틱 발생)
        # 09:03: 틱 재개
        det.ingest(Tick(sym, base + timedelta(minutes=3, seconds=1), 70100, 10, 50, 70110, 70090), base)
        det.ingest(Tick(sym, base + timedelta(minutes=3, seconds=59), 70100, 10, 60, 70110, 70090), base)
        # 09:04:00 (체류시간 240초 도달)
        det.ingest(Tick(sym, base + timedelta(minutes=4, seconds=0), 70150, 10, 70, 70160, 70140), base)

        # 240초 체류했으나 결측으로 인해 usable_bars는 4개 미만 (0개 반환)
        usable = det.usable_bars(sym)
        self.assertLess(len(usable), 4, "Tick drought must prevent reaching 4 usable bars")
        self.assertEqual(len(usable), 0, "Missing minute breaks consecutive usable bars")

    def test_counterexample_subscription_gap_resets_usable_bars_despite_240s_dwell(self):
        """[반례 2: 구독 공백] 240초 동안 체류 중 stale_seconds(5초)를 초과하는 구독 공백이
        발생한 경우, pending gap으로 인해 usable_bars가 즉시 무효화됨을 검증.
        """
        cfg = Config()
        auto = AutomationConfig()
        costs = {'buy': 0.00015, 'sell': 0.00015, 'tax': 0.002, 'entry_slippage': 0.002, 'exit_slippage': 0.003}
        det = PatternDetector(cfg, auto, costs)
        det.continuous_observation = True
        sym = '005930'
        base = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        det.observation_start[sym] = base

        for m in range(4):
            det.ingest(Tick(sym, base + timedelta(minutes=m, seconds=1), 70000, 10, 10, 70010, 69990), base)
            det.ingest(Tick(sym, base + timedelta(minutes=m, seconds=59), 70000, 10, 20, 70010, 69990), base)

        # 09:04 시점에 6초 이상의 구독 단절 발생
        det.mark_coverage_gap(sym, base + timedelta(minutes=4), reason='SUBSCRIPTION_GAP')

        # pending gap으로 인해 usable_bars는 즉각 [] 반환
        self.assertEqual(det.usable_bars(sym), [], "Coverage gap must immediately invalidate usable bars")

    def test_counterexample_boundary_alignment_requires_additional_dwell_time(self):
        """[반례 3: 경계 시각 불일치] 09:00:35에 중도 편입된 경우, 정확히 240초가 지난
        09:04:35 시점에는 완성봉이 3개(09:01, 09:02, 09:03)뿐이며 4개가 되지 않음을 검증.
        (정각 1분 단위 경계로 인해 최대 300초(5분) 체류가 필요함을 증명)
        """
        cfg = Config()
        auto = AutomationConfig()
        costs = {'buy': 0.00015, 'sell': 0.00015, 'tax': 0.002, 'entry_slippage': 0.002, 'exit_slippage': 0.003}
        det = PatternDetector(cfg, auto, costs)
        det.continuous_observation = True
        sym = '005930'
        base = datetime(2026, 10, 26, 9, 0, 0, tzinfo=KST)
        start_mid = base + timedelta(seconds=35)
        det.observation_start[sym] = start_mid

        # 09:00:35 ~ 09:04:35 (정확히 240초 체류)
        # 09:00 불완전 분봉
        det.ingest(Tick(sym, base + timedelta(seconds=36), 70000, 10, 10, 70010, 69990), start_mid)
        det.ingest(Tick(sym, base + timedelta(seconds=58), 70000, 10, 20, 70010, 69990), start_mid)
        # 09:01, 09:02, 09:03 분봉
        for m in (1, 2, 3):
            det.ingest(Tick(sym, base + timedelta(minutes=m, seconds=1), 70000 + m * 10, 10, 10, 70010, 69990), start_mid)
            det.ingest(Tick(sym, base + timedelta(minutes=m, seconds=58), 70000 + m * 10, 10, 20, 70010, 69990), start_mid)
        # 09:04:35 시점의 틱 (체류 240초 경과)
        det.ingest(Tick(sym, base + timedelta(minutes=4, seconds=1), 70040, 10, 90, 70050, 70030), start_mid)
        det.ingest(Tick(sym, base + timedelta(minutes=4, seconds=35), 70040, 10, 100, 70050, 70030), start_mid)

        # 240초가 지났으나 첫 35초가 불완전 봉이었으므로 usable_bars는 4개가 될 수 없음
        usable = det.usable_bars(sym)
        self.assertLess(len(usable), 4, "Mid-minute start must not guarantee 4 usable bars at 240s")

    def test_counterexample_delayed_stale_ticks_rejected_by_execution_freshness(self):
        """[반례 4: 데이터 지연] 4개 완성봉이 있더라도, 돌파 시점의 체결 틱이
        네트워크/거래소 지연(stale_seconds=5s 초과)으로 지연 도착하면 매수가 차단됨을 검증.
        """
        cfg = Config()
        sym = '005930'
        base = datetime(2026, 10, 26, 9, 4, 10, tzinfo=KST)
        # 8초 전 체결된 지연 틱
        stale_tick = Tick(sym, base - timedelta(seconds=8), 70050, 10, 100, 70060, 70040)
        freshness = tick_freshness(stale_tick, base, cfg.stale_seconds)

        self.assertFalse(freshness['fresh'], "Tick older than stale_seconds must be marked not fresh")
        self.assertGreater(freshness['effective_age_seconds'], cfg.stale_seconds)

    def test_rejoined_watch_stock_preserves_dwell_tenure_protection(self):
        """[09:43:03 플래핑 수정 검증 1] 이른 아침(09:16:12)에 최초 편입된 이력이 있더라도,
        09:43:03에 재편입(rejoin)된 경우 watch_entered_at 기준 체류시간이 0.5초이므로
        min_dwell_seconds(240s) 보호를 정상 적용받아 고점수 도전자에게 즉시 밀려나지 않음을 검증.
        (과거 first_watch_at 잔존으로 인한 300ms 초고속 탈락 버그 해결 입증)
        """
        base = datetime(2026, 10, 27, 9, 43, 3, tzinfo=KST)
        current_watch = {
            '078350': {
                'code': '078350',
                'discovery_score': 80.0,
                'source_count': 2,
                'first_watch_at': datetime(2026, 10, 27, 9, 16, 12, tzinfo=KST).isoformat(),
                'watch_entered_at': base.isoformat(),
            }
        }
        candidates = [
            {'code': '078350', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '098460', 'discovery_score': 88.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        readiness_map = {
            '078350': {'tier': TIER_RANK_ONLY, 'timestamp': base.isoformat()},
            '098460': {'tier': TIER_RANK_ONLY, 'timestamp': base.isoformat()},
        }
        # 0.5초 경과 시점
        eval_clock = (base + timedelta(milliseconds=500)).isoformat()
        selection = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=eval_clock)
        self.assertEqual(selection.next_watch, ['078350'], "Rejoined incumbent must be tenure-protected against rank challenger")

    def test_warming_incumbent_with_fresh_weak_tier_retains_dwell_tenure(self):
        """[09:43:03 플래핑 수정 검증 2] 체류 초기 틱/거래대금 부족으로 일시적 TIER_FRESH_WEAK(Tier 4)가
        되더라도, 구조적 가격 훼손(p_blocked)이 없는 한 dwell tenure 보호가 유지되어
        미관측 도전자(Tier 3)에 의해 300ms 만에 축출되지 않음을 검증.
        """
        base = datetime(2026, 10, 27, 9, 43, 3, tzinfo=KST)
        current_watch = {
            '078350': {
                'code': '078350',
                'discovery_score': 80.0,
                'source_count': 2,
                'watch_entered_at': base.isoformat(),
            }
        }
        candidates = [
            {'code': '078350', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '098460', 'discovery_score': 95.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        # 현 주식이 웜업/거래대금 미달로 TIER_FRESH_WEAK인 상태 (가격 구조 훼손은 없음)
        readiness_map = {
            '078350': {'tier': TIER_FRESH_WEAK, 'timestamp': base.isoformat(), 'gates': {'liquidity': {'result': 'FAIL'}}},
            '098460': {'tier': TIER_RANK_ONLY, 'timestamp': base.isoformat()},
        }
        eval_clock = (base + timedelta(seconds=10)).isoformat()
        selection = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=eval_clock)
        self.assertEqual(selection.next_watch, ['078350'], "Warming incumbent with FRESH_WEAK must retain tenure against unobserved challenger")

    def test_structurally_broken_incumbent_evicted_despite_dwell(self):
        """[09:43:03 플래핑 수정 검증 3] 최소 체류기간(240s) 이내라도, 박스 지지선 이탈 또는
        손절선 무효화 등 구조적 가격 훼손(p_blocked)이 발생하면 tenure 보호가 즉시 박탈되어
        적격 종목으로 교체됨을 검증. (불필요한 무효 종목 체류 방지)
        """
        base = datetime(2026, 10, 27, 9, 43, 3, tzinfo=KST)
        current_watch = {
            '078350': {
                'code': '078350',
                'discovery_score': 80.0,
                'source_count': 2,
                'watch_entered_at': base.isoformat(),
            }
        }
        candidates = [
            {'code': '078350', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '098460', 'discovery_score': 75.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        # 가격 구조 무효화 발생 (지지선 붕괴 등)
        readiness_map = {
            '078350': {
                'tier': TIER_FRESH_WEAK,
                'timestamp': base.isoformat(),
                'setup_trials': [{'setup': 'A', 'candidate_generated': True, 'gates': {'invalidation': {'result': 'FAIL'}}}]
            },
            '098460': {'tier': TIER_RANK_ONLY, 'timestamp': base.isoformat()},
        }
        eval_clock = (base + timedelta(seconds=10)).isoformat()
        selection = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=eval_clock)
        self.assertEqual(selection.next_watch, ['098460'], "Structurally broken incumbent must be evicted despite tenure")

    def test_signal_ready_candidate_displaces_incumbent_at_capacity(self):
        """[09:43:03 플래핑 수정 검증 4] 기존 WATCH 종목이 tenure 보호 중이더라도,
        진짜 주문 직전 신호(TIER_SIGNAL_READY, Tier 0)를 확보한 종목이 나타나면
        용량 한도(max_watchlist) 내에서 기존 종목을 정당하게 교체함을 검증.
        (Section ⑥-3 규칙의 일관성 확인)
        """
        base = datetime(2026, 10, 27, 9, 43, 3, tzinfo=KST)
        current_watch = {
            '078350': {
                'code': '078350',
                'discovery_score': 80.0,
                'source_count': 2,
                'watch_entered_at': base.isoformat(),
            }
        }
        candidates = [
            {'code': '078350', 'discovery_score': 80.0, 'source_count': 2, 'status': 'WATCH'},
            {'code': '098460', 'discovery_score': 70.0, 'source_count': 2, 'status': 'ELIGIBLE'},
        ]
        # 098460은 모든 게이트 통과된 완벽한 SIGNAL_READY (Tier 0)
        readiness_map = {
            '078350': {'tier': TIER_RANK_ONLY, 'timestamp': base.isoformat()},
            '098460': {'tier': TIER_SIGNAL_READY, 'timestamp': base.isoformat()},
        }
        eval_clock = (base + timedelta(seconds=10)).isoformat()
        selection = select_focus(current_watch, candidates, readiness_map, max_watchlist=1, clock_iso=eval_clock)
        self.assertEqual(selection.next_watch, ['098460'], "Tier 0 SIGNAL_READY must displace lower-tier incumbent")


if __name__ == '__main__':
    unittest.main()
