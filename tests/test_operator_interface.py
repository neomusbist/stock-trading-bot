"""번호 메뉴가 기존 권한/위험 경계를 우회하지 않는지 가짜 연결로 검사한다."""
import copy
import json
import tempfile
import unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST,ROOT
from core.util import atomic_json,ProcessLock
from automation.policy import Policy,AutomationConfig
from automation.engine import AutonomousEngine
from automation.allocation import efficiency
from core.strategy import Tick
from tests import test_automation as fixture
from operator_service import OperatorService,STRATEGY_VERSION,decision_line
from operator_console import handle,main,show_status,show_result

class FakeGateway:
    def __init__(self,test):
        self.t=test; self.calls=[]; self.orders=[]; self.snapshot_data={'cash':5000,'equity':100000,
             'holdings':{'267320':{'qty':1,'available':1,'avg':2855}},'orders':[]}
    def snapshot(self): self.calls.append('snapshot'); return copy.deepcopy(self.snapshot_data)
    def command(self,args,timeout=60):
        self.calls.append(tuple(args)); root=self.t.root; stamp=self.t.clock[0]
        if args==['--readiness-check']:
            cfg,auto,p=self.t.s.settings(); policy=Policy(root/'runtime/automation_policy.json'); broker=fixture.FakeBroker()
            e=AutonomousEngine(cfg,auto,policy,root/'runtime/automation-smoke',broker,lambda:self.t.clock[0])
            e.reconcile(self.snapshot_data); e.connected=True; e.market='3'
            e.market_status[fixture.SYMBOL]={'pass':True}
            sig={'symbol':fixture.SYMBOL,'limit':10020,'stop':9800,'pattern':'B','time':stamp.isoformat(),
                 'efficiency':efficiency(10020,9800,10600,.8,30,e.costs)}
            e.enter(sig,Tick(fixture.SYMBOL,stamp,10000,100,100,10010,9990))
            for sym in ('005930','017900'): e.submit('SELL',sym,1,10000,{'ownership':'EXISTING_POLICY'})
            self.orders+=broker.orders; e.save()
            hb={'time':stamp.isoformat(),'market':'3','connected':True,'account_ok':True,'strategy':'SCAN'}
            atomic_json(e.folder/'heartbeat.json',hb)
            atomic_json(root/'automation_check.json',{'started':stamp.isoformat(),'finished':stamp.isoformat(),
                'counts':{'account_checks':1,'subscription_acks':1,'radar_cycles':2,'ticks':5},'fatal':'','real_orders_sent':0})
            atomic_json(root/'watchlist.json',{'asof':stamp.date().isoformat(),'items':[{'code':fixture.SYMBOL}]})
        elif args==['--check-connection']:
            atomic_json(root/'connection_check.json',{'started':stamp.isoformat(),'finished':stamp.isoformat(),'passed':True,
                'counts':{'valid_regular_ticks':3}})
        else: raise AssertionError('unknown command')
        return 0
    def start(self,args):
        self.calls.append(('start',tuple(args))); root=self.t.root; cfg,auto,data=self.t.s.settings()
        policy=Policy(root/'runtime/automation_policy.json'); broker=fixture.FakeBroker()
        if policy.state.get('test_capital_release',{}).get('status')=='APPROVED':
            e=AutonomousEngine(cfg,auto,policy,root/'runtime/automation-production',broker,lambda:self.t.clock[0])
            snap=self.snapshot(); snap['books']={'267320':{'time':self.t.clock[0].isoformat(),'bid':2700,'bid_qty':10,
                'regular':True,'status':{'pass':True}}}
            e.reconcile(snap); e.connected=True; e.market='3'; e.timer()
            for sym in ('005930','017900','052260',fixture.SYMBOL):
                e.submit('SELL',sym,1,2000,{'ownership':'EXISTING_POLICY','policy':'TEST_CAPITAL_RELEASE'})
                e.submit('BUY',sym,1,2000,{})
            self.orders+=broker.orders; order=next(iter(e.s['orders'].values()))
            e.cumulative_fill(order,1,2700); self.t.clock[0]+=timedelta(seconds=1)
            e.reconcile({'cash':2700,'equity':2700,'holdings':{},'orders':[],
                         'query_started_at':self.t.clock[0].isoformat()})
            atomic_json(e.folder/'heartbeat.json',{'time':self.t.clock[0].isoformat(),'market':'3'})
        return Mock()

class OperatorInterfaceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        legacy=patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',True); legacy.start(); self.addCleanup(legacy.stop)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.clock=[datetime(2026,10,1,9,10,tzinfo=KST)]
        atomic_json(self.root/'config.json',asdict(Config()))
        atomic_json(self.root/'automation.json',asdict(AutomationConfig()))
        self.policy=Policy(self.root/'runtime/automation_policy.json')
        self.gateway=FakeGateway(self); self.s=OperatorService(self.root,lambda:self.clock[0],self.gateway,sleep=lambda x:None)

    def save_config(self,**changes):
        p=self.root/'config.json'; d=json.loads(p.read_text()); d.update(changes); atomic_json(p,d)
    def save_auto(self,**changes):
        p=self.root/'automation.json'; d=json.loads(p.read_text()); d.update(changes); atomic_json(p,d)
    def pilot_proof(self):
        return self.s.save_result('pilot',{'verdict':'PASS','orders':0,'samsung_orders':0,'gwang_orders':0})
    def ready(self):
        self.pilot_proof(); self.s.confirm_costs('0.015','0.015','0.2',True)
        self.assertEqual(self.s.prepare_live()['verdict'],'PASS')

    def test_status_shows_all_fields_and_unknown_instead_of_zero_cash(self):
        st=self.s.public_status(); self.assertIsNone(st['cash']); self.assertIsNone(st['available'])
        for key in ('time','supported','mode','live_mode','cap','costs_confirmed','websocket','account','positions','pending',
                    'test_release','error','strategy_version','order_permission','decisions','last_order'): self.assertIn(key,st)
        out=[]; show_status(st,out.append); text='\n'.join(out)
        self.assertIn('삼성전자 005930: HARD_LOCK / 자동매매 금지',text)
        self.assertIn('광전자 017900: HARD_LOCK / 자동매매 금지',text)
        self.assertIn('실제 주문가능현금: 미확인',text)
    def test_old_heartbeat_never_displayed_as_current_connected(self):
        folder=self.root/'runtime/automation-smoke'; atomic_json(folder/'heartbeat.json',
            {'time':(self.clock[0]-timedelta(days=1)).isoformat(),'connected':True,'account_ok':True})
        self.assertIn('현재 정상 미확인',self.s.public_status()['websocket'])
        self.assertIn('감시 미실행/현재 관측 미확인',self.s.public_status()['watch'])
    def test_pilot_fake_engine_cap0_has_zero_general_and_locked_orders(self):
        result=self.s.pilot(); self.assertEqual(result['verdict'],'PASS')
        self.assertEqual((result['orders'],result['samsung_orders'],result['gwang_orders']),(0,0,0))
        self.assertEqual(self.gateway.orders,[]); self.assertEqual(result['available'],0)
        st=self.s.public_status(); self.assertIn('승인한도 0원',' '.join(st['decisions']))
    def test_after_hours_pilot_no_gateway_call(self):
        self.clock[0]=self.clock[0].replace(hour=20)
        self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
    def test_pilot_preflight_blocking_conditions_table(self):
        # 1. live_mode
        self.save_config(live_mode=True); self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        self.save_config(live_mode=False)
        # 2. nonzero cap
        self.save_auto(AUTO_TRADING_CAP_KRW=3000); self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        self.save_auto(AUTO_TRADING_CAP_KRW=0)
        # 3. costs confirmed
        self.save_auto(costs_confirmed=True); self.assertEqual(self.s.pilot()['verdict'],'FAIL')
        self.assertTrue(self.s.settings()[1].costs_confirmed); self.assertFalse(self.gateway.calls)
        self.save_auto(costs_confirmed=False)
        # 4. live authorized record
        self.policy.command('LIVE',confirmation='LIVE'); self.policy.command('ARMED')
        self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        self.policy.state['live_authorized']=False; atomic_json(self.policy.path,self.policy.state)
        # 5. active release
        self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        self.policy.state.pop('test_capital_release',None); atomic_json(self.policy.path,self.policy.state)
        # 6. positions or pending
        p=self.root/'runtime/automation-production/state.json'
        for state in ({'positions':{'x':{'qty':1}},'orders':{}},{'positions':{},'orders':{'x':{'status':'UNKNOWN'}}}):
            atomic_json(p,state); self.assertEqual(self.s.pilot()['verdict'],'FAIL')
        self.assertFalse(self.gateway.calls)
        atomic_json(p,{'positions':{},'orders':{}})
        # 7. broken hard definition
        with patch('operator_service.HARD_LOCKS',{'005930'}):
            self.assertEqual(self.s.pilot()['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        # 8. corrupt state
        p.write_text('{broken')
        with self.assertRaises(ValueError): self.s.pilot()
        self.assertFalse(self.gateway.calls)
    def test_pilot_no_regular_ticks_inconclusive(self):
        original=self.gateway.command
        def no_ticks(args,timeout=60):
            value=original(args,timeout); p=self.root/'automation_check.json'; d=json.loads(p.read_text()); d['counts']['ticks']=0; atomic_json(p,d); return value
        self.gateway.command=no_ticks; self.assertEqual(self.s.pilot()['verdict'],'INCONCLUSIVE')
    def test_pilot_stale_previous_result_not_reused(self):
        self.gateway.command=Mock(return_value=0)
        atomic_json(self.root/'automation_check.json',{'started':(self.clock[0]-timedelta(days=1)).isoformat(),'finished':self.clock[0].isoformat()})
        self.assertEqual(self.s.pilot()['verdict'],'FAIL')
    def test_release_requires_human_confirm_and_today_pilot_pass(self):
        with self.assertRaises(ValueError): self.s.release(False)
        self.assertEqual(self.s.release(True)['verdict'],'FAIL'); self.assertFalse(self.gateway.calls)
        self.assertNotIn('test_capital_release',self.policy.refresh())
    def test_release_only_ninetech_sell1_and_cash_refresh_cap0(self):
        self.pilot_proof(); result=self.s.release(True)
        self.assertEqual(result['verdict'],'PASS'); self.assertEqual(self.gateway.orders,[('SELL','267320',1,2700)])
        self.assertEqual((result['cash'],result['cap'],result['available']),(2700,0,0))
        cfg,auto,policy=self.s.settings(); self.assertFalse(cfg.live_mode); self.assertFalse(auto.costs_confirmed)
        self.assertEqual(policy['mode'],'ARMED'); self.assertFalse(policy['live_authorized'])
        self.assertEqual(policy['test_capital_release']['status'],'COMPLETED')
    def test_release_invalid_quantity_or_unavailable_never_approves(self):
        for qty, available in ((2, 1), (1, 0)):
            self.pilot_proof(); self.gateway.snapshot_data['holdings']['267320']={'qty':qty,'available':available,'avg':2855}
            self.assertEqual(self.s.release(True)['verdict'],'FAIL')
            self.assertNotIn('test_capital_release',self.policy.refresh()); self.assertFalse(self.gateway.orders)
    def test_repeat_release_cannot_resend(self):
        self.pilot_proof(); self.s.release(True)
        with self.assertRaises(ValueError): self.s.release(True)
        self.assertEqual(len(self.gateway.orders),1)
    def test_cancel_menu_release_leaves_approval_disabled(self):
        handle(self.s,'5',reader=lambda x:'',writer=lambda x:None)
        self.assertNotIn('test_capital_release',self.policy.refresh()); self.assertFalse(self.gateway.calls)
    def test_invalid_menu_never_activates_live(self):
        for text in ('LIVE','3000','4 & LIVE','-1','',None):
            with self.assertRaises(ValueError): self.s.select(text)
        self.assertFalse(self.s.settings()[0].live_mode); self.assertEqual(self.s.settings()[1].capital_limit,0)
        self.assertEqual(self.policy.refresh()['mode'],'ARMED'); self.assertFalse(self.gateway.calls)
    def test_prepare_costs_unconfirmed_blocks_live_and_network(self):
        result=self.s.prepare_live(); self.assertEqual(result['verdict'],'FAIL')
        self.assertTrue(any('비용 설정 미확인' in r for r in result['reasons'])); self.assertFalse(self.gateway.calls)
        with self.assertRaises(ValueError): self.s.activate_live('3000','실제매매시작')
    def test_cost_confirmation_requires_explicit_actual_numbers(self):
        with self.assertRaises(ValueError): self.s.confirm_costs('0.015','0.015','0.2',False)
        for value in ('', 'nan','-1','100','abc'):
            with self.assertRaises(ValueError): self.s.confirm_costs(value,'0','0',True)
        self.assertFalse(self.s.settings()[1].costs_confirmed)
    def test_cost_save_does_not_change_cap_live_or_mode(self):
        self.s.confirm_costs('0.015','0.015','0.2',True)
        cfg,auto,policy=self.s.settings(); self.assertTrue(auto.costs_confirmed); self.assertEqual(auto.commission_buy,.00015)
        self.assertEqual(auto.capital_limit,0); self.assertFalse(cfg.live_mode); self.assertEqual(policy['mode'],'ARMED')
    def test_prepare_pass_only_read_checks_no_live(self):
        self.ready(); cfg,auto,policy=self.s.settings()
        self.assertFalse(cfg.live_mode); self.assertEqual(auto.capital_limit,0); self.assertEqual(policy['mode'],'ARMED')
        self.assertEqual(self.gateway.orders,[]); self.assertNotIn(('start',()),self.gateway.calls)
    def test_live_activation_invalid_conditions_table(self):
        # 1. invalid amount or phrase
        self.ready()
        for amount,confirmation in (('','실제매매시작'),('3001','실제매매시작'),('3000',''),('3000','예')):
            with self.assertRaises(ValueError): self.s.activate_live(amount,confirmation)
        # 2. expired proof
        self.clock[0]+=timedelta(seconds=61)
        with self.assertRaises(ValueError): self.s.activate_live('3000','실제매매시작')
        # 3. missing pilot proof
        self.clock[0]-=timedelta(seconds=61)
        (self.root/'runtime/operator_pilot.json').unlink()
        with self.assertRaises(ValueError): self.s.activate_live('3000','실제매매시작')
        self.assertFalse(self.s.settings()[0].live_mode); self.assertFalse(self.policy.refresh()['live_authorized'])
    def test_live_correct_double_confirmation_uses_existing_runner(self):
        self.ready(); result=self.s.activate_live('3000','실제매매시작')
        self.assertEqual(result['verdict'],'STARTED'); cfg,auto,policy=self.s.settings()
        self.assertTrue(cfg.live_mode); self.assertEqual(auto.capital_limit,3000); self.assertEqual(policy['mode'],'LIVE')
        self.assertIn(('start',()),self.gateway.calls); self.assertFalse(self.gateway.orders)
    def test_live_launch_failure_restores_safe_settings(self):
        self.ready(); self.gateway.start=Mock(side_effect=OSError('fake launch error'))
        with self.assertRaises(OSError): self.s.activate_live('3000','실제매매시작')
        cfg,auto,policy=self.s.settings(); self.assertFalse(cfg.live_mode); self.assertEqual(auto.capital_limit,0); self.assertEqual(policy['mode'],'ARMED')
    def test_live_unknown_actual_orders_blocks_prepare(self):
        self.s.confirm_costs('0','0','0',True); self.gateway.snapshot_data['orders']=[{'ord_no':'MANUAL'}]
        self.assertEqual(self.s.prepare_live()['verdict'],'FAIL'); self.assertFalse(self.s.settings()[0].live_mode)
    def test_stop_commands_preserve_meaning_and_protected_symbols(self):
        for command in ('STOP','STOP_FLAT','KILL'):
            self.s.stop(command); policy=self.policy.refresh(); self.assertEqual(policy['mode'],command)
            self.assertFalse(self.gateway.orders); self.assertTrue(self.s.hard_locks_ok())
    def test_idle_lock_prevents_configuration_write_while_engine_running(self):
        with ProcessLock(self.root/'runtime/process.lock'):
            with self.assertRaises(RuntimeError): self.s.confirm_costs('0','0','0',True)
        self.assertFalse(self.s.settings()[1].costs_confirmed)
    def test_safe_armed_restore_only_zero_no_bot_orders(self):
        self.s.stop('STOP'); self.s.arm_safe(True); self.assertEqual(self.policy.refresh()['mode'],'ARMED')
        self.save_config(live_mode=True)
        with self.assertRaises(ValueError): self.s.arm_safe(True)
    def test_console_bad_choice_then_exit_keeps_default_settings(self):
        responses=iter(['LIVE','0']); out=[]
        self.assertEqual(main(reader=lambda x:next(responses),writer=out.append,service=self.s,argv=[]),0)
        self.assertIn('0~9','\n'.join(out)); self.assertEqual(self.policy.refresh()['mode'],'ARMED')
        self.assertFalse(self.gateway.calls)
    def test_recent_result_is_human_readable_and_original_events_unchanged(self):
        self.s.pilot(); eventpath=self.root/'runtime/automation-smoke/events.jsonl'; before=eventpath.read_bytes()
        st=self.s.public_status(); show_result(json.loads((self.root/'runtime/operator_last_result.json').read_text()),lambda x:None)
        self.assertEqual(eventpath.read_bytes(),before); self.assertIn('판정: PASS',(self.root/'사용자_최근결과.txt').read_text(encoding='utf-8-sig'))
        self.assertTrue(st['decisions'])
    def test_missing_cash_is_not_imputed(self):
        for cash in (None,True,float('nan'),-1):
            with self.assertRaises(ValueError): self.s.remember_account({'cash':cash,'orders':[],'holdings':{}})
    def test_decision_translation_identifies_reason_and_gate(self):
        line=decision_line({'timestamp':self.clock[0].isoformat(),'symbol':'ABC','setup':'B','decision':'NO_BUY',
            'primary_reason_code':'PATTERN_OR_GATE_NOT_READY','gates':{'warmup':{'result':'FAIL'}}})
        self.assertIn('B패턴',line); self.assertIn('관찰시간 부족',line); self.assertIn('매수 안 함',line)

    def test_pilot_inconclusive_conditions_table(self):
        original=self.gateway.command
        # 1. watchlist invalid or empty
        for watch in ({'asof':'2026-09-30','items':[{'code':fixture.SYMBOL}]},{'asof':'2026-10-01','items':[]}):
            def command_watch(args,timeout=60):
                rc=original(args,timeout); atomic_json(self.root/'watchlist.json',watch); return rc
            self.gateway.command=command_watch
            result=self.s.pilot(); self.assertEqual(result['verdict'],'INCONCLUSIVE')
        # 2. missing strategy evaluation events
        def command_events(args,timeout=60):
            rc=original(args,timeout); (self.root/'runtime/automation-smoke/events.jsonl').write_text('',encoding='utf-8'); return rc
        self.gateway.command=command_events
        self.assertEqual(self.s.pilot()['verdict'],'INCONCLUSIVE')
        # 3. last account or connection failure
        for field in ('connected','account_ok'):
            def command_hb(args,timeout=60):
                rc=original(args,timeout); path=self.root/'runtime/automation-smoke/heartbeat.json'
                hb=json.loads(path.read_text()); hb[field]=False; atomic_json(path,hb); return rc
            self.gateway.command=command_hb
            self.assertEqual(self.s.pilot()['verdict'],'INCONCLUSIVE')
        self.assertFalse(self.gateway.orders)
    def test_zero_signals_with_complete_evaluation_can_pass(self):
        original=self.gateway.command
        def command(args,timeout=60):
            rc=original(args,timeout); path=self.root/'runtime/automation-smoke/events.jsonl'
            rows=[json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()]
            for row in rows: row['signal_detected']=False
            path.write_text('\n'.join(json.dumps(x) for x in rows),encoding='utf-8'); return rc
        self.gateway.command=command
        result=self.s.pilot(); self.assertEqual(result['verdict'],'PASS'); self.assertEqual(result['signals'],0)
    def test_release_launch_failure_stops_and_preserves_one_shot_approval(self):
        self.pilot_proof(); self.gateway.start=Mock(side_effect=OSError('fake launch error'))
        with self.assertRaises(OSError): self.s.release(True)
        policy=self.policy.refresh(); self.assertEqual(policy['mode'],'STOP')
        self.assertEqual(policy['test_capital_release']['status'],'APPROVED'); self.assertFalse(self.gateway.orders)
        with self.assertRaises(ValueError): self.s.arm_safe(True)
    def test_emergency_entry_does_not_start_account_or_network(self):
        out=[]; self.assertEqual(main(service=self.s,writer=out.append,reader=lambda x:'',argv=['--emergency','KILL']),0)
        self.assertEqual(self.policy.refresh()['mode'],'KILL'); self.assertFalse(self.gateway.calls); self.assertFalse(self.gateway.orders)
    def test_missing_policy_is_not_recreated_by_emergency(self):
        self.policy.path.unlink()
        with self.assertRaises(ValueError): self.s.stop('KILL')
        self.assertFalse(self.policy.path.exists()); self.assertFalse(self.gateway.calls)

if __name__=='__main__': unittest.main()
