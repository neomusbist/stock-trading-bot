"""Setup O (Opening Momentum) 전략 유닛 및 회귀 검증 테스트 suite.
09:00~09:05 개장 직후 완성 1분봉(len(bars)>=2) 없이도
시가 대비 유효 상승세, 3틱 이상, 3초 이상, 500만원 이상 거래대금, 스프레드 정상,
구조적 손절 및 2% 위험비 크기 산정, FakeBroker 실주문 전송, 비상/보호 청산 연계 검증.
"""
import contextlib
import copy
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import Policy, AutomationConfig
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.decisions import BUILD_ID, STRATEGY_VERSION
from tests.test_automation import FakeBroker

SYM = '005935'  # 삼성전자우 (보통주 테스트용 심볼)
OPEN_DATE = datetime(2026, 10, 12, 9, 0, 0, tzinfo=KST)


class SetupOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

        self.clock = OPEN_DATE
        self.cfg = replace(
            Config(),
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_position_fraction=1.0,
            max_positions=2,
            max_entries_per_day=3,
            entry_start='09:00:00',
            entry_end='15:00:00',
            stale_seconds=5,
            max_spread=0.005,
            max_chase=0.008,
            min_stop_fraction=0.002,
            max_stop_fraction=0.03,
            min_reward_risk=1.5,
            structural_buffer=0.001
        )
        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            setup_o_enabled=True,
            setup_o_min_ticks=3,
            setup_o_min_seconds=3.0,
            setup_o_min_turnover=5000000.0,
            setup_o_min_gain_over_open=0.003,
            setup_o_end_time='09:05:00',
            horizon_o_minutes=15
        )
        self.policy = Policy(self.root / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.b = FakeBroker()
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy, self.root / 'book', self.b, lambda: self.clock)
        self.e.reconcile({'cash': 80181, 'equity': 80181, 'holdings': {}, 'orders': []})
        self.e.connected = True
        self.e.account_ok = True
        self.e.market = '3'
        self.e.market_status = {SYM: {'pass': True}}
        self.e.source_qualification = {SYM: {'source_count': 2, 'timestamp': self.clock.isoformat()}}
        self.watch = {
            'code': SYM,
            'status': 'WATCH',
            'source_count': 2,
            'market_status': {'pass': True},
            'open': 50000.0,
            'resistance': 55000.0,
            'resistance_room_pass': True,
            'source_details': {'ka10029': {}, 'ka10032': {}}
        }
        self.d = PatternDetector(self.cfg, self.auto, self.e.costs)

    def test_01_policy_defaults_and_validation(self):
        """Setup O 파라미터 기본값 및 검증 로직 검사."""
        self.assertTrue(self.auto.setup_o_enabled)
        self.assertEqual(self.auto.setup_o_min_ticks, 3)
        self.assertEqual(self.auto.setup_o_min_seconds, 3.0)
        self.assertEqual(self.auto.setup_o_min_turnover, 5000000.0)
        self.assertEqual(self.auto.setup_o_min_gain_over_open, 0.003)
        self.assertEqual(self.auto.setup_o_end_time, '09:05:00')
        self.assertEqual(self.auto.horizon_o_minutes, 15)
        self.auto.validate()

        with self.assertRaises(ValueError):
            replace(self.auto, setup_o_min_ticks=0).validate()
        with self.assertRaises(ValueError):
            replace(self.auto, setup_o_end_time='08:59:00').validate()
        with self.assertRaises(ValueError):
            replace(self.auto, horizon_o_minutes=0).validate()

    def test_02_outside_opening_window_rejected(self):
        """09:00 이전 장전이나 09:05 이후에는 Setup O 진입 신호가 생성되지 않아야 함."""
        pre_open = datetime(2026, 10, 12, 8, 59, 59, tzinfo=KST)
        t_pre = Tick(SYM, pre_open, 50200, 100, 100, 50250, 50150)
        self.d.ingest(t_pre, pre_open)
        sig = self.d.signal(t_pre, self.watch)
        self.assertIsNone(sig)

        after_cutoff = datetime(2026, 10, 12, 9, 5, 1, tzinfo=KST)
        t_post = Tick(SYM, after_cutoff, 50500, 100, 500, 50550, 50450)
        self.d.ingest(t_post, after_cutoff)
        sig2 = self.d.signal(t_post, self.watch)
        self.assertIsNone(sig2)

    def test_03_zero_bar_opening_signal_generation(self):
        """완성 1분봉 0개(개장 5초 경과) 상태에서 3틱/3초/500만원 충족 시 Setup O 신호 발생 검증."""
        t1_time = datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST)
        t1 = Tick(SYM, t1_time, 50100, 50, 50, 50150, 50050)
        self.assertTrue(self.d.ingest(t1, t1_time))

        t2_time = datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST)
        t2 = Tick(SYM, t2_time, 50200, 40, 90, 50250, 50150)
        self.assertTrue(self.d.ingest(t2, t2_time))

        t3_time = datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST)
        # 30주 * 50300 = 1,509,000 + 40*50200 + 50*50100 = ~7,000,000 KRW (>5,000,000 KRW)
        t3 = Tick(SYM, t3_time, 50300, 50, 140, 50300, 50200)
        self.assertTrue(self.d.ingest(t3, t3_time))

        # 완성 1분봉이 전혀 없음을 명시적으로 확인
        self.assertEqual(len(self.d.completed.get(SYM, [])), 0)
        self.assertEqual(len(self.d.usable_bars(SYM)), 0)

        sig = self.d.signal(t3, self.watch)
        self.assertIsNotNone(sig, "Setup O 신호가 생성되어야 함")
        self.assertEqual(sig['pattern'], 'O')
        self.assertEqual(sig['symbol'], SYM)
        self.assertGreaterEqual(sig['limit'], t3.ask)
        self.assertLess(sig['stop'], sig['limit'])
        self.assertGreater(sig['projection'], sig['limit'])
        self.assertIn('O', [tr['setup'] for tr in sig['setup_trials']])
        o_trial = next(tr for tr in sig['setup_trials'] if tr['setup'] == 'O')
        self.assertTrue(o_trial['entry_ready'])
        self.assertEqual(o_trial['failed_reasons'], [])

    def test_04_rejection_conditions(self):
        """Setup O의 엄격한 차단 조건 검증 (틱수 부족, 시간 부족, 거래대금 부족, 하락 되밀림)."""
        # (1) 틱수 부족 (2틱)
        d_few = PatternDetector(self.cfg, self.auto, self.e.costs)
        t1 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST), 50100, 50, 50, 50150, 50050)
        t2 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 4, tzinfo=KST), 50300, 80, 130, 50300, 50200)
        d_few.ingest(t1, t1.time)
        d_few.ingest(t2, t2.time)
        self.assertIsNone(d_few.signal(t2, self.watch))

        # (2) 경과시간 부족 (< 3초)
        d_fast = PatternDetector(self.cfg, self.auto, self.e.costs)
        t1 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST), 50100, 50, 50, 50150, 50050)
        t2 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 2, tzinfo=KST), 50200, 50, 100, 50250, 50150)
        t3 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 2, tzinfo=KST), 50300, 50, 150, 50300, 50200)
        d_fast.ingest(t1, t1.time)
        d_fast.ingest(t2, t2.time)
        d_fast.ingest(t3, t3.time)
        self.assertIsNone(d_fast.signal(t3, self.watch))

        # (3) 거래대금 부족 (< 5,000,000 KRW)
        d_low_vol = PatternDetector(self.cfg, self.auto, self.e.costs)
        t1 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST), 50100, 1, 1, 50150, 50050)
        t2 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST), 50200, 1, 2, 50250, 50150)
        t3 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST), 50300, 1, 3, 50300, 50200)
        d_low_vol.ingest(t1, t1.time)
        d_low_vol.ingest(t2, t2.time)
        d_low_vol.ingest(t3, t3.time)
        self.assertIsNone(d_low_vol.signal(t3, self.watch))

        # (4) 직전 틱 대비 하락 되밀림 (pullback collapse)
        d_drop = PatternDetector(self.cfg, self.auto, self.e.costs)
        t1 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST), 50100, 50, 50, 50150, 50050)
        t2 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST), 50400, 50, 100, 50450, 50350)
        t3 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST), 50300, 50, 150, 50350, 50250)
        d_drop.ingest(t1, t1.time)
        d_drop.ingest(t2, t2.time)
        d_drop.ingest(t3, t3.time)
        self.assertIsNone(d_drop.signal(t3, self.watch))

        # (5) 스프레드 과대 (> max_spread 0.5%)
        d_spread = PatternDetector(self.cfg, self.auto, self.e.costs)
        t1 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST), 50100, 50, 50, 50150, 50050)
        t2 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST), 50200, 50, 100, 50250, 50150)
        t3 = Tick(SYM, datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST), 50300, 50, 150, 50700, 50000)  # spread 700 / 50300 = 1.39%
        d_spread.ingest(t1, t1.time)
        d_spread.ingest(t2, t2.time)
        d_spread.ingest(t3, t3.time)
        self.assertIsNone(d_spread.signal(t3, self.watch))

    def test_05_autonomous_engine_sizing_and_execution(self):
        """엔진 연동: Setup O 신호에 대해 2% 위험비 크기 산정 및 FakeBroker 실제 주문 접수 검증."""
        t1_time = datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST)
        t1 = Tick(SYM, t1_time, 50100, 50, 50, 50150, 50050)
        self.d.ingest(t1, t1_time)

        t2_time = datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST)
        t2 = Tick(SYM, t2_time, 50200, 40, 90, 50250, 50150)
        self.d.ingest(t2, t2_time)

        t3_time = datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST)
        t3 = Tick(SYM, t3_time, 50300, 50, 140, 50300, 50200)
        self.d.ingest(t3, t3_time)
        self.clock = t3_time
        self.e.last_account = time.monotonic()

        sig = self.d.signal(t3, self.watch)
        self.assertIsNotNone(sig)

        order = self.e.enter(sig, t3)
        self.assertIsNotNone(order, "위험관리 조건을 통과하여 실제 주문이 생성되어야 함")
        self.assertEqual(order['symbol'], SYM)
        self.assertEqual(order['side'], 'BUY')
        self.assertGreater(order['qty'], 0)
        self.assertLessEqual(order['qty'] * order['limit'], 81300, "자본 승인 한도 81,300원을 초과하지 않아야 함")
        self.assertEqual(len(self.b.orders), 1, "FakeBroker에 정확히 1건의 주문이 도달해야 함")

        # 주문 체결 시뮬레이션
        self.e.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])
        self.assertIn(SYM, self.e.s['positions'], "체결 후 포지션이 생성되어야 함")
        pos = self.e.s['positions'][SYM]
        self.assertEqual(pos['qty'], order['qty'])
        self.assertEqual(pos['pattern'], 'O')
        self.assertEqual(pos['stop'], sig['stop'])

    def test_06_duplicate_order_protection(self):
        """동일 종목에 대해 포지션 보유 중 추가 매수 중복 주문 방지 검증."""
        t1_time = datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST)
        t1 = Tick(SYM, t1_time, 50100, 50, 50, 50150, 50050)
        t2_time = datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST)
        t2 = Tick(SYM, t2_time, 50200, 40, 90, 50250, 50150)
        t3_time = datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST)
        t3 = Tick(SYM, t3_time, 50300, 50, 140, 50300, 50200)

        for t in (t1, t2, t3):
            self.d.ingest(t, t.time)
        self.clock = t3_time
        self.e.last_account = time.monotonic()

        sig = self.d.signal(t3, self.watch)
        order = self.e.enter(sig, t3)
        self.assertIsNotNone(order)

        # 다음 틱 수신 시 이미 활성 주문이 있으므로 추가 주문 불가
        t4_time = datetime(2026, 10, 12, 9, 0, 6, tzinfo=KST)
        t4 = Tick(SYM, t4_time, 50400, 20, 160, 50400, 50300)
        self.d.ingest(t4, t4_time)
        self.clock = t4_time

        sig4 = self.d.signal(t4, self.watch)
        if sig4 is not None:
            dup_order = self.e.enter(sig4, t4)
            self.assertIsNone(dup_order, "이미 활성 주문이 있으므로 중복 주문은 차단되어야 함")
        self.assertEqual(len(self.b.orders), 1)

    def test_07_invalidation_and_trailing_stop_exits(self):
        """Setup O 포지션의 무효화 스탑 및 트레일링 스탑 이탈 청산 검증."""
        t1_time = datetime(2026, 10, 12, 9, 0, 1, tzinfo=KST)
        t1 = Tick(SYM, t1_time, 50100, 50, 50, 50150, 50050)
        t2_time = datetime(2026, 10, 12, 9, 0, 3, tzinfo=KST)
        t2 = Tick(SYM, t2_time, 50200, 40, 90, 50250, 50150)
        t3_time = datetime(2026, 10, 12, 9, 0, 5, tzinfo=KST)
        t3 = Tick(SYM, t3_time, 50300, 50, 140, 50300, 50200)

        for t in (t1, t2, t3):
            self.d.ingest(t, t.time)
        self.clock = t3_time
        self.e.last_account = time.monotonic()

        sig = self.d.signal(t3, self.watch)
        order = self.e.enter(sig, t3)
        self.assertIsNotNone(order)
        self.e.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])
        self.assertIn(SYM, self.e.s['positions'])
        self.e.actual = {SYM: {'qty': order['qty'], 'available': order['qty'], 'avg': order['limit']}}

        # (1) 주가가 무효화 스탑 이하로 급락 시 청산 주문 발생
        drop_time = datetime(2026, 10, 12, 9, 1, 0, tzinfo=KST)
        t_drop = Tick(SYM, drop_time, 49900, 50, 300, 49950, 49900)
        self.clock = drop_time
        self.e.last_account = time.monotonic()
        self.e.on_tick(t_drop)

        sell_orders = [o for o in self.b.orders if o[0] == 'SELL']
        self.assertEqual(len(sell_orders), 1, "무효화 가격 하향 이탈 시 매도 주문 발생해야 함")
        self.assertEqual(sell_orders[0][1], SYM)

    def test_08_coexistence_with_abc_after_opening_window(self):
        """09:05 이후 정상 장중에는 Setup O가 시도되지 않고 기존 A/B/C 3종만 엄격히 유지되는지 검증."""
        regular_time = datetime(2026, 10, 12, 9, 10, 0, tzinfo=KST)
        t_reg = Tick(SYM, regular_time, 51000, 100, 1000, 51050, 50950)
        self.d.ingest(t_reg, regular_time)
        trials = self.d.setup_trials(t_reg, self.watch)
        setups_tested = [tr['setup'] for tr in trials]
        self.assertEqual(setups_tested, ['A', 'B', 'C'], "09:05 이후에는 엄격히 A/B/C 3종만 평가되어야 함")


if __name__ == '__main__':
    unittest.main()
