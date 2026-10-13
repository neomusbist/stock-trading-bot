"""Backlog replay is bounded and FIFO; no live broker or fabricated fresh ticks."""
import queue
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from automation.runner import drain_feed, process_message
from tests import test_subscription_refresh as fixture
START=fixture.START
SYM=fixture.SYM


class FeedCatchupTests(unittest.TestCase):
    def test_burst_keeps_prices_and_fills_in_original_order(self):
        feed=SimpleNamespace(q=queue.Queue());seen=[]
        kinds=['0D','0B','00','0B']*40
        for i,kind in enumerate(kinds):
            feed.q.put((START,{'trnm':'REAL','data':[{'type':kind,'sequence':i}]}))
        batches=[]
        while not feed.q.empty():
            batches.append(drain_feed(feed,lambda stamp,msg,lag:seen.append(msg['data'][0]),
                                      clock=lambda:START,monotonic=lambda:0)[0])
        self.assertEqual(batches,[64,64,32])
        self.assertEqual([r['sequence'] for r in seen],list(range(160)))
        self.assertEqual([r['type'] for r in seen],kinds)

    def test_watchdog_and_reconcile_get_control_at_transport_barrier(self):
        for barrier in ('DISCONNECTED','ERROR','REG'):
            feed=SimpleNamespace(q=queue.Queue());seen=[]
            for kind in ('REAL',barrier,'REAL'):
                feed.q.put((START,{'trnm':kind}))
            drain_feed(feed,lambda stamp,msg,lag:seen.append(msg['trnm']),clock=lambda:START,monotonic=lambda:0)
            self.assertEqual(seen,['REAL',barrier]);self.assertEqual(feed.q.qsize(),1)

    def test_processing_time_budget_yields_even_when_backlog_remains(self):
        feed=SimpleNamespace(q=queue.Queue());elapsed=[0.]
        for i in range(20):feed.q.put((START,{'trnm':'REAL'}))
        def slow_consumer(*args):elapsed[0]+=.03
        count,lag=drain_feed(feed,slow_consumer,clock=lambda:START,monotonic=lambda:elapsed[0])
        self.assertEqual(count,2);self.assertEqual(feed.q.qsize(),18)


class FeedCatchupIntegrationTests(unittest.TestCase):
    setUp=fixture.SubscriptionRefreshTests.setUp
    frame=fixture.SubscriptionRefreshTests.frame
    tick=fixture.SubscriptionRefreshTests.tick

    def test_backlogged_real_ticks_build_history_but_cannot_trade_in_the_past(self):
        self.clock=START+timedelta(seconds=180)
        for sec in range(130):self.feed.q.put((START+timedelta(seconds=sec),self.frame(self.tick(sec=sec))))
        def consume(received,msg,lag):
            process_message(self.e,self.d,msg,received,{SYM},{SYM:self.watch},self.radar,
                            self.prices,self.account,True,self.sub)
        with patch('automation.runner.evaluate_actual_entry') as enter:
            while not self.feed.q.empty():
                drain_feed(self.feed,consume,clock=lambda:self.clock,monotonic=lambda:0)
            enter.assert_not_called()
        self.assertGreaterEqual(len(self.d.usable_bars(SYM)),2)
        self.assertEqual(self.broker.orders,[])
        self.assertEqual(self.e.last_regular_tick_at,START+timedelta(seconds=129))

    def test_fresh_frame_after_backlog_is_evaluated_without_outer_loop_per_quote(self):
        self.clock=START+timedelta(seconds=125)
        for sec in range(126):self.feed.q.put((START+timedelta(seconds=sec),self.frame(self.tick(sec=sec))))
        seen=[]
        def consume(received,msg,lag):
            process_message(self.e,self.d,msg,received,{SYM},{SYM:self.watch},self.radar,
                            self.prices,self.account,True,self.sub)
        with patch('automation.runner.evaluate_actual_entry',side_effect=lambda e,d,t,*a:seen.append(t.time)):
            loops=0
            while not self.feed.q.empty():
                drain_feed(self.feed,consume,clock=lambda:self.clock,monotonic=lambda:0);loops+=1
        self.assertEqual(loops,2)
        self.assertEqual(seen[-1],self.clock)
        self.assertTrue(all((self.clock-t).total_seconds()<=5 for t in seen))
        self.assertEqual(self.broker.orders,[])


if __name__=='__main__':unittest.main()
