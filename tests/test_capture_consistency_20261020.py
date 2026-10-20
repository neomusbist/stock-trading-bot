"""Causal gap/scheduling regressions. Synthetic data; no broker/network access."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.radar import RadarWorker
from automation.runner import SubscriptionState
from automation.runner import prewarm_handoff
import queue
from core.settings import Config, KST
from core.strategy import Tick
from core.realtime import parse_tick
from automation.fast_momentum import FastMomentumStrategy

AT=datetime(2026,10,20,11,0,tzinfo=KST)
SYM='999990'


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config()
        self.auto=AutomationConfig()
        self.detector=PatternDetector(self.cfg,self.auto,Costs(.00015,.00015,.002,.002,.003))
        self.detector.continuous_observation=True
        self.cumulative=0

    def feed(self,at):
        self.cumulative+=100
        t=Tick(SYM,at,1000,100,self.cumulative,1001,1000)
        return self.detector.ingest(t,at)

    def history(self):
        for second in range(0,186,5):self.feed(AT+timedelta(seconds=second))

    def test_gap_preserves_closed_bars_but_rejects_false_continuity(self):
        self.history();d=self.detector
        before=list(d.history[SYM]);gap=AT+timedelta(seconds=186)
        d.mark_coverage_gap(SYM,gap)
        self.assertEqual(list(d.history[SYM]),before)
        self.assertEqual(d.usable_bars(SYM),[])
        self.assertTrue(self.feed(gap+timedelta(seconds=2)))
        self.assertEqual(d.usable_bars(SYM),[])
        self.assertEqual(d.current[SYM]['gap_reason'],'SUBSCRIPTION_GAP')
        self.assertEqual(d.observation_start[SYM],gap+timedelta(seconds=2))

    def test_queued_pre_gap_print_cannot_rewarm_after_a_drop(self):
        self.history();gap=AT+timedelta(seconds=190)
        self.detector.mark_coverage_gap(SYM,gap,'OBSERVATION_QUEUE_GAP')
        self.assertFalse(self.feed(gap-timedelta(seconds=1)))
        self.assertFalse(self.feed(gap))
        self.assertNotIn(SYM,self.detector.buffers)

    def test_recovery_requires_new_consecutive_complete_minutes(self):
        self.history();gap=AT+timedelta(seconds=186)
        self.detector.mark_coverage_gap(SYM,gap)
        for second in range(190,361,5):self.feed(AT+timedelta(seconds=second))
        bars=self.detector.usable_bars(SYM)
        self.assertEqual([b['time'] for b in bars],[AT+timedelta(minutes=4),AT+timedelta(minutes=5)])
        self.assertTrue(all(b['gap_reason'] is None for b in bars))
        self.assertTrue(self.detector.base_gates(self.detector.last[SYM],{'status':'WATCH'})['warmup']['result']=='PASS')

    def test_subscription_detach_marks_gap_even_inside_retention_grace(self):
        self.history();d=self.detector;events=[];forgot=[]
        engine=SimpleNamespace(event=lambda *a,**k:events.append((a,k)),connected=True,account_ok=True,
            fast_strategy=SimpleNamespace(forget=lambda *a:forgot.append(a)))
        feed=SimpleNamespace(update_symbols=lambda *a:None,transport_failed=False)
        state=SubscriptionState([SYM]);gap=AT+timedelta(seconds=186)
        state.refresh(engine,d,feed,[],gap,grace_seconds=360)
        state.refresh(engine,d,feed,[SYM],gap+timedelta(seconds=2),grace_seconds=360)
        self.assertEqual(len(d.history[SYM]),3)
        self.assertEqual(d.usable_bars(SYM),[])
        self.assertEqual(forgot,[(SYM,'SUBSCRIPTION_GAP')])

    def test_stale_handoff_cannot_resurrect_pre_gap_eligibility(self):
        self.history();other=PatternDetector(self.cfg,self.auto,self.detector.costs)
        self.assertTrue(other.handoff_from(self.detector,SYM))
        self.detector.mark_coverage_gap(SYM,AT+timedelta(seconds=186))
        self.assertFalse(self.detector.handoff_from(other,SYM))
        self.assertEqual(self.detector.usable_bars(SYM),[])

    def test_rest_reference_never_becomes_signal_warmup(self):
        bars=[{'time':AT+timedelta(minutes=i),'open':1000,'high':1005,'low':995,'close':1001,'volume':100}
              for i in range(3)]
        d=self.detector;asof=AT+timedelta(minutes=3)
        d.price_context.merge_history(SYM,bars,asof,'KRX')
        self.assertEqual(len(d.price_context.reference_bars(SYM,asof,'KRX')),3)
        self.assertEqual(d.preload_from_context(SYM,asof),0)
        self.assertEqual(d.usable_bars(SYM),[])
        self.assertNotIn(SYM,d.observation_start)

    def test_actual_received_references_keep_their_provenance(self):
        self.history();d=self.detector;asof=AT+timedelta(minutes=4)
        self.assertTrue(all(b['observation_source']=='ACTUAL_RECEIVED' for b in d.price_context.rows[SYM]['bars']))
        d.history.pop(SYM)
        self.assertEqual(d.preload_from_context(SYM,asof),3)

    def test_duplicate_gap_does_not_erase_new_recovered_history(self):
        self.history();gap=AT+timedelta(seconds=186)
        self.detector.mark_coverage_gap(SYM,gap)
        self.feed(gap+timedelta(seconds=2))
        self.assertFalse(self.detector.mark_coverage_gap(SYM,gap))
        self.assertFalse(self.detector.coverage_gaps[SYM]['pending'])

    def test_real_caller_promotion_scope_keeps_queued_prewarm_ticks(self):
        self.history();d=self.detector
        at=AT+timedelta(seconds=186)
        self.cumulative+=100
        t=Tick(SYM,at,1001,100,self.cumulative,1001,1000)
        feed=SimpleNamespace(observation_symbols=frozenset({SYM}),observation_drops=0,
            observation_q=queue.Queue(),q=queue.Queue(),overflow=SimpleNamespace(is_set=lambda:False),
            transport_failed=False,subscription_lock=None,
            configure_observation=lambda *a:None)
        feed.observation_q.put((at,t))
        engine=SimpleNamespace(s={'positions':{},'automation':{'existing':{}}},
            active_orders=lambda:[],event=lambda *a,**k:None)
        # Actual runner passes observed=desired-critical, excluding this newly
        # promoted WATCH. The older test used an impossible overlapping scope.
        prewarm_handoff(engine,d,feed,set(),{SYM},set(),0)
        self.assertEqual(d.last[SYM].cumulative,self.cumulative)
        self.assertEqual(d.current[SYM]['close'],1001)
        self.assertEqual(feed.observation_q.unfinished_tasks,0)


class FairScanTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.cfg=replace(Config(),max_watchlist=12)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,structural_breakout_enabled=True,max_scan_charts=20)
        policy=SimpleNamespace(locked=lambda s:s=='005930')
        self.worker=RadarWorker(self.cfg,self.auto,policy,SimpleNamespace(),Path(tmp.name))
        self.worker.radar.available_capital=71822

    def row(self,s,hot=False,price=1000):
        return {'code':s,'source_details':{'a':{'observed_rank':1,'values':{'flu_rt':1}}},
                'reference_price':price,'discovery_context':{'last_cycle_return':.01 if hot else 0}}

    def order(self,rows):
        with patch('automation.radar.now',return_value=AT):return self.worker.scheduled_items(rows)

    def test_cached_status_never_spends_the_fifo_admission_floor(self):
        rows=[self.row(f'{i:06d}',True) for i in range(1,41)]
        for r in rows[:10]:self.worker.status_cache[r['code']]={'checked_at':AT.isoformat(),'pass':True}
        scheduled=self.order(rows)
        self.assertEqual({r['code'] for r in scheduled[:10]},{r['code'] for r in rows[:10]})
        self.assertTrue(all(not self.worker.status_needs_query(r['code'],AT) for r in scheduled[:10]))

    def test_aged_rows_get_four_of_existing_spare_slots_before_new_movers(self):
        old=[self.row(f'{i:06d}') for i in range(1,9)]
        self.order(old)
        active=[self.row(f'{i:06d}') for i in range(101,113)]
        self.worker.status_priority_symbols=frozenset(r['code'] for r in active)
        hot=[self.row(f'{i:06d}',True) for i in range(201,241)]
        ordered=self.order(active+hot+old)
        queried=[r['code'] for r in ordered[:20]]
        self.assertEqual(set(queried[:12]),set(self.worker.status_priority_symbols))
        self.assertTrue({r['code'] for r in old[:4]}<=set(queried[12:]))
        self.assertEqual(len(queried),self.auto.max_scan_charts)

    def test_unaffordable_and_locked_rows_do_not_take_aging_floor(self):
        invalid=[self.row('005930'),self.row('999999',price=100000)]
        old=[self.row(f'{i:06d}') for i in range(31,35)]
        self.order(invalid+old)
        hot=[self.row(f'{i:06d}',True) for i in range(201,241)]
        scheduled=self.order(hot+invalid+old)
        actual=[r['code'] for r in scheduled if r['code'] not in {'005930','999999'}][:20]
        self.assertTrue({r['code'] for r in old}<=set(actual))

    def test_cache_and_order_side_status_ttls_are_not_extended(self):
        w=self.worker;w.status_cache[SYM]={'checked_at':AT.isoformat(),'pass':True}
        self.assertFalse(w.status_needs_query(SYM,AT+timedelta(seconds=180)))
        self.assertTrue(w.status_needs_query(SYM,AT+timedelta(seconds=181)))
        w.status_priority_symbols=frozenset({SYM})
        self.assertFalse(w.status_needs_query(SYM,AT+timedelta(seconds=120)))
        self.assertTrue(w.status_needs_query(SYM,AT+timedelta(seconds=121)))
        self.assertTrue(w.status_needs_query(SYM,AT-timedelta(seconds=2)))


class BookClockTests(unittest.TestCase):
    def setUp(self):
        self.strategy=FastMomentumStrategy(Config(),AutomationConfig())

    def quote(self,**kw):
        values=dict(symbol=SYM,time=AT,price=1000,volume=100,cumulative=100,
                    ask=1001,bid=1000,ask_qty=10000,bid_qty=10000,depth_available=True,
                    book_time=AT,received_at=AT.isoformat(),received_monotonic=103.,
                    book_received_at=AT.isoformat(),book_received_monotonic=90.)
        values.update(kw);return Tick(**values)

    def test_fresh_trade_does_not_refresh_an_old_book_after_clock_correction(self):
        t=self.quote()
        with patch('core.market_time.time.monotonic',return_value=103.):
            self.assertEqual(self.strategy._quote(t,AT),'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')

    def test_fresh_matched_book_remains_executable(self):
        t=self.quote(book_received_monotonic=102.)
        with patch('core.market_time.time.monotonic',return_value=103.):
            self.assertIsNone(self.strategy._quote(t,AT))

    def test_future_exchange_book_still_rejected(self):
        t=self.quote(book_time=AT+timedelta(seconds=2),book_received_monotonic=102.)
        with patch('core.market_time.time.monotonic',return_value=103.):
            self.assertEqual(self.strategy._quote(t,AT),'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')

    def test_receiver_keeps_book_receipt_separate_from_later_trade(self):
        received=AT+timedelta(seconds=1)
        book={'time':AT.isoformat(),'received_at':AT.isoformat(),'received_monotonic':100.,
              'best_ask':1001,'best_bid':1000,'ask_qty':12,'bid_qty':15,'depth_available':True}
        item={'item':SYM,'values':{'20':'110001','10':'1001','15':'2','13':'100',
                                  '27':'1001','28':'1000','290':'2'}}
        t=parse_tick(item,received,book,received_monotonic=101.)
        self.assertTrue(t.depth_available)
        self.assertEqual(t.book_received_at,AT.isoformat())
        self.assertEqual(t.book_received_monotonic,100.)
        self.assertEqual(t.received_at,received.isoformat())

    def test_mismatched_prices_remain_unknown_and_are_explained(self):
        t=self.quote(depth_available=False,depth_source='BOOK_PRICE_MISMATCH',book_received_monotonic=102.)
        with patch('core.market_time.time.monotonic',return_value=103.):
            self.assertEqual(self.strategy._quote(t,AT),'EXECUTABLE_DEPTH_UNKNOWN')
            self.strategy._note(t,'UNKNOWN','EXECUTABLE_DEPTH_UNKNOWN',decision_at=AT)
        ev=self.strategy.last_evaluation[SYM]['quote_evidence']
        self.assertEqual(ev['depth_source'],'BOOK_PRICE_MISMATCH')
        self.assertTrue(ev['book_freshness']['fresh'])


if __name__=='__main__':unittest.main()
