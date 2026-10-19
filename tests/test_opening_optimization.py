"""Opening observation and lossless hot-path optimization: offline only."""
import contextlib,io,queue,tempfile,threading,time,unittest
from collections import deque
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from core.realtime import Feed,parse_tick
from core.util import atomic_json,write_csv
from automation.capital import Costs
from automation.policy import AutomationConfig,Policy
from automation.patterns import PatternDetector
from automation.shadow_observation import ShadowObserver
from automation.observation_worker import ObservationWorker
from automation.radar import DynamicRadar
from automation.focus_selection import select_focus
from automation import runner
from tests import test_audit_followup as fixtures
from tests.test_v1 import bars
from tests.test_runner_loop import FakeRest

class OpeningOptimizationTests(unittest.TestCase):
    def fixture(self,pattern='B'):
        f=fixtures.AuditFixture();f.setUp();self.addCleanup(f.doCleanups)
        return f,*fixtures.SetupReachabilityTests.build(f,pattern)
    def test_single_snapshot_computes_bars_and_each_candidate_price_gates_once(self):
        f,d,t,w=self.fixture();d.ingest(t,t.time)
        with patch.object(d,'usable_bars',wraps=d.usable_bars) as usable,patch.object(d,'setup_gates',wraps=d.setup_gates) as prices:
            sig=d.signal(t,w)
        self.assertEqual(sig['pattern'],'B');self.assertEqual(usable.call_count,1)
        self.assertEqual(prices.call_count,sum(x['candidate_generated'] for x in sig['setup_trials']))
        self.assertEqual(sig['efficiency'],sig['evaluation_gates']['economic']['values']['score'])
    def test_same_timestamp_trade_counter_changed_spread_does_not_reuse_pass(self):
        f,d,t,w=self.fixture();d.ingest(t,t.time);self.assertIsNotNone(d.signal(t,w))
        bad=replace(t,ask=t.price*1.1)
        self.assertEqual(d.evaluation_gates(bad,w)['spread']['result'],'FAIL')
    def test_same_timestamp_changed_market_status_does_not_reuse_pass(self):
        f,d,t,w=self.fixture();d.ingest(t,t.time);d.signal(t,w)
        w['market_status']['pass']=False
        self.assertEqual(d.evaluation_gates(t,w)['market_status']['result'],'FAIL')
    def test_same_timestamp_depth_change_is_rechecked(self):
        f,d,t,w=self.fixture();t=replace(t,time=t.time.replace(hour=17),depth_available=True,ask_qty=100,bid_qty=100)
        d.signal(t,w);self.assertEqual(d.evaluation_gates(t,w)['liquidity']['result'],'PASS')
        self.assertEqual(d.evaluation_gates(replace(t,ask_qty=0),w)['liquidity']['result'],'FAIL')
    def test_window_boundaries_and_future_tick_exclusion_remain_exact(self):
        f,d,t,w=self.fixture();d.buffers[t.symbol]=deque(replace(t,time=t.time-timedelta(seconds=s),price=1000,volume=10) for s in [71,70,69,10,9,0,-1])
        gates,_,fast,base=d._base_evaluation(t,w)
        self.assertEqual((fast,base),(2,2));self.assertEqual(gates['liquidity']['values']['window_turnover'],20000)
    def test_virtual_size_runs_only_for_emitted_shadow_records_not_promotion(self):
        f,d,t,w=self.fixture();records=[]
        o=ShadowObserver(f.cfg,f.auto,f.e.costs,lambda kind,**kw:records.append((kind,kw)))
        o.detector=d;o.detector.continuous_observation=True
        row={**w,'reference_price':t.price,'affordability':{'entry_eligible':True},'shadow_view':w}
        o.update([row],t.time)
        with patch.object(o,'virtual_size',wraps=o.virtual_size) as sizing:
            for s in (0,2,4,6):
                x=replace(t,time=t.time+timedelta(seconds=s),cumulative=t.cumulative+s);o.tick(x,x.time)
            emitted=[v for k,v in records if k=='SHADOW_EVAL']
            self.assertEqual(sizing.call_count,sum(x['candidate_generated'] for v in emitted for x in v['setup_trials']))
        self.assertTrue(emitted);self.assertTrue(all(v['actual_order_permission'] is False for v in emitted))
        self.assertTrue(o.last_tier)
    def test_daily_file_cache_invalidates_on_file_change_day_and_deletion(self):
        f,d,t,w=self.fixture();worker=ObservationWorker(f.cfg,f.auto,f.e.costs,f.root)
        path=f.root/'data/raw/999990_daily.csv';history=bars();write_csv(path,history)
        from candidate_builder import bars_from_csv
        with patch('automation.observation_worker.bars_from_csv',wraps=bars_from_csv) as read:
            initial=worker.daily_bars('999990',path,t.time.date());self.assertIs(initial,worker.daily_bars('999990',path,t.time.date()))
            self.assertEqual(read.call_count,1)
            history[-1]['volume']+=1;write_csv(path,history)
            self.assertEqual(worker.daily_bars('999990',path,t.time.date())[-1]['volume'],history[-1]['volume']);self.assertEqual(read.call_count,2)
            worker.daily_bars('999990',path,t.time.date()+timedelta(days=1));self.assertEqual(read.call_count,3)
            path.unlink();self.assertEqual(worker.daily_bars('999990',path,t.time.date()),[]);self.assertNotIn('999990',worker.daily_cache)
    def test_status_refresh_preserves_independent_fetched_chart_and_retry_budget(self):
        f,d,t,w=self.fixture();worker=ObservationWorker(f.cfg,f.auto,f.e.costs,f.root)
        worker.metadata['999990']={'checked':t.time,'status':{'pass':True},'bars':bars(),'last_chart_attempt':t.time}
        row={**w,'reference_price':1000,'affordability':{'entry_eligible':True}}
        with patch('automation.observation_worker.fetch_bars',side_effect=AssertionError('duplicate chart REST')):
            worker.prepare([row],t.time,{})
        self.assertEqual(len(worker.metadata['999990']['bars']),750)
        self.assertEqual(worker.metadata['999990']['last_chart_attempt'],t.time)
    def test_daily_cache_is_bounded_without_releasing_observation(self):
        f,d,t,w=self.fixture();worker=ObservationWorker(f.cfg,f.auto,f.e.costs,f.root,limit=2)
        path=f.root/'data/raw/daily.csv';write_csv(path,bars())
        worker.observer.rows={'999990':{'status':'WATCH'}}
        for i in range(9):worker.daily_bars(str(i),path,t.time.date())
        self.assertEqual(len(worker.daily_cache),4);self.assertEqual(set(worker.observer.rows),{'999990'})
    def test_new_high_tier_replaces_weak_incumbent_and_same_tier_plus2_holds(self):
        stamp=datetime(2026,10,8,9,1,tzinfo=KST)
        def row(sym,score):return {'code':sym,'status':'WATCH','source_count':1,'discovery_score':score,'affordability':{'entry_eligible':True},'market_status':{'pass':True}}
        a=row('999980',20);b=row('999990',21)
        tied=select_focus({'999980':a},[a,b],{},1,True,stamp.isoformat());self.assertEqual(tied.next_watch,['999980'])
        gates={k:{'result':'PASS'} for k in ('warmup','acceleration','liquidity','spread','structure','market_status')}
        ready={'999990':{'timestamp':stamp.isoformat(),'gates':gates,'tier':2},'999980':{'timestamp':stamp.isoformat(),'gates':{'acceleration':{'result':'FAIL'}},'tier':4}}
        promoted=select_focus({'999980':a},[a,b],ready,1,True,stamp.isoformat());self.assertEqual(promoted.next_watch,['999990'])
    def test_feed_frozen_tick_shared_with_watch_without_second_parser(self):
        f,d,t,w=self.fixture();sink=[];broker=Mock();broker.cfg=f.cfg;feed=Feed(broker,[t.symbol])
        feed.configure_observation({t.symbol},{t.symbol},lambda tick,received:sink.append(tick))
        item={'type':'0B','item':t.symbol,'values':{'20':t.time.strftime('%H%M%S'),'10':str(t.price),'15':str(t.volume),'13':str(t.cumulative),'27':str(t.ask),'28':str(t.bid),'290':'2'}}
        with patch('core.realtime.now',return_value=t.time):feed.emit({'trnm':'REAL','data':[item]})
        received,message=feed.q.get_nowait()
        with patch('automation.runner.now',return_value=t.time),patch('automation.runner.parse_tick',side_effect=AssertionError('duplicate parser')):
            self.assertEqual(runner.process_message(f.e,d,message,received,{t.symbol},{},Mock(),{},Mock(),True),1)
        self.assertIs(d.last[t.symbol],sink[0]);self.assertEqual(sink[0],parse_tick(item,received,received_monotonic=message['_received_monotonic']))
        self.assertEqual(f.broker.orders,[])
    def test_untrusted_or_other_received_frame_cannot_supply_parsed_tick(self):
        f,d,t,w=self.fixture()
        item={'type':'0B','item':t.symbol,'values':{'20':t.time.strftime('%H%M%S'),'10':str(t.price),'15':str(t.volume),'13':str(t.cumulative),'27':str(t.ask),'28':str(t.bid),'290':'2'}}
        for supplied in ([t.time,asdict(t)],(t.time-timedelta(seconds=1),t)):
            d.reset();item['_observation_tick']=supplied
            with patch('automation.runner.now',return_value=t.time),patch('automation.runner.parse_tick',wraps=parse_tick) as parse:
                runner.process_message(f.e,d,{'trnm':'REAL','data':[item]},t.time,{t.symbol},{},Mock(),{},Mock(),True)
            parse.assert_called_once()

class OpeningStreamTests(unittest.TestCase):
    def test_preopen_multi_symbol_universe_to_three_setups_and_one_fake_fill(self):
        # The GUI daily click and real runner time boundary are independently covered above
        # and in test_next_day_first_fill; here exercise real discovery + observation + order core.
        f=fixtures.AuditFixture();f.setUp();self.addCleanup(f.doCleanups)
        cfg=replace(f.cfg,risk_fraction=.02,max_daily_loss_fraction=.04,max_positions=1,max_entries_per_day=3,max_watchlist=12)
        f.cfg=cfg;f.e.c=cfg;f.clock[0]=datetime(2026,10,8,8,55,tzinfo=KST);f.e.roll_day();f.e.reconcile(f.snap);f.e.connected=True
        radar=DynamicRadar(cfg,f.auto,f.policy,fallback_source1=True)
        radar.capital_context={'effective_auto_cap':20000,'account_fresh':True}
        raw=[{'code':f'{900000+i:06}','name':'합성','reference_price':1000 if i<24 else 30000,
              'source_details':{key:{'observed_rank':i+1,'values':{}} for key in (('one','two') if i<12 else ('one',))}} for i in range(28)]
        history=bars()
        for b in history:b.update(datetime=(datetime.fromisoformat(b['datetime'])+timedelta(days=8)).date().isoformat(),open=900,high=950,low=880,close=900,volume=2000000)
        def evaluate(row):return {**__import__('automation.radar',fromlist=['observation_structure']).observation_structure(history,row['reference_price'],cfg,f.clock[0].date()),'market_status':{'pass':True}}
        rows,watch,_=radar.update(raw,evaluate,f.clock[0]);self.assertEqual(len(rows),28);self.assertEqual(len(watch),12)
        self.assertEqual(sum(r['affordability']['entry_eligible'] is True for r in rows),24)
        observer=ShadowObserver(cfg,f.auto,f.e.costs,lambda *a,**k:None)
        observer.update(rows,f.clock[0]);self.assertEqual(len(observer.rows),24)
        feed=Feed(f.broker,[]);subscriptions=runner.SubscriptionState()
        self.assertTrue(subscriptions.refresh(f.e,PatternDetector(cfg,f.auto,f.e.costs),feed,set(observer.rows),f.clock[0]))
        self.assertEqual(set(feed.symbols),set(observer.rows));self.assertEqual(f.broker.orders,[])
        # Stronger tier may replace an incumbent; same-tier +2 remains covered separately.
        f.clock[0]=f.clock[0].replace(hour=9,minute=0);f.e.connected=True;f.e.market='3';f.e.last_radar_at=f.clock[0];f.e.last_regular_tick_at=f.clock[0]
        self.assertEqual(runner.readiness_snapshot(f.e,{'subscription_acks':1},True,f.clock[0])['state'],'OPEN_READY')
        for index,pattern in enumerate(('A','B','C')):
            f.clock[0]=f.clock[0].replace(minute=2 if pattern=='A' else 4 if pattern=='B' else 5);f.e.last_account=time.monotonic()
            detector,t,w=fixtures.SetupReachabilityTests.build(f,pattern);detector.ingest(t,t.time);sig=detector.signal(t,w)
            self.assertEqual(sig['pattern'],pattern)
            sym=f'{900000+index:06}';t=replace(t,symbol=sym);sig={**sig,'symbol':sym};f.e.market_status[sym]={'pass':True}
            order=f.e.enter(sig,t)
            if pattern=='A':
                self.assertIsNotNone(order);self.assertEqual(f.e.s['entries'],{})
                f.e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
                f.e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
                self.assertEqual(sum(f.e.s['entries'].values()),1)
            else:self.assertIsNone(order)
        self.assertEqual(len(f.broker.orders),1);self.assertEqual(len(f.e.s['positions']),1)
        self.assertEqual(f.e.s['entry_diagnostics']['entry_count'],1);self.assertEqual(f.e.c.max_entries_per_day,3)
    def test_real_ingest_can_first_generate_a2_b4_c5_at_the_earliest_minutes(self):
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,max_watchlist=12)
        auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        start=datetime(2026,10,8,9,0,tzinfo=KST)
        sequences={'A':[(980,1000,997,1000)]*2,
            # A valid B control must pass net RR and the existing chase cap.
            'B':[(960,1000,994,1000),(980,1040,1030,2000),(1020,1030,1028,200),(1020,1030,1028,100)],
            'C':[(1000,1010,1005,1000)]*5}
        for pattern,seq in sequences.items():
            with self.subTest(pattern=pattern):
                d=PatternDetector(cfg,auto,costs);d.continuous_observation=True;sym='999990';cum=0
                pre=Tick(sym,start-timedelta(seconds=1),999,100,100,1000,998)
                self.assertFalse(d.ingest(pre,pre.time));self.assertNotIn(sym,d.buffers)
                w={'status':'WATCH','source_count':2,'source_details':{'one':{},'two':{}},'market_status':{'pass':True},'resistance':2000}
                for minute,(low,high,close,vol) in enumerate(seq):
                    for second in range(60):
                        p=low if second==0 else high if second==1 else close;cum+=vol
                        t=Tick(sym,start+timedelta(minutes=minute,seconds=second),p,vol,cum,p+1,p-1)
                        self.assertTrue(d.ingest(t,t.time))
                        if minute==0 and second==0:self.assertIsNone(d.signal(t,w))
                price={'A':1001,'B':1031,'C':1011}[pattern];ask=price+1 if pattern=='A' else price
                t=Tick(sym,start+timedelta(minutes=len(seq)),price,100000,cum+100000,ask,price-1)
                self.assertTrue(d.ingest(t,t.time));sig=d.signal(t,w)
                self.assertIsNotNone(sig);self.assertEqual(sig['pattern'],pattern)
                self.assertEqual(len(d.usable_bars(sym)),len(seq));self.assertEqual(d.observation_start[sym],start)
                self.assertEqual(sig['time'],t.time.isoformat())
    def test_preopen_actual_runner_prepares_12_watch_subscriptions_before_open(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            root=Path(folder);auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
            atomic_json(root/'automation.json',asdict(auto));policy=Policy(root/'runtime/automation_policy.json');policy.command('ARMED')
            stamp=datetime(2026,10,8,8,55,tzinfo=KST);clock=[stamp]
            rows=[{'code':f'{900000+i:06}','name':'개장합성','status':'WATCH','source_count':1,'fallback_source1':True,'resistance':2000,
                   'reference_price':1000,'market_status':{'pass':True},'affordability':{'entry_eligible':True}} for i in range(12)]
            radar=Mock();radar.q=queue.Queue();radar.cache={};radar.q.put({'time':stamp.isoformat(),'rows':rows,'watch':rows,'changes':{},'error':None})
            account=Mock();account.q=queue.Queue();account.request=threading.Event()
            account.q.put({'snapshot':{'cash':20480,'equity':20480,'holdings':{},'orders':[]},'fills':[],'started':time.monotonic(),'error':None})
            feed=Mock();feed.overflow.is_set.return_value=False;calls=[];states=[]
            def frame(**kw):
                calls.append(clock[0])
                if len(calls)==1:return stamp,{'trnm':'REG','return_code':0}
                if len(calls)==2:
                    states.append((root/'runtime/automation-production/heartbeat.json').read_text(encoding='utf-8'))
                    clock[0]=stamp.replace(hour=9,minute=0);s=clock[0]
                    # Opening radar evidence remains separately mandatory and freshly produced.
                    radar.q.put({'time':s.isoformat(),'rows':rows,'watch':rows,'changes':{},'error':None})
                    account.q.put({'snapshot':{'cash':20480,'equity':20480,'holdings':{},'orders':[]},'fills':[],'started':time.monotonic(),'error':None})
                    return s,{'trnm':'REAL','data':[{'type':'0B','item':rows[0]['code'],'values':{'20':'090000','10':'1000','15':'100','13':'100','27':'1001','28':'999','290':'2'}}]}
                policy.command('STOP');raise queue.Empty
            feed.q.get.side_effect=frame;FakeRest.calls=[]
            with patch.object(runner,'Rest',FakeRest),patch.object(runner,'RadarWorker',return_value=radar),patch.object(runner,'AccountWorker',return_value=account),patch.object(runner,'Feed',return_value=feed),patch.object(runner,'Reporter',return_value=Mock()),patch.object(runner,'now',side_effect=lambda:clock[0]):
                result=runner.run(replace(Config(),max_watchlist=12,risk_fraction=.02,max_daily_loss_fraction=.04),root)
            feed.update_symbols.assert_any_call(sorted(r['code'] for r in rows))
            self.assertTrue(states);self.assertIn('PREOPEN',states[0]);self.assertEqual(result['counts']['ticks'],1)
            self.assertEqual(result['readiness']['state'],'OPEN_READY');self.assertEqual(result['real_orders_sent'],0);self.assertEqual(FakeRest.calls,[])

if __name__=='__main__':unittest.main()
