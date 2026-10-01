"""기본 수치로 실제 틱→패턴→가짜 LIVE 주문→체결→청산을 연결하는 회귀 시험."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import Mock,patch
from core.settings import Config,KST,ROOT
from core.strategy import Tick
from core.util import atomic_json
from core.realtime import Feed
from automation.policy import AutomationConfig,Policy,load_automation
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import collect,RANKS
from automation.runner import process_message,update_market
from automation.performance import report
from automation.session import supported
from automation.runner import error_record
from automation.allocation import efficiency,apply_empirical
from core.broker import Rest,Rejected
from concurrent.futures import ThreadPoolExecutor
import os
from tests.test_automation import FakeBroker

START=datetime(2026,9,30,14,40,tzinfo=KST)
SYMBOL='999990'

def replay_prices(pattern):
    # 모든 OHLC와 거래량은 합성. 시세 구독 시작부터 완성된 1분봉만 사용한다.
    if pattern=='A':
        shapes=[(9850,10000,9970,100),(9850,9990,9970,100)]
    elif pattern=='B':
        shapes=[(10000,10050,10040,100),(10040,10200,10190,300),
                (10130,10180,10140,30),(10130,10160,10150,20)]
    else: shapes=[(9860,10000,9970,100)]*5
    result=[]
    for minute,(lo,hi,close,vol) in enumerate(shapes):
        for second in range(60):
            price=lo if second<10 else hi if second<25 else close
            result.append((minute*60+second,price,vol))
    end=len(shapes)*60; baseline=shapes[-1][2]; breakout=10210 if pattern=='B' else 10010
    for sec in range(5): result.append((end+sec,baseline,shapes[-1][3]))
    result.append((end+5,breakout,4000))
    return result

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name); self.clock=[START]; self.broker=FakeBroker()
        self.policy=Policy(self.folder/'policy.json'); self.policy.command('LIVE',confirmation='LIVE')
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=1000000,costs_confirmed=True)
        self.cfg=replace(Config(),live_mode=True)
        self.out=contextlib.redirect_stdout(io.StringIO()); self.out.__enter__(); self.addCleanup(self.out.__exit__,None,None,None)
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.folder/'book',self.broker,lambda:self.clock[0])
        self.e.market_status={SYMBOL:{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'}}
        self.e.reconcile({'cash':1000000,'equity':1000000,'orders':[],'holdings':{}}); self.e.connected=True; self.e.market='3'
    def full_pattern(self,pattern):
        detector=PatternDetector(self.cfg,self.auto,self.e.costs); cumulative=0; signal=None
        watch={'status':'WATCH','resistance':11000,'previous_close':10000,'source_details':{'ka10032':{},'ka10027':{}}}
        for second,price,volume in replay_prices(pattern):
            self.clock[0]=START+timedelta(seconds=second); cumulative+=volume
            t=Tick(SYMBOL,self.clock[0],price,volume,cumulative,price+10,price-10)
            self.assertTrue(detector.ingest(t,self.clock[0])); signal=detector.signal(t,watch)
        self.assertIsNotNone(signal,f'{pattern} default parameters did not signal')
        self.assertEqual(signal['pattern'],pattern); self.e.last_account=__import__('time').monotonic()
        order=self.e.enter(signal,t); self.assertIsNotNone(order); self.assertEqual(len(self.broker.orders),1)
        self.assertFalse(self.e.s['positions'])  # 주문 접수는 체결이 아니다.
        values={'9203':order['id'],'9001':SYMBOL,'907':'2','913':'체결','905':'+매수','900':str(order['qty']),
                '902':'0','903':str(order['qty']*order['limit'])}
        self.e.on_order_event(values); p=self.e.s['positions'][SYMBOL]
        self.e.actual={SYMBOL:{'qty':p['qty'],'available':p['qty'],'avg':p['avg']}}
        avg=p['avg']; stops=[]
        for px in (avg+200,avg+400,avg+300,avg+200):
            self.clock[0]+=timedelta(seconds=1); cumulative+=100
            self.e.on_tick(Tick(SYMBOL,self.clock[0],px,100,cumulative,px+10,px-10)); stops.append(p['stop'])
            if len(self.broker.orders)>1: break
        self.assertEqual(stops,sorted(stops)); self.assertEqual(self.broker.orders[-1][0],'SELL')
        sell=list(self.e.s['orders'].values())[-1]; self.e.cumulative_fill(sell,sell['qty'],sell['qty']*(px-10))
        self.assertFalse(self.e.s['positions']); self.assertEqual(report(self.e.s['trades'])['all']['count'],1)
        self.assertEqual(report(self.e.s['trades'])['completed_trades'][0]['pattern'],pattern)
        self.assertGreater(self.e.s['cash'],1000000)
    def test_A_tick_to_fill_to_profit_protection(self): self.full_pattern('A')
    def test_B_tick_to_fill_to_profit_protection(self): self.full_pattern('B')
    def test_C_tick_to_fill_to_profit_protection(self): self.full_pattern('C')
    def test_dynamic_subscription_replace_keeps_order_and_market_feed(self):
        feed=Feed(Mock(),['999990']); feed.socket=Mock(); feed.logged_in=True
        self.assertTrue(feed.update_symbols(['999980','999990']))
        payload=json.loads(feed.socket.send.call_args.args[0]); self.assertEqual(payload['refresh'],'0')
        self.assertEqual(payload['data'][1]['type'],['00','0s'])
        self.assertFalse(feed.update_symbols(['999980','999990']))
    def test_subscription_ack_keeps_warmed_bars(self):
        detector=Mock(); self.e.connected=False; self.e.reset_detector=False
        process_message(self.e,detector,{'trnm':'REG','return_code':0},self.clock[0],set(),{},Mock(),{},Mock(),True)
        detector.reset.assert_not_called(); self.assertFalse(self.e.account_ok)
    def test_market_state_forbids_auction_entry(self):
        update_market(self.e,'2'); self.assertFalse(self.e.entry_window()); self.assertTrue(self.e.session_ending())
    def test_pending_prior_day_restore_waits_account_not_guess(self):
        sig={'symbol':SYMBOL,'limit':10020,'stop':9800,'pattern':'A','efficiency':{'decision':'TRADE'}}
        order=self.e.enter(sig,Tick(SYMBOL,self.clock[0],10000,100,100,10010,9990)); self.assertIsNotNone(order)
        self.clock[0]+=timedelta(days=1)
        restored=AutonomousEngine(self.cfg,self.auto,self.policy,self.folder/'book',self.broker,lambda:self.clock[0])
        self.assertFalse(restored.account_ok); self.assertEqual(restored.active_orders()[0]['id'],order['id'])
        self.assertFalse(restored.s['positions'])
    def test_default_file_zero_armed_without_release(self):
        from dataclasses import asdict
        defaults=self.folder/'default_automation.json'; atomic_json(defaults,asdict(AutomationConfig()))
        self.assertEqual(load_automation(defaults).capital_limit,0)
        policy=Policy(self.folder/'default_policy.json'); self.assertEqual(policy.mode,'ARMED')
        self.assertFalse(policy.state['live_authorized']); self.assertEqual(policy.state['releases'],{})
    def test_radar_preserves_raw_unknown_and_signed_delta_inputs(self):
        rest=Mock()
        def pages(api,payload,key,**kw):
            return ([{'stk_cd':SYMBOL,'stk_nm':'합성','flu_rt':'-3.0','cur_prc':'-10000'}],{})
        rest.pages.side_effect=pages; item=collect(rest)[0]
        self.assertEqual(item['reference_price'],10000)
        self.assertEqual(item['source_details']['ka10029']['values']['flu_rt'],-3)
        self.assertIsNone(item['source_details']['ka10029']['values']['cntr_str'])
    def test_explicit_policy_change_to_off_does_not_drop_protection(self):
        self.e.s['positions'][SYMBOL]={'ownership':'BOT','qty':1}; self.e.save()
        self.policy.command('OFF'); self.e.controls(); self.assertEqual(self.policy.mode,'STOP')

    def test_supported_session_exact_boundaries(self):
        for hour,minute,second,expected in ((8,59,59,False),(9,0,0,True),(15,19,59,True),(15,20,0,False),(20,0,0,False)):
            self.assertEqual(supported(START.replace(hour=hour,minute=minute,second=second)),expected)
        self.assertFalse(supported(START.replace(tzinfo=None)))
    def test_after_hours_regular_flag_cannot_force_buy_or_sell(self):
        self.clock[0]=START.replace(hour=20)
        sig={'symbol':SYMBOL,'limit':10020,'stop':9800,'pattern':'A','efficiency':{'decision':'TRADE'}}
        self.e.enter(sig,Tick(SYMBOL,self.clock[0],10000,100,100,10010,9990))
        self.assertIsNone(self.e.submit('BUY',SYMBOL,1,10020,sig))
        self.e.s['positions'][SYMBOL]={'qty':1,'ownership':'BOT','qty_blocked':False}
        self.assertIsNone(self.e.submit('SELL',SYMBOL,1)); self.assertFalse(self.broker.orders)
    def test_1520_guard_denies_even_with_saved_order_intent(self):
        self.clock[0]=START.replace(hour=15,minute=20)
        with self.assertRaisesRegex(Rejected,'OUT_OF_SUPPORTED_SESSION'):
            self.e.order_guard('kt10000',{'stk_cd':SYMBOL,'ord_qty':'10','ord_uv':'10020'})
    def test_after_hours_tick_not_ingested(self):
        d=PatternDetector(self.cfg,self.auto,self.e.costs); stamp=START.replace(hour=20)
        self.assertFalse(d.ingest(Tick(SYMBOL,stamp,10000,100,100,10010,9990),stamp))
    def test_after_hours_diagnostic_never_creates_broker(self):
        import main
        with patch.object(main,'ROOT',self.folder),patch.object(main,'now',return_value=START.replace(hour=20)),\
             patch.object(main,'load_config',return_value=Config()),patch.object(main,'prerequisites'),\
             patch.object(main,'Rest',side_effect=AssertionError('network client created')),patch('sys.argv',['main.py','--check-account']):
            self.assertEqual(main.main(),0)
        result=json.loads((self.folder/'diagnostic_session_guard.json').read_text(encoding='utf-8'))
        self.assertEqual(result['classification'],'OUT_OF_SUPPORTED_SESSION'); self.assertIsNone(result['passed'])
    def test_runtime_rest_time_guard_before_http(self):
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE-SESSION','KIWOOM_APP_SECRET':'FAKE'}):
            opener=Mock(side_effect=AssertionError('network'))
            client=Rest(Config(),opener=opener); client.session_guard=lambda:False
            with self.assertRaisesRegex(Rejected,'OUT_OF_SUPPORTED_SESSION'): client._http('/oauth2/token',{}, {})
            opener.assert_not_called()
    def test_rejected_code_message_raw_response_preserved_exactly(self):
        response={'return_code':20,'return_msg':'  테스트 오류 원문: 인증 확인 필요  ','extra':'원 값'}
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE-ERROR','KIWOOM_APP_SECRET':'FAKE'}):
            client=Rest(Config()); client.get_token=lambda:'FAKE'; client._http=lambda *args:(response,{})
            with self.assertRaises(Rejected) as caught: client.query('kt00018',{})
        row=error_record(caught.exception); self.assertEqual(row['raw_response'],response)
        self.assertEqual(row['return_code'],20); self.assertEqual(row['return_msg'],response['return_msg'])
    def test_parallel_readers_share_single_authentication(self):
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE-SHARED','KIWOOM_APP_SECRET':'FAKE'}):
            one=Rest(Config()); two=Rest(Config(),auth_session=one.auth_session)
            fake=Mock(return_value=({'token':'FAKE','expires_dt':'20500101000000'},{}))
            one._http=fake; two._http=fake
            with ThreadPoolExecutor(max_workers=6) as pool:
                tokens=list(pool.map(lambda i:(one if i%2 else two).get_token(),range(12)))
            self.assertEqual(tokens,['FAKE']*12); self.assertEqual(fake.call_count,1)
    def test_empirical_adapter_replaces_score_after_minimum_sample(self):
        score=efficiency(10020,9800,11000,.8,30,self.e.costs)
        sig={'symbol':SYMBOL,'pattern':'B','efficiency':score}
        trades=[{'trade_id':str(i),'symbol':SYMBOL,'opened':START.isoformat(),'closed':(START+timedelta(seconds=i+1)).isoformat(),
                 'qty':1,'entry':10020,'pnl_estimate':-100,'MFE':20,'MAE':100,'holding_seconds':60,
                 'completed':True,'ownership':'BOT','pattern':'B'} for i in range(50)]
        self.assertEqual(apply_empirical(sig,trades[:49],self.auto)['efficiency']['basis'],'HEURISTIC_NOT_EXPECTED_VALUE')
        updated=apply_empirical(sig,trades,self.auto)
        self.assertEqual(updated['efficiency']['basis'],'EMPIRICAL_CONSERVATIVE_SAMPLE_NOT_FORECAST')
        self.assertEqual(updated['efficiency']['decision'],'NO_TRADE')
    def test_opening_gap_does_not_claim_fill_at_protective_stop(self):
        p={'symbol':SYMBOL,'ownership':'BOT','qty':10,'avg':5822,'initial_r':122,'initial_stop':5700,'stop':6140,'high':6240,'low':5822,
           'stop_history':[],'opened':self.clock[0].isoformat(),'last_tick':self.clock[0].isoformat(),
           'exit_requested':False,'exit_reason':'','exit_attempts':0,'qty_blocked':False,'trade_id':'GAP','pattern':'A',
           'entry_hour':'14','market_state':'3','signal':{}}
        self.e.s['positions'][SYMBOL]=p; self.e.actual={SYMBOL:{'qty':10,'available':10,'avg':5822}}
        self.e.on_tick(Tick(SYMBOL,self.clock[0],5200,100,100,5210,5190))
        self.assertEqual(p['stop'],6140); sell=list(self.e.s['orders'].values())[-1]
        self.e.cumulative_fill(sell,10,10*5190)
        self.assertLess(self.e.s['trades'][-1]['pnl_estimate'],0); self.assertEqual(self.e.s['trades'][-1]['exit'],5190)

if __name__=='__main__': unittest.main()
