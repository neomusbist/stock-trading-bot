"""038500 incident: quote silence, socket failure, detector rejection and executable breakout."""
import contextlib,copy,io,tempfile,time,threading,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from core.broker import Rejected
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.position_health import PositionDataHealth
from automation.runner import SubscriptionState,process_message,AccountWorker
from automation.patterns import PatternDetector
from tests.test_automation import FakeBroker

SYM='038500';BASE=datetime(2026,10,8,13,32,34,tzinfo=KST)

class PositionHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        out=contextlib.redirect_stdout(io.StringIO());out.__enter__();self.addCleanup(out.__exit__,None,None,None)
        self.root=Path(self.tmp.name);self.clock=BASE
        self.cfg=replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04,max_positions=1)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True)
        self.policy=Policy(self.root/'policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.b=FakeBroker();self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.b,lambda:self.clock)
        self.e.reconcile({'cash':81300,'equity':81300,'orders':[],'holdings':{}})
        self.e.connected=True;self.e.market='3'
        self.e.s['positions'][SYM]={'qty':9,'avg':7210,'stop':7140,'initial_stop':7140,'initial_r':70,
            'high':7240,'low':7170,'ownership':'BOT','qty_blocked':False,'opened':BASE.isoformat(),
            'last_tick':BASE.isoformat(),'exit_requested':False,'exit_reason':'','exit_attempts':0,'signal':{'pattern':'C'}}
        self.e.actual={SYM:{'qty':9,'available':9,'avg':7210}};self.e.prices={SYM:7170}
        self.feed=SimpleNamespace(subscription_lock=threading.RLock(),orderbooks={},last_message_at=BASE)
        self.account=SimpleNamespace(position_symbols=frozenset(),request=threading.Event(),need_fills=False)
        self.sub=SubscriptionState({SYM});self.h=PositionDataHealth(self.e,self.feed,self.account,self.sub)
    def advance(self,seconds):
        self.clock=BASE+timedelta(seconds=seconds);self.feed.last_message_at=self.clock
        self.e.last_account=time.monotonic()
    def book(self,bid=7170,age=0):
        return {'time':(self.clock-timedelta(seconds=age)).isoformat(),'best_bid':bid,'best_ask':bid+10}
    def sell_count(self):return sum(o[0]=='SELL' for o in self.b.orders)
    def test_quiet_trade_fresh_websocket_book_no_emergency_sale(self):
        for second in (16,30,50,90):
            self.advance(second);self.feed.orderbooks[SYM]=self.book()
            self.e.timer(self.e.prices)
            self.assertFalse(self.e.s['positions'][SYM]['exit_requested']);self.assertFalse(self.e.s['halt'])
        self.assertEqual(self.sell_count(),0)
        self.assertEqual(self.e.s['positions'][SYM]['data_health']['reason_code'],'TRADE_QUIET_QUOTE_FRESH')
    def test_no_book_schedules_read_only_probe_and_blocks_new_entry(self):
        self.advance(16);self.e.timer(self.e.prices)
        self.assertEqual(self.account.position_symbols,frozenset({SYM}));self.assertTrue(self.account.request.is_set())
        self.assertEqual(self.e.new_entry_reason('999990'),'POSITION_DATA_RECHECK_PENDING');self.assertEqual(self.sell_count(),0)
    def test_fresh_rest_book_restores_management_without_fabricating_tick(self):
        self.advance(16);self.e.timer(self.e.prices);before=copy.deepcopy(self.e.s['positions'][SYM])
        book={'time':self.clock.isoformat(),'bid':7170,'regular':True,'status':{'pass':True}}
        self.assertTrue(self.h.accept_book(SYM,book,self.clock,'ka10004'));self.advance(18);self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),0)
        for key in ('last_tick','high','low','stop'):self.assertEqual(self.e.s['positions'][SYM][key],before[key])
    def test_ping_and_account_alone_cannot_hold_blind_position_forever(self):
        self.advance(16);self.e.timer(self.e.prices)
        self.advance(32);self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),1)
        self.assertEqual(self.e.s['positions'][SYM]['data_health']['reason_code'],'POSITION_DATA_UNVERIFIED_TIMEOUT')
        self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),1)
    def test_real_disconnect_not_excused_by_cached_fresh_book(self):
        self.advance(16);self.feed.orderbooks[SYM]=self.book();self.e.disconnect()
        self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),1)
        self.assertEqual(self.e.s['positions'][SYM]['data_health']['reason_code'],'GLOBAL_DISCONNECTED')
    def test_account_failure_keeps_sell_intent_but_no_unsafe_submission(self):
        self.advance(16);self.e.connected=False;self.e.account_ok=False
        self.e.timer(self.e.prices);self.assertTrue(self.e.s['positions'][SYM]['exit_requested'])
        self.assertEqual(self.sell_count(),0)
    def test_book_bid_touch_is_not_structural_trade_stop(self):
        self.advance(16);self.feed.orderbooks[SYM]=self.book(7140);self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),0)
    def test_book_bid_collapse_remains_emergency_exit(self):
        self.advance(16);self.feed.orderbooks[SYM]=self.book(7100);self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),1);self.assertEqual(self.e.s['positions'][SYM]['exit_reason'],'호가 급락/유동성 붕괴')
    def test_stale_future_invalid_and_after_rest_book_never_refresh_health(self):
        self.advance(16)
        for book in [self.book(age=20),self.book(age=-5),{'time':self.clock.isoformat(),'best_bid':0,'best_ask':10}]:
            self.assertFalse(self.h.accept_book(SYM,book,self.clock))
        self.clock=BASE.replace(hour=17)
        self.assertFalse(self.h.accept_book(SYM,{'time':self.clock.isoformat(),'bid':7170,'regular':True},self.clock,'ka10004'))
    def test_detector_rejected_fresh_trade_still_triggers_stop(self):
        self.advance(1);d=Mock();d.completed={};d.ingest.return_value=False
        packet={'trnm':'REAL','data':[{'type':'0B','item':SYM,'values':{'20':self.clock.strftime('%H%M%S'),
            '10':'7140','15':'0','13':'100','27':'7150','28':'7140','290':'2'}}]}
        with patch('automation.runner.now',return_value=self.clock):
            self.assertEqual(process_message(self.e,d,packet,self.clock,{SYM},{},Mock(),self.e.prices,self.account,True,self.sub),0)
        self.assertEqual(self.sell_count(),1);self.assertEqual(self.e.s['positions'][SYM]['exit_reason'],'구조/이익보호선 이탈')
    def test_hard_lock_never_sells_even_on_real_connection_failure(self):
        p=self.e.s['positions'].pop(SYM)
        for sym in ('005930','017900'):self.e.s['positions'][sym]=copy.deepcopy(p)
        self.advance(16);self.e.connected=False;self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),0)
    def test_repeated_book_does_not_refresh_timestamp(self):
        self.advance(16);book=self.book();self.assertTrue(self.h.accept_book(SYM,book,self.clock))
        self.advance(32);self.assertFalse(self.h.accept_book(SYM,book,self.clock))
        self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),0)
        self.advance(48);self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),1)
    def test_real_breakout_price_with_ask_below_breakout_is_rejected(self):
        d=PatternDetector(self.cfg,self.auto,self.e.costs)
        t=Tick(SYM,self.clock,7230,100,1000,7210,7200)
        setup={'pattern':'C','breakout':7220,'support':7150,'projection':7360}
        gate=d.setup_gates(t,{'status':'WATCH','resistance':7500},setup)['breakout']
        self.assertEqual(gate['result'],'FAIL');self.assertEqual(gate['values']['reason'],'BREAKOUT_QUOTE_NOT_CONFIRMED')
    def test_engine_boundary_rechecks_executable_breakout(self):
        self.e.s['positions'].clear();self.e.market_status={SYM:{'pass':True}}
        signal={'symbol':SYM,'limit':7210,'stop':7140,'pattern':'C','time':self.clock.isoformat(),
            'efficiency':{'decision':'TRADE'},'evaluation_gates':{'breakout':{'result':'PASS','values':{'breakout':7220}}}}
        t=Tick(SYM,self.clock,7230,100,1000,7210,7200)
        self.assertIsNone(self.e.enter(signal,t));self.assertEqual(self.b.orders,[])

    def test_closed_position_does_not_leave_permanent_entry_block_or_probe(self):
        self.advance(16);self.e.timer(self.e.prices);self.assertIn(SYM,self.h.waiting)
        self.e.s['positions'].clear()
        self.assertNotEqual(self.e.new_entry_reason('999990'),'POSITION_DATA_RECHECK_PENDING')
        self.assertEqual(self.h.waiting,{});self.assertEqual(self.account.position_symbols,frozenset())

    def test_probe_requests_stop_when_fresh_book_recovers(self):
        self.advance(16);self.e.timer(self.e.prices)
        self.assertTrue(self.h.accept_book(SYM,self.book(),self.clock))
        self.assertEqual(self.account.position_symbols,frozenset())

    def test_book_probe_is_read_only_worker_path_for_held_symbol(self):
        rest=Mock();worker=AccountWorker(rest,5);worker.position_symbols=frozenset({SYM})
        rest.profile={};rest.fills.return_value=[]
        rest.snapshot.return_value={'cash':15000,'holdings':{SYM:{'qty':9}},'orders':[]}
        rest.existing_book.return_value={'time':BASE.isoformat(),'bid':7170,'regular':True}
        worker.request.wait=lambda _:worker.stop.set()
        worker.run();result=worker.q.get_nowait()
        rest.existing_book.assert_called_once_with(SYM)
        rest.fills.assert_called_once_with()
        self.assertIn(SYM,result['snapshot']['books'])
        rest.submit.assert_not_called();rest.cancel.assert_not_called()

    def test_unknown_sell_does_not_generate_duplicate(self):
        self.e.s['orders']['unknown']={'status':'UNKNOWN','side':'SELL','symbol':SYM,'qty':9,'filled':0,
            'created':BASE.isoformat(),'id':'UNCONFIRMED','local':'unknown','limit':0}
        self.advance(16);self.e.connected=False
        with patch.object(self.e,'cancel') as cancel:
            self.e.timer(self.e.prices);self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),0);self.assertTrue(self.e.s['positions'][SYM]['exit_requested'])

    def test_actual_fresh_trade_stop_remains_independent_of_quote_health(self):
        self.advance(1);self.feed.orderbooks[SYM]=self.book()
        self.e.on_tick(Tick(SYM,self.clock,7140,10,100,7150,7140))
        self.assertEqual(self.sell_count(),1)

    def test_non_finite_book_never_refreshes_health(self):
        self.advance(16)
        for bid in (float('nan'),float('inf')):
            self.assertFalse(self.h.accept_book(SYM,self.book(bid),self.clock))

    def test_final_broker_guard_rejects_forged_below_breakout_intent(self):
        self.e.s['positions'].clear()
        self.e.s['orders']['forged']={'local':'forged','status':'INTENT','side':'BUY','symbol':SYM,'qty':9,
            'limit':7210,'filled':0,'signal':{'evaluation_gates':{'breakout':{'values':{'breakout':7220}}}}}
        with self.assertRaisesRegex(Rejected,'BREAKOUT_QUOTE_NOT_CONFIRMED'):
            self.e.order_guard('kt10000',{'stk_cd':SYM,'ord_qty':'9','ord_uv':'7210'})
        self.assertEqual(self.b.orders,[])

if __name__=='__main__':unittest.main()
