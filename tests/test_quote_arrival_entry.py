"""Real production paths with a FakeBroker; quote arrival never invents trades."""
from dataclasses import replace
from datetime import timedelta
from queue import Queue
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from automation import runner
from core.realtime import parse_book, parse_tick
from tests import test_r6_fast_live_pipeline as fast_pipeline
CAUSAL, SYM = fast_pipeline.CAUSAL, fast_pipeline.SYM


class QuoteArrivalEntryTests(unittest.TestCase):
    def setUp(self):
        self.h = fast_pipeline.FastPipelineProductionLikeFlagsTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.feed = SimpleNamespace(q=Queue(), latest_ticks={},
            transport_failed=False, overflow=SimpleNamespace(is_set=lambda: False))
        self.books = {}
        self.feed.orderbooks = self.books
        self.h.e.entry_quote_provider = lambda sym: (self.feed.latest_ticks.get(sym), self.books.get(sym, {}))
        self.h.e._last_eval_frame = {}

    def trade(self, price, *, depth=True):
        message = self.h.frame(price, depth=depth)
        item = message['data'][0]
        tick = parse_tick(item, self.h.clock[0])
        self.feed.latest_ticks[SYM] = tick
        if depth:
            self.books[SYM] = self.book(tick)
        self.h.feed(message)
        return tick

    def book(self, tick, *, at=None, received=None, ask=None, qty=10000):
        at = at or self.h.clock[0]
        received = received or at
        return parse_book({'values': {'21': at.strftime('%H%M%S'),
            '41': str(tick.ask if ask is None else ask), '51': str(tick.bid),
            '61': str(qty), '71': str(qty)}}, received)

    def seed(self, *, after=False):
        if after:
            self.h.clock[0] = self.h.clock[0].replace(hour=16, minute=30)
            self.h.watch[SYM]['venue'] = 'SOR'
            self.h.watch[SYM]['observed_at'] = self.h.clock[0].isoformat()
            self.h.cfg = replace(self.h.cfg, after_enabled=True)
            self.h.e.c = self.h.cfg
            self.h.e.after_enabled = True
            self.h.e.after_live_authorized = True  # isolated FakeBroker policy only
        for price in CAUSAL[:-1]:
            self.trade(price)
            self.h.clock[0] += timedelta(seconds=1)
        tick = self.trade(CAUSAL[-1], depth=False)
        self.assertEqual(self.h.orders('BUY'), [])
        self.assertIn(self.h.e.fast_strategy.last_evaluation[SYM]['reason'],
                      {'EXECUTABLE_DEPTH_UNKNOWN', 'BOOK_TIMESTAMP_UNKNOWN_OR_STALE',
                       'EXECUTABLE_ASK_NOT_ABOVE_BREAKOUT'})
        return tick

    def send_book(self, tick, **kwargs):
        self.h.clock[0] += timedelta(milliseconds=100)
        book = self.book(tick, **kwargs)
        self.books[SYM] = book
        self.h.feed({'trnm': 'REAL', 'data': [{'type': '0D', 'item': SYM, '_book': book}]})
        self.drain()
        return book

    def drain(self):
        # On the original code the runtime has no book retry stage at all.
        drain = getattr(runner, 'drain_quote_opportunities', None)
        if drain is not None:
            drain(self.h.e, self.h.detector, self.feed, self.h.watch, self.h.radar,
                  self.h.prices, self.h.account, True)

    def test_later_matching_book_reaches_real_intent_and_fake_buy(self):
        tick = self.seed()
        self.send_book(tick)
        self.assertEqual(len(self.h.orders('BUY')), 1)
        order = self.h.buy_order()
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertEqual(order['signal']['pattern'], 'FAST')
        self.assertEqual(order['qty'], order['signal']['planned_qty'])

    def test_unchanged_quote_values_do_not_repeat_same_trade_evaluation(self):
        tick=self.seed()
        with patch.object(runner,'evaluate_actual_entry',return_value=None) as evaluate:
            self.send_book(tick)
            self.assertEqual(evaluate.call_count,1)
            self.send_book(tick)
            self.assertEqual(evaluate.call_count,1)
            self.send_book(tick,qty=20000)
            self.assertEqual(evaluate.call_count,2)
        self.assertEqual(self.h.orders('BUY'),[])

    def test_after_session_later_book_reaches_same_sor_order_boundary(self):
        tick = self.seed(after=True)
        self.send_book(tick)
        self.assertEqual(len(self.h.orders('BUY')), 1, (self.h.e.new_entry_reason(SYM),
            self.h.e.fast_strategy.last_evaluation[SYM], self.h.e.decisions.last))
        self.assertEqual(self.h.buy_order()['signal']['session'], 'AFTER')
        self.assertEqual(self.h.buy_order()['signal']['venue'], 'SOR')

    def test_already_received_book_is_used_without_waiting_for_another_trade(self):
        for price in CAUSAL[:-1]:
            self.trade(price)
            self.h.clock[0] += timedelta(seconds=1)
        message = self.h.frame(CAUSAL[-1], depth=False)
        tick = parse_tick(message['data'][0], self.h.clock[0])
        self.feed.latest_ticks[SYM] = tick
        self.books[SYM] = self.book(tick)
        self.h.feed(message)
        self.assertEqual(len(self.h.orders('BUY')), 1)

    def test_quote_does_not_add_volume_or_completed_bars_or_renew_trade_time(self):
        tick = self.seed()
        tape = self.h.e.fast_strategy.get_tape(SYM)
        initial = (len(tape.ticks), sum(r[2] for r in tape.ticks),
                   len(self.h.detector.history.get(SYM, ())), self.h.detector.last[SYM])
        self.send_book(tick)
        self.assertEqual((len(tape.ticks), sum(r[2] for r in tape.ticks),
            len(self.h.detector.history.get(SYM, ())), self.h.detector.last[SYM]), initial)
        self.assertEqual(len(self.h.orders('BUY')), 1)
        self.assertEqual(self.h.buy_order()['signal']['fast_entry_evidence']['metrics']['latest_time'], tick.time.isoformat())

    def test_old_trade_is_not_revived_by_a_fresh_book(self):
        tick = self.seed()
        self.h.clock[0] += timedelta(seconds=6)
        self.send_book(tick)
        self.assertEqual(self.h.orders('BUY'), [])

    def test_after_approval_is_still_required(self):
        tick = self.seed(after=True)
        self.h.e.after_live_authorized = False
        self.send_book(tick)
        self.assertEqual(self.h.orders('BUY'), [])

    def test_quote_retry_keeps_vi_and_daily_risk_gates(self):
        tick = self.seed()
        self.h.watch[SYM]['vi_active'] = True
        self.send_book(tick)
        self.assertEqual(self.h.orders('BUY'), [])
        self.h.watch[SYM]['vi_active'] = False
        self.h.e.s['daily_loss'] = self.h.e.daily_risk_budget()
        self.send_book(tick)
        self.assertEqual(self.h.orders('BUY'), [])

    def test_nonwatch_book_stays_off_fifo_but_can_promote_through_normal_focus(self):
        tick = self.seed()
        candidate = {**self.h.watch.pop(SYM), 'status': 'ELIGIBLE'}
        self.h.e._last_shadow_eval_frame = {SYM: (tick.time, tick.cumulative)}
        self.h.clock[0] += timedelta(milliseconds=100)
        self.books[SYM] = self.book(tick)
        queue_books = getattr(runner, 'queue_current_quote_books', None)
        drain = getattr(runner, 'drain_quote_opportunities', None)
        if queue_books is not None and drain is not None:
            queue_books(self.h.e, self.h.detector, self.feed, {SYM})
            drain(self.h.e, self.h.detector, self.feed, self.h.watch, self.h.radar,
                  self.h.prices, self.h.account, True, by_symbol={SYM: candidate})
        self.assertEqual(len(self.h.orders('BUY')), 1)
        self.assertEqual(self.h.watch[SYM]['status'], 'WATCH')
        self.assertTrue(self.feed.q.empty())

    def test_stale_book_remains_blocked(self):
        tick = self.seed()
        self.send_book(tick, at=self.h.clock[0]-timedelta(seconds=6))
        self.assertEqual(self.h.orders('BUY'), [])

    def test_future_or_nonexecutable_changed_book_remains_blocked(self):
        tick = self.seed()
        self.send_book(tick, at=self.h.clock[0]+timedelta(seconds=10))
        self.send_book(tick, ask=tick.ask+20)
        self.assertEqual(self.h.orders('BUY'), [])

    def test_zero_or_insufficient_depth_does_not_bypass_quantity_checks(self):
        tick = self.seed()
        self.send_book(tick, qty=0)
        self.send_book(tick, qty=1)
        self.assertEqual(self.h.orders('BUY'), [])

    def test_control_frame_precedes_quote_retry_and_duplicate_book_cannot_order_twice(self):
        tick = self.seed()
        self.feed.q.put((self.h.clock[0], {'trnm': 'DISCONNECTED'}))
        self.send_book(tick)
        self.assertEqual(self.h.orders('BUY'), [])
        self.feed.q.get_nowait()
        self.drain()
        self.assertEqual(len(self.h.orders('BUY')), 1)
        self.send_book(tick)
        self.drain()
        self.assertEqual(len(self.h.orders('BUY')), 1)

    def test_late_quote_buy_fill_and_emergency_sell_remain_accounted(self):
        tick = self.seed()
        self.send_book(tick)
        self.assertEqual(len(self.h.orders('BUY')), 1)
        order = self.h.buy_order()
        self.h.e.actual = {SYM: {'qty': order['qty'], 'available': order['qty'], 'avg': order['limit']}}
        self.h.feed(self.h.fill_frame(order, order['qty'], order['qty']*order['limit']))
        self.assertEqual(self.h.e.s['positions'][SYM]['qty'], order['qty'])
        self.h.clock[0] += timedelta(seconds=1)
        self.trade(order['signal']['emergency']-1)
        self.assertEqual(len(self.h.orders('SELL')), 1)
        sell = self.h.e.active_orders(SYM, 'SELL')[0]
        self.h.e.actual = {}
        self.h.feed(self.h.fill_frame(sell, sell['qty'], sell['qty']*(order['signal']['emergency']-2)))
        self.assertEqual(self.h.e.s['positions'], {})
        self.assertEqual(self.h.e.active_orders(), [])


class ABCQuoteArrivalTests(unittest.TestCase):
    def lifecycle(self, setup, hour=16):
        from tests import test_live_ready_20261021 as original
        from core.util import tick_size
        cfg, auto = original.settings()
        clock = original.AT.replace(hour=hour, minute=30)
        if hour!=16:
            auto=replace(auto,fast_momentum_enabled=False)
        with patch.object(original, 'AT', clock), patch.object(original, 'settings',
                return_value=(replace(cfg, after_enabled=True), auto)):
            h = original.LiveReadyLifecycle('runTest')
            h.setUp(); self.addCleanup(h.doCleanups)
            h.e.after_live_authorized = True  # isolated test policy only
            normal_feed = h.feed
            final_price = {'A': 10040, 'B': 1041, 'C': 10160}[setup]

            def quote_missing(at, price, volume=100, **kwargs):
                if at < clock or price != final_price:
                    return normal_feed(at, price, volume, **kwargs)
                h.clock = at; h.cumulative += volume
                item = {'type': '0B', 'item': original.SYM, 'values': {
                    '20': at.strftime('%H%M%S'), '10': str(price), '15': str(volume),
                    '13': str(h.cumulative), '27': str(price),
                    '28': str(price-tick_size(price)) if hour==16 else '0', '290': '2'}}
                h.latest = parse_tick(item, at); h.book = {}
                h.e.last_account = time.monotonic()
                return runner.process_message(h.e, h.detector, {'trnm': 'REAL', 'data': [item]},
                    at, {original.SYM}, h.lookup, h.radar, h.prices, h.account, True)

            h.feed = quote_missing
            h.seed(setup)
            self.assertEqual(h.broker.orders, [])
            h.clock += timedelta(milliseconds=100)
            h.book = parse_book({'values': {'21': h.clock.strftime('%H%M%S'),
                '41': str(h.latest.ask), '51': str(h.latest.price-tick_size(h.latest.price)), '61': '1000000', '71': '1000000'}}, h.clock)
            feed = SimpleNamespace(q=Queue(), latest_ticks={original.SYM: h.latest},
                transport_failed=False, overflow=SimpleNamespace(is_set=lambda: False))
            runner.process_message(h.e, h.detector, {'trnm': 'REAL', 'data': [
                {'type': '0D', 'item': original.SYM, '_book': h.book}]}, h.clock,
                {original.SYM}, h.lookup, h.radar, h.prices, h.account, True)
            drain = getattr(runner, 'drain_quote_opportunities', None)
            if drain is not None:
                drain(h.e, h.detector, feed, h.lookup, h.radar, h.prices, h.account, True)
            buys = [o for o in h.e.s['orders'].values() if o['side'] == 'BUY']
            self.assertEqual(len(buys), 1, h.events[-5:])
            buy = buys[0]; self.assertEqual(buy['signal']['pattern'], setup)
            h.fill(buy, buy['qty'], buy['limit'])
            self.assertEqual(h.e.s['positions'][original.SYM]['qty'], buy['qty'])
            pos = h.e.s['positions'][original.SYM]
            hard = (pos['signal'].get('invalidation_policy') or {}).get('emergency_stop') or pos.get('emergency') or pos['stop']
            sell_price = hard-tick_size(hard)
            normal_feed(h.clock+timedelta(seconds=1), sell_price)
            sells = [o for o in h.e.s['orders'].values() if o['side'] == 'SELL']
            self.assertEqual(len(sells), 1, h.events[-5:])
            h.fill(sells[0], buy['qty'], sell_price)
            self.assertEqual(h.e.s['positions'], {})
            self.assertEqual(h.e.active_orders(), [])
            self.assertEqual([x['side'] for x in h.broker.intent_checks], ['BUY', 'SELL'])

    def test_A_after_late_book_order_fill_exit(self): self.lifecycle('A')
    def test_B_after_late_book_order_fill_exit(self): self.lifecycle('B')
    def test_C_after_late_book_order_fill_exit(self): self.lifecycle('C')

    def test_A_regular_late_book_order_fill_exit(self): self.lifecycle('A', hour=10)
    def test_B_regular_late_book_order_fill_exit(self): self.lifecycle('B', hour=10)
    def test_C_regular_late_book_order_fill_exit(self): self.lifecycle('C', hour=10)


if __name__ == '__main__':
    unittest.main()
