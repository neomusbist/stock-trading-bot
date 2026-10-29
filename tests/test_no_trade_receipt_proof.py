"""Synthetic no-trade evidence; this test never connects or places orders."""
import unittest
from datetime import datetime, timedelta
from core.settings import Config,KST
from core.strategy import Tick
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs

class NoTradeReceiptProofTests(unittest.TestCase):
    def setUp(self):
        self.sym='999990';self.start=datetime(2026,10,29,10,0,2,tzinfo=KST)
        self.det=PatternDetector(Config(stale_seconds=5),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        self.det.continuous_observation=True
        for minute,cum in enumerate((1100,1200,1300)):
            at=self.start+timedelta(minutes=minute);mono=1000+60*minute
            t=Tick(self.sym,at,2500,100,cum,2500,2490,True,venue='KRX',received_at=at.isoformat(),received_monotonic=mono)
            self.assertTrue(self.det.ingest(t,at))
        self.old=self.start+timedelta(minutes=2)

    def chain(self,broken=False,refresh=False,wire=None):
        for seconds in range(0,133,4):
            if broken and 30<=seconds<=40:continue
            if refresh and seconds==64:self.det.clear_no_trade_chain()
            at=self.old+timedelta(seconds=seconds);mono=1120+seconds
            b={'received_at':at.isoformat(),'received_monotonic':mono}
            self.det.note_no_trade_book(self.sym,at,b,wire or self.sym)

    def resume(self,cumulative=1400,volume=100,venue='KRX'):
        at=self.start+timedelta(minutes=4,seconds=13)
        tick=Tick(self.sym,at,2510,volume,cumulative,2510,2500,True,venue=venue,
                  received_at=at.isoformat(),received_monotonic=1253)
        self.assertTrue(self.det.ingest(tick,at))
        return self.det.current[self.sym]

    def test_no_book_proof_stays_unknown(self):
        b=self.resume();self.assertEqual(b.get('gap_reason'),'NO_TRADE_COVERAGE_UNPROVEN')
        self.assertFalse(b.get('resumed_from_no_trade_gap',False));self.assertEqual(self.det.usable_bars(self.sym),[])

    def test_dense_actual_book_receipt_chain_preserves_only_real_bars(self):
        self.chain();b=self.resume();self.assertTrue(b.get('resumed_from_no_trade_gap'))
        bars=self.det.usable_bars(self.sym);self.assertEqual(len(bars),3)
        self.assertEqual([x['time'].minute for x in bars],[0,1,2])

    def test_wrong_volume_delta_is_not_no_trade(self):
        self.chain();b=self.resume(cumulative=1305);self.assertEqual(b.get('gap_reason'),'CUMULATIVE_INCONSISTENT')
        self.assertEqual(self.det.usable_bars(self.sym),[])

    def test_missing_volume_is_data_loss(self):
        self.chain();b=self.resume(cumulative=1450);self.assertEqual(b.get('gap_reason'),'DATA_LOSS_GAP')
        self.assertEqual(self.det.usable_bars(self.sym),[])

    def test_venue_change_is_not_no_trade(self):
        self.chain();b=self.resume(venue='NXT');self.assertEqual(b.get('gap_reason'),'VENUE_OR_SESSION_CHANGED')
        self.assertEqual(self.det.usable_bars(self.sym),[])

    def test_book_silence_or_group_refresh_breaks_proof(self):
        for mode in ('broken','refresh'):
            with self.subTest(mode=mode):
                self.setUp();self.chain(broken=mode=='broken',refresh=mode=='refresh')
                self.assertEqual(self.resume().get('gap_reason'),'NO_TRADE_COVERAGE_UNPROVEN')

    def test_actual_subscription_gap_invalidates_book_chain(self):
        self.chain();self.det.mark_coverage_gap(self.sym,self.old+timedelta(seconds=20))
        self.assertEqual(self.resume().get('gap_reason'),'SUBSCRIPTION_GAP')
        self.assertEqual(self.det.usable_bars(self.sym),[])

    def test_handoff_does_not_reuse_a_different_detector_book_chain(self):
        self.chain();self.assertIn(self.sym,self.det._no_trade_book_chains)
        source=PatternDetector(Config(stale_seconds=5),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        source.continuous_observation=True
        for i in range(4):
            earlier=self.start+timedelta(minutes=i-1)
            seed=Tick(self.sym,earlier,2500,100,100*(i+1),2500,2490,True,venue='KRX',received_at=earlier.isoformat(),received_monotonic=940+60*i)
            self.assertTrue(source.ingest(seed,earlier))
        at=self.old+timedelta(seconds=1)
        tick=Tick(self.sym,at,2500,1,401,2500,2490,True,venue='KRX',received_at=at.isoformat(),received_monotonic=1121)
        self.assertTrue(source.ingest(tick,at));self.assertTrue(self.det.handoff_from(source,self.sym))
        self.assertNotIn(self.sym,self.det._no_trade_book_chains)

    def test_session_boundary_does_not_relabel_regular_reference_as_after(self):
        from unittest.mock import patch
        d=PatternDetector(Config(stale_seconds=5),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        d.continuous_observation=True
        first=datetime(2026,10,29,15,19,2,tzinfo=KST);second=datetime(2026,10,29,15,40,15,tzinfo=KST)
        a=Tick(self.sym,first,2500,100,1000,2500,2490,True,venue='KRX',received_at=first.isoformat(),received_monotonic=1000)
        b=Tick(self.sym,second,2510,100,1100,2510,2500,True,venue='KRX',received_at=second.isoformat(),received_monotonic=2273)
        self.assertTrue(d.ingest(a,first))
        with patch.object(d.price_context,'record',wraps=d.price_context.record) as record:
            self.assertTrue(d.ingest(b,second))
        self.assertEqual(record.call_args.args[1]['venue'],'KRX')
        self.assertEqual(d.current[self.sym]['gap_reason'],'VENUE_OR_SESSION_CHANGED')
        self.assertEqual(d.usable_bars(self.sym,after_only=True),[])

    def test_older_tick_is_not_accepted(self):
        old=self.old-timedelta(seconds=1)
        t=Tick(self.sym,old,2500,1,1301,2500,2490,True,venue='KRX',received_at=old.isoformat(),received_monotonic=1119)
        self.assertFalse(self.det.ingest(t,old));self.assertEqual(self.det.last[self.sym].time,self.old)

class ReceiptRoutingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_subscription_refresh import SubscriptionRefreshTests
        self.h=SubscriptionRefreshTests('runTest');self.h.setUp();self.addCleanup(self.h.doCleanups)

    def test_actual_runner_routes_book_receipt_and_group_ack_invalidates_chain(self):
        h=self.h;symbol='999990';stamp=h.clock
        book={'received_at':stamp.isoformat(),'received_monotonic':1000.,'time':stamp.isoformat(),
              'best_ask':10010.,'best_bid':10000.,'ask_qty':10,'bid_qty':10,'depth_available':True}
        packet={'trnm':'REAL','data':[{'type':'0D','item':symbol,'_subscription_item':symbol,'_book':book}]}
        h.send(packet);self.assertIn(symbol,h.d._no_trade_book_chains)
        h.send({'trnm':'REG','return_code':'0'});self.assertNotIn(symbol,h.d._no_trade_book_chains)

    def _feed_actual_dispatcher_gap(self,refresh):
        h=self.h;symbol='999990';start=h.clock
        for minute,cum in enumerate((1100,1200,1300)):
            at=start+timedelta(minutes=minute,seconds=2);h.clock=at
            t=Tick(symbol,at,2500,100,cum,2500,2490,True,venue='KRX')
            packet=h.frame(t);packet['_received_monotonic']=1000+60*minute
            h.send(packet)
        origin=start+timedelta(minutes=2,seconds=2)
        for seconds in range(0,133,4):
            h.clock=origin+timedelta(seconds=seconds)
            if refresh and seconds==64:h.send({'trnm':'REG','return_code':'0'})
            book={'received_at':h.clock.isoformat(),'received_monotonic':1120+seconds,'time':h.clock.isoformat(),
                  'best_ask':2510.,'best_bid':2500.,'ask_qty':10,'bid_qty':10,'depth_available':True}
            h.send({'trnm':'REAL','data':[{'type':'0D','item':symbol,'_subscription_item':symbol,'_book':book}]})
        h.clock=start+timedelta(minutes=4,seconds=15)
        tick=Tick(symbol,h.clock,2510,100,1400,2510,2500,True,venue='KRX')
        packet=h.frame(tick);packet['_received_monotonic']=1253;h.send(packet)
        return h.d.current[symbol],h.d.usable_bars(symbol)

    def test_proven_gap_through_real_dispatcher(self):
        bar,bars=self._feed_actual_dispatcher_gap(False)
        self.assertTrue(bar.get('resumed_from_no_trade_gap'));self.assertEqual(len(bars),3)
        self.assertEqual(self.h.broker.orders,[])

    def test_group_refresh_through_real_dispatcher_denies_gap_proof(self):
        bar,bars=self._feed_actual_dispatcher_gap(True)
        self.assertEqual(bar.get('gap_reason'),'NO_TRADE_COVERAGE_UNPROVEN');self.assertEqual(bars,[])
        self.assertEqual(self.h.broker.orders,[])

    def test_pending_subscription_cannot_start_book_chain(self):
        h=self.h;symbol='999990';stamp=h.clock;h.sub.refresh_pending=True
        book={'received_at':stamp.isoformat(),'received_monotonic':1000.,'time':stamp.isoformat(),
              'best_ask':10010.,'best_bid':10000.,'ask_qty':10,'bid_qty':10,'depth_available':True}
        h.send({'trnm':'REAL','data':[{'type':'0D','item':symbol,'_subscription_item':symbol,'_book':book}]})
        self.assertNotIn(symbol,h.d._no_trade_book_chains)

    def test_diagnostic_book_failure_does_not_stop_dispatch_and_invalidates_proof(self):
        from unittest.mock import patch
        h=self.h;symbol='999990';stamp=h.clock
        book={'received_at':stamp.isoformat(),'received_monotonic':1000.,'time':stamp.isoformat(),
              'best_ask':10010.,'best_bid':10000.,'ask_qty':10,'bid_qty':10,'depth_available':True}
        h.d._no_trade_book_chains[symbol]={'sentinel':True}
        with patch.object(h.d,'note_no_trade_book',side_effect=ValueError('bad evidence')):
            h.send({'trnm':'REAL','data':[{'type':'0D','item':symbol,'_subscription_item':symbol,'_book':book}]})
        self.assertNotIn(symbol,h.d._no_trade_book_chains)
        self.assertTrue(h.d._no_trade_evidence_broken)
        self.assertEqual(h.e._no_trade_evidence_errors,1)
        self.assertEqual(h.broker.orders,[])

if __name__=='__main__':unittest.main()
