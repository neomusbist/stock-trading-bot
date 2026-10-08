"""Offline incident-shaped scenarios; historical OHLC is not claimed to be reproduced."""
import copy,json,threading,time,unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.broker import Rest,Rejected
from core.realtime import Feed
from core.strategy import Tick
from automation.runner import AccountWorker
from automation.patterns import PatternDetector
from tests import test_position_data_health as health_fixture
from tests import test_after_market as after_fixture
BASE=health_fixture.BASE;SYM=health_fixture.SYM


class AfterQuoteRecheckTests(unittest.TestCase):
    setUp=health_fixture.PositionHealthTests.setUp
    def prepare(self):
        self.clock=BASE.replace(hour=18)
        self.e.after_enabled=True;self.e.after_live_authorized=True
        self.e.s['positions'][SYM]['last_tick']=(self.clock-timedelta(seconds=90)).isoformat()
        self.e.s['positions'][SYM]['signal']['venue']='SOR'
        self.feed.last_message_at=self.clock;self.e.last_account=time.monotonic()
    def test_after_quote_silence_requests_exchange_specific_read_only_probe(self):
        self.prepare();self.e.timer(self.e.prices)
        self.assertEqual(self.account.position_context[SYM],{'session':'AFTER','venue':'SOR'})
        self.assertTrue(self.account.request.is_set());self.assertEqual(self.b.orders,[])
        self.assertEqual(self.e.s['positions'][SYM]['data_health']['probe']['result'],'REQUESTED')
    def test_after_verified_rest_quote_avoids_timeout_without_faking_trade_or_bars(self):
        self.prepare();self.e.timer(self.e.prices);before=copy.deepcopy(self.e.s['positions'][SYM])
        self.clock+=timedelta(seconds=5)
        book={'time':self.clock.isoformat(),'best_bid':7170,'best_ask':7180,'session':'AFTER','venue':'SOR'}
        self.h.probe_results({'books':{SYM:book}},self.clock)
        self.assertTrue(self.h.accept_book(SYM,book,self.clock,'ka10004'))
        self.feed.last_message_at=self.clock;self.e.timer(self.e.prices)
        self.assertFalse(self.e.s['halt']);self.assertEqual(self.b.orders,[])
        for key in ('last_tick','high','low','stop'):self.assertEqual(self.e.s['positions'][SYM][key],before[key])
    def test_after_old_krx_or_wrong_venue_never_excuses_blind_position(self):
        self.prepare()
        for venue in ('KRX','NXT'):
            book={'time':self.clock.isoformat(),'best_bid':7170,'best_ask':7180,'session':'AFTER','venue':venue}
            self.assertFalse(self.h.accept_book(SYM,book,self.clock,'ka10004'))
        self.assertFalse(self.h.accept_book(SYM,{'time':self.clock.isoformat(),'bid':7170,'regular':True},self.clock,'ka10004'))
    def test_after_failed_probe_keeps_existing_bounded_emergency_guard(self):
        self.prepare();self.e.timer(self.e.prices)
        self.h.probe_results({'book_errors':{SYM:{'error':'TIMEOUT','api':'ka10004'}}},self.clock)
        self.clock+=timedelta(seconds=16);self.feed.last_message_at=self.clock
        self.e.timer(self.e.prices)
        self.assertEqual(self.b.orders[0][0],'SELL')
        self.assertEqual(self.e.s['positions'][SYM]['data_health']['probe']['result'],'PROBE_FAILED')
    def test_worker_uses_after_context_and_never_krx_existing_book_for_bot(self):
        rest=Mock();worker=AccountWorker(rest,5);worker.position_symbols=frozenset({SYM})
        worker.position_context={SYM:{'session':'AFTER','venue':'SOR'}}
        rest.snapshot.return_value={'holdings':{SYM:{'qty':9}},'orders':[]}
        rest.position_book.return_value={'time':BASE.replace(hour=18).isoformat(),'venue':'SOR','session':'AFTER'}
        worker.request.wait=lambda _:worker.stop.set();worker.run()
        rest.position_book.assert_called_once_with(SYM,session='AFTER',venue='SOR')
        rest.existing_book.assert_not_called();rest.submit.assert_not_called();rest.cancel.assert_not_called()
    def test_official_rest_symbol_suffix_and_exchange_time_not_receipt_time(self):
        rest=object.__new__(Rest);rest.query=Mock(return_value={'bid_req_base_tm':'183000','buy_fpr_bid':'-7170',
            'sel_fpr_bid':'+7180','buy_fpr_req':'30','sel_fpr_req':'40'})
        current=BASE.replace(hour=18,minute=30,second=10)
        with patch('core.broker.now',return_value=current):
            for venue,suffix in [('SOR','_AL'),('NXT','_NX')]:
                book=rest.position_book(SYM,'AFTER',venue)
                self.assertEqual(rest.query.call_args.args[1],{'stk_cd':SYM+suffix})
                self.assertEqual(book['time'],current.replace(second=0).isoformat())
                self.assertEqual(book['ask_qty'],40)
        with self.assertRaises(Rejected):rest.position_book(SYM,'AFTER','KRX')


class FinalBuyEvidenceTests(unittest.TestCase):
    setUp=after_fixture.AfterMarketTests.setUp
    def ready(self,depth=100):
        self.e.a=replace(self.e.a,AUTO_TRADING_CAP_KRW=81300)
        self.e.s['cash']=81300;self.e.available_cash=81300
        self.tick=Tick('999990',self.clock,6340,100,1000,6340,6330,ask_qty=depth,bid_qty=depth,
                       venue='SOR',depth_available=True,book_time=self.clock,depth_source='0D')
        self.book={'time':self.clock.isoformat(),'best_ask':6340,'best_bid':6330,
                   'ask_qty':depth,'bid_qty':depth,'depth_available':True}
        self.signal={'symbol':'999990','time':self.clock.isoformat(),'pattern':'B','limit':6340,'stop':6290,
            'source_count':2,'session':'AFTER','venue':'SOR','efficiency':{'decision':'TRADE'},
            'evaluation_gates':{'breakout':{'result':'PASS','values':{'breakout':6320}}}}
        self.feed=Feed(Mock(cfg=self.cfg),['999990']);self.feed.latest_ticks['999990']=self.tick
        self.feed.orderbooks['999990']=self.book;self.e.entry_quote_provider=self.feed.entry_snapshot
    def test_actual_risk_sized_qty_exceeds_old_20000_estimate_and_insufficient_depth_blocks(self):
        self.ready(depth=3);qty,_=self.e.entry_gates(self.signal,self.tick)
        self.assertGreater(qty,3)
        self.assertIsNone(self.e.enter(self.signal,self.tick));self.assertEqual(self.b.orders,[])
    def test_sufficient_depth_reaches_fake_order_boundary_with_actual_quantity(self):
        self.ready();qty,_=self.e.entry_gates(self.signal,self.tick)
        order=self.e.enter(self.signal,self.tick)
        self.assertEqual(order['status'],'ACCEPTED');self.assertEqual(order['qty'],qty)
        self.assertEqual(order['signal']['planned_qty'],qty)
        evidence=order['signal']['entry_execution_evidence']
        self.assertEqual(evidence['phase'],'PRE_HTTP');self.assertEqual(evidence['quantity'],qty)
        self.assertEqual(evidence['price'],6340);self.assertEqual(evidence['book_ask_qty'],100)
    def test_one_to_six_shares_are_checked_against_the_actual_boundary_quantity(self):
        for quantity in range(1,7):
            with self.subTest(quantity=quantity):
                self.ready(depth=quantity)
                self.assertIsNone(self.e.entry_market_reason(self.signal,quantity))
                self.feed.orderbooks['999990']['ask_qty']=quantity-1
                self.assertEqual(self.e.entry_market_reason(self.signal,quantity),'ENTRY_DEPTH_BELOW_ACTUAL_QUANTITY')
    def test_latest_tick_retracted_breakout_even_if_original_signal_was_good(self):
        self.ready();self.feed.latest_ticks['999990']=replace(self.tick,price=6310,ask=6320,bid=6310)
        self.assertIsNone(self.e.enter(self.signal,self.tick));self.assertEqual(self.b.orders,[])
    def test_final_boundary_rechecks_after_api_delay_not_only_at_signal_time(self):
        self.ready();original=self.b.submit
        def delayed(*args,**kwargs):
            self.feed.latest_ticks['999990']=replace(self.tick,price=6310,ask=6320,bid=6310)
            return original(*args,**kwargs)
        self.b.submit=delayed
        order=self.e.enter(self.signal,self.tick)
        self.assertEqual(order['status'],'REJECTED');self.assertEqual(self.b.orders,[])
        evidence=order['signal']['entry_execution_evidence']
        self.assertEqual(evidence['phase'],'PRE_HTTP');self.assertEqual(evidence['price'],6310)
    def test_disconnect_erases_receiver_entry_evidence(self):
        self.ready();self.feed.emit({'trnm':'DISCONNECTED'})
        self.assertIsNone(self.e.enter(self.signal,self.tick));self.assertEqual(self.b.orders,[])
    def test_pre_transport_quote_failure_requires_new_signal_but_does_not_ban_symbol_all_day(self):
        self.ready();original=self.b.submit
        def delayed(*args,**kwargs):
            self.feed.latest_ticks['999990']=replace(self.tick,price=6310,ask=6320,bid=6310)
            return original(*args,**kwargs)
        self.b.submit=delayed
        rejected=self.e.enter(self.signal,self.tick)
        self.assertEqual(rejected['status'],'REJECTED');self.assertTrue(rejected['fresh_signal_required'])
        self.assertIsNone(self.e.entry_failure('999990'));self.assertEqual(self.b.orders,[])
        self.feed.latest_ticks['999990']=self.tick;self.b.submit=original
        accepted=self.e.enter(dict(self.signal),self.tick)
        self.assertEqual(accepted['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1)
    def test_stale_depth_and_widened_spread_block_without_orders(self):
        self.ready();self.feed.orderbooks['999990']={**self.book,'time':(self.clock-timedelta(seconds=6)).isoformat()}
        self.assertIsNone(self.e.enter(self.signal,self.tick));self.assertEqual(self.b.orders,[])
        self.feed.latest_ticks['999990']=replace(self.tick,ask=6400)
        self.assertIsNone(self.e.enter(self.signal,self.tick));self.assertEqual(self.b.orders,[])


class WickImpulseTests(unittest.TestCase):
    def detector(self):
        from core.settings import Config
        from automation.policy import AutomationConfig
        from automation.capital import Costs
        return PatternDetector(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
    def test_down_wick_then_sideways_is_not_a_confirmed_B_impulse(self):
        d=self.detector()
        # Synthetic geometry matching 6100 wick / 6340 peak / 6300 pullback.
        bars=[{'low':6100,'high':6330,'close':6330,'volume':10000},
              {'low':6310,'high':6340,'close':6330,'volume':3000},
              {'low':6300,'high':6320,'close':6310,'volume':200},
              {'low':6300,'high':6320,'close':6320,'volume':100}]
        setup,reason,evidence=d.pullback_analysis(bars)
        self.assertIsNone(setup);self.assertEqual(reason,'IMPULSE_CLOSE_PROGRESS_INSUFFICIENT')
        self.assertEqual(evidence['wick_based_rise'],240);self.assertEqual(evidence['impulse_close_progress'],0)
    def test_real_closed_price_advance_retains_existing_B_geometry_and_thresholds(self):
        d=self.detector()
        bars=[{'low':10000,'high':10050,'close':10040,'volume':1000},
              {'low':10040,'high':10200,'close':10190,'volume':2000},
              {'low':10100,'high':10180,'close':10140,'volume':300},
              {'low':10110,'high':10160,'close':10150,'volume':200}]
        s=d.pullback(bars);self.assertIsNotNone(s)
        self.assertEqual((s['breakout'],s['support'],s['projection']),(10180,10100,10400))
        self.assertEqual(d.a.pullback_min_rise,.008)
    def test_declining_closes_do_not_create_up_impulse_from_extreme_wick(self):
        d=self.detector()
        bars=[{'low':6100,'high':6330,'close':6330,'volume':1000},
              {'low':6310,'high':6340,'close':6310,'volume':1000},
              {'low':6300,'high':6320,'close':6310,'volume':100},
              {'low':6300,'high':6310,'close':6305,'volume':50}]
        self.assertIsNone(d.pullback(bars))
