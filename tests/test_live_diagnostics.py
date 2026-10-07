"""소액 LIVE 운영 진단. 계좌/주문은 합성 입력과 가짜 브로커만 사용한다."""
import copy,json,time,queue,unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock,patch
from tests import test_automation as f
from tests import test_operator_interface as ui
from tests import test_preflight as pf
from automation import runner
from automation.allocation import efficiency
from automation.radar import DynamicRadar
from core.strategy import Tick
from core.engine import Engine
from core.util import atomic_json,read_json_retry
from operator_console import handle,yes,show_status

class HaltTests(unittest.TestCase):
    setUp=f.AutomationTests.setUp
    make_engine=f.AutomationTests.make_engine
    signal=f.AutomationTests.signal
    tick=f.AutomationTests.tick
    buy=f.AutomationTests.buy
    def fault(self):
        self.engine.halt('데이터/상태 오류: PermissionError')
        self.engine.s['halt_info']['exception']={'filename2':str(self.engine.path),'error':'PermissionError'}
    def fresh(self):
        self.clock[0]+=timedelta(seconds=2)
        self.engine.reconcile({**self.snapshot,'query_started_at':self.clock[0].isoformat()})
    def test_safe_io_recovery_persists_history_and_recurrence(self):
        self.fault(); self.fresh(); self.assertTrue(self.engine.recover_io_halt())
        self.assertEqual(read_json_retry(self.engine.path)['halt'],'')
        self.assertFalse(self.engine.s['halt_history'][-1]['active'])
        self.fault(); self.assertTrue(self.engine.s['halt']); self.assertTrue(self.engine.s['halt_info']['active'])
    def test_armed_command_alone_never_clears_halt(self):
        self.fault(); self.policy.command('ARMED'); self.assertFalse(self.engine.recover_io_halt()); self.assertTrue(self.engine.s['halt'])
    def test_dangerous_halts_never_auto_clear_or_downgrade(self):
        for reason in ('주문결과 불명확: 자동 재전송 금지, HTS 확인 필요','관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요','주문번호 없는 전송 의도: HTS 수동 대조 필요'):
            self.engine.halt(reason); self.fresh(); self.assertFalse(self.engine.recover_io_halt()); self.assertEqual(self.engine.s['halt'],reason)
        self.engine.halt('주문번호 없는 전송 의도: HTS 수동 대조 필요'); self.fault(); self.fresh()
        self.assertEqual(self.engine.s['halt_info']['classification'],'MANUAL_REVIEW'); self.assertFalse(self.engine.recover_io_halt())
    def test_old_query_connection_loss_and_account_orders_block_recovery(self):
        self.fault(); self.clock[0]+=timedelta(seconds=2)
        self.engine.reconcile({**self.snapshot,'query_started_at':(self.clock[0]-timedelta(seconds=3)).isoformat()})
        self.assertFalse(self.engine.recover_io_halt())
        self.fresh(); self.engine.disconnect(); self.assertFalse(self.engine.recover_io_halt())
        self.engine.connected=True; self.engine.reconcile_has_orders=True; self.assertFalse(self.engine.recover_io_halt())
    def test_positions_and_pending_intent_block_recovery(self):
        self.buy(); self.fault(); self.fresh(); self.assertFalse(self.engine.recover_io_halt())
        self.engine.s['positions']={}; self.engine.s['orders']['x']={'status':'INTENT','symbol':f.SYMBOL,'side':'BUY'}
        self.assertFalse(self.engine.recover_io_halt())
    def test_legacy_reason_only_halt_needs_manual_review(self):
        self.engine.s['halt']='데이터/상태 오류: PermissionError'; self.engine.s.pop('halt_info',None); self.fresh()
        self.assertFalse(self.engine.recover_io_halt()); self.assertTrue(self.engine.s['halt'])
    def test_storage_failure_and_unknown_path_never_auto_recover(self):
        self.fault(); self.fresh()
        with patch.object(self.engine,'save',side_effect=PermissionError('still locked')):
            with self.assertRaises(PermissionError): self.engine.recover_io_halt()
        self.assertTrue(self.engine.s['halt'])
        self.fault(); self.engine.s['halt_info']['exception']={}; self.fresh()
        self.assertFalse(self.engine.recover_io_halt()); self.assertTrue(self.engine.s['halt'])
        self.assertEqual(self.engine.s['halt_info']['classification'],'MANUAL_REVIEW')
    def test_blocked_live_is_rejected_without_paper_acceptance(self):
        with patch.object(self.engine,'live_order_allowed',return_value=False):
            o=Engine.submit(self.engine,'BUY',f.SYMBOL,1,500,{'stop':495})
        self.assertEqual(o['status'],'REJECTED'); self.assertEqual(o['execution'],'BLOCKED'); self.assertFalse(o['id']); self.assertEqual(self.broker.orders,[])
    def test_missing_actual_order_id_is_unknown_and_no_retry(self):
        with patch.object(self.broker,'submit',return_value='') as submit:
            o=Engine.submit(self.engine,'BUY',f.SYMBOL,1,500,{'stop':495})
        self.assertEqual(o['status'],'UNKNOWN'); self.assertTrue(self.engine.s['halt']); self.assertEqual(submit.call_count,1)
    def test_reg_ack_reconcile_disconnect_use_same_gate(self):
        detector=Mock(); account=Mock(); radar=Mock()
        self.engine.disconnect(); self.engine.account_ok=False
        runner.process_message(self.engine,detector,{'trnm':'REG','return_code':0},self.clock[0],set(),{},radar,{},account,False)
        self.assertTrue(self.engine.connected); self.assertFalse(self.engine.account_ok); account.request.set.assert_called()
        self.fresh(); self.assertTrue(self.engine.account_ok)
        runner.process_message(self.engine,detector,{'trnm':'DISCONNECTED'},self.clock[0],set(),{},radar,{},account,False)
        self.assertFalse(self.engine.connected); self.assertFalse(self.engine.account_ok)
    def test_3000_cap_synthetic_complete_roundtrip_existing_core(self):
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=3000,replacement_enabled=False)
        self.engine=self.make_engine(); self.engine.reconcile({**self.snapshot,'cash':3420}); self.engine.connected=True; self.engine.market='3'
        sig={'symbol':f.SYMBOL,'limit':500,'stop':495,'pattern':'B','time':self.clock[0].isoformat(),
             'efficiency':efficiency(500,495,600,.8,30,self.engine.costs)}
        o=self.engine.enter(sig,Tick(f.SYMBOL,self.clock[0],500,100,100,501,499))
        self.assertIsNotNone(o); self.assertEqual(o['execution'],'REAL'); self.assertEqual(o['qty'],1)
        self.assertLessEqual(o['qty']*500*(1+self.auto.commission_buy),600)
        self.engine.cumulative_fill(o,1,500); self.engine.actual={f.SYMBOL:{'qty':1,'available':1,'avg':500}}
        self.engine.request_exit(f.SYMBOL,'합성 보호청산'); self.engine.drive_exits({f.SYMBOL:499})
        sell=self.engine.active_orders(side='SELL')[0]; self.engine.cumulative_fill(sell,1,499)
        self.assertFalse(self.engine.s['positions']); self.assertEqual(len(self.engine.s['trades']),1)
        self.engine.reconcile({**self.snapshot,'cash':3418}); self.assertEqual(self.engine.a.capital_limit,3000)
        self.assertEqual([x[0] for x in self.broker.orders],['BUY','SELL'])
    def test_affordability_includes_buy_cost_and_approved_budget(self):
        a=replace(self.auto,AUTO_TRADING_CAP_KRW=3000); radar=DynamicRadar(self.cfg,a,self.policy)
        radar.available_capital=3000
        item={'reference_price':3000}; self.assertFalse(radar.affordable(item)); self.assertAlmostEqual(item['affordability']['one_share_cost'],3000.45,places=8)
        radar.available_capital=2700; self.assertFalse(radar.affordable({'reference_price':2800})); self.assertTrue(radar.affordable({'reference_price':500}))

class ReleaseTests(unittest.TestCase):
    setUp=pf.PreflightTests.setUp
    make_engine=pf.PreflightTests.make_engine
    zero_armed=pf.PreflightTests.zero_armed
    holding=pf.PreflightTests.holding
    approve=pf.PreflightTests.approve
    def test_blocked_release_and_five_second_boundary_guards(self):
        self.zero_armed(); self.holding(); self.approve()
        with patch.object(self.engine,'live_order_allowed',return_value=False): self.engine.timer()
        order=list(self.engine.s['orders'].values())[0]
        self.assertEqual(order['execution'],'BLOCKED'); self.assertEqual(order['status'],'REJECTED'); self.assertEqual(self.broker.orders,[])
        self.assertEqual(self.policy.refresh()['test_capital_release']['status'],'SUBMITTING')
        self.engine.timer(); self.assertEqual(self.broker.orders,[])
        self.setUp(); self.zero_armed(); self.holding(); self.approve(); self.clock[0]+=timedelta(seconds=5.001)
        self.engine.timer(); self.assertEqual(self.broker.orders,[])

class OperatorDiagnosticsTests(unittest.TestCase):
    setUp=ui.OperatorInterfaceTests.setUp
    save_config=ui.OperatorInterfaceTests.save_config
    save_auto=ui.OperatorInterfaceTests.save_auto
    pilot_proof=ui.OperatorInterfaceTests.pilot_proof
    ready=ui.OperatorInterfaceTests.ready
    def test_operator_confirmation_and_preflight_expiration_invariants(self):
        for word in ('yes','YES','Yes'): self.assertTrue(yes('',lambda _:word))
        for word in ('예','','y','실제매매시작'): self.assertFalse(yes('',lambda _:word))
        self.ready()
        with self.assertRaises(ValueError): self.s.activate_live('3000','yes',start=False)
        self.setUp(); self.ready(); self.assertEqual(self.s.live_ready_seconds(),60)
        def reader(prompt):
            self.clock[0]+=timedelta(seconds=61); return 'yes'
        with patch.object(self.s,'prepare_live',return_value={'verdict':'PASS'}):
            output=[]; handle(self.s,'6',reader,output.append)
        self.assertEqual(self.s.live_ready_seconds(),0); self.assertIn('유효시간이 지났',''.join(output)); self.assertFalse(self.s.settings()[0].live_mode)
    def test_halt_visible_when_runner_stopped_and_old_ui_error_labeled(self):
        folder=self.root/'runtime/automation-production'
        atomic_json(folder/'state.json',{'positions':{},'orders':{},'halt':'주문결과 불명확','automation':{}})
        atomic_json(self.root/'runtime/operator_error.json',{'timestamp':(self.clock[0]-timedelta(hours=1)).isoformat(),'error':'old menu error'})
        status=self.s.public_status(); output=[]; show_status(status,output.append,details=False)
        self.assertIn('HALT: 주문결과 불명확',''.join(output)); self.assertEqual(status['order_loop'],'NOT RUNNING')
        self.assertEqual(status['current_error'],'주문결과 불명확'); self.assertFalse(status['trading_on'])
    def test_unsubmitted_release_fail_fast_and_startup_hold_detection(self):
        self.pilot_proof(); self.gateway.start=lambda _:None; elapsed=[0]
        self.s.monotonic=lambda:elapsed[0]
        def sleep(n): elapsed[0]+=n
        self.s.sleep=sleep
        result=self.s.release(True)
        self.assertEqual(result['verdict'],'FAIL'); self.assertLess(elapsed[0],180)
        self.assertEqual(result['order_status'],'NOT_SUBMITTED'); self.assertEqual(self.policy.refresh()['test_capital_release']['status'],'APPROVED')
        self.setUp(); self.pilot_proof(); elapsed=[0]; self.s.monotonic=lambda:elapsed[0]
        self.s.sleep=lambda n:elapsed.__setitem__(0,elapsed[0]+n)
        def start(_):
            stamp=self.clock[0]+timedelta(seconds=1); self.clock[0]+=timedelta(seconds=2)
            folder=self.root/'runtime/automation-production'; folder.mkdir(parents=True,exist_ok=True)
            (folder/'events.jsonl').write_text(json.dumps({'event':'EXIT_EVAL','symbol':'267320','timestamp':stamp.isoformat(),'decision':'HOLD','primary_reason_code':'HALT_ACTIVE'})+'\n')
        self.gateway.start=start
        result=self.s.release(True)
        self.assertEqual(result['blocking_reason'],'HALT_ACTIVE'); self.assertEqual(elapsed[0],0)
        self.assertEqual(self.policy.refresh()['test_capital_release']['status'],'APPROVED')
    def test_approved_only_cancel_and_submitting_cannot_reset(self):
        self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        self.assertEqual(self.s.cancel_test_release(True)['verdict'],'SAVED'); self.assertNotIn('test_capital_release',self.policy.refresh())
        self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE'); self.policy.state['test_capital_release'].update(status='SUBMITTING',order_local='x'); atomic_json(self.policy.path,self.policy.state)
        with self.assertRaises(ValueError): self.s.cancel_test_release(True)
        self.assertEqual(self.policy.refresh()['test_capital_release']['order_local'],'x')

if __name__=='__main__': unittest.main()
