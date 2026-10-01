"""운영 래퍼만 검증한다. 계좌/API/주문은 전부 모의이며 실제 연결은 없다."""
import contextlib,io,json,os,tempfile,time,unittest,queue,threading,subprocess,sys
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import ROOT,Config,KST
from core.util import atomic_json,ProcessLock
from core.strategy import Tick
from automation.policy import Policy,AutomationConfig
from automation.engine import AutonomousEngine
from automation.allocation import efficiency
from automation import runner
from tests import test_automation as fixture
from operator_service import OperatorService,STRATEGY_VERSION,read_json
from operator_console import handle,main,show_status
from operator_process import process_identity
import operator_runner

class Gateway:
    def __init__(self,t): self.t=t; self.starts=0
    def start(self,args):
        self.starts+=1; self.t.record('RUNNING'); return Mock()
    def snapshot(self): raise AssertionError('No account network in continuous front end')
    def command(self,*a,**k): raise AssertionError('No second smoke test')

class ContinuousTests(unittest.TestCase):
    def setUp(self):
        legacy=patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',True); legacy.start(); self.addCleanup(legacy.stop)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.clock=[datetime(2026,10,1,9,30,tzinfo=KST)]
        atomic_json(self.root/'config.json',asdict(Config())); atomic_json(self.root/'automation.json',asdict(AutomationConfig()))
        self.policy=Policy(self.root/'runtime/automation_policy.json'); self.gateway=Gateway(self)
        self.s=OperatorService(self.root,clock=lambda:self.clock[0],gateway=self.gateway,sleep=lambda n:None)
        self.s.save_result('pilot',{'verdict':'PASS'})
        self.output=contextlib.redirect_stdout(io.StringIO()); self.output.__enter__(); self.addCleanup(self.output.__exit__,None,None,None)
    def record(self,status='RUNNING',**extras):
        cfg,auto,policy=self.s.settings()
        atomic_json(self.root/'runtime/operator_runner.json',{'pid':os.getpid(),'identity':process_identity(os.getpid()),
            'token':'test','started':self.clock[0].isoformat(),'heartbeat':self.clock[0].isoformat(),'status':status,
            'initial_mode':policy['mode'],'mode':policy['mode'],'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),**extras})
    def live(self):
        cfg=replace(Config(),live_mode=True); auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=1000000,costs_confirmed=True)
        atomic_json(self.root/'config.json',asdict(cfg)); atomic_json(self.root/'automation.json',asdict(auto))
        self.policy.command('LIVE',confirmation='LIVE')
    def engine(self):
        cfg,auto,_=self.s.settings(); broker=fixture.FakeBroker()
        engine=AutonomousEngine(cfg,auto,self.policy,self.root/'runtime/automation-production',broker,lambda:self.clock[0])
        engine.reconcile({'cash':1000000,'equity':1000000,'holdings':{},'orders':[]})
        engine.connected=True; engine.market='3'; engine.market_status[fixture.SYMBOL]={'pass':True}
        return engine,broker
    def signal(self,e):
        return {'symbol':fixture.SYMBOL,'limit':10020,'stop':9800,'pattern':'B','time':self.clock[0].isoformat(),
                'efficiency':efficiency(10020,9800,10600,.8,30,e.costs)}
    def tick(self,price=10000): return Tick(fixture.SYMBOL,self.clock[0],price,100,100,price+10,price-10)
    def test_armed_start_separate_from_smoke_and_config_unchanged(self):
        before=[(self.root/p).read_bytes() for p in ('config.json','automation.json','runtime/automation_policy.json')]
        self.assertEqual(self.s.start_continuous()['verdict'],'STARTED'); self.assertEqual(self.gateway.starts,1)
        self.assertEqual(before,[(self.root/p).read_bytes() for p in ('config.json','automation.json','runtime/automation_policy.json')])
    def test_start_requires_first_pass_but_not_repeat_150_seconds(self):
        (self.root/'runtime/operator_pilot.json').unlink(); self.assertEqual(self.s.start_continuous()['verdict'],'FAIL')
        self.assertEqual(self.gateway.starts,0)
        self.s.save_result('pilot',{'verdict':'PASS'}); self.clock[0]+=timedelta(days=1)
        self.assertEqual(self.s.start_continuous()['verdict'],'STARTED'); self.assertEqual(self.gateway.starts,1)
    def test_duplicate_start_no_new_instance(self):
        self.s.start_continuous()
        for _ in range(3): self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING')
        self.assertEqual(self.gateway.starts,1)
    def test_alive_pid_old_heartbeat_never_launches_second_instance(self):
        self.record(heartbeat=(self.clock[0]-timedelta(minutes=2)).isoformat())
        info=self.s.process_info(); self.assertTrue(info['running']); self.assertFalse(info['process_heartbeat_fresh'])
        self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING'); self.assertEqual(self.gateway.starts,0)
    def test_dead_pid_ignored_without_clearing_order_ledger(self):
        self.record(pid=2147483000,identity='dead'); self.assertTrue(self.s.process_info()['stale'])
        self.assertEqual(self.s.start_continuous()['verdict'],'STARTED')
    def test_reused_pid_identity_does_not_attach_wrong_process(self):
        self.record(identity='different-creation-time'); self.assertFalse(self.s.running())
        self.assertTrue(self.s.process_info()['stale']); self.assertEqual(self.s.start_continuous()['verdict'],'STARTED')
    def test_existing_legacy_runner_lock_blocks_start_even_no_pid(self):
        with ProcessLock(self.root/'runtime/process.lock'):
            self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING')
        self.assertEqual(self.gateway.starts,0)
    def test_launch_lock_and_worker_lock_prevent_duplicate_popen(self):
        with ProcessLock(self.root/'runtime/operator_launch.lock'),patch('operator_service.subprocess.Popen') as popen:
            with self.assertRaises(RuntimeError): self.s.launch_continuous()
            popen.assert_not_called()
        with ProcessLock(self.root/'runtime/operator_runner.lock'),patch('operator_service.subprocess.Popen') as popen:
            with self.assertRaises(ValueError): self.s.launch_continuous()
            popen.assert_not_called()
    def test_stopped_record_with_live_lock_still_blocks(self):
        self.record('STOPPED')
        with ProcessLock(self.root/'runtime/supervisor.lock'):
            self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING')
    def test_corrupt_process_metadata_refuses_start(self):
        (self.root/'runtime/operator_runner.json').write_text('{broken')
        with self.assertRaises(ValueError): self.s.start_continuous()
        self.assertEqual(self.gateway.starts,0)
    def test_after_hours_start_no_process_no_network(self):
        for hour,minute in ((8,59),(15,20),(20,0)):
            self.clock[0]=self.clock[0].replace(hour=hour,minute=minute)
            self.assertEqual(self.s.start_continuous()['verdict'],'FAIL')
        self.assertEqual(self.gateway.starts,0)
    def test_active_release_cannot_be_started_as_general_monitor(self):
        self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        self.assertEqual(self.s.start_continuous()['verdict'],'FAIL'); self.assertEqual(self.gateway.starts,0)
    def test_live_start_needs_existing_permissions_and_explicit_confirmation(self):
        self.live(); self.assertEqual(self.s.start_continuous()['verdict'],'FAIL')
        self.assertEqual(self.s.start_continuous(confirm_live=True)['verdict'],'STARTED')
    def test_live_zero_cap_costs_or_authorization_missing_blocks(self):
        self.live()
        for change in ({'AUTO_TRADING_CAP_KRW':0},{'costs_confirmed':False}):
            _,auto,_=self.s.settings(); atomic_json(self.root/'automation.json',asdict(replace(auto,**change)))
            self.assertEqual(self.s.start_continuous(confirm_live=True)['verdict'],'FAIL')
            self.live()
        policy=self.policy.refresh(); policy['live_authorized']=False; atomic_json(self.policy.path,policy)
        self.assertEqual(self.s.start_continuous(confirm_live=True)['verdict'],'FAIL'); self.assertEqual(self.gateway.starts,0)
    def test_armed_continuous_signals_keep_logging_without_orders(self):
        self.s.start_continuous(); e,broker=self.engine()
        for _ in range(3): e.enter(self.signal(e),self.tick()); self.clock[0]+=timedelta(seconds=e.a.decision_snapshot_seconds+1)
        events=[json.loads(x) for x in (e.folder/'events.jsonl').read_text().splitlines()]
        self.assertGreaterEqual(sum(x.get('event')=='ENTRY_EVAL' for x in events),3)
        self.assertTrue(all(x.get('decision')=='NO_BUY' for x in events if x.get('event')=='ENTRY_EVAL'))
        self.assertEqual(broker.orders,[]); self.assertFalse(e.s['positions'])
    def test_live_continuous_calls_original_order_intent_path(self):
        self.live(); self.s.start_continuous(confirm_live=True); e,broker=self.engine()
        order=e.enter(self.signal(e),self.tick()); self.assertIsNotNone(order)
        self.assertEqual(order['status'],'ACCEPTED'); self.assertEqual(broker.orders[0][0:2],('BUY',fixture.SYMBOL))
        self.assertFalse(e.s['positions']); self.assertTrue(e.s['orders'])
    def test_stop_blocks_new_entry_keeps_live_position_protection(self):
        self.live(); e,broker=self.engine(); order=e.enter(self.signal(e),self.tick())
        e.cumulative_fill(order,order['qty'],order['qty']*order['limit']); self.s.stop('STOP')
        before=e.s['positions'][fixture.SYMBOL]['stop']; self.assertFalse(e.controls())
        self.assertTrue(e.s['positions']); self.assertEqual(e.s['positions'][fixture.SYMBOL]['stop'],before)
        self.assertIsNone(e.enter(self.signal(e),self.tick())); self.assertEqual(len(broker.orders),1)
    def test_stop_flat_uses_original_exit_request(self):
        self.live(); e,broker=self.engine(); order=e.enter(self.signal(e),self.tick())
        e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
        e.actual[fixture.SYMBOL]={'qty':order['qty'],'available':order['qty'],'avg':order['limit']}
        self.s.stop('STOP_FLAT'); e.controls({fixture.SYMBOL:10000})
        self.assertTrue(e.s['positions'][fixture.SYMBOL]['exit_requested']); self.assertEqual(self.policy.mode,'STOP_FLAT')
        self.assertEqual(broker.orders[-1][0],'SELL')
    def test_kill_waits_for_new_account_before_exit(self):
        self.live(); e,_=self.engine(); self.s.stop('KILL'); self.assertFalse(e.controls()); self.assertFalse(e.account_ok)
        self.assertFalse(e.kill_checked); self.assertEqual(self.policy.mode,'KILL')
    def test_hard_locks_sell_never_reach_broker(self):
        self.live(); e,broker=self.engine()
        for sym in ('005930','017900'):
            for reason in ('STOP','STOP_FLAT','KILL','SESSION_FLATTEN','PROTECTION','FORCED'):
                self.assertIsNone(e.submit('SELL',sym,1,10000,{'ownership':'BOT','reason':reason}))
        self.assertEqual(broker.orders,[])
    def test_1505_cutoff_1510_flatten_1520_support_unchanged(self):
        self.live(); e,broker=self.engine()
        self.clock[0]=self.clock[0].replace(hour=15,minute=5); self.assertEqual(e.new_entry_reason(fixture.SYMBOL),'NEW_ENTRY_CUTOFF')
        self.clock[0]=self.clock[0].replace(minute=20); self.assertEqual(e.new_entry_reason(fixture.SYMBOL),'OUT_OF_SUPPORTED_SESSION')
        self.assertEqual(e.a.force_flatten_start,'15:10:00'); self.assertFalse(e.a.ALLOW_OVERNIGHT); self.assertEqual(broker.orders,[])
    def test_menu_only_exit_leaves_runner_and_permission_unchanged(self):
        self.s.start_continuous(); before=self.policy.path.read_bytes()
        self.assertFalse(handle(self.s,'0',reader=lambda x:'yes',writer=lambda x:None))
        self.assertTrue(self.s.running()); self.assertEqual(before,self.policy.path.read_bytes())
    def test_default_exit_requests_stop_without_killing_process(self):
        self.s.start_continuous()
        self.assertTrue(handle(self.s,'0',reader=lambda x:'',writer=lambda x:None))
        self.assertEqual(self.policy.refresh()['mode'],'STOP'); self.assertTrue(self.s.running())
    def test_live_transition_menu_refuses_hot_configuration_change(self):
        self.s.start_continuous(); before=(self.root/'config.json').read_bytes(); out=[]
        handle(self.s,'6',reader=Mock(side_effect=AssertionError('no prompt')),writer=out.append)
        self.assertIn('먼저 4번',' '.join(out)); self.assertEqual(before,(self.root/'config.json').read_bytes())
    def test_live_permission_save_does_not_start_runner(self):
        from tests import test_operator_interface as ui
        gateway=ui.FakeGateway(self); self.s.gateway=gateway
        self.s.confirm_costs('0.015','0.015','0.2',True); self.s.prepare_live()
        self.s.activate_live('3000','실제매매시작',start=False)
        self.assertTrue(self.s.settings()[0].live_mode); self.assertFalse(self.s.running()); self.assertNotIn(('start',()),gateway.calls)
    def test_wrapper_delegates_and_restores_armed_after_stop(self):
        def original_supervise(): self.s.stop('STOP'); return 0
        self.assertEqual(operator_runner.run(self.root,lambda:self.clock[0],original_supervise),0)
        info=self.s.process_info(); self.assertFalse(info['running']); self.assertEqual(info['status'],'STOPPED')
        self.assertEqual(self.policy.refresh()['mode'],'ARMED')
    def test_wrapper_exception_records_crashed_and_keeps_ledger(self):
        crash=Mock(side_effect=RuntimeError('synthetic crash'))
        with self.assertRaises(RuntimeError): operator_runner.run(self.root,lambda:self.clock[0],crash)
        self.assertEqual(self.s.process_info()['status'],'CRASHED'); self.assertFalse(self.s.running())
        self.assertEqual(self.policy.refresh()['mode'],'ARMED')
    def test_wrapper_default_calls_same_supervisor_not_new_trading_loop(self):
        with patch('supervisor.main',return_value=0) as original:
            self.assertEqual(operator_runner.run(self.root,lambda:self.clock[0]),0)
            original.assert_called_once_with()
    def test_wrapper_after_hours_never_calls_supervisor(self):
        supervise=Mock(); self.clock[0]=self.clock[0].replace(hour=20)
        with self.assertRaises(ValueError): operator_runner.run(self.root,lambda:self.clock[0],supervise)
        supervise.assert_not_called()
    def test_session_end_requests_stop_preserving_other_commands(self):
        self.clock[0]=self.clock[0].replace(hour=15,minute=20)
        self.assertTrue(operator_runner.session_end(self.root,lambda:self.clock[0])); self.assertEqual(self.policy.refresh()['mode'],'STOP')
        self.policy.command('KILL'); operator_runner.session_end(self.root,lambda:self.clock[0]); self.assertEqual(self.policy.refresh()['mode'],'KILL')
    def test_resume_live_requires_confirmation_and_same_settings(self):
        self.live(); self.record('STOPPED'); self.s.stop('STOP')
        with self.assertRaises(ValueError): self.s.resume_live_continuous(False)
        self.assertEqual(self.s.resume_live_continuous(True)['verdict'],'STARTED'); self.assertEqual(self.gateway.starts,1)
    def test_resume_live_changed_cap_blocked_without_auto_increase(self):
        self.live(); self.record('STOPPED'); self.s.stop('STOP'); _,auto,_=self.s.settings()
        atomic_json(self.root/'automation.json',asdict(replace(auto,AUTO_TRADING_CAP_KRW=3000)))
        with self.assertRaises(ValueError): self.s.resume_live_continuous(True)
        self.assertEqual(self.policy.refresh()['mode'],'STOP'); self.assertEqual(self.gateway.starts,0)
    def test_status_separates_mode_process_and_stale_market(self):
        self.s.start_continuous(); st=self.s.public_status(); self.assertEqual(st['mode'],'ARMED'); self.assertTrue(st['process']['running'])
        self.assertFalse(st['trading_on']); out=[]; show_status(st,out.append,details=False)
        self.assertIn('실제매매: OFF','\n'.join(out)); self.assertNotIn('최근 감시목록','\n'.join(out))
    def test_native_identity_checks_alive_and_invalid_pid(self):
        self.assertIsNotNone(process_identity(os.getpid())); self.assertIsNone(process_identity(-1)); self.assertIsNone(process_identity(2147483000))

    def test_original_continuous_runner_updates_watch_and_evaluates_until_stop(self):
        account=Mock(); account.q=queue.Queue(); account.request=threading.Event()
        account.q.put({'snapshot':{'cash':5000,'equity':5000,'holdings':{},'orders':[]},'fills':[],'started':time.monotonic(),'error':None})
        radar=Mock(); radar.q=queue.Queue(); radar.cache={}
        row={'code':fixture.SYMBOL,'name':'합성후보','status':'WATCH','resistance':11000,'market_status':{'pass':True}}
        payload={'time':self.clock[0].isoformat(),'rows':[row],'watch':[row],'changes':{'added':[fixture.SYMBOL],'removed':[]},'error':None}
        radar.q.put(payload); pending=queue.Queue()
        pending.put({'trnm':'REG','return_code':0})
        for index in range(3):
            pending.put({'trnm':'REAL','data':[{'type':'0B','item':fixture.SYMBOL,'values':{'20':'093000','10':'10000','15':'100',
                '13':str((index+1)*100),'27':'10010','28':'9990','290':'2'}}]})
        def receive(**kwargs):
            if pending.empty(): self.s.stop('STOP'); raise queue.Empty
            frame=pending.get_nowait()
            if frame['trnm']=='REAL':
                self.clock[0]+=timedelta(seconds=31); frame['data'][0]['values']['20']=self.clock[0].strftime('%H%M%S')
                if pending.qsize()==1: radar.q.put(payload)
            return self.clock[0],frame
        feed=Mock(); feed.q.get.side_effect=receive; feed.overflow.is_set.return_value=False
        fake=Mock(); fake.auth_session={}; fake.identity='continuous-offline'; fake.submit.side_effect=AssertionError('no armed order')
        with patch.object(runner,'Rest',return_value=fake),patch.object(runner,'RadarWorker',return_value=radar),\
             patch.object(runner,'AccountWorker',return_value=account),patch.object(runner,'Feed',return_value=feed),\
             patch.object(runner,'Reporter',return_value=Mock()),patch.object(runner,'now',side_effect=lambda:self.clock[0]):
            result=runner.run(Config(),self.root,seconds=0)
        self.assertEqual(result['counts']['ticks'],3); self.assertEqual(result['counts']['radar_cycles'],2)
        self.assertEqual(result['real_orders_sent'],0); fake.submit.assert_not_called(); feed.close.assert_called_once()
        events=[json.loads(x) for x in (self.root/'runtime/automation-production/events.jsonl').read_text().splitlines()]
        self.assertGreaterEqual(sum(x.get('event')=='ENTRY_EVAL' for x in events),3)

    def test_popen_handshake_uses_single_existing_supervisor_wrapper(self):
        child=Mock(); child.poll.return_value=None
        def popen(args,**kwargs):
            self.record(token=args[args.index('--token')+1]); return child
        with patch('operator_service.subprocess.Popen',side_effect=popen) as factory:
            self.assertIs(self.s.launch_continuous(),child); self.assertEqual(factory.call_count,1)
            arguments=factory.call_args.args[0]
            self.assertIn(str(self.root/'operator_runner.py'),arguments); self.assertNotIn('--paper-seconds',arguments)
            with self.assertRaises(ValueError): self.s.launch_continuous()
            self.assertEqual(factory.call_count,1)
    def test_real_offline_child_pid_lock_stop_and_stale_detection(self):
        stopfile=self.root/'end_offline_stub'
        script="""import sys,time
from pathlib import Path
from datetime import datetime
from core.settings import KST
from core.util import ProcessLock
from automation.policy import Policy
import operator_runner
root=Path(sys.argv[1]); stopfile=root/'end_offline_stub'
def stub():
    with ProcessLock(root/'runtime/supervisor.lock'),ProcessLock(root/'runtime/process.lock'):
        while not stopfile.exists(): time.sleep(.05)
        Policy(root/'runtime/automation_policy.json').command('STOP')
    return 0
operator_runner.run(root,lambda:datetime(2026,10,1,9,30,tzinfo=KST),stub,token='offline-process')
"""
        child=subprocess.Popen([sys.executable,'-X','utf8','-c',script,str(self.root)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            until=time.monotonic()+10
            while time.monotonic()<until and not (self.root/'runtime/operator_runner.json').exists() and child.poll() is None: time.sleep(.05)
            self.assertIsNone(child.poll()); info=self.s.process_info(); self.assertTrue(info['running']); self.assertEqual(info['pid'],child.pid)
            self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING'); self.assertEqual(self.gateway.starts,0)
        finally:
            stopfile.touch(); stdout,stderr=child.communicate(timeout=10)
        self.assertEqual(child.returncode,0,stderr.decode('utf-8',errors='replace'))
        self.assertFalse(self.s.running()); self.assertIsNone(process_identity(child.pid)); self.assertEqual(self.policy.refresh()['mode'],'ARMED')
        record=read_json(self.root/'runtime/operator_runner.json'); record['status']='RUNNING'; atomic_json(self.root/'runtime/operator_runner.json',record)
        self.assertTrue(self.s.process_info()['stale']); self.assertFalse(self.s.running())
    def test_restore_armed_does_not_discard_live_or_pending_position(self):
        self.live(); e,_=self.engine(); order=e.enter(self.signal(e),self.tick()); e.save(); self.s.stop('STOP')
        self.assertFalse(operator_runner.restore_armed(self.root,'ARMED')); self.assertEqual(self.policy.refresh()['mode'],'STOP')
        self.assertTrue(read_json(e.folder/'state.json')['orders'])
    def test_wrapper_duplicate_call_is_refused_before_supervisor(self):
        supervisor=Mock()
        with ProcessLock(self.root/'runtime/operator_runner.lock'):
            with self.assertRaises((RuntimeError,OSError)): operator_runner.run(self.root,lambda:self.clock[0],supervisor)
        supervisor.assert_not_called()
    def test_previous_market_heartbeat_cannot_make_new_live_process_on(self):
        self.live(); self.record(); folder=self.root/'runtime/automation-production'
        atomic_json(folder/'heartbeat.json',{'time':(self.clock[0]-timedelta(seconds=1)).isoformat(),'connected':True,'account_ok':True})
        st=self.s.public_status(); self.assertFalse(st['trading_on']); self.assertIn('미연결',st['websocket'])

if __name__=='__main__': unittest.main()
