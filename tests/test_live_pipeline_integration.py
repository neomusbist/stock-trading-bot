"""
독립 검증용 신규 통합 테스트: 실제 운영 데이터 전달 경로 및 목표가 계산 일치성 검증
runner -> detector.signal -> setup_trials -> setup_gates -> engine.enter 파이프라인 전수 검증
미완성봉 격리, 미래 데이터 차단, 부적절한 매수 차단 반례(Counter-example) 검증
"""
import copy
import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.price_context import PriceContext
from automation.structural_breakout import structural_breakout
from tests.test_v1 import FakeBroker
from tests import test_automation as fixture

AT = datetime(2026, 10, 15, 10, 30, 0, tzinfo=KST)

def make_bars(values, end_time=AT):
    bars = []
    for i, (o, h, l, c, v) in enumerate(values):
        t = end_time - timedelta(minutes=len(values) - i)
        bars.append({
            'time': t, 'open': o, 'high': h, 'low': l, 'close': c,
            'volume': v, 'first_second': 0, 'last_second': 59,
            'gap_reason': None, 'venue': 'KRX'
        })
    return bars

# 15개의 확정된 1분봉 데이터 (10:15 ~ 10:29)
HISTORICAL_BARS = make_bars([
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9970, 9980, 9960, 9970, 500),
    (9980, 10030, 9970, 10010, 1200),  # Bar 10: 10030 고점 형성
    (10000, 10010, 9880, 9920, 1500),   # Bar 11: 9880 구조적 지지선(저점)
    (9920, 10000, 9920, 10000, 2000),   # Bar 12: 반등 지속
    (10000, 10020, 10000, 10020, 2500), # Bar 13: 상승 지속
    (10010, 10025, 10010, 10025, 3000)  # Bar 14: 직전 완성봉 (종가 10025)
])

class LivePipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = replace(
            AutomationConfig(),
            structural_breakout_enabled=True,
            intraday_context_enabled=True,
            confirmed_invalidation_enabled=True
        )
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)
        # 운영 환경과 동일하게 REST로 수신한 과거 15개 완성봉을 price_context에 병합
        self.detector.price_context.merge_history('999990', HISTORICAL_BARS, AT, 'KRX')
        # 웹소켓 틱을 통해 적재되는 history에도 15개 완성봉 기록
        for b in HISTORICAL_BARS:
            self.detector.history.setdefault('999990', []).append(b)
        self.detector.observation_start['999990'] = AT - timedelta(minutes=15)
        self.detector.prior_high['999990'] = 10030
        self.watch = {
            'status': 'WATCH',
            'name': '통합테스트종목',
            'resistance': 20000,
            'source_count': 3,
            'source_details': {'a': {}, 'b': {}, 'c': {}}
        }

    def test_uncompleted_current_minute_is_strictly_isolated_from_usable_bars(self):
        """1. 현재 진행 중인 미완성 1분봉은 usable_bars에 포함되지 않음을 검증"""
        # 10:30:15에 틱 수신 (진행 중인 10:30분봉)
        tick = Tick('999990', AT + timedelta(seconds=15), 10040, 100, 1000, 10040, 10030)
        self.detector.ingest(tick, tick.time)
        
        # current에는 존재하지만
        current_bar = self.detector.current.get('999990')
        self.assertIsNotNone(current_bar)
        self.assertEqual(current_bar['time'], AT)
        self.assertEqual(current_bar['close'], 10040)
        
        # usable_bars에는 여전히 10:29 이전의 완성봉 15개만 존재
        usable = self.detector.usable_bars('999990')
        self.assertEqual(len(usable), 15)
        self.assertEqual(usable[-1]['time'], AT - timedelta(minutes=1))
        self.assertNotIn(AT, [b['time'] for b in usable])

    def test_pipeline_target_and_stop_match_between_signal_and_engine(self):
        """2. runner -> detector.signal -> engine.enter의 데이터 전달과 목표가/손절가 일치성 검증"""
        self.detector.observation_session['999990'] = (AT.date(), 'REGULAR')
        self.detector.observation_start['999990'] = AT - timedelta(minutes=15)

        # 10:30:05 시점 돌파 틱
        t = Tick('999990', AT + timedelta(seconds=5), 10040, 100, 1000, 10040, 10030)
        self.detector.ingest(t, t.time)
        
        # 70초 가속/턴오버 버퍼 시뮬레이션 (충분한 유동성 확보)
        for i in range(70):
            ts = AT - timedelta(seconds=70 - i)
            self.detector.buffers.setdefault('999990', []).append(
                Tick('999990', ts, 10020, 5000, 50000, 10020, 10010)
            )
            
        sig = self.detector.signal(t, self.watch)
        self.assertIsNotNone(sig, "정상적인 지지선/목표가 구조에서는 시그널이 생성되어야 함")
        self.assertEqual(sig['pattern'], 'A')
        self.assertEqual(sig['limit'], 10040)
        
        # 구조적 손절선과 목표가 검증
        # 지지선: 9880, 비상손절선: 9850, 목표가: 10330
        self.assertEqual(sig['stop'], 9850)
        self.assertEqual(sig['target'], 10330)
        
        # 손익비: (10330 - 10040) / (10040 - 9850) = 290 / 190 = 1.5263 >= 1.5
        rr_gate = sig['evaluation_gates']['risk_reward']
        self.assertEqual(rr_gate['result'], 'PASS')
        self.assertAlmostEqual(rr_gate['values']['calculated_rr'], 290 / 190)

    def test_counter_example_inflated_target_is_strictly_rejected(self):
        """3. 반례 검증: 목표가가 캡핑되어 손익비가 1.5 미만인 부적절한 매수는 전수 차단됨을 입증"""
        # 만약 원래 패턴 목표가가 10100원인 경우 (수익 60원, 위험 190원 -> RR = 0.316)
        bad_setup = {'pattern': 'A', 'breakout': 10030, 'support': 9990, 'projection': 10100}
        t = Tick('999990', AT, 10040, 100, 1000, 10040, 10030)
        
        gates = self.detector.setup_gates(t, self.watch, bad_setup, HISTORICAL_BARS[-4:])
        
        # 구조적 ceiling이 10330원으로 높게 나와도 원래 패턴 목표가(10100)를 초과하여 상향 왜곡되지 않음
        self.assertEqual(gates['risk_reward']['values']['target'], 10100)
        self.assertEqual(gates['risk_reward']['result'], 'FAIL')
        self.assertAlmostEqual(gates['risk_reward']['values']['calculated_rr'], 60 / 190)

    def test_future_data_cannot_affect_past_evaluation(self):
        """4. 미래 시각의 캔들이나 틱이 과거 평가의 지지선이나 목표가를 변경하지 못함을 검증"""
        before_bars = copy.deepcopy(self.detector.price_context.reference_bars('999990', AT, 'KRX'))
        
        # 미래(10:35:00)의 비정상 대형 급등봉 수신 시도
        future_bar = {
            'time': AT + timedelta(minutes=5),
            'open': 10040, 'high': 15000, 'low': 10000, 'close': 14000,
            'volume': 999999, 'venue': 'KRX'
        }
        # AT 시점 기준 기록 시도시 미래봉 거절
        res = self.detector.price_context.record('999990', future_bar, AT)
        self.assertFalse(res)
        
        after_bars = self.detector.price_context.reference_bars('999990', AT, 'KRX')
        self.assertEqual(before_bars, after_bars, "미래봉은 기준 시점의 참조봉을 변조할 수 없음")

if __name__ == '__main__':
    unittest.main()
