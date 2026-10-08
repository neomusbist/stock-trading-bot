import copy,json,os,queue,sys,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from core.realtime import Feed,parse_book,parse_tick
from core.settings import Config,KST
from core.strategy import Tick
from automation.patterns import PatternDetector
from automation.capital import Costs
from automation.policy import AutomationConfig
from automation.runner import process_message

BASE=datetime(2026,10,7,18,10,tzinfo=KST);SYM='042510'
class DepthWarmupTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(Config(),after_enabled=True,risk_fraction=.02,max_daily_loss_fraction=.04,max_watchlist=12)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        costs=Costs(self.auto.commission_buy,self.auto.commission_sell,self.auto.sell_tax,
                    self.cfg.entry_slippage_fraction,self.cfg.exit_slippage_fraction)
        self.d=PatternDetector(self.cfg,self.auto,costs);self.d.continuous_observation=True
        self.watch={'code':SYM,'status':'WATCH','source_count':2,'market_status':{'pass':True},'resistance':20000}
        self.rest=Mock();self.rest.cfg=self.cfg
        self.feed=Feed(self.rest,[SYM])
    def book(self,ask=16310,bid=16300,aq=34,bq=56):
        return {'type':'0D','item':SYM,'values':{'21':'181000','41':str(ask),'51':str(bid),'61':str(aq),'71':str(bq),'121':'12345','125':'23456'}}
    def trade(self,stamp=BASE,price=16300,ask=16310,bid=16300,cumulative=1000):
        return {'type':'0B','item':SYM,'values':{'20':stamp.strftime('%H%M%S'),'10':str(price),'15':'10','13':str(cumulative),'27':str(ask),'28':str(bid),'290':'2'}}
    def tick(self,second,price=16300):
        return Tick(SYM,BASE+timedelta(seconds=second),price,100,10000+second,price+10,price,
                    ask_qty=10,bid_qty=10,depth_available=True)
    def emit(self,data,stamp=BASE):
        with patch('core.realtime.now',return_value=stamp):self.feed.emit({'trnm':'REAL','data':data})
    def test_fid_best_and_total_mapping_are_distinct(self):
        book=parse_book(self.book(),BASE)
        self.assertEqual([book[k] for k in ['best_ask','ask_qty','best_bid','bid_qty','total_ask_qty','total_bid_qty']],
            [16310,34,16300,56,12345,23456]);self.assertTrue(book['depth_available'])
    def test_watch_and_fast_parser_use_same_attached_depth(self):
        sink=[];self.feed.configure_observation({SYM},{SYM},lambda t,r:sink.append(t))
        self.emit([self.book(),self.trade()]);stamp,msg=self.feed.q.get_nowait()
        parsed=parse_tick(msg['data'][1],stamp)
        self.assertEqual((parsed.ask_qty,parsed.bid_qty),(34,56))
        self.assertEqual(parsed,sink[0]);self.rest.get_token.assert_not_called()
    def test_real_main_message_path_preserves_depth_without_order_evaluation(self):
        self.emit([self.book(),self.trade()]);stamp,msg=self.feed.q.get_nowait()
        e=Mock();e.c=self.cfg;e.market='3';e._depth_log_at={}
        with patch('automation.runner.now',return_value=BASE):
            n=process_message(e,self.d,msg,stamp,{SYM},{},Mock(),{},Mock(),True)
        self.assertEqual(n,1);self.assertEqual(self.d.last[SYM].ask_qty,34)
        e.enter.assert_not_called();e.event.assert_called_once()
        self.assertEqual(e.event.call_args.args[0],'BOOK_DEPTH_OBSERVED')
        self.assertEqual(e.event.call_args.kwargs['book']['raw_fields']['61'],'34')
    def test_planned_one_to_six_shares_can_pass_actual_best_depth(self):
        for price in [16300,9990,6660,4990,3990,3320]:
            planned=int(20000//price);item=self.trade(price=price,ask=price+1,bid=price)
            item['_book']=parse_book(self.book(ask=price+1,bid=price,aq=planned,bq=planned),BASE)
            t=parse_tick(item,BASE);gate=self.d.base_gates(t,self.watch)['liquidity']
            with self.subTest(planned=planned):
                self.assertEqual(gate['result'],'PASS');self.assertEqual(gate['values']['planned_qty'],1) # observation minimum; actual quantity rechecked at BUY boundary
    def test_known_small_or_zero_best_depth_still_fails_not_total_substitution(self):
        for ask_qty in [0,1]:
            item=self.trade(price=7990,ask=8000,bid=7990)
            item['_book']=parse_book(self.book(ask=8000,bid=7990,aq=ask_qty,bq=100),BASE)
            gate=self.d.base_gates(parse_tick(item,BASE),self.watch)['liquidity']
            self.assertEqual(gate['result'],'FAIL' if ask_qty==0 else 'PASS');self.assertEqual(gate['values']['data_status'],'AVAILABLE')
            self.assertEqual(gate['values']['quantity_scope'],'MINIMUM_OBSERVATION_ONLY')
    def test_explicit_zero_or_missing_depth_never_borrows_watch_snapshot_quantity(self):
        watch={**self.watch,'ask_qty':9999,'bid_qty':9999}
        for available,expected in [(True,'FAIL'),(False,'UNKNOWN')]:
            t=replace(self.tick(0),ask_qty=0,bid_qty=0,depth_available=available)
            gate=self.d.base_gates(t,watch)['liquidity']
            self.assertEqual(gate['result'],expected)
            self.assertEqual((gate['values']['ask_qty'],gate['values']['bid_qty']),(0,0))
    def test_missing_or_price_only_depth_is_data_unavailable(self):
        for book in [None,parse_book({'type':'0C','values':{'27':'16310','28':'16300'}},BASE)]:
            item=self.trade();item['_book']=book
            gate=self.d.base_gates(parse_tick(item,BASE),self.watch)['liquidity']
            self.assertEqual(gate['result'],'UNKNOWN');self.assertEqual(gate['values']['data_status'],'DATA_UNAVAILABLE')
    def test_stale_and_mismatched_book_never_use_old_positive_depth(self):
        stale=parse_book(self.book(),BASE-timedelta(seconds=6))
        mismatch=parse_book(self.book(ask=16320),BASE)
        for book,reason in [(stale,'BOOK_STALE'),(mismatch,'BOOK_PRICE_MISMATCH')]:
            item=self.trade();item['_book']=book;t=parse_tick(item,BASE)
            self.assertFalse(t.depth_available);self.assertEqual(t.depth_source,reason)
            self.assertEqual(self.d.base_gates(t,self.watch)['liquidity']['result'],'UNKNOWN')
    def test_disconnect_clears_cached_depth(self):
        self.emit([self.book()]);self.assertIn(SYM,self.feed.orderbooks)
        self.feed.emit({'trnm':'DISCONNECTED'});self.assertEqual(self.feed.orderbooks,{})
        self.emit([self.trade()]);self.assertFalse(parse_tick(self.trade(),BASE).depth_available)
    def test_sparse_rolling_span_reproduces_72_to_68_but_warmup_stays_pass(self):
        for second in list(range(111))+list(range(115,183)):
            t=self.tick(second);self.assertTrue(self.d.ingest(t,t.time))
        first=self.d.base_gates(t,self.watch)['warmup']
        t=self.tick(183);self.assertTrue(self.d.ingest(t,t.time))
        second=self.d.base_gates(t,self.watch)['warmup']
        self.assertEqual((first['values']['buffer_span_seconds'],second['values']['buffer_span_seconds']),(72,68))
        self.assertEqual((first['values']['elapsed_seconds'],second['values']['elapsed_seconds']),(182,183))
        self.assertEqual((first['result'],second['result']),('PASS','PASS'))
        self.assertEqual((first['values']['completed_bars'],second['values']['completed_bars']),(3,3))
    def test_elapsed_68_70_72_monotonic_without_lowering_70_second_requirement(self):
        for second in range(73):
            t=self.tick(second);self.d.ingest(t,t.time)
            if second in {68,70,72}:
                gate=self.d.base_gates(t,self.watch)['warmup']
                self.assertEqual(gate['values']['elapsed_seconds'],second)
                self.assertEqual(gate['values']['required_seconds'],70)
                if second==68:self.assertEqual(gate['result'],'FAIL')
    def test_actual_gap_and_explicit_reset_clear_epoch(self):
        self.d.continuous_observation=False
        for second in range(181):t=self.tick(second);self.d.ingest(t,t.time)
        t=self.tick(186);self.assertTrue(self.d.ingest(t,t.time))
        self.assertEqual(self.d.base_gates(t,self.watch)['warmup']['values']['elapsed_seconds'],0)
        self.assertEqual(self.d.last_gap_reason[SYM],'DISCONNECT_GAP')
        self.d.forget(SYM);self.assertNotIn(SYM,self.d.observation_start)
        t=self.tick(190);self.d.ingest(t,t.time);self.d.reset()
        self.assertEqual(self.d.observation_start,{})
    def test_role_change_retains_epoch_and_b_c_bar_minima(self):
        for second in range(181):t=self.tick(second);self.d.ingest(t,t.time)
        start=self.d.observation_start[SYM]
        for status in ['WATCH','RESERVE','WATCH']:
            watch={**self.watch,'status':status};self.assertEqual(self.d.base_gates(t,watch)['warmup']['result'],'PASS')
            trials=self.d.setup_trials(t,watch)
            for trial in trials[1:]:self.assertIn('INSUFFICIENT_USABLE_BARS',trial['failed_reasons'])
        self.assertEqual(self.d.observation_start[SYM],start)
    def test_regular_to_after_has_separate_warmup_epoch(self):
        morning=replace(self.tick(0),time=BASE.replace(hour=14))
        self.assertTrue(self.d.ingest(morning,morning.time))
        t=replace(self.tick(0),cumulative=20000);self.assertTrue(self.d.ingest(t,t.time))
        self.assertEqual(self.d.base_gates(t,self.watch)['warmup']['values']['elapsed_seconds'],0)
        self.assertEqual(self.d.observation_start[SYM],BASE)
    def test_new_numeric_diagnostics_are_json_serializable(self):
        self.emit([self.book(),self.trade()]);stamp,msg=self.feed.q.get_nowait()
        t=parse_tick(msg['data'][1],stamp);self.d.ingest(t,t.time)
        json.dumps(self.d.base_gates(t,self.watch));json.dumps(self.d.setup_trials(t,self.watch))

if __name__=='__main__':unittest.main(verbosity=2)
