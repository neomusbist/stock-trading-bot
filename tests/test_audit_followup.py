"""관찰/실제 주문 분리, Setup 도달 가능성, 장애 시 청산을 합성 입력으로 검증."""
import contextlib
import copy
import io
import json
import queue
import tempfile
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST,ROOT
from core.strategy import Tick
from core.broker import Rejected,AmbiguousOrder,Rest
from core.realtime import Feed
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.capital import Costs
from automation.allocation import efficiency
from automation.radar import DynamicRadar,RadarWorker,observation_structure
from automation.runner import AccountWorker,ReadOnlyPilotPolicy,process_message,qualify_raw
from automation.observation_worker import ObservationWorker
from tests.test_automation import FakeBroker
from tests.test_v1 import bars

class AuditFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=[datetime(2026,10,2,12,0,tzinfo=KST)]
        self.cfg=replace(Config(),live_mode=True,max_position_fraction=1)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        self.policy=Policy(self.root/'policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.broker=FakeBroker()
        output=contextlib.redirect_stdout(io.StringIO());output.__enter__();self.addCleanup(output.__exit__,None,None,None)
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.broker,lambda:self.clock[0])
        self.snap={'cash':20420,'equity':20420,'orders':[],'holdings':{}}
        self.e.reconcile(self.snap);self.e.connected=True;self.e.market='3'
        self.e.market_status={'999990':{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'}}
    def tick(self,price=1001):return Tick('999990',self.clock[0],price,100,100,price+1,price-1)
    def signal(self,pattern='A'):
        return {'symbol':'999990','time':self.clock[0].isoformat(),'limit':1002,'stop':979,'pattern':pattern,'source_count':2,
            'efficiency':efficiency(1002,979,1040,.8,10,self.e.costs)}
    def buy(self,pattern='A',partial=False):
        order=self.e.enter(self.signal(pattern),self.tick());self.assertIsNotNone(order)
        total=1 if partial else order['qty'];self.e.cumulative_fill(order,total,total*order['limit'])
        self.e.actual={'999990':{'qty':total,'available':total,'avg':1002}};return order
    def sells(self):return [o for o in self.broker.orders if o[0]=='SELL']

class ExitAuditTests(AuditFixture):
    def test_invalidation_all_setups_persisted_and_market_sell(self):
        for setup in ('A','B','C'):
            with self.subTest(setup=setup):
                self.assertFalse(self.e.s['positions'])
                order=self.buy(setup);position=self.e.s['positions']['999990']
                self.assertEqual(position['initial_stop'],979);self.assertEqual(position['signal']['pattern'],setup)
                self.e.on_tick(self.tick(979));self.assertEqual(self.sells()[-1][3],0)
                sell=self.e.active_orders('999990','SELL')[0]
                self.e.cumulative_fill(sell,sell['qty'],sell['qty']*978)
                # 각 Setup은 독립 장부에서 검증한다. 재진입 금지를 해제하지 않는다.
                if setup!='C':
                    self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/setup,self.broker,lambda:self.clock[0]);self.e.reconcile(self.snap);self.e.connected=True;self.e.market='3';self.e.market_status={'999990':{'pass':True}}
    def test_fast_gap_loss_can_exceed_budget_but_no_duplicate_sell(self):
        buy=self.buy();self.e.on_tick(self.tick(900));self.e.on_tick(self.tick(890))
        self.assertEqual(len(self.sells()),1)
        sell=self.e.active_orders('999990','SELL')[0];self.e.cumulative_fill(sell,sell['qty'],sell['qty']*890)
        self.assertGreater(self.e.s['daily_loss'],60);self.assertNotIn('999990',self.e.s['positions'])
    def test_partial_buy_cancel_confirmation_before_residual_sell(self):
        buy=self.buy(partial=True);self.e.on_tick(self.tick(975))
        self.assertEqual(self.sells(),[]);self.assertEqual(buy['status'],'CANCEL_SENT')
        snap={**self.snap,'holdings':copy.deepcopy(self.e.actual)};self.e.reconcile(snap)
        self.e.drive_exits();self.assertEqual(self.sells()[0][2],1)
    def test_profit_then_reversal_exits_monotone_protection(self):
        self.buy();self.e.on_tick(self.tick(1050));stop=self.e.s['positions']['999990']['stop']
        self.assertGreater(stop,1002);self.e.on_tick(self.tick(stop+2));self.assertEqual(self.sells(),[])
        self.assertGreaterEqual(self.e.s['positions']['999990']['stop'],stop)
        self.e.on_tick(self.tick(stop));self.assertEqual(len(self.sells()),1)
    def test_large_rise_kept_without_fixed_profit_target(self):
        self.buy()
        for price in (1030,1100,1200,1300):self.e.on_tick(self.tick(price))
        self.assertEqual(self.sells(),[]);self.assertEqual(self.e.s['positions']['999990']['high'],1300)
    def test_1505_no_entry_1510_flatten_1520_no_order(self):
        self.buy();self.clock[0]=self.clock[0].replace(hour=15,minute=5)
        self.e.on_tick(self.tick())
        self.assertFalse(self.e.entry_window());self.e.timer();self.assertEqual(self.sells(),[])
        self.clock[0]=self.clock[0].replace(minute=10);self.e.on_tick(self.tick());self.e.timer();self.assertEqual(len(self.sells()),1)
        self.assertEqual(self.e.s['positions']['999990']['exit_reason'],'당일 청산 시각')
        self.clock[0]=self.clock[0].replace(minute=20);self.assertIsNone(self.e.submit('SELL','999990',1));self.assertEqual(len(self.sells()),1)
    def test_rejection_bounded_attempts_retains_position_and_exit_intent(self):
        self.buy()
        with patch.object(self.broker,'submit',side_effect=Rejected('SYNTHETIC_REJECT')) as submit:
            self.e.request_exit('999990','synthetic');self.e.drive_exits()
            for _ in range(8):self.e.drive_exits()
        self.assertEqual(submit.call_count,self.cfg.max_exit_attempts)
        self.assertTrue(self.e.s['positions']['999990']['exit_requested']);self.assertTrue(self.e.s['halt'])
    def test_ambiguous_sell_not_retransmitted(self):
        self.buy()
        with patch.object(self.broker,'submit',side_effect=AmbiguousOrder('SYNTHETIC_UNKNOWN')) as submit:
            self.e.request_exit('999990','synthetic');self.e.drive_exits()
            for _ in range(8):self.e.drive_exits()
        self.assertEqual(submit.call_count,1);self.assertEqual(self.e.active_orders('999990','SELL')[0]['status'],'UNKNOWN')
    def test_unfilled_sell_waits_cancel_then_account_confirmation(self):
        self.buy();self.e.request_exit('999990','synthetic');self.e.drive_exits();first=self.e.active_orders('999990','SELL')[0]
        self.clock[0]+=timedelta(seconds=self.cfg.order_timeout_seconds+1);self.e.timer()
        self.assertEqual(first['status'],'CANCEL_SENT');self.e.drive_exits();self.assertEqual(len(self.sells()),1)
        self.e.reconcile({**self.snap,'holdings':copy.deepcopy(self.e.actual)});self.e.drive_exits();self.assertEqual(len(self.sells()),2)
    def test_partial_sell_only_remaining_quantity_after_cancel(self):
        self.buy();self.e.request_exit('999990','synthetic');self.e.drive_exits();first=self.e.active_orders('999990','SELL')[0]
        self.assertGreater(first['qty'],1);self.e.cumulative_fill(first,1,980);self.e.actual['999990']['qty']-=1;self.e.actual['999990']['available']-=1
        self.e.drive_exits();self.assertEqual(len(self.sells()),1)
        self.e.cancel(first);self.e.reconcile({**self.snap,'holdings':copy.deepcopy(self.e.actual)});self.e.drive_exits()
        self.assertEqual(self.sells()[-1][2],first['qty']-1)
    def test_api_disconnect_keeps_exit_intent_until_fresh_rest_reconcile(self):
        self.buy();self.e.disconnect();self.e.account_ok=False;self.clock[0]+=timedelta(seconds=16);self.e.timer()
        self.assertTrue(self.e.s['positions']['999990']['exit_requested']);self.assertEqual(self.sells(),[])
        self.e.reconcile({**self.snap,'holdings':copy.deepcopy(self.e.actual)});self.e.timer();self.assertEqual(len(self.sells()),1)
    def test_reconciliation_mismatch_blocks_sell_without_discarding_position(self):
        self.buy();self.e.reconcile(self.snap);self.e.request_exit('999990','synthetic');self.e.drive_exits()
        self.assertTrue(self.e.s['positions']['999990']['qty_blocked']);self.assertEqual(self.sells(),[])
    def test_shadow_signal_and_source_floor_blocked_at_engine_boundary(self):
        for extra in ({'SHADOW_ONLY':True},{'actual_order_permission':False},{'source_count':0}):
            self.assertIsNone(self.e.enter({**self.signal(),**extra},self.tick()))
        self.e.a=replace(self.auto,min_source_count=2)
        self.assertIsNone(self.e.enter({**self.signal(),'source_count':1},self.tick()))
        self.assertEqual(self.broker.orders,[])
    def test_cap_zero_hardlocks_and_stop_keep_meaning(self):
        self.policy.command('STOP');self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        for sym in ('005930','017900'):self.assertIsNone(self.e.submit('SELL',sym,1))
        self.assertEqual(self.broker.orders,[])
    def test_raw_qualification_updates_buy_eligibility(self):
        self.e.a=replace(self.auto,min_source_count=2)
        # 1. source_count 1 revokes, source_count 2 restores
        qualify_raw(self.e,[{'code':'999990','source_count':1}],self.clock[0].isoformat())
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
        qualify_raw(self.e,[{'code':'999990','source_count':2}],self.clock[0].isoformat())
        self.assertIsNotNone(self.e.enter(self.signal(),self.tick()))
        # 2. empty raw revokes
        self.setUp()
        qualify_raw(self.e,[],self.clock[0].isoformat())
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
    def test_raw_qualification_does_not_block_protective_exit(self):
        self.buy();qualify_raw(self.e,[{'code':'999990','source_count':1}],self.clock[0].isoformat())
        self.e.on_tick(self.tick(975));self.assertEqual(len(self.sells()),1)
    def test_affordable_one_share_can_still_exceed_risk_budget_with_minimum_stop(self):
        details=self.e.quantity_details({'limit':10000,'stop':9980},20000,20000,200)
        self.assertGreater(details['capital_quantity'],0);self.assertGreater(details['risk_per_share'],60)
        self.assertEqual(details['quantity'],0);self.assertEqual(details['reason_code'],'RISK_BUDGET_ZERO_QTY')

class ObservationAuditTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000)
        self.costs=Costs(.00015,.00015,.002,.002,.003);self.start=datetime(2026,10,2,11,0,tzinfo=KST)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def row(self,sym='999990',sources=1):
        return {'code':sym,'name':'합성','reference_price':1000,'source_count':sources,'discovery_score':10*sources,
            'affordability':{'entry_eligible':True},'status':'REJECT','reason':'소스 교집합 부족',
            'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True}}}
    def test_worker_unknown_metadata_does_not_invent_normal_status(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root)
        row=self.row();del row['shadow_view'];worker.prepare([row],self.start.isoformat(),{})
        self.assertEqual(worker.state()['symbols'],[])
    def test_unaffordable_candidate_skips_queries_and_structure(self):
        # 1. economic exclusion skips stock_status
        rest=Mock();worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root,rest)
        row=self.row();row['affordability']={'entry_eligible':False,'reason_code':'ONE_SHARE_UNAFFORDABLE'}
        worker.prepare([row],self.start.isoformat(),{});rest.stock_status.assert_not_called();self.assertEqual(worker.state()['symbols'],[])
        # 2. unaffordable does not reach structure evaluation
        radar=DynamicRadar(self.cfg,self.auto,Policy(self.root/'policy'));radar.available_capital=1000
        item={'code':'999990','name':'합성','reference_price':2000,'source_details':{'one':{'observed_rank':1,'values':{}},'two':{'observed_rank':1,'values':{}}}}
        evaluate=Mock();rows,watch,_=radar.update([item],evaluate,self.start);evaluate.assert_not_called();self.assertEqual(watch,[])
    def test_dynamic_cap_changes_universe_without_hardcoded_price(self):
        policy=Policy(self.root/'policy');radar=DynamicRadar(self.cfg,self.auto,policy)
        radar.capital_context={'effective_auto_cap':5000,'account_fresh':True}
        row={'reference_price':6000};self.assertFalse(radar.affordable(row))
        radar.capital_context['effective_auto_cap']=10000;self.assertTrue(radar.affordable(row))
    def test_single_source_still_not_live_watch(self):
        radar=DynamicRadar(self.cfg,self.auto,Policy(self.root/'policy'));radar.available_capital=20000
        item={'code':'999990','name':'합성','reference_price':1000,'source_details':{'one':{'observed_rank':1,'values':{}}}}
        evaluate=Mock();rows,watch,_=radar.update([item],evaluate,self.start);self.assertEqual(watch,[]);evaluate.assert_not_called()
    def test_low_price_not_banned_from_observation(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);row=self.row();row['reference_price']=400
        worker.observer.update([row],self.start);self.assertIn('999990',worker.observer.rows)
    def test_first_discovery_price_not_replaced_by_later_source_price(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);row=self.row()
        row.update(first_raw_at=self.start.isoformat(),first_raw_price=990,reference_price=1000)
        worker.observer.update([row],self.start+timedelta(seconds=4))
        self.assertEqual(worker.observer.first['999990']['first_raw_price'],990)
    def test_observer_capacity_and_tenure_table(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root,limit=2)
        worker.observer.update([self.row('000010'),self.row('000020')],self.start)
        # 1. tenure covers box warmup
        worker.observer.update([self.row('000010'),self.row('000020'),self.row('999990')],self.start+timedelta(seconds=300))
        self.assertNotIn('999990',worker.observer.rows)
        # 2. replaces old after tenure (361s)
        worker.observer.update([self.row('000010'),self.row('000020'),self.row('999990')],self.start+timedelta(seconds=361))
        self.assertIn('999990',worker.observer.rows);self.assertEqual(len(worker.observer.rows),2)
        # 3. strong discovery replaces despite warmup
        worker2=ObservationWorker(self.cfg,self.auto,self.costs,self.root,limit=2)
        worker2.observer.update([self.row('000010'),self.row('000020')],self.start)
        strong=self.row('999990',2);worker2.observer.update([self.row('000010'),self.row('000020'),strong],self.start+timedelta(seconds=31))
        self.assertIn('999990',worker2.observer.rows)
    def test_continuous_observation_tick_handling(self):
        detector=PatternDetector(self.cfg,self.auto,self.costs);detector.continuous_observation=True
        for sec in range(0,181,10):
            tick=Tick('999990',self.start+timedelta(seconds=sec),1000,100,100+sec,1001,999);self.assertTrue(detector.ingest(tick,tick.time))
        self.assertEqual(len(detector.history['999990']),3)
        # delayed tick rejected
        delayed=Tick('999990',self.start,1000,100,100,1001,999)
        self.assertFalse(detector.ingest(delayed,self.start+timedelta(seconds=10)))
        detector.reset();self.assertEqual(detector.history,{})
    def test_warmup_counts_usable_bars_and_geometry_before_acceleration(self):
        detector=PatternDetector(self.cfg,self.auto,self.costs)
        detector.history['999990']=deque([{'time':self.start,'first_second':30,'low':990,'high':1000,'close':995,'volume':100}]*5)
        tick=Tick('999990',self.start,1000,100,100,1001,999)
        self.assertEqual(detector.base_gates(tick,{'status':'WATCH'})['warmup']['values']['completed_bars'],0)
    def test_resistance_only_defers_to_ac_without_ignoring_other_failures(self):
        data=bars();data[-30]['high']=10100
        result=observation_structure(data,10000,self.cfg,self.start.date())
        # 기존 bars fixture는 9/30 기준 전일까지. 설정의 허용 신선도 안에 있다.
        self.assertEqual(result['status'],'ELIGIBLE');self.assertEqual(result['setup_allowlist'],['A','C'])
        data[-1]['volume']=0
        blocked=observation_structure(data,9800,self.cfg,self.start.date());self.assertEqual(blocked['status'],'REJECT')
    def test_breakout_target_not_capped_by_already_crossed_resistance_b_still_capped(self):
        detector=PatternDetector(self.cfg,self.auto,self.costs);tick=Tick('999990',self.start,8470,100,100,8480,8460)
        setup={'pattern':'A','projection':8700};self.assertEqual(detector.target(tick,{'resistance':8370},setup,8480),8700)
        setup['pattern']='B';self.assertEqual(detector.target(tick,{'resistance':8370},setup,8480),8370)
        setup['pattern']='C';self.assertEqual(detector.target(tick,{'resistance':8550},setup,8480),8550)
    def test_diagnostic_feed_isolation_and_queue_overflow_guard(self):
        feed=Feed(Mock(),[]);sink=Mock();feed.configure_observation({'999990'},{'000010'},sink)
        feed.observation_q=queue.Queue(maxsize=1)
        frame={'trnm':'REAL','data':[{'type':'0B','item':'000010','values':{'20':'110000','10':'400','15':'100','13':'100','27':'401','28':'399','290':'2'}}]}
        with patch('core.realtime.now',return_value=self.start):
            feed.emit(frame)
            self.assertTrue(feed.q.empty());self.assertEqual(feed.observation_q.qsize(),1);sink.assert_called_once();self.assertFalse(feed.overflow.is_set())
            # second frame overflows observation_q without overflowing priority queue
            feed.emit(frame)
            self.assertEqual(feed.observation_drops,1);self.assertFalse(feed.overflow.is_set())
            feed.emit({'trnm':'REAL','data':[{'type':'00','values':{}}]});self.assertEqual(feed.q.qsize(),1)
    def test_virtual_worker_never_receives_engine_or_broker(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root)
        for attr in ('engine','broker','enter','submit_order'):self.assertFalse(hasattr(worker,attr))
    def test_runtime_view_does_not_serialize_detailed_snapshot(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);worker.clock=lambda:self.start
        worker.publish([self.row()],self.start,{})
        tick=Tick('999990',self.start,1000,100,100,1001,999);worker.observer.tick(tick,self.start)
        with patch('automation.observation_worker.json.dumps',side_effect=AssertionError('상세 JSON을 보호 루프에서 직렬화하면 안 됨')):
            view=worker.runtime_view()
        self.assertEqual(set(view['symbols']),{'999990'});self.assertNotIn('setup_trials',view['evaluations']['999990'])
    def test_metadata_wait_does_not_block_tick_processing(self):
        rest=Mock();entered=threading.Event();release=threading.Event()
        def status(symbol):
            entered.set();release.wait(2);return {'pass':False,'reason_code':'SYNTHETIC_BLOCK'}
        rest.stock_status.side_effect=status
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root,rest);worker.clock=lambda:self.start
        worker.publish([self.row()],self.start,{})
        self.addCleanup(worker.close);self.addCleanup(release.set);worker.start()
        unknown=self.row('000010');unknown.pop('shadow_view')
        worker.submit([unknown],self.start.isoformat(),{});self.assertTrue(entered.wait(1))
        worker.deliver(Tick('999990',self.start,1000,100,100,1001,999),self.start)
        deadline=time.monotonic()+1
        while '999990' not in worker.observer.detector.last and time.monotonic()<deadline:time.sleep(.01)
        self.assertIn('999990',worker.observer.detector.last);self.assertFalse(release.is_set())
    def test_worker_stale_queue_tick_cannot_generate_virtual_signal(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);worker.clock=lambda:self.start+timedelta(seconds=10)
        worker.publish([self.row()],self.start,{})
        self.addCleanup(worker.close);worker.start();worker.deliver(Tick('999990',self.start,1000,100,100,1001,999),self.start)
        deadline=time.monotonic()+1
        while not worker.ticks.empty() and time.monotonic()<deadline:time.sleep(.01)
        worker.close()
        # Healthy receiver-time data can be retained as history. Consumer delay
        # still forbids a current virtual signal or readiness publication.
        self.assertIn('999990',worker.observer.detector.last)
        self.assertFalse(worker.state().get('evaluations'))
    def test_concurrent_snapshot_read_does_not_lock_shadow_writer(self):
        from operator_service import read_json
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);worker.clock=lambda:self.start
        worker.publish([self.row()],self.start,{})
        self.addCleanup(worker.close);worker.start()
        for count in range(100):
            worker.deliver(Tick('999990',self.start,1000,100,count+1,1001,999),self.start)
            worker.runtime_view();worker.state();read_json(self.root/'runtime/shadow_snapshot.json',{})
        deadline=time.monotonic()+2
        while not worker.ticks.empty() and time.monotonic()<deadline:time.sleep(.01)
        worker.close();self.assertNotIn('last_error',worker.state());self.assertEqual(worker.observer.detector.last['999990'].cumulative,100)
    def test_raw_phase_is_published_before_slow_structure(self):
        rest=Mock();rest.profile={}
        worker=RadarWorker(self.cfg,self.auto,Policy(self.root/'policy'),rest,self.root);worker.available_capital=20000
        worker.observation_sink=Mock()
        item={'code':'999990','name':'합성','reference_price':1000,'source_details':{'a':{'observed_rank':1,'values':{}},'b':{'observed_rank':2,'values':{}}}}
        def evaluate(row):
            worker.observation_sink.assert_called_once();raw=worker.q.queue[0]
            self.assertEqual(raw['phase'],'RAW');self.assertEqual(raw['rows'][0]['source_count'],2)
            worker.stop.set();worker.request.set();return {'status':'ELIGIBLE','overhead_share_estimate':0}
        worker.evaluate=evaluate
        with patch('automation.radar.collect',return_value=[item]),patch('automation.radar.now',return_value=self.start):worker.run()
        first=worker.q.get_nowait();last=worker.q.get_nowait()
        self.assertEqual(last['source_observed_at'],first['time']);self.assertIsNotNone(last['performance']['cycle_ms'])
    def test_far_external_book_queries_reduce_but_near_monitoring_remains(self):
        rest=Mock();worker=AccountWorker(rest,5);worker.external_symbols={'052260'};worker.external_prices={'052260':5300}
        rest.snapshot.return_value={'holdings':{'052260':{'qty':13}},'orders':[],'cash':20000,'equity':20000}
        rest.existing_book.return_value={'time':self.start.isoformat(),'bid':5300,'bid_qty':100}
        clock=[100.];cycles=[0]
        def wait(_):
            clock[0]+=5;cycles[0]+=1
            if cycles[0]==8:worker.stop.set()
        worker.request.wait=wait
        with patch('automation.runner.time.monotonic',side_effect=lambda:clock[0]):worker.run()
        self.assertEqual(rest.snapshot.call_count,8);self.assertEqual(rest.existing_book.call_count,2)
        worker.external_prices['052260']=5835;self.assertTrue(worker.book_due('052260',clock[0]))
    def test_diagnostic_client_blocks_mutations_and_defers_http(self):
        with patch.dict('os.environ',{'KIWOOM_APP_KEY':'synthetic-key','KIWOOM_APP_SECRET':'synthetic-secret'}):
            rest=Rest(replace(self.cfg,live_mode=True),opener=Mock())
        rest.diagnostic_only=True
        # 1. mutations blocked
        for api in ('kt10000','kt10001','kt10003'):
            with self.assertRaises(Rejected):rest.call(api,'/api/dostk/ordr',{'stk_cd':'999990','ord_qty':'1'},mutation=True)
        # 2. http defers without reserving order slot
        rest.auth_session['next_call']=time.monotonic()+2;before=rest.auth_session['next_call']
        with self.assertRaises(Rejected):rest._http('/query',{}, {})
        self.assertEqual(rest.auth_session['next_call'],before);rest.opener.assert_not_called()
    def test_readonly_pilot_does_not_edit_policy_or_grant_live(self):
        policy=Policy(self.root/'policy');policy.command('LIVE',confirmation='LIVE');before=policy.path.read_bytes()
        isolated=ReadOnlyPilotPolicy(policy);self.assertEqual(isolated.mode,'ARMED');self.assertFalse(isolated.state['live_authorized'])
        isolated.command('STOP');self.assertEqual(policy.path.read_bytes(),before)
        policy.command('KILL');isolated.refresh();self.assertEqual(isolated.mode,'STOP')
    def test_cli_operational_pilot_accepts_live_file_but_dispatches_readonly_flag(self):
        import main
        with patch.object(main,'ROOT',self.root),patch.object(main,'prerequisites'),patch.object(main,'load_config',return_value=replace(self.cfg,live_mode=True)),\
             patch.object(main,'load_credentials'),patch.object(main,'now',return_value=self.start),\
             patch('automation.runner.run',return_value={'passed':True}) as run,patch('sys.argv',['main.py','--paper-seconds','150','--recover-pilot','--operational-pilot']):
            self.assertEqual(main.main(),0)
        self.assertTrue(run.call_args.kwargs['operational_pilot']);self.assertTrue(run.call_args.kwargs['recover_pilot'])
    def test_external_book_query_due_boundaries(self):
        worker=AccountWorker(Mock(),5);worker.external_prices={'052260':5300};worker.book_queries={'052260':100}
        self.assertFalse(worker.book_due('052260',110));self.assertTrue(worker.book_due('052260',130))
        worker.external_prices['052260']=5830;self.assertTrue(worker.book_due('052260',110))
        worker2=AccountWorker(Mock(),5);worker2.book_queries={'052260':100}
        self.assertTrue(worker2.book_due('052260',105))
    def test_new_strong_observation_displaces_weak_multi_source_watch(self):
        radar=DynamicRadar(replace(self.cfg,max_watchlist=1),self.auto,Policy(self.root/'policy'));radar.available_capital=20000
        rows=[]
        for sym,sources in [('000010',3),('999990',2)]:
            rows.append({'code':sym,'name':'합성','reference_price':1000,'source_details':{str(i):{'observed_rank':1,'values':{}} for i in range(sources)}})
        radar.momentum={'000010':{'timestamp':self.start.isoformat(),'gates':{'acceleration':{'result':'FAIL'}}},
            '999990':{'timestamp':self.start.isoformat(),'gates':{'acceleration':{'result':'PASS'},'liquidity':{'result':'PASS'}}}}
        evaluate=Mock(return_value={'status':'ELIGIBLE','overhead_share_estimate':0})
        _,watch,_=radar.update(rows,evaluate,self.start);self.assertEqual(watch[0]['code'],'999990')

class SetupReachabilityTests(AuditFixture):
    def build(self,pattern):
        detector=PatternDetector(self.cfg,self.auto,self.e.costs);detector.continuous_observation=True
        if pattern=='A':
            base=997;top=1000;price=1001;ask=1002
            seq=[(980,1000,997,1000)]*2
        elif pattern=='B':
            base=1017;top=1020;price=1021;ask=1021
            seq=[(980,1000,997,1000),(990,1020,1015,2000),(1005,1010,1008,200),(1005,1010,1008,100)]
        else:
            base=1007;top=1010;price=1011;ask=1011
            seq=[(1000,1010,1005,1000)]*5
        detector.history['999990']=deque([{'time':self.clock[0]-timedelta(minutes=len(seq)-i),'low':low,'high':high,'close':close,'open':close,'volume':vol,'first_second':0} for i,(low,high,close,vol) in enumerate(seq)])
        # Seed the real observation epoch for these directly seeded completed bars.
        detector.observation_start['999990']=self.clock[0]-timedelta(minutes=len(seq))
        detector.observation_session['999990']=(self.clock[0].date(),'REGULAR')
        detector.day_high['999990']=top;detector.prior_high['999990']=top
        detector.buffers['999990']=deque(Tick('999990',self.clock[0]-timedelta(seconds=sec),base,100,1000-sec,base+1,base-1) for sec in range(70,0,-1))
        tick=Tick('999990',self.clock[0],price,100000,200000,ask,price-1)
        watch={'code':'999990','name':'합성','status':'WATCH','source_count':2,'source_details':{'one':{},'two':{}},'market_status':{'pass':True},'resistance':2000}
        return detector,tick,watch
    def test_setups_reach_real_order_path_synthetic_only(self):
        for pattern in ('A','B','C'):
            with self.subTest(pattern=pattern):
                self.setUp()
                self.route(pattern)
    def route(self,pattern):
        detector,tick,watch=self.build(pattern)
        history=bars()
        for bar in history:bar.update(open=900,high=950,low=880,close=900,volume=2000000)
        radar=Mock();radar.cache={(self.clock[0].date(),'999990'):history}
        frame={'trnm':'REAL','data':[{'type':'0B','item':'999990','values':{'20':tick.time.strftime('%H%M%S'),'10':str(tick.price),'15':str(tick.volume),'13':str(tick.cumulative),'27':str(tick.ask),'28':str(tick.bid),'290':'2'}}]}
        with patch('automation.runner.now',return_value=self.clock[0]):process_message(self.e,detector,frame,self.clock[0],{'999990'},{'999990':watch},radar,{},Mock(),True)
        self.assertEqual(len(self.broker.orders),1);self.assertEqual(self.broker.orders[0][0],'BUY')
        order=self.e.active_orders('999990','BUY')[0];self.assertEqual(order['signal']['pattern'],pattern);self.assertGreater(order['qty'],0)
    def test_armed_same_reachable_setup_has_zero_orders(self):
        self.policy.command('ARMED');detector,tick,watch=self.build('A');detector.ingest(tick,tick.time)
        signal=detector.signal(tick,watch);self.assertIsNotNone(signal);self.e.enter(signal,tick)
        self.assertEqual(self.broker.orders,[]);self.assertFalse(self.e.s['positions'])
    def test_source_churn_observation_retained_but_buy_qualified_only_after_two_sources(self):
        self.e.a=replace(self.auto,min_source_count=2)
        detector,tick,watch=self.build('A');detector.ingest(tick,tick.time);saved=detector.history['999990']
        self.assertIsNotNone(detector.signal(tick,watch));self.assertIs(saved,detector.history['999990'])
        sig=detector.signal(tick,{**watch,'source_count':1});self.e.enter(sig,tick);self.assertEqual(self.broker.orders,[])
        self.e.enter(detector.signal(tick,watch),tick);self.assertEqual(len(self.broker.orders),1)

if __name__=='__main__':unittest.main()
