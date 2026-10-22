"""Offline regressions; synthetic frames prove delivery, never market returns."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from automation import runner
from automation.fast_momentum import FastMomentumStrategy
from core.realtime import Feed, parse_book, parse_tick
from tests import test_r6_fast_live_pipeline as pipeline
from tests import test_quote_arrival_entry as quote_tests

SYM = pipeline.SYM
MEASUREMENTS = []


class CurrentBookTests(unittest.TestCase):
    def setUp(self):
        self.h = quote_tests.QuoteArrivalEntryTests('runTest')
        self.h.setUp(); self.addCleanup(self.h.doCleanups)
        # Known before the first print; enough net headroom at the new ask.
        self.h.h.watch[SYM]['resistance'] = 1060

    def test_changed_fresh_bbo_reaches_intent_fill_and_protection(self):
        h = self.h; tick = h.seed()
        tape = h.h.e.fast_strategy.get_tape(SYM)
        before = (len(tape.ticks), sum(r[2] for r in tape.ticks), h.h.detector.last[SYM])
        h.send_book(tick, ask=tick.ask + 1)
        self.assertEqual(len(h.h.orders('BUY')), 1, h.h.e.fast_strategy.last_evaluation[SYM])
        order = h.h.buy_order()
        self.assertEqual(order['limit'], tick.ask + 1)
        self.assertEqual((len(tape.ticks), sum(r[2] for r in tape.ticks), h.h.detector.last[SYM]), before)
        h.h.e.actual = {SYM: {'qty': order['qty'], 'available': order['qty'], 'avg': order['limit']}}
        h.h.feed(h.h.fill_frame(order, order['qty'], order['qty'] * order['limit']))
        h.h.clock[0] += timedelta(seconds=1)
        h.trade(order['signal']['emergency'] - 1)
        self.assertEqual(len(h.h.orders('SELL')), 1)
        sell = h.h.e.active_orders(SYM, 'SELL')[0]
        h.h.e.actual = {}
        h.h.feed(h.h.fill_frame(sell, sell['qty'], sell['qty'] * (order['signal']['emergency'] - 2)))
        self.assertEqual(h.h.e.s['positions'], {})
        self.assertEqual(h.h.e.active_orders(), [])

    def test_new_bbo_still_rejects_chase_spread_or_insufficient_depth(self):
        h = self.h; tick = h.seed()
        h.send_book(tick, ask=tick.ask + 20)
        self.assertEqual(h.h.orders('BUY'), [])

    def test_higher_ask_must_recompute_cost_inclusive_reward_risk(self):
        h = self.h; h.h.watch[SYM]['resistance'] = 1045
        tick = h.seed(); h.send_book(tick, ask=tick.ask + 1)
        self.assertEqual(h.h.orders('BUY'), [])
        self.assertEqual(h.h.e.fast_strategy.last_evaluation[SYM]['reason'], 'NET_REWARD_RISK_LOW')
        h.send_book(tick, ask=tick.ask + 1, qty=1)
        self.assertEqual(h.h.orders('BUY'), [])

    def test_receipt_and_volume_are_immutable_when_quote_changes(self):
        h = self.h; tick = h.seed()
        h.h.clock[0] += timedelta(milliseconds=100)
        book = h.book(tick, ask=tick.ask + 1)
        h.books[SYM] = book
        paired = runner.current_entry_quote(h.h.e, tick)
        self.assertEqual(paired.ask, book['best_ask'])
        self.assertEqual((paired.time, paired.received_at, paired.received_monotonic,
                          paired.volume, paired.cumulative, paired.price),
                         (tick.time, tick.received_at, tick.received_monotonic,
                          tick.volume, tick.cumulative, tick.price))


class WatchBurstTests(unittest.TestCase):
    def setUp(self):
        self.h = pipeline.FastPipelineProductionLikeFlagsTests('runTest')
        self.h.setUp(); self.addCleanup(self.h.doCleanups)

    def test_received_prefixes_build_history_once_but_only_current_trade_is_evaluated(self):
        self.burst()

    def test_burst_preserves_synchronous_orders_while_diagnostic_disk_is_blocked(self):
        from core import event_log
        h=self.h; entered=threading.Event();release=threading.Event()
        original=event_log._append_line
        writer=event_log.AsyncDiagnosticEventWriter(h.e.folder/'events.jsonl')
        h.e.diagnostic_event_writer=writer
        def delayed(path,line,max_bytes):
            if threading.current_thread() is writer.thread:
                entered.set()
                if not release.wait(10):raise AssertionError('diagnostic barrier not released')
            return original(path,line,max_bytes)
        try:
            with patch.object(event_log,'_append_line',delayed):
                writer.append({'event':'NO_TRADE','time':h.clock[0].isoformat()})
                self.assertTrue(entered.wait(1))
                self.burst()
                self.assertFalse(release.is_set())
                persisted=json.loads(h.e.path.read_text(encoding='utf-8'))
                self.assertEqual(next(o for o in persisted['orders'].values() if o['side']=='BUY')['status'],'ACCEPTED')
        finally:
            release.set();h.e.diagnostic_event_writer=None;writer.close()

    def burst(self):
        h = self.h; h.e._last_eval_frame = {}
        feed = Feed(SimpleNamespace(cfg=h.cfg), [SYM])
        h.e.entry_quote_provider = feed.entry_snapshot
        start = h.clock[0]
        prices = [984] * 300 + pipeline.CAUSAL
        for index, price in enumerate(prices):
            h.clock[0] = start + timedelta(seconds=index * 4 / (len(prices) - 1))
            with patch('core.realtime.now', return_value=h.clock[0]):
                feed.emit(h.frame(price, vol=1 if index<300 else 1000))
        h.clock[0] += timedelta(milliseconds=100)
        original = h.detector.signal
        seen = []; protected = []
        original_protection = h.e.on_tick
        def evaluate(tick, watch):
            seen.append(tick.cumulative)
            return original(tick, watch)
        def protect(tick, *args):
            protected.append(tick.cumulative)
            return original_protection(tick, *args)
        h.detector.signal = evaluate; h.e.on_tick = protect
        accepted = 0; started = time.perf_counter(); drains = 0
        def consume(received, message, lag):
            nonlocal accepted
            accepted += h.feed(message, received)
        while not feed.q.empty():
            drains += 1
            runner.drain_feed(feed, consume, clock=lambda: h.clock[0], timeout=0,
                              max_messages=512, max_seconds=.25)
        MEASUREMENTS.append({'scenario': '311_received_watch_prints_4_seconds',
            'accepted': accepted, 'protected_tick_calls': len(protected),
            'entry_evaluations': len(seen), 'remaining_queue': feed.q.qsize(),
            'seconds': time.perf_counter()-started, 'drain_cycles': drains,
            'fake_buys': len(h.orders('BUY')), 'not_market_replay': True,
            'diagnostic_worker_attached': bool(getattr(h.e,'diagnostic_event_writer',None))})
        self.assertEqual(accepted, len(prices))
        self.assertEqual(len(protected), len(prices))
        self.assertEqual(seen, [feed.latest_ticks[SYM].cumulative])
        self.assertEqual(len(h.e.fast_strategy.get_tape(SYM).ticks), len(prices))
        self.assertEqual(len(h.orders('BUY')), 1, h.e.fast_strategy.last_evaluation.get(SYM))
        self.assertFalse(feed.overflow.is_set())

    def test_different_day_or_invalid_receiver_does_not_starve_current_frame(self):
        h = self.h
        msg = h.frame(1001); t = parse_tick(msg['data'][0], h.clock[0])
        for latest in (replace(t, time=t.time + timedelta(days=1), cumulative=t.cumulative + 1),
                       replace(t, cumulative=t.cumulative + 1, regular=False),
                       replace(t, time=t.time - timedelta(seconds=10), cumulative=t.cumulative + 1)):
            with self.subTest(latest=latest):
                h.e.entry_quote_provider = lambda s: (latest, {})
                with patch.object(h.detector, 'signal', wraps=h.detector.signal) as detect:
                    runner.evaluate_actual_entry(h.e, h.detector, t, h.watch[SYM],
                                                 h.radar, h.prices, h.account, {})
                    self.assertEqual(detect.call_count, 1)


class FastContextLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.h = pipeline.FastPipelineProductionLikeFlagsTests('runTest')
        self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.s = self.h.e.fast_strategy

    def ingest(self, prices):
        last = None
        for price in prices:
            last = parse_tick(self.h.frame(price)['data'][0], self.h.clock[0])
            self.assertTrue(self.s.ingest_tick(last, self.h.clock[0]))
            self.h.clock[0] += timedelta(seconds=1)
        return last

    def test_restore_preserves_previous_distinct_high_on_flat_record_high(self):
        self.ingest([984, 996, 1000, 1000])
        target = FastMomentumStrategy(self.h.cfg, self.h.auto)
        self.assertTrue(target.restore_tape(SYM, self.s.snapshot_tape(SYM)))
        self.assertEqual(target.prior_high[SYM], 996)

    def test_next_day_cannot_inherit_old_session_peak(self):
        self.ingest([1200, 1210])
        self.h.clock[0] += timedelta(days=1)
        self.ingest([1000])
        self.assertEqual(self.s.day_high[SYM], 1000)
        self.assertNotIn(SYM, self.s.prior_high)

    def test_explicit_reset_clears_session_price_context(self):
        self.ingest([1200, 1210]); self.s.reset()
        self.assertEqual(self.s.day_high, {})
        self.assertEqual(self.s.prior_high, {})

    def test_future_unavailable_or_cleared_peak_cannot_veto_recovery(self):
        tick = self.ingest(pipeline.CAUSAL)
        metrics = self.s.get_tape(SYM).metrics(tick.time)
        peak = {'price': 1500, 'time': (tick.time-timedelta(minutes=10)).isoformat(),
                'confirmed_at': (tick.time-timedelta(minutes=8)).isoformat()}
        for change in ({'cleared_at': (tick.time-timedelta(seconds=2)).isoformat()},
                       {'available_at': (tick.time+timedelta(seconds=1)).isoformat()},
                       {'confirmed_at': (tick.time+timedelta(seconds=1)).isoformat()}):
            with self.subTest(change=change):
                regime, reason, _ = self.s.classify_regime(tick, metrics, 'REGULAR',
                    {**self.h.watch[SYM], 'day_high': 1001, 'nearest_confirmed_peak': {**peak, **change}})
                self.assertEqual(regime, 'A', reason)

    def test_ask_only_crossing_confirmed_ceiling_is_not_a_trade_breakout(self):
        tick = self.ingest([980, 984, 988, 992, 999, 990, 992, 994, 996, 998, 1000])
        tick = replace(tick, price=1000, ask=1001, bid=1000)
        peak = {'price': 1000, 'time': (tick.time-timedelta(minutes=10)).isoformat(),
                'confirmed_at': (tick.time-timedelta(minutes=8)).isoformat()}
        result = self.s.calculate_invalidation_and_target(tick, {},
            {**self.h.watch[SYM], 'nearest_confirmed_peak': peak}, asof=tick.time)
        self.assertFalse(result['pass'], result)
        self.assertEqual(result['reason'], 'KNOWN_CEILING_NET_REWARD_RISK_LOW')

    def test_explicit_zero_percentage_is_honored_without_moving_structural_stop(self):
        tick = self.ingest([984, 988, 992, 996, 1000, 998, 999, 1001])
        self.s.auto = replace(self.h.auto, min_stop_distance_fraction=0.)
        result = self.s.calculate_invalidation_and_target(tick, {}, self.h.watch[SYM], asof=tick.time)
        self.assertTrue(result['pass'], result)
        self.assertLess((result['entry'] - result['stop']) / result['entry'], .012)
        self.assertLessEqual(result['quantity'] * result['risk_net_per_share'], result['risk_budget'])


@unittest.skipUnless(os.environ.get('R6_PRESERVED_LEDGER_20261022'), 'Private Oct22 ledger required')
class NextDayLedgerCompatibility(unittest.TestCase):
    def test_oct23_roll_preserves_cash_history_external_holds_and_requires_fresh_reconcile(self):
        from automation.engine import AutonomousEngine
        from automation.policy import Policy
        from tests.test_automation import FakeBroker
        from tests.test_live_ready_20261021 import settings
        from datetime import datetime
        from core.settings import KST
        original=json.loads(Path(os.environ['R6_PRESERVED_LEDGER_20261022']).read_text(encoding='utf-8-sig'))
        self.assertEqual(original['day'],'2026-10-22')
        self.assertEqual(original['positions'],{})
        self.assertFalse([o for o in original['orders'].values() if o['status'] not in {'FILLED','CANCELED','REJECTED'}])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);book=root/'book';book.mkdir()
            (book/'state.json').write_text(json.dumps(original),encoding='utf-8')
            cfg,auto=settings();broker=FakeBroker();broker.identity=original['identity']
            policy=Policy(root/'policy.json')
            e=AutonomousEngine(cfg,auto,policy,book,broker,lambda:datetime(2026,10,23,8,50,tzinfo=KST))
            self.assertEqual(e.s['day'],'2026-10-23')
            self.assertEqual(e.s['trades'],original['trades'])
            self.assertEqual(e.s['automation']['existing'],original['automation']['existing'])
            self.assertEqual(e.s['automation']['cash'],original['automation']['cash'])
            self.assertEqual(e.s['daily_loss'],0)
            self.assertEqual(e.s['entries'],{})
            self.assertFalse(e.account_ok)
            self.assertEqual(broker.orders,[])
            saved=json.loads(e.path.read_text(encoding='utf-8'))
            self.assertEqual(saved,e.s)


if __name__ == '__main__': unittest.main()
