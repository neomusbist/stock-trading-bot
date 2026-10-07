"""시운전 전 권한 분리/현금 재조회/0원 signal을 합성 입력으로 확인. 실제 연결 없음."""
import copy
import io
import json
import time
import threading
import unittest
from datetime import timedelta
from dataclasses import replace
from unittest.mock import Mock,patch
from tests import test_automation as fixture
from tests import test_pipeline as pipeline
from automation.policy import HARD_LOCKS,BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,AutomationConfig
from automation.patterns import PatternDetector
from automation import runner
from core.broker import Rest,Rejected
from core.util import atomic_json

class PreflightTests(unittest.TestCase):
    setUp=fixture.AutomationTests.setUp
    make_engine=fixture.AutomationTests.make_engine
    tick=fixture.AutomationTests.tick
    signal=fixture.AutomationTests.signal
    buy=fixture.AutomationTests.buy

    def zero_armed(self,cap=0):
        self.policy.state['live_authorized']=False; atomic_json(self.policy.path,self.policy.state)
        self.policy.command('ARMED'); self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=cap)
        self.cfg=replace(self.cfg,live_mode=False); self.engine=self.make_engine()
        self.engine.connected=True; self.engine.market='3'

    def holding(self,qty=1,available=None,symbol=TEST_RELEASE_SYMBOL):
        h={'qty':qty,'available':qty if available is None else available,'avg':2855}
        b={'time':self.clock[0].isoformat(),'bid':2700,'bid_qty':10,'regular':True,'status':{'pass':True}}
        snap={'cash':0,'equity':100000,'orders':[],'holdings':{symbol:h},'books':{symbol:b}}
        self.engine.reconcile(snap); return snap

    def approve(self): self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE')

    def release(self):
        self.zero_armed(); self.holding(); self.approve(); self.engine.timer()
        self.assertEqual(self.broker.orders,[('SELL',TEST_RELEASE_SYMBOL,1,2700)])
        return next(iter(self.engine.s['orders'].values()))

    def events(self):
        return [json.loads(x) for x in (self.engine.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]

    def forced_lock(self,symbol):
        self.buy(); original=copy.deepcopy(self.engine.s['positions'][fixture.SYMBOL])
        self.engine.s['positions']={symbol:{**original,'symbol':symbol}}
        self.engine.actual={symbol:{'qty':original['qty'],'available':original['qty'],'avg':original['avg']}}
        for reason in ('전략 신호','구조/이익보호선 이탈','당일 청산 시각','사용자 STOP_FLAT',
                       '사용자 KILL','비용/안전마진 포함 상대효율 교체','잘못된 강제 SELL'):
            with self.subTest(symbol=symbol,reason=reason):
                self.engine.request_exit(symbol,reason); self.engine.drive_exits({symbol:9000})
                self.assertIsNone(self.engine.submit('SELL',symbol,original['qty'],0,{'reason':reason}))
                self.assertIsNone(self.engine.submit('SELL',symbol,original['qty'],10000,
                    {'ownership':'EXISTING_POLICY','policy':'BREAKEVEN_EXIT','reason':reason}))
                self.assertIsNone(self.engine.submit('SELL',symbol,original['qty'],0,
                    {'ownership':'LEGACY_REPLACEMENT','reason':reason}))
        self.engine.on_tick(self.tick(price=9000,sym=symbol))
        self.clock[0]=self.clock[0].replace(hour=15,minute=10)
        self.engine.s['positions'][symbol]['last_tick']=self.clock[0].isoformat()
        self.engine.last_account=time.monotonic(); self.engine.timer()
        for mode in ('STOP_FLAT','KILL'):
            self.policy.command(mode); self.engine.controls(); self.engine.kill_checked=True
            self.engine.controls(); self.engine.drive_exits({symbol:9000})
        self.assertEqual([o for o in self.broker.orders if o[0]=='SELL'],[])
        for reason in ('stop','protection','flatten','kill','opportunity'):
            with self.assertRaisesRegex(Rejected,'HARD_LOCK'):
                self.engine.order_guard('kt10001',{'stk_cd':symbol,'ord_qty':'1','ord_uv':'9000','reason':reason})

    def test_hard_locks_forced_sell_paths_zero_broker_sells(self):
        for sym in ('005930','017900'):
            self.setUp()
            self.forced_lock(sym)

    def test_final_static_lock_cannot_be_removed_by_old_release_flag(self):
        for symbol in HARD_LOCKS:
            self.policy.command('RELEASE',symbol,'RELEASE')
            self.assertFalse(self.policy.locked(symbol))  # 기존 명령 계약은 유지
            self.assertIsNone(self.engine.submit('SELL',symbol,1,10000,{'ownership':'EXISTING_POLICY'}))
            with self.assertRaisesRegex(Rejected,'HARD_LOCK'):
                self.engine.order_guard('kt10001',{'stk_cd':symbol,'ord_qty':'1','ord_uv':'10000'})
        self.assertEqual(set(BREAKEVEN_EXITS),{'052260'}); self.assertFalse(self.broker.orders)

    def test_rest_http_final_lock_prevents_opener_for_both_symbols(self):
        with patch.dict('os.environ',{'KIWOOM_APP_KEY':'FAKE-PREFLIGHT','KIWOOM_APP_SECRET':'FAKE'}):
            opener=Mock(side_effect=AssertionError('network'))
            rest=Rest(self.cfg,opener=opener); rest.order_guard=self.engine.order_guard
            for symbol in HARD_LOCKS:
                with self.assertRaisesRegex(Rejected,'HARD_LOCK'):
                    rest._http('/api/dostk/ordr',{'stk_cd':symbol,'ord_qty':'1','ord_uv':'10000'},{'api-id':'kt10001'})
            opener.assert_not_called()

    def test_release_armed_zero_false_unconfirmed_only_one_real_path_fake_broker(self):
        o=self.release(); self.engine.timer(); self.engine.controls()
        self.assertFalse(o['id'].startswith('PAPER-')); self.assertEqual(o['status'],'ACCEPTED')
        self.assertEqual(self.policy.mode,'ARMED'); self.assertFalse(self.policy.state['live_authorized'])
        self.assertFalse(self.engine.c.live_mode); self.assertFalse(self.engine.a.costs_confirmed)
        self.assertEqual(self.engine.a.capital_limit,0); self.assertFalse(self.engine.s['positions'])
        self.assertEqual(len(self.broker.orders),1)

    def test_release_no_approval_no_order(self):
        self.zero_armed(); self.holding(); self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_release_approval_is_sell_only_one_share_one_shot(self):
        self.zero_armed(); self.approve(); a=self.policy.state['test_capital_release']
        self.assertEqual((a['symbol'],a['side'],a['max_qty'],a['quantity'],a['one_shot']),('267320','SELL',1,1,True))

    def test_release_approval_does_not_allow_other_sales_or_any_buy(self):
        self.release()
        for symbol in ('005930','017900','052260','267320',fixture.SYMBOL):
            self.assertIsNone(self.engine.submit('BUY',symbol,1,2000,self.signal(symbol)))
            self.assertIsNone(self.engine.submit('SELL',symbol,1,2000,{'ownership':'EXISTING_POLICY','policy':'TEST_CAPITAL_RELEASE'}))
        self.engine.enter(self.signal(),self.tick())
        self.assertEqual(self.broker.orders,[('SELL','267320',1,2700)])

    def test_release_two_shares_or_zero_available_no_order(self):
        self.zero_armed(); self.approve()
        for qty,available in ((2,2),(1,0)):
            self.holding(qty,available); self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_release_prerequisites_and_manual_open_order_blocks(self):
        self.zero_armed(); self.holding(); self.approve()
        for attr,value in (('account_ok',False),('connected',False),('market','2'),('market','UNKNOWN')):
            old=getattr(self.engine,attr); setattr(self.engine,attr,value); self.engine.timer()
            setattr(self.engine,attr,old); self.assertFalse(self.broker.orders)
        self.clock[0]+=timedelta(seconds=6); self.engine.timer(); self.assertFalse(self.broker.orders)
        self.setUp(); self.zero_armed(); self.holding(); self.approve()
        self.engine.books['267320']['status']={'pass':None}; self.engine.timer(); self.assertFalse(self.broker.orders)
        self.engine.books['267320']['status']={'pass':True}; self.engine.external_open_orders=[{'symbol':'267320','id':'MANUAL'}]
        self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_release_outside_session_and_stop_modes_block(self):
        self.zero_armed(); self.holding(); self.approve()
        for mode in ('OFF','STOP','STOP_FLAT','KILL'):
            self.policy.command(mode); self.engine.timer(); self.assertFalse(self.broker.orders)
        self.policy.command('ARMED'); self.clock[0]=self.clock[0].replace(hour=15,minute=20)
        self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_release_partial_or_zero_fill_does_not_complete(self):
        o=self.release(); self.engine.cumulative_fill(o,0,0); self.policy.refresh()
        self.assertEqual(o['filled'],0); self.assertEqual(o['status'],'ACCEPTED')
        self.assertEqual(self.policy.state['test_capital_release']['status'],'SUBMITTING')
        self.assertFalse(self.engine.s['automation'].get('capital_refresh_pending',False))
        self.setUp(); o=self.release(); self.engine.cumulative_fill(o,0.5,1350); self.policy.refresh()
        self.assertEqual(o['filled'],0); self.assertEqual(self.policy.state['test_capital_release']['status'],'SUBMITTING')
        self.assertTrue(self.engine.s['halt']); self.assertEqual(len(self.broker.orders),1)

    def test_release_restart_pending_and_duplicate_fill_do_not_resend(self):
        o=self.release(); snap=self.holding()
        snap['orders']=[{'ord_no':o['id'],'stk_cd':'267320','oso_qty':'1','cntr_tot_amt':'0'}]
        restored=self.make_engine(); restored.connected=True; restored.market='3'; restored.reconcile(snap)
        restored.timer(); self.assertEqual(len(self.broker.orders),1)
        restored.cumulative_fill(next(iter(restored.s['orders'].values())),1,2700)
        restored.cumulative_fill(next(iter(restored.s['orders'].values())),1,2700)
        self.assertEqual(len(restored.s['automation']['external_exit_fills']),1)

    def test_release_timeout_and_ambiguous_send_recovery_invariants(self):
        o=self.release(); self.clock[0]+=timedelta(seconds=self.cfg.order_timeout_seconds+1)
        self.engine.timer(); self.assertEqual(o['status'],'ACCEPTED')
        self.assertTrue(o['manual_cancel_required']); self.assertFalse(self.broker.cancels)
        self.assertEqual(len(self.broker.orders),1)
        self.setUp(); self.zero_armed(); self.holding(); self.approve()
        self.broker.submit=Mock(side_effect=TimeoutError('synthetic lost response'))
        self.engine.timer(); self.engine.timer(); self.assertEqual(self.broker.submit.call_count,1)
        restored=self.make_engine(); restored.connected=True; restored.market='3'
        restored.timer(); self.assertEqual(self.broker.submit.call_count,1)
        self.assertEqual(next(iter(restored.s['orders'].values()))['status'],'UNKNOWN')

    def test_release_rest_false_flag_requires_saved_exact_intent(self):
        self.zero_armed(); self.holding(); self.approve()
        class Response(io.BytesIO):
            headers={}
        response=Response(json.dumps({'return_code':0,'ord_no':'FAKE-REST-1'}).encode())
        with patch.dict('os.environ',{'KIWOOM_APP_KEY':'FAKE-PREFLIGHT','KIWOOM_APP_SECRET':'FAKE'}):
            opener=Mock(return_value=response); rest=Rest(self.cfg,opener=opener)
            rest.get_token=lambda:'FAKE'; rest.order_guard=self.engine.order_guard
            rest.test_release_authorizer=self.engine.test_release_authorized
            self.engine.broker=rest
            with self.assertRaises(Rejected): rest.submit('SELL','267320',1,2700)
            opener.assert_not_called()
            self.engine.timer(); self.assertEqual(opener.call_count,1)
            body=json.loads(opener.call_args.args[0].data)
            self.assertEqual((body['stk_cd'],body['ord_qty']),('267320','1'))
            for side,symbol,qty in (('BUY','267320',1),('SELL','052260',1),('SELL','267320',2)):
                with self.assertRaises(Rejected): rest.submit(side,symbol,qty,2700)
            self.assertEqual(opener.call_count,1)

    def test_release_final_recheck_revoked_during_auth_never_sends(self):
        self.zero_armed(); self.holding(); self.approve()
        with patch.dict('os.environ',{'KIWOOM_APP_KEY':'FAKE-PREFLIGHT','KIWOOM_APP_SECRET':'FAKE'}):
            opener=Mock(side_effect=AssertionError('network')); rest=Rest(self.cfg,opener=opener)
            def revoke(): self.policy.command('STOP'); return 'FAKE'
            rest.get_token=revoke; rest.order_guard=self.engine.order_guard
            rest.test_release_authorizer=self.engine.test_release_authorized; self.engine.broker=rest
            self.engine.timer(); opener.assert_not_called()
            self.assertEqual(next(iter(self.engine.s['orders'].values()))['status'],'REJECTED')

    def capital_requery(self,cash,cap):
        o=self.release(); self.engine.cumulative_fill(o,1,2700)
        self.assertTrue(self.engine.s['automation']['capital_refresh_pending']); self.assertFalse(self.engine.account_ok)
        # cap은 명시 승인 설정을 주입한 테스트값. 체결 코드가 자동 변경하지 않는다.
        self.engine.a=replace(self.engine.a,AUTO_TRADING_CAP_KRW=cap)
        rest=Mock(); rest.snapshot.return_value={'cash':cash,'equity':cash,'holdings':{},'orders':[]}
        worker=runner.AccountWorker(rest,60)
        worker.request.wait=Mock(side_effect=lambda *a:worker.stop.set())
        with patch.object(runner,'now',return_value=self.clock[0]+timedelta(seconds=1)):
            worker.run()
        rest.snapshot.assert_called_once()
        self.engine.reconcile(worker.q.get_nowait()['snapshot'])
        self.assertFalse(self.engine.s['automation']['capital_refresh_pending']); self.assertTrue(self.engine.account_ok)
        self.assertEqual(self.engine.available_cash,cash); self.assertEqual(self.engine.a.capital_limit,cap)
        self.assertEqual(self.engine.s['cash'],0)
        self.assertTrue(any(r['event']=='ACCOUNT_CAPITAL_REFRESHED' for r in self.events()))
        return self.engine.s['automation']['capital_snapshot']['auto_available_capital']

    def test_capital_requery_budget_matrix(self):
        for cash,cap,expected in ((2700,0,0),(2700,3000,2700),(5000,3000,3000)):
            self.setUp()
            self.assertEqual(self.capital_requery(cash,cap),expected)

    def test_pre_fill_or_unknown_start_snapshot_cannot_finish_requery(self):
        o=self.release(); self.engine.cumulative_fill(o,1,2700)
        snap={'cash':5000,'equity':5000,'orders':[],'holdings':{}}
        self.engine.reconcile(snap); self.assertFalse(self.engine.account_ok)
        snap['query_started_at']=(self.clock[0]-timedelta(seconds=1)).isoformat()
        self.engine.reconcile(snap); self.assertFalse(self.engine.account_ok)
        self.assertTrue(self.engine.s['automation']['capital_refresh_pending'])

    def test_fill_discovered_in_rest_still_requires_followup_snapshot(self):
        o=self.release(); snap={'cash':2700,'equity':2700,'orders':[],'holdings':{},'query_started_at':self.clock[0].isoformat()}
        fills=[{'ord_no':o['id'],'ord_stt':'체결','cntr_qty':'1','cntr_pric':'2700'}]
        self.engine.reconcile(snap,fills); self.assertFalse(self.engine.account_ok)
        self.assertTrue(self.engine.s['automation']['capital_refresh_pending'])

    def test_completed_release_restart_keeps_refresh_pending_cap0(self):
        o=self.release(); self.engine.cumulative_fill(o,1,2700)
        restored=self.make_engine(); self.assertTrue(restored.s['automation']['capital_refresh_pending'])
        self.assertEqual(restored.a.capital_limit,0); self.assertEqual(len(self.broker.orders),1)

    def test_fill_requests_existing_account_worker_automatically_only_in_session(self):
        o=self.release(); account=Mock(); account.request=threading.Event()
        self.assertFalse(runner.request_capital_refresh(self.engine,account,True))
        self.engine.cumulative_fill(o,1,2700)
        self.assertFalse(runner.request_capital_refresh(self.engine,account,False)); self.assertFalse(account.request.is_set())
        self.assertTrue(runner.request_capital_refresh(self.engine,account,True)); self.assertTrue(account.request.is_set())

    def test_time_limited_read_check_refuses_active_release_approval(self):
        from dataclasses import asdict
        self.zero_armed(); self.approve(); atomic_json(self.folder/'automation.json',asdict(self.auto))
        self.policy.path=self.folder/'runtime/automation_policy.json'; atomic_json(self.policy.path,self.policy.state)
        with patch.object(runner,'Rest',return_value=self.broker),patch.object(runner,'now',return_value=self.clock[0]),\
             patch.object(runner,'RadarWorker') as radar,patch.object(runner,'Feed') as feed:
            with self.assertRaisesRegex(ValueError,'시험매도 승인 비활성'): runner.run(self.cfg,self.folder,150)
            radar.assert_not_called(); feed.assert_not_called()

    def armed_signal(self,pattern):
        self.zero_armed(); self.engine.reconcile(self.snapshot)
        d=PatternDetector(self.cfg,self.auto,self.engine.costs); cumulative=0
        watch={'status':'WATCH','name':'합성 시운전','resistance':11000,'previous_close':10000,
               'market_status':{'pass':True},'source_details':{'a':{},'b':{}},
               'affordability':{'entry_eligible':False,'reason_code':'OBSERVATION_ONLY_ZERO_CAP'}}
        account=Mock(); radar=Mock(); radar.cache={(self.clock[0].date(),fixture.SYMBOL):[{}]}
        origin=self.clock[0]
        with patch.object(runner,'now',side_effect=lambda:self.clock[0]),\
             patch.object(runner,'structure',return_value={'status':'ELIGIBLE','resistance':11000}):
            for second,price,volume in pipeline.replay_prices(pattern):
                self.clock[0]=origin+timedelta(seconds=second); cumulative+=volume
                frame={'trnm':'REAL','data':[{'type':'0B','item':fixture.SYMBOL,'values':{
                    '20':self.clock[0].strftime('%H%M%S'),'10':str(price),'15':str(volume),
                    '13':str(cumulative),'27':str(price+10),'28':str(price-10),'290':'2'}}]}
                runner.process_message(self.engine,d,frame,self.clock[0],{fixture.SYMBOL},{fixture.SYMBOL:watch},radar,{},account,True)
        records=[r for r in self.events() if r['event']=='ENTRY_EVAL' and r.get('signal_detected')]
        self.assertTrue(records); row=records[-1]; self.assertEqual(row['setup'],pattern)
        self.assertEqual(row['decision'],'NO_BUY'); self.assertEqual(row['primary_reason_code'],'AUTO_CAP_ZERO')
        self.assertIsNotNone(row['signal_at']); self.assertGreater(row['invalidation'],0)
        for gate in ('acceleration','liquidity','spread','invalidation','risk_reward','market_status'):
            self.assertEqual(row['gates'][gate]['result'],'PASS')
        self.assertEqual(row['gates']['policy']['result'],'FAIL'); self.assertEqual(row['quantity'],0)
        self.assertFalse(self.engine.s['orders']); self.assertFalse(self.engine.s['positions']); self.assertFalse(self.broker.orders)

    def test_zero_armed_A_B_C_full_runner_signal_and_log_no_order(self):
        for pat in ('A','B','C'):
            self.setUp()
            self.armed_signal(pat)

    def test_zero_cap_discovery_still_calls_structure_evaluator(self):
        from automation.radar import DynamicRadar
        self.zero_armed(); radar=DynamicRadar(self.cfg,self.auto,self.policy); radar.available_capital=0
        evaluate=Mock(return_value={'status':'ELIGIBLE','overhead_share_estimate':0})
        item={'code':fixture.SYMBOL,'reference_price':10000,'source_details':{s:{'observed_rank':1,'values':{}} for s in ('a','b')}}
        _,watch,_=radar.update([item],evaluate,self.clock[0]); evaluate.assert_called_once()
        self.assertTrue(watch); self.assertFalse(watch[0]['affordability']['entry_eligible'])

if __name__=='__main__': unittest.main()
