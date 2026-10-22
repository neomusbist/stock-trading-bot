"""R6 FAST LIVE full-path tests.

Every scenario starts from fake WebSocket frames handed to the production
``automation.runner.process_message`` (candidate lookup -> tick parse ->
detector/FAST ingestion -> FAST evaluation -> structure/liquidity checks ->
``AutonomousEngine.enter`` -> order guard -> broker interface).  The broker is
the in-repo ``FakeBroker`` behind the real ``order_guard``; no network or real
order API is touched.  Synthetic ticks prove plumbing and safety, not profit.
"""
import contextlib
import copy
import io
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from core.broker import AmbiguousOrder
from core.settings import Config, KST, ROOT
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy, load_automation
from automation.runner import process_message
from tests.test_automation import FakeBroker
from tests.test_v1 import DAY, bars

SYM = '999990'
SYM2 = '999980'
CAUSAL = [984, 988, 992, 996, 1000, 990, 992, 994, 996, 998, 1001]
PRODUCTION_LIKE = dict(confirmed_invalidation_enabled=True, after_momentum_failure_enabled=True,
                       structural_pullback_enabled=True, structural_breakout_enabled=True,
                       fresh_box_basis_enabled=True, confirmed_structure_protection=True)


class PipelineMixin:
    AUTO_FLAGS = {}
    MAX_POSITIONS = 1

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        out = contextlib.redirect_stdout(io.StringIO()); out.__enter__(); self.addCleanup(out.__exit__, None, None, None)
        self.clock = [datetime.combine(DAY, dtime(10, 30), KST)]
        self.cfg = replace(Config(), live_mode=True, risk_fraction=.02, max_daily_loss_fraction=.04,
                           max_positions=self.MAX_POSITIONS)
        self.cum = {}
        self.build()

    def build(self, enabled=True, cash=81300, mode='LIVE', **auto_overrides):
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True,
                            fast_momentum_enabled=enabled, **{**self.AUTO_FLAGS, **auto_overrides})
        self.policy = Policy(self.root / f'policy_{mode}_{enabled}_{cash}.json')
        if mode == 'LIVE':
            self.policy.command('LIVE', confirmation='LIVE')
        self.broker = FakeBroker()
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy, self.root / f'book_{mode}_{enabled}_{cash}',
                                  self.broker, lambda: self.clock[0])
        self.snap = {'cash': cash, 'equity': cash, 'orders': [], 'holdings': {}}
        self.e.reconcile(self.snap); self.e.connected = True; self.e.market = '3'
        self.e.last_account = time.monotonic()
        for s in (SYM, SYM2):
            self.e.market_status[s] = {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'}
        self.detector = PatternDetector(self.cfg, self.auto, self.e.costs)
        self.detector.continuous_observation = True
        self.detector.fast_strategy = self.e.fast_strategy
        self.watch = {s: self.make_watch(s) for s in (SYM, SYM2)}
        self.radar = Mock(); self.radar.cache = {}
        self.set_history()
        self.symbols = {SYM, SYM2}; self.prices = {}; self.account = Mock()

    def make_watch(self, sym, **extra):
        w = {'code': sym, 'name': '합성', 'status': 'WATCH', 'source_count': 2, 'fallback_source1': True,
             'source_details': {'a': {}, 'b': {}}, 'market_status': {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'},
             'resistance': 1045, 'reference_price': 984, 'observed_at': self.clock[0].isoformat()}
        w['nearest_confirmed_peak']=dict(price=1045,time=(self.clock[0]-timedelta(minutes=1)).isoformat(),
            confirmed_at=self.clock[0].isoformat(),available_at=self.clock[0].isoformat(),
            basis='SYNTHETIC_PREKNOWN_INTRADAY_LEVEL')
        w.update(extra); return w

    def set_history(self, low=880, high=950, close=900, volume=2000000, symbols=(SYM, SYM2)):
        for s in symbols:
            history = bars()
            for b in history: b.update(open=close, high=high, low=low, close=close, volume=volume)
            self.radar.cache[(self.clock[0].date(), s)] = history

    # -- fake WebSocket ------------------------------------------------------
    def frame(self, price, sym=SYM, vol=1000, ask=None, bid=None, ask_qty=10000, bid_qty=10000, depth=True):
        self.cum[sym] = self.cum.get(sym, 0) + vol
        t = self.clock[0]
        values = {'20': t.strftime('%H%M%S'), '10': str(price), '15': str(vol), '13': str(self.cum[sym]),
                  '27': str(price if ask is None else ask), '28': str(price - 1 if bid is None else bid), '290': '2'}
        if depth: values.update({'61': str(ask_qty), '71': str(bid_qty)})
        return {'trnm': 'REAL', 'data': [{'type': '0B', 'item': sym, 'values': values}]}

    def feed(self, message, received=None):
        received = received or self.clock[0]
        with patch('automation.runner.now', return_value=self.clock[0]):
            return process_message(self.e, self.detector, message, received, self.symbols, self.watch, self.radar,
                                   self.prices, self.account, True)

    def series(self, prices=CAUSAL, sym=SYM, **kw):
        for p in prices:
            self.feed(self.frame(p, sym=sym, **kw)); self.clock[0] += timedelta(seconds=1)

    def fill_frame(self, order, filled, amount):
        buy = order['side'] == 'BUY'
        values = {'9203': order['id'], '9001': order['symbol'], '907': '2' if buy else '1', '913': '체결',
                  '905': '매수' if buy else '매도', '900': str(order['qty']), '902': str(order['qty'] - filled),
                  '903': str(amount)}
        return {'trnm': 'REAL', 'data': [{'type': '00', 'values': values}]}

    def orders(self, side): return [o for o in self.broker.orders if o[0] == side]
    def buy_order(self): return self.e.active_orders(SYM, 'BUY')[0] if self.e.active_orders(SYM, 'BUY') else None

    def enter_and_fill(self, partial=None):
        self.series()
        self.assertEqual(len(self.orders('BUY')), 1, self.e.fast_strategy.last_evaluation.get(SYM))
        order = next(o for o in self.e.s['orders'].values() if o['side'] == 'BUY')
        filled = partial or order['qty']
        self.e.actual = {SYM: {'qty': filled, 'available': filled, 'avg': order['limit']}}
        self.feed(self.fill_frame(order, filled, filled * order['limit']))
        return order

    # -- 1. activation switch ----------------------------------------------------
    def test_default_is_off_and_example_config_is_off(self):
        self.assertFalse(AutomationConfig().fast_momentum_enabled)
        self.assertFalse(load_automation(ROOT / 'automation.example.json').fast_momentum_enabled)

    def test_disabled_flag_blocks_fast_evaluation_ingestion_and_orders(self):
        self.build(enabled=False)
        self.series()
        self.assertEqual(self.broker.orders, [])
        self.assertEqual(self.e.fast_strategy.tapes, {})

    def test_enabled_flag_without_live_authority_never_orders(self):
        self.build(enabled=True, mode='ARMED')
        self.series()
        self.assertEqual(self.broker.orders, [])
        sig = self.e.fast_strategy.active_signals.get(SYM)
        self.assertTrue(sig is None or sig.extra['SHADOW_ONLY'])

    # -- 2. full path ---------------------------------------------------------------
    def test_ws_to_buy_partial_fill_position_structural_exit_and_sell(self):
        self.series()
        buys = self.orders('BUY')
        self.assertEqual(len(buys), 1, self.e.fast_strategy.last_evaluation.get(SYM))
        side, sym, qty, limit = buys[0]
        self.assertEqual((side, sym), ('BUY', SYM)); self.assertGreater(qty, 0)
        order = next(o for o in self.e.s['orders'].values() if o['side'] == 'BUY')
        self.assertEqual(order['signal']['pattern'], 'FAST'); self.assertEqual(order['status'], 'ACCEPTED')
        # 2% per-trade budget (1,626 KRW on 81,300) bounds price risk of the actual order
        self.assertLessEqual(qty * (limit - order['signal']['stop']), 1626)
        # partial fill creates the position with only filled shares; remainder stays reserved once
        self.assertGreater(qty, 2)
        self.e.actual = {SYM: {'qty': 2, 'available': 2, 'avg': limit}}
        self.feed(self.fill_frame(order, 2, 2 * limit))
        self.assertEqual(self.e.s['positions'][SYM]['qty'], 2); self.assertEqual(self.e.s['positions'][SYM]['pattern'], 'FAST')
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertAlmostEqual(self.e.pending_buy_reservation(), (qty - 2) * limit * (1 + self.auto.commission_buy), places=2)
        # no second BUY while the first is open, even with more qualifying ticks
        self.series([1002, 1002]); self.assertEqual(len(self.orders('BUY')), 1)
        # complete the BUY
        self.e.actual = {SYM: {'qty': qty, 'available': qty, 'avg': limit}}
        self.feed(self.fill_frame(order, qty, qty * limit))
        pos = self.e.s['positions'][SYM]; self.assertEqual(pos['qty'], qty)
        self.assertEqual(self.e.pending_buy_reservation(), 0)
        # structural failure: two consecutive trades through the invalidation line
        stop = pos['initial_stop']
        self.series([stop - 1, stop - 2], bid=None)
        sells = self.orders('SELL')
        self.assertEqual(len(sells), 1, pos.get('exit_reason')); self.assertEqual(sells[0][1:3], (SYM, qty))
        # duplicate sell guard: more breaking ticks do not create another SELL
        self.series([stop - 3, stop - 4]); self.assertEqual(len(self.orders('SELL')), 1)
        sell = next(o for o in self.e.s['orders'].values() if o['side'] == 'SELL')
        self.feed(self.fill_frame(sell, qty, qty * (stop - 3)))
        self.assertNotIn(SYM, self.e.s['positions']); self.assertEqual(len(self.e.s['trades']), 1)
        self.assertIn(SYM, self.e.fast_strategy.last_exit_by_symbol)

    def test_emergency_plunge_sells_through_broker_interface(self):
        order = self.enter_and_fill()
        pos = self.e.s['positions'][SYM]
        self.clock[0] += timedelta(seconds=1)
        self.feed(self.frame(pos['emergency'] - 5, bid=pos['emergency'] - 6, ask=pos['emergency'] - 5))
        self.assertEqual(len(self.orders('SELL')), 1); self.assertTrue(pos['exit_requested'])

    def test_partial_buy_exit_waits_for_cancel_then_sells_only_filled_shares(self):
        order = self.enter_and_fill(partial=2)
        pos = self.e.s['positions'][SYM]
        self.clock[0] += timedelta(seconds=1)
        self.feed(self.frame(pos['emergency'] - 5, bid=pos['emergency'] - 6, ask=pos['emergency'] - 5))
        self.assertEqual(self.orders('SELL'), []); self.assertEqual(order['status'], 'CANCEL_SENT')
        self.e.actual = {SYM: {'qty': 2, 'available': 2, 'avg': order['limit']}}
        self.e.reconcile({**self.snap, 'holdings': copy.deepcopy(self.e.actual)}); self.e.drive_exits()
        self.assertEqual([o[2] for o in self.orders('SELL')], [2])

    # -- 3. failures ---------------------------------------------------------------
    def test_duplicate_buy_blocked_while_order_open_and_after_position(self):
        self.enter_and_fill()
        self.series([1002, 1003, 1003]); self.assertEqual(len(self.orders('BUY')), 1)

    def test_ws_disconnect_resets_fast_tape_and_blocks_until_reconnect_and_account(self):
        self.series(CAUSAL[:8])
        self.assertIn(SYM, self.e.fast_strategy.tapes)
        self.feed({'trnm': 'DISCONNECTED', 'reason': 'synthetic'})
        self.assertFalse(self.e.connected); self.assertNotIn(SYM, self.e.fast_strategy.tapes)
        self.series(CAUSAL[8:]); self.assertEqual(self.broker.orders, [])
        self.feed({'trnm': 'REG', 'return_code': 0})
        self.assertTrue(self.e.connected); self.assertFalse(self.e.account_ok)
        self.series(CAUSAL); self.assertEqual(self.broker.orders, [])

    def test_expired_account_blocks_buy(self):
        self.e.last_account = time.monotonic() - 10000
        self.series(); self.assertEqual(self.broker.orders, [])

    def test_order_readiness_off_blocks_buy(self):
        self.e.readiness_reason = lambda: 'WS_TRANSPORT_NOT_READY'
        self.series(); self.assertEqual(self.broker.orders, [])

    def test_unknown_order_latches_halt_and_blocks_more_orders(self):
        with patch.object(self.broker, 'submit', side_effect=AmbiguousOrder('SYNTHETIC_UNKNOWN')) as submit:
            self.series([*CAUSAL, 1002, 1002, 1003])
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(next(iter(self.e.s['orders'].values()))['status'], 'UNKNOWN'); self.assertTrue(self.e.s['halt'])

    # -- 4. mandatory safety checks stay alive ------------------------------------------------
    def test_trading_halt_status_blocks(self):
        self.watch[SYM]['market_status'] = {'pass': False, 'reason_code': 'TRADING_HALTED'}
        self.series(); self.assertEqual(self.broker.orders, [])

    def test_vi_or_single_price_auction_blocks(self):
        for key in ('vi_active', 'single_price_auction'):
            with self.subTest(key=key):
                self.setUp(); self.watch[SYM][key] = True
                self.series(); self.assertEqual(self.broker.orders, [])

    def test_non_watch_status_blocks(self):
        self.watch[SYM]['status'] = 'RAW'; self.series(); self.assertEqual(self.broker.orders, [])

    def test_stale_current_tick_blocks(self):
        for p in CAUSAL[:-1]:
            self.feed(self.frame(p)); self.clock[0] += timedelta(seconds=1)
        late = self.frame(CAUSAL[-1]); self.clock[0] += timedelta(seconds=9)
        self.feed(late); self.assertEqual(self.broker.orders, [])

    def test_missing_or_thin_depth_blocks(self):
        self.series(CAUSAL[:-1]); self.feed(self.frame(CAUSAL[-1], depth=False))
        self.assertEqual(self.broker.orders, [])
        self.setUp(); self.series(CAUSAL[:-1]); self.feed(self.frame(CAUSAL[-1], ask_qty=3))
        self.assertEqual(self.broker.orders, [])

    def test_daily_history_and_liquidity_checks_are_not_bypassed(self):
        self.radar.cache.clear(); self.series(); self.assertEqual(self.broker.orders, [], 'no history')
        self.setUp(); self.set_history(volume=10); self.series(); self.assertEqual(self.broker.orders, [], 'thin daily turnover')

    def test_insufficient_cash_blocks(self):
        self.build(cash=500); self.series(); self.assertEqual(self.broker.orders, [])

    def test_unknown_or_low_reward_target_blocks(self):
        for resistance in (None, 1005):
            with self.subTest(resistance=resistance):
                self.setUp(); self.watch[SYM]['resistance'] = resistance
                self.series(); self.assertEqual(self.broker.orders, [])

    def test_daily_four_percent_risk_exhausted_blocks(self):
        self.e.s['daily_loss'] = 3250.0
        self.series(); self.assertEqual(self.broker.orders, [])

    def test_single_holding_limit_even_if_config_allows_two(self):
        self.MAX_POSITIONS = 2
        self.cfg = replace(self.cfg, max_positions=2); self.build()
        self.enter_and_fill()
        self.series(CAUSAL, sym=SYM2); self.assertEqual(len(self.orders('BUY')), 1)

    def test_single_holding_limit_blocks_second_symbol(self):
        self.enter_and_fill()
        self.series(CAUSAL, sym=SYM2); self.assertEqual(len(self.orders('BUY')), 1)

    def test_after_hours_fast_is_never_live(self):
        self.clock[0] = datetime.combine(DAY, dtime(16, 30), KST)
        self.build(); self.set_history(); self.watch[SYM]['observed_at'] = self.clock[0].isoformat()
        self.series(); self.assertEqual(self.broker.orders, [])


class FastPipelineDefaultFlagsTests(PipelineMixin, unittest.TestCase):
    pass


class FastPipelineProductionLikeFlagsTests(PipelineMixin, unittest.TestCase):
    AUTO_FLAGS = PRODUCTION_LIKE


if __name__ == '__main__':
    unittest.main()
