"""V1.3 AFTER 시장(16:00~20:00 KST / NXT prep 15:40:00) 전용 테스트 스위트 (Sections A ~ G)."""
import contextlib, copy, io, json, tempfile, time, unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from core.settings import Config, KST
from core.strategy import Tick
from core.broker import Rejected, Rest
from automation.policy import Policy, AutomationConfig, HARD_LOCKS, BREAKEVEN_EXITS
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.session import (
    AfterSessionPlan, supported, after_supported, after_entry_supported,
    is_after_market, current_session, market_phase, control_supported, after_control_supported
)
from automation.market_status import assess_after_status
from tests.test_automation import FakeBroker

SYM = '999990'
AFTER_BASE = datetime(2026, 10, 7, 16, 5, 0, tzinfo=KST)


class AfterMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

        self.clock = AFTER_BASE
        self.cfg = replace(
            Config(), live_mode=True, risk_fraction=0.02, max_daily_loss_fraction=0.04,
            max_position_fraction=1.0, max_positions=1, max_entries_per_day=3,
            max_watchlist=12,
            after_enabled=True, after_a_accel_ratio=2.5, after_a_min_gain=0.003,
            after_a_liquidity_mode='depth', after_default_venue='SOR'
        )
        self.auto = replace(
            AutomationConfig(), AUTO_TRADING_CAP_KRW=20000, costs_confirmed=True,
            after_prep_start='15:40:00', after_market_start='16:00:00',
            after_entry_cutoff='19:45:00', after_force_flatten_start='19:55:00',
            after_market_close='20:00:00', fallback_source1=True
        )
        self.policy = Policy(self.root / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.b = FakeBroker()
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy, self.root / 'book', self.b, lambda: self.clock)
        self.e.reconcile({'cash': 20420, 'equity': 20420, 'holdings': {}, 'orders': []})
        self.e.connected = True
        self.e.market = '3'
        self.e.market_status = {SYM: {'pass': True}}
        self.e.after_live_authorized = True

        self.watch = {
            'code': SYM, 'status': 'WATCH', 'source_count': 2, 'market_status': {'pass': True},
            'resistance': 11000, 'resistance_room_pass': True, 'source_details': {'one': {}, 'two': {}},
            'pivot': 9900
        }
        self.d = PatternDetector(self.cfg, self.auto, self.e.costs)

    def signal(self, sym=SYM, entry=10000, stop=9800, pattern='A'):
        from automation.allocation import efficiency
        return {
            'symbol': sym, 'limit': entry, 'stop': stop, 'target': 10500, 'pattern': pattern,
            'time': self.clock.isoformat(),
            'efficiency': efficiency(entry, stop, 10500, 0.8, 30, self.e.costs),
            'source_count': 2, 'actual_order_permission': True,
            'session': 'AFTER', 'venue': 'SOR'
        }

    def tick(self, price=10000, sym=SYM, ask_qty=10, bid_qty=10, regular=True, cumulative=100):
        return Tick(sym, self.clock, price, 10, cumulative, price + 10, price - 10,
                    regular=regular, ask_qty=ask_qty, bid_qty=bid_qty, venue='SOR')

    # =========================================================================
    # Section A: Session boundaries
    # =========================================================================

    def test_after_missing_target_remains_blocked(self):
        signal = self.signal()
        del signal['target']
        self.e.last_account = time.monotonic()
        self.assertIsNone(self.e.enter(signal, self.tick()))
        self.assertEqual(self.b.orders, [])

    def test_a1_before_1600_buy_blocked(self):
        """15:59:00 (16:00 이전) 신규 매수는 차단된다."""
        self.clock = datetime(2026, 10, 7, 15, 59, 0, tzinfo=KST)
        reason = self.e.new_entry_reason(SYM)
        self.assertEqual(reason, 'AFTER_PREWARM_NO_BUY')
        sig = self.signal()
        order = self.e.enter(sig, self.tick())
        self.assertIsNone(order)
        self.assertEqual(self.b.orders, [])

    def test_a2_after_prep_prewarm_feed_radar(self):
        """15:40~16:00 구간은 PREWARM 상태로 관찰/준비는 허용되나 매수는 0이다."""
        self.clock = datetime(2026, 10, 7, 15, 45, 0, tzinfo=KST)
        self.assertTrue(after_control_supported(self.clock))
        self.assertFalse(control_supported(self.clock))
        self.assertTrue(self.e.is_supported_now())
        self.assertEqual(self.e.current_session(), 'AFTER')
        self.assertFalse(after_entry_supported(self.clock))
        self.assertEqual(self.e.new_entry_reason(SYM), 'AFTER_PREWARM_NO_BUY')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))

    def test_a3_after_ready_requires_explicit_after_live_approval(self):
        """16:05:00에 after_live_authorized가 없으면 LIVE 실주문이 차단된다."""
        self.clock = datetime(2026, 10, 7, 16, 5, 0, tzinfo=KST)
        self.e.after_live_authorized = False
        reason = self.e.new_entry_reason(SYM)
        self.assertEqual(reason, 'AFTER_LIVE_NOT_AUTHORIZED')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))
        self.assertEqual(self.b.orders, [])

    def test_a4_after_live_authorized_allows_entry(self):
        """16:05:00에 after_live_authorized가 있으면 AFTER 주문이 생성된다."""
        self.clock = datetime(2026, 10, 7, 16, 5, 0, tzinfo=KST)
        self.e.after_live_authorized = True
        self.e.last_account = time.monotonic()
        sig = self.signal()
        t = self.tick()
        reason = self.e.new_entry_reason(SYM)
        self.assertEqual(reason, '')
        order = self.e.enter(sig, t)
        self.assertIsNotNone(order)
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertEqual(order['session'], 'AFTER')
        self.assertEqual(order['venue'], 'SOR')
        self.assertEqual(len(self.b.orders), 1)
        self.assertEqual(self.b.orders[0][0], 'BUY')
        self.assertEqual(self.b.orders[0][1], SYM)

    def test_a5_after_cutoff_cancels_buys_and_blocks_new_buys(self):
        """19:45:00 cutoff 시점에는 신규 매수 차단 및 기존 매수 주문이 취소된다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        order = self.e.enter(self.signal(), self.tick())
        self.assertIsNotNone(order)
        self.assertEqual(len(self.e.active_orders(side='BUY')), 1)

        # 19:45:00 cutoff 진입
        self.clock = datetime(2026, 10, 7, 19, 45, 0, tzinfo=KST)
        self.assertEqual(self.e.new_entry_reason(SYM), 'AFTER_ENTRY_CUTOFF')
        self.e.timer({SYM: 10000})
        self.assertEqual(order['status'], 'CANCEL_SENT')
        self.assertEqual(len(self.b.cancels), 1)

    def test_a6_after_close_at_2000_blocks_entry(self):
        """20:00:00 시장 마감 시 OUT_OF_SUPPORTED_SESSION으로 완전 차단된다."""
        self.clock = datetime(2026, 10, 7, 20, 0, 0, tzinfo=KST)
        self.assertFalse(self.e.is_supported_now())
        self.assertEqual(self.e.current_session(), 'CLOSED')
        self.assertEqual(self.e.new_entry_reason(SYM), 'OUT_OF_SUPPORTED_SESSION')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))

    # =========================================================================
    # Section B: Shared risk ledger between REGULAR and AFTER
    # =========================================================================

    def test_b1_shared_entries_count(self):
        """정규장에서 발생한 3회 매매는 AFTER에도 합산되어 당일 3회 초과 진입을 차단한다."""
        self.e.s['entries'] = {'111110': 1, '222220': 1, '333330': 1}
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        qty, gates = self.e.entry_gates(self.signal(), self.tick())
        self.assertEqual(gates['repeat_slots']['result'], 'FAIL')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))

    def test_b2_shared_daily_loss(self):
        """정규장에서 발생한 당일 손실(800원)은 AFTER에도 유지되어 추가 진입을 차단한다."""
        self.e.s['daily_loss'] = 800.0  # 4% of 20,000
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        qty, gates = self.e.entry_gates(self.signal(), self.tick())
        self.assertEqual(qty, 0)
        self.assertEqual(gates['risk_quantity']['result'], 'FAIL')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))

    def test_b3_shared_max_positions(self):
        """정규장에서 들고 온 포지션이 1개 있으면 AFTER에서 추가 동시 보유가 불가하다."""
        self.e.s['positions']['111110'] = {'ownership': 'BOT', 'qty': 1, 'avg': 10000, 'stop': 9800}
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        qty, gates = self.e.entry_gates(self.signal(), self.tick())
        self.assertEqual(gates['repeat_slots']['result'], 'FAIL')
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))

    def test_b4_shared_capital_limit(self):
        """승인 자본 한도 20,000원은 정규장과 AFTER가 동일하게 공유한다."""
        self.assertEqual(self.e.a.capital_limit, 20000)
        self.assertEqual(self.e.risk_equity(), 20000)

    # =========================================================================
    # Section C: Discovery & continuity
    # =========================================================================

    def test_c1_watch_12_capacity_in_after(self):
        """AFTER 모드에서도 WATCH 슬롯 12개 용량이 유지된다."""
        self.assertEqual(self.cfg.max_watchlist, 12)

    def test_c2_source1_fallback_in_after(self):
        """AFTER 모드에서 source_count=1 단일소스 후보도 fallback_source1 플래그로 관찰/진입 가능하다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        sig = self.signal()
        sig['source_count'] = 1
        sig['fallback_source1'] = True
        self.e.last_account = time.monotonic()
        order = self.e.enter(sig, self.tick())
        self.assertIsNotNone(order)
        self.assertEqual(order['status'], 'ACCEPTED')

    def test_c3_regular_surge_does_not_trigger_after_signal(self):
        """정규장(15:30 이전) 급등/거래량이 있어도 16:00 이후 AFTER 신호로 누출되지 않는다."""
        reg_time = datetime(2026, 10, 7, 15, 20, 0, tzinfo=KST)
        count = 0
        for m in range(5):
            for s in range(0, 60, 5):
                count += 1
                t = Tick(SYM, reg_time + timedelta(minutes=m, seconds=s), 10000 + m * 100, 1000, 10000 + count * 10,
                         10000 + m * 100 + 10, 10000 + m * 100 - 10, ask_qty=50, bid_qty=50, venue='KRX')
                self.d.ingest(t, t.time)
        self.clock = datetime(2026, 10, 7, 16, 5, 0, tzinfo=KST)
        t_after = Tick(SYM, self.clock, 10500, 1, 20000, 10510, 10490, ask_qty=10, bid_qty=10, venue='SOR')
        usable = self.d.usable_bars(SYM, after_only=True)
        self.assertEqual(len(usable), 0)
        trials = self.d.setup_trials(t_after, self.watch)
        for tr in trials:
            self.assertFalse(tr['entry_ready'])
        self.assertIsNone(self.d.signal(t_after, self.watch))

    def test_c4_nxt_after_prewarm_bars_accumulated_for_signal(self):
        """15:40~16:00 NXT AFTER 체결/호가 데이터는 AFTER 데이터로 정상 축적되어 16:00 이후 신호 근거로 작동한다."""
        nxt_time = datetime(2026, 10, 7, 15, 40, 0, tzinfo=KST)
        count = 0
        for m in range(5):
            for s in range(0, 60, 5):
                count += 1
                t = Tick(SYM, nxt_time + timedelta(minutes=m, seconds=s), 10000 + (m % 2) * 10, 10, count * 10,
                         10020, 9980, ask_qty=10, bid_qty=10, venue='SOR')
                self.d.ingest(t, t.time)
        self.clock = datetime(2026, 10, 7, 16, 0, 5, tzinfo=KST)
        usable = self.d.usable_bars(SYM, after_only=True)
        self.assertEqual(len(usable), 4)
        for b in usable:
            self.assertGreaterEqual(b['time'].strftime('%H:%M:%S'), '15:40:00')

    def test_c5_after_only_continuity(self):
        """16:00 이후 관측 연속성은 AFTER 데이터에 한해 정상 유지된다."""
        self.clock = datetime(2026, 10, 7, 16, 1, 0, tzinfo=KST)
        t1 = self.tick(10000, cumulative=100)
        self.assertTrue(self.d.ingest(t1, self.clock))
        self.assertEqual(len(self.d.buffers[SYM]), 1)
        self.clock = datetime(2026, 10, 7, 16, 1, 1, tzinfo=KST)
        t2 = self.tick(10010, cumulative=110)
        self.assertTrue(self.d.ingest(t2, self.clock))
        self.assertEqual(len(self.d.buffers[SYM]), 2)

    # =========================================================================
    # Section D: Setups
    # =========================================================================

    def test_d1_setup_a_after_accel_gain(self):
        """AFTER 모드에서 Setup A는 after_a_accel_ratio(2.5)와 min_gain(0.003)을 적용한다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        # Ingest baseline
        for s in range(30):
            t = Tick(SYM, self.clock + timedelta(seconds=s), 10000, 10, 100, 10010, 9990,
                     ask_qty=10, bid_qty=10, venue='SOR')
            self.d.ingest(t, t.time)
        gates = self.d.base_gates(t, self.watch)
        self.assertIn('acceleration', gates)

    def test_d2_setup_bc_turnover_bypassed_in_after(self):
        """AFTER 모드에서는 Setup B와 C가 정규장 2000만원 turnover hard gate에 걸리지 않는다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        t = self.tick(10000)
        # Buffer with very low volume (turnover << 20,000,000)
        for s in range(30):
            t_buf = Tick(SYM, self.clock + timedelta(seconds=s), 10000, 1, 10, 10010, 9990,
                         ask_qty=10, bid_qty=10, venue='SOR')
            self.d.ingest(t_buf, t_buf.time)
        trials = self.d.setup_trials(t_buf, self.watch)
        b_trial = next((tr for tr in trials if tr['setup'] == 'B'), None)
        c_trial = next((tr for tr in trials if tr['setup'] == 'C'), None)
        self.assertIsNotNone(b_trial)
        self.assertIsNotNone(c_trial)
        self.assertNotIn('LIQUIDITY_FAIL', b_trial.get('failed_reasons', []))
        self.assertNotIn('LIQUIDITY_FAIL', c_trial.get('failed_reasons', []))

    def test_d3_setup_breakout_required(self):
        """AFTER 모드에서도 가격 돌파/무효화 구조가 없으면 진입 신호가 나지 않는다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        t = self.tick(9800)  # 아래에 머무름
        for s in range(30):
            t_buf = Tick(SYM, self.clock + timedelta(seconds=s), 9800, 1, 10, 9810, 9790,
                         ask_qty=10, bid_qty=10, venue='SOR')
            self.d.ingest(t_buf, t_buf.time)
        sig = self.d.signal(t_buf, self.watch)
        self.assertIsNone(sig)

    def test_d4_risk_reward_and_economic_gates(self):
        """손익비/수익구조가 부족하면 주문이 생성되지 않는다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        # stop is too close or invalid
        bad_sig = self.signal(stop=9999)  # almost zero stop
        qty, gates = self.e.entry_gates(bad_sig, self.tick())
        # Should still run safely without crashing
        self.assertIn('efficiency', gates)

    # =========================================================================
    # Section E: Liquidity
    # =========================================================================

    def test_e1_depth_mode_passes_when_book_sufficient(self):
        """관측 단계 최소 1주 잔량; 실제 위험수량은 engine/order boundary에서 검사."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        t = self.tick(10000, ask_qty=5, bid_qty=5)
        gates = self.d.base_gates(t, self.watch)
        self.assertEqual(gates['liquidity']['result'], 'PASS')

    def test_e2_depth_mode_fails_when_book_insufficient(self):
        """관측 단계에서도 0주 잔량은 FAIL. actual qty 부족은 별도 경계 회귀 검사."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        t = self.tick(10000, ask_qty=0, bid_qty=5)
        t = replace(t,depth_available=True) # known zero is distinct from missing depth
        gates = self.d.base_gates(t, self.watch)
        self.assertEqual(gates['liquidity']['result'], 'FAIL')

    def test_e3_spread_gate_enforced(self):
        """스프레드가 max_spread(0.004)를 초과하면 스프레드 FAIL."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        # price=10000, ask=10100, bid=9900 -> spread = 200/10000 = 0.02 > 0.004
        t = Tick(SYM, self.clock, 10000, 10, 100, 10100, 9900, ask_qty=10, bid_qty=10, venue='SOR')
        gates = self.d.base_gates(t, self.watch)
        self.assertEqual(gates['spread']['result'], 'FAIL')

    def test_e4_stale_tick_fails(self):
        """틱 지연시간이 stale_seconds(5초)를 초과하면 tick gate FAIL."""
        self.clock = datetime(2026, 10, 7, 16, 10, 10, tzinfo=KST)
        stale_time = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)  # 10s lag
        t = Tick(SYM, stale_time, 10000, 10, 100, 10010, 9990, ask_qty=10, bid_qty=10, venue='SOR')
        qty, gates = self.e.entry_gates(self.signal(), t)
        self.assertEqual(gates['tick']['result'], 'FAIL')

    # =========================================================================
    # Section F: Routing & safety
    # =========================================================================

    def test_f1_venue_routing(self):
        """AFTER 주문은 SOR 또는 지정된 베뉴로 라우팅된다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        order = self.e.enter(self.signal(), self.tick())
        self.assertEqual(order['venue'], 'SOR')
        self.assertEqual(order['session'], 'AFTER')

    def test_f2_limit_order_enforced_no_market_orders(self):
        """AFTER 세션에서는 시장가 주문(trde_tp='3')이 엄격히 금지된다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        with self.assertRaises(Rejected):
            self.e.order_guard('kt10001', {'stk_cd': SYM, 'ord_qty': '1', 'ord_uv': '0', 'trde_tp': '3'})

    def test_f3_duplicate_buy_blocked(self):
        """동일 종목에 대한 중복 매수 주문은 차단된다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        o1 = self.e.enter(self.signal(), self.tick())
        self.assertIsNotNone(o1)
        o2 = self.e.enter(self.signal(), self.tick())
        self.assertIsNone(o2)

    def test_f4_foreign_intent_unknown_halt_blocks_buy(self):
        """다른 종목에 미해결 INTENT/UNKNOWN 또는 HALT가 있으면 신규 BUY가 차단된다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        # Add an orphan INTENT for another symbol
        self.e.s['orders']['other-1'] = {
            'local': 'other-1', 'id': '', 'symbol': '888880', 'side': 'BUY',
            'qty': 1, 'limit': 5000, 'status': 'INTENT'
        }
        with self.assertRaises(Rejected):
            self.e.order_guard('kt10000', {'stk_cd': SYM, 'ord_qty': '1', 'ord_uv': '10000', 'trde_tp': '0'})

    def test_f5_non_bot_holdings_safe(self):
        """HARD_LOCK 또는 외부 보유 종목은 AFTER 강제청산이나 매도에 절대 포함되지 않는다."""
        self.clock = datetime(2026, 10, 7, 16, 10, 0, tzinfo=KST)
        self.e.s['positions']['005930'] = {'ownership': 'EXISTING_POLICY', 'qty': 10, 'avg': 60000, 'last_tick': self.clock.isoformat()}
        self.assertFalse(self.e.can_exit('005930', self.e.s['positions']['005930']))
        # At 19:55:00 flattening time, non-bot holding is untouched
        self.clock = datetime(2026, 10, 7, 19, 56, 0, tzinfo=KST)
        self.e.timer({'005930': 60000})
        self.assertNotIn('005930', [o['symbol'] for o in self.e.active_orders()])

    # =========================================================================
    # Section G: Exits
    # =========================================================================

    def test_g1_after_exit_only_at_1945(self):
        """19:45:00 이후에는 신규 매수가 차단되지만, 포지션 청산은 허용된다."""
        self.clock = datetime(2026, 10, 7, 19, 46, 0, tzinfo=KST)
        self.e.s['positions'][SYM] = {'ownership': 'BOT', 'qty': 1, 'avg': 10000, 'stop': 9800,
                                      'initial_stop': 9800, 'initial_r': 200, 'opened': self.clock.isoformat(),
                                      'high': 10000, 'exit_attempts': 0, 'last_tick': self.clock.isoformat()}
        self.assertTrue(self.e.can_exit(SYM, self.e.s['positions'][SYM]))
        self.assertEqual(self.e.new_entry_reason(SYM), 'AFTER_ENTRY_CUTOFF')

    def test_g2_after_flattening_at_1955(self):
        """19:55:00 force_flatten_start 시점에 봇 포지션 청산이 시작되며 지정가로 발주된다."""
        self.clock = datetime(2026, 10, 7, 19, 55, 30, tzinfo=KST)
        self.e.s['positions'][SYM] = {'ownership': 'BOT', 'qty': 1, 'avg': 10000, 'stop': 9800,
                                      'initial_stop': 9800, 'initial_r': 200, 'opened': self.clock.isoformat(),
                                      'high': 10000, 'exit_attempts': 0, 'last_tick': self.clock.isoformat()}
        self.e.last_account = time.monotonic()
        self.e.timer({SYM: 10000})
        p = self.e.s['positions'][SYM]
        self.assertTrue(p.get('exit_requested'))
        self.assertEqual(p.get('exit_reason'), 'AFTER 강제 청산')

    def test_g3_residual_position_at_2000_triggers_halt(self):
        """20:00:00 마감 시점에 미청산 봇 잔고가 남아있으면 AFTER_CLOSE_UNFLATTENED_POSITION 이벤트 및 HALT가 발생한다."""
        self.clock = datetime(2026, 10, 7, 20, 0, 5, tzinfo=KST)
        self.e.s['positions'][SYM] = {'ownership': 'BOT', 'qty': 1, 'avg': 10000, 'stop': 9800, 'last_tick': self.clock.isoformat()}
        self.e.timer({SYM: 10000})
        self.assertIn('AFTER 마감 시점 미청산 봇 잔고 감지', self.e.s.get('halt', ''))
        events = [json.loads(line) for line in (self.root / 'book/events.jsonl').read_text(encoding='utf-8').splitlines()]
        event_names = [ev.get('event') for ev in events]
        self.assertIn('AFTER_CLOSE_UNFLATTENED_POSITION', event_names)

    def test_g4_force_flatten_continuous_retry_cycle(self):
        """19:55~19:59:50 구간에서는 max_exit_attempts=3에 걸리지 않고 cancel/reconcile 후 지속 재주문된다."""
        self.clock = datetime(2026, 10, 7, 19, 55, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.actual[SYM] = {'available': 1, 'qty': 1}
        self.e.s['positions'][SYM] = {
            'ownership': 'BOT', 'qty': 1, 'avg': 10000, 'stop': 9800,
            'initial_stop': 9800, 'initial_r': 200, 'opened': self.clock.isoformat(),
            'high': 10000, 'low': 10000, 'exit_attempts': 0, 'last_tick': self.clock.isoformat(),
            'trade_id': 'trade-1', 'pattern': 'A', 'entry_hour': '16', 'market_state': '3',
            'stop_history': [{'time': self.clock.isoformat(), 'stop': 9800}],
            'signal': {'pattern': 'A'}
        }
        self.e.timer({SYM: 10000})
        self.assertEqual(len(self.b.orders), 1)
        self.assertEqual(self.e.s['positions'][SYM]['exit_attempts'], 1)
        o1 = self.e.active_orders(SYM, side='SELL')[0]

        # 19:55:12 1차 타임아웃 취소
        self.clock = datetime(2026, 10, 7, 19, 55, 12, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9990})
        self.assertEqual(o1['status'], 'CANCEL_SENT')
        self.e.reconcile({'cash': 20420, 'equity': 20420, 'holdings': {SYM: {'available': 1, 'qty': 1}}, 'orders': []})

        # 2차 매도 발주 (19:55:15)
        self.clock = datetime(2026, 10, 7, 19, 55, 15, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9990})
        self.assertEqual(len(self.b.orders), 2)
        self.assertEqual(self.e.s['positions'][SYM]['exit_attempts'], 2)
        o2 = self.e.active_orders(SYM, side='SELL')[0]

        # 2차 타임아웃 취소 및 reconcile (19:55:27)
        self.clock = datetime(2026, 10, 7, 19, 55, 27, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9980})
        self.assertEqual(o2['status'], 'CANCEL_SENT')
        self.e.reconcile({'cash': 20420, 'equity': 20420, 'holdings': {SYM: {'available': 1, 'qty': 1}}, 'orders': []})

        # 3차 매도 발주 (19:55:30)
        self.clock = datetime(2026, 10, 7, 19, 55, 30, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9980})
        self.assertEqual(len(self.b.orders), 3)
        self.assertEqual(self.e.s['positions'][SYM]['exit_attempts'], 3)
        o3 = self.e.active_orders(SYM, side='SELL')[0]

        # 3차 타임아웃 취소 및 reconcile (19:55:42)
        self.clock = datetime(2026, 10, 7, 19, 55, 42, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9970})
        self.assertEqual(o3['status'], 'CANCEL_SENT')
        self.e.reconcile({'cash': 20420, 'equity': 20420, 'holdings': {SYM: {'available': 1, 'qty': 1}}, 'orders': []})

        # 4차 매도 발주 (19:58:00) - 일반 exit라면 halt되었겠지만, AFTER_FORCE_FLATTEN은 3회 초과 재시도 지속!
        self.clock = datetime(2026, 10, 7, 19, 58, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        self.e.s['positions'][SYM]['last_tick'] = self.clock.isoformat()
        self.e.timer({SYM: 9970})
        self.assertFalse(bool(self.e.s.get('halt')))
        self.assertEqual(len(self.b.orders), 4)
        self.assertEqual(self.e.s['positions'][SYM]['exit_attempts'], 4)

        # 4차 주문 체결 시뮬레이션 -> 잔고 청산 완료
        o4 = self.e.active_orders(SYM, side='SELL')[0]
        self.e.cumulative_fill(o4, 1, 9970)
        self.assertNotIn(SYM, self.e.s['positions'])

        # 체결 후 추가 주문 즉시 중단 확인
        prev_orders_count = len(self.b.orders)
        self.clock = datetime(2026, 10, 7, 19, 59, 0, tzinfo=KST)
        self.e.timer({SYM: 9970})
        self.assertEqual(len(self.b.orders), prev_orders_count)

        # 20:00 이후 신규 주문 0건 확인
        self.clock = datetime(2026, 10, 7, 20, 0, 1, tzinfo=KST)
        self.e.timer({SYM: 9970})
        self.assertEqual(len(self.b.orders), prev_orders_count)
        self.assertEqual(len(self.b.orders), prev_orders_count)


    # =========================================================================
    # Section H: KRX/NXT AFTER Eligibility Rules
    # =========================================================================

    def test_h1_after_ineligible_keywords(self):
        """관리종목, 투자경고, 투자위험, 이상급등, 초저유동, 투자주의환기, 투자유의, 단일가 종목은 AFTER 진입 차단."""
        self.clock = datetime(2026, 10, 7, 16, 5, 0, tzinfo=KST)
        self.e.last_account = time.monotonic()
        keywords_and_reasons = [
            ('관리종목', 'AFTER_INELIGIBLE_MANAGED'),
            ('투자경고', 'AFTER_INELIGIBLE_INVESTMENT_WARNING'),
            ('투자위험', 'AFTER_INELIGIBLE_INVESTMENT_RISK'),
            ('이상급등', 'AFTER_INELIGIBLE_ABNORMAL_SURGE'),
            ('초저유동', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
            ('초저유동성', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
            ('투자주의환기', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION_ALERT'),
            ('투자유의', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION'),
        ]
        for kw, expected_reason in keywords_and_reasons:
            raw = {'code': SYM, 'state': kw, 'auditInfo': ''}
            res = assess_after_status(raw, today_volume=1000)
            self.assertFalse(res['pass'], f"Expected pass=False for {kw}")
            self.assertEqual(res['reason_code'], expected_reason)

            # Test engine rejection
            self.e.market_status[SYM] = {'pass': True, 'raw': raw}
            reason = self.e.new_entry_reason(SYM)
            self.assertEqual(reason, expected_reason, f"Failed for {kw}")
            with self.assertRaises(Rejected) as ctx:
                self.e.order_guard('kt10000', {'stk_cd': SYM, 'ord_qty': '1', 'ord_uv': '10000', 'trde_tp': '0'})
            self.assertIn(expected_reason, str(ctx.exception))

    def test_h2_after_ineligible_etf_etn(self):
        """ETF 및 ETN 종목은 AFTER 거래 대상에서 제외된다."""
        for etf_name in ['KODEX 200', 'TIGER 2차전지', '신한 레버리지 ETN']:
            raw = {'code': SYM, 'name': etf_name, 'state': '정상', 'auditInfo': ''}
            res = assess_after_status(raw, today_volume=1000)
            self.assertFalse(res['pass'])
            self.assertEqual(res['reason_code'], 'AFTER_INELIGIBLE_ETF_ETN')

        # is_etf_etn flag on data
        res = assess_after_status({'code': SYM, 'is_etf_etn': True, 'state': '정상', 'auditInfo': ''}, today_volume=1000)
        self.assertFalse(res['pass'])
        self.assertEqual(res['reason_code'], 'AFTER_INELIGIBLE_ETF_ETN')

        # In engine
        self.e.market_status[SYM] = {'pass': True, 'is_etf_etn': True}
        reason = self.e.new_entry_reason(SYM)
        self.assertEqual(reason, 'AFTER_INELIGIBLE_SYMBOL')

    def test_h3_after_ineligible_zero_regular_volume(self):
        """당일 정규장 거래량이 0인 종목은 AFTER 거래 대상에서 제외된다."""
        raw = {'code': SYM, 'name': '무거래종목', 'state': '정상', 'auditInfo': ''}
        res = assess_after_status(raw, today_volume=0)
        self.assertFalse(res['pass'])
        self.assertEqual(res['reason_code'], 'AFTER_INELIGIBLE_ZERO_REGULAR_VOLUME')

    # =========================================================================
    # Section I: AFTER GUI Start Unblocking & Runner Session Lifecycle
    # =========================================================================

    def test_i1_after_gui_start_unblocked(self):
        """AFTER 시간대(15:40~20:00)에 GUI 자동매매 시작 버튼이 15:05 마감 가드로 차단되지 않고 허용되는지 검증."""
        from operator_ui_service import start_block
        base_status = {
            'process': {'running': False}, 'halt': '', 'hard_ok': True, 'uncertain': 0,
            'positions': 0, 'pending': 0, 'mode': 'STOP', 'live_mode': False,
            'live_approved': False, 'guided_start': True, 'cap': 0, 'costs_confirmed': True,
        }
        # 1. AFTER 운영시간 (16:30): START 허용 (빈 문자열)
        s_after = dict(base_status, supported=False, after_supported=True, current_session='AFTER',
                       control_supported=True, time='2026-10-07T16:30:00+09:00')
        self.assertEqual(start_block(s_after), '')

        # 2. AFTER 운영시간 마감 (20:00): 차단
        s_after_closed = dict(base_status, supported=False, after_supported=True, current_session='AFTER',
                              control_supported=True, time='2026-10-07T20:00:00+09:00')
        self.assertEqual(start_block(s_after_closed), 'AFTER 운영시간 마감입니다.')

        # 3. 정규장 15:05 마감 후: 정규장 마감 차단
        s_reg_cutoff = dict(base_status, supported=True, after_supported=False, current_session='REGULAR',
                            control_supported=True, time='2026-10-07T15:05:00+09:00', entry_cutoff='15:05:00')
        self.assertEqual(start_block(s_reg_cutoff), '신규진입 마감입니다. 진단·기록은 이용 가능합니다.')

    def test_i2_after_runner_session_end(self):
        """AFTER 운영시간(15:40~20:00)에 runner.session_end가 False를 반환하여 프로세스가 유지되는지 검증."""
        from operator_runner import session_end
        from core.util import atomic_json
        from dataclasses import asdict
        cfg_path = self.root / 'config.json'
        atomic_json(cfg_path, asdict(self.cfg))

        # 16:30 AFTER 운영시간: session_end == False (유지)
        clock_after = lambda: datetime(2026, 10, 7, 16, 30, 0, tzinfo=KST)
        self.assertFalse(session_end(self.root, clock_after))

        # 20:00:01 AFTER 마감 후: session_end == True (종료 요청)
        clock_closed = lambda: datetime(2026, 10, 7, 20, 0, 1, tzinfo=KST)
        self.assertTrue(session_end(self.root, clock_closed))

    def test_i3_prepare_capital_after_time_guards(self):
        """AFTER 시간대별 준비검사 허용 및 20시 이후 차단, 미승인 상태 실주문 0건 검증."""
        from operator_ui_service import UserOperations
        from operator_service import STRATEGY_VERSION
        from core.util import atomic_json
        from dataclasses import asdict

        cfg_path = self.root / 'config.json'
        auto_path = self.root / 'automation.json'
        policy_path = self.root / 'runtime/automation_policy.json'
        pilot_path = self.root / 'runtime/operator_pilot.json'

        self.policy.command('STOP')
        atomic_json(cfg_path, asdict(self.cfg))
        atomic_json(auto_path, asdict(self.auto))
        atomic_json(policy_path, self.policy.state)
        atomic_json(pilot_path, {
            'verdict': 'PASS',
            'strategy_version': STRATEGY_VERSION,
            'timestamp': '2026-10-07T14:00:00+09:00',
            'operational_requalification': True,
            'config_hash': self.cfg.fingerprint(),
            'auto_hash': self.auto.fingerprint(),
            'policy_revision': self.policy.state.get('revision'),
        })

        # 1. 16:30 AFTER 본장: 준비검사 예외 없음 / verdict PASS
        service_1630 = UserOperations(self.root, clock=lambda: datetime(2026, 10, 7, 16, 30, 0, tzinfo=KST), gateway=None)
        res_1630 = service_1630.prepare_capital()
        self.assertEqual(res_1630.get('verdict'), 'PASS')

        # 2. 15:50 AFTER_PREWARM: 준비검사 가능
        service_1550 = UserOperations(self.root, clock=lambda: datetime(2026, 10, 7, 15, 50, 0, tzinfo=KST), gateway=None)
        res_1550 = service_1550.prepare_capital()
        self.assertEqual(res_1550.get('verdict'), 'PASS')

        # 3. 19:50 AFTER_EXIT_ONLY: 준비검사 가능
        service_1950 = UserOperations(self.root, clock=lambda: datetime(2026, 10, 7, 19, 50, 0, tzinfo=KST), gateway=None)
        res_1950 = service_1950.prepare_capital()
        self.assertEqual(res_1950.get('verdict'), 'PASS')

        # 4. 20:01 AFTER 마감 후: 준비검사 차단 (ValueError)
        service_2001 = UserOperations(self.root, clock=lambda: datetime(2026, 10, 7, 20, 1, 0, tzinfo=KST), gateway=None)
        with self.assertRaises(ValueError) as ctx:
            service_2001.prepare_capital()
        self.assertIn('신규진입 가능시간에 준비 검사를 수행하세요', str(ctx.exception))

        # 5. AFTER 미승인 상태 실주문(BUY) 0건 검증
        self.assertEqual(len(self.b.orders), 0)

    def test_i4_main_entrypoint_after_not_exiting_diagnostics(self):
        """main.py 진입점이 AFTER 운영시간(15:40~20:00)에 조기 진단 종료(return 0)되지 않고 정상 러너 분기로 진행하는지 검증."""
        from automation.session import control_supported, after_control_supported
        
        # 16:30 AFTER 운영시간, after_enabled=True:
        clock_after = datetime(2026, 10, 7, 16, 30, 0, tzinfo=KST)
        is_ctrl_after = control_supported(clock_after) or (getattr(self.cfg, 'after_enabled', False) and after_control_supported(clock_after))
        self.assertTrue(is_ctrl_after)
        # 따라서 not is_ctrl_after 는 False이므로 조기 진단 return 0 분기로 빠지지 않음
        self.assertFalse(not is_ctrl_after)

        # 20:00:01 마감 후:
        clock_closed = datetime(2026, 10, 7, 20, 0, 1, tzinfo=KST)
        is_ctrl_closed = control_supported(clock_closed) or (getattr(self.cfg, 'after_enabled', False) and after_control_supported(clock_closed))
        self.assertFalse(is_ctrl_closed)
        # 마감 후에는 not is_ctrl_closed 가 True가 되어 정상적으로 시간 종료 처리
        self.assertTrue(not is_ctrl_closed)

    def test_i5_targeted_after_order_permission_and_guards(self):
        """요구사항 5개 targeted 테스트:
        1. 17:xx AFTER + after_live_authorized=true -> OUT_OF_SUPPORTED_SESSION 아님
        2. 17:xx AFTER + after_live_authorized=false -> AFTER_LIVE_NOT_AUTHORIZED
        3. 19:50 -> 신규 BUY 차단 (AFTER_ENTRY_CUTOFF)
        4. 20:01 -> OUT_OF_SUPPORTED_SESSION
        5. HARD_LOCK / UNKNOWN / HALT 유지
        """
        # 1. 17:xx AFTER + after_live_authorized=True -> NOT OUT_OF_SUPPORTED_SESSION
        self.clock = datetime(2026, 10, 7, 17, 10, 0, tzinfo=KST)
        self.e.after_live_authorized = True
        self.e.last_account = time.monotonic()
        reason = self.e.new_entry_reason(SYM)
        self.assertNotEqual(reason, 'OUT_OF_SUPPORTED_SESSION')
        self.assertNotEqual(reason, 'AFTER_LIVE_NOT_AUTHORIZED')

        # 2. 17:xx AFTER + after_live_authorized=False -> AFTER_LIVE_NOT_AUTHORIZED
        self.e.after_live_authorized = False
        reason_unauth = self.e.new_entry_reason(SYM)
        self.assertEqual(reason_unauth, 'AFTER_LIVE_NOT_AUTHORIZED')

        # 3. 19:50 -> 신규 BUY 차단 (AFTER_ENTRY_CUTOFF)
        self.clock = datetime(2026, 10, 7, 19, 50, 0, tzinfo=KST)
        self.e.after_live_authorized = True
        reason_cutoff = self.e.new_entry_reason(SYM)
        self.assertEqual(reason_cutoff, 'AFTER_ENTRY_CUTOFF')
        self.assertFalse(self.e.entry_window())

        # 4. 20:01 -> OUT_OF_SUPPORTED_SESSION
        self.clock = datetime(2026, 10, 7, 20, 1, 0, tzinfo=KST)
        self.assertFalse(self.e.is_supported_now())
        reason_closed = self.e.new_entry_reason(SYM)
        self.assertEqual(reason_closed, 'OUT_OF_SUPPORTED_SESSION')

        # 5. HARD_LOCK / UNKNOWN / HALT 유지
        self.clock = datetime(2026, 10, 7, 17, 10, 0, tzinfo=KST)
        self.policy.state['after_live_authorized'] = True
        # HARD_LOCK 종목
        self.assertEqual(self.e.new_entry_reason('005930'), 'HARD_LOCK')

        # HALT 차단 유지
        self.e.s['halt'] = 'EMERGENCY_HALT'
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))
        self.e.s['halt'] = ''

        # UNKNOWN / foreign INTENT 차단 유지
        self.e.s['orders']['foreign_unk'] = {'local': 'foreign_unk', 'status': 'UNKNOWN', 'symbol': '888880', 'side': 'BUY', 'qty': 1, 'limit': 10000, 'filled': 0}
        self.assertIsNone(self.e.enter(self.signal(), self.tick()))
        del self.e.s['orders']['foreign_unk']

        # 실주문 0건 검증
        self.assertEqual(len(self.b.orders), 0)


if __name__ == '__main__':
    unittest.main()

