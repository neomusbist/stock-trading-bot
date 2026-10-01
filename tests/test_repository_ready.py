import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch,Mock
from core.settings import ROOT,Config,KST,load_config
from core.util import atomic_json
from automation.policy import Policy,AutomationConfig,load_automation,HARD_LOCKS
from automation.engine import AutonomousEngine
from core.strategy import Tick
from core.broker import Rest,Rejected
from operator_service import OperatorService
from operator_console import main as menu
from local_diagnostics import diagnose,account_diagnostic

def tool(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
guard=tool('repo_guard')
AFTER=datetime(2026,10,1,16,0,tzinfo=KST)

class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        for name in ('config.example.json','automation.example.json'):
            (self.root/name).write_bytes((ROOT/name).read_bytes())
    def test_example_live_false(self): self.assertFalse(load_config(self.root/'config.json').live_mode)
    def test_example_cap_zero(self): self.assertEqual(load_automation(self.root/'automation.json').capital_limit,0)
    def test_example_costs_unconfirmed(self): self.assertFalse(load_automation(self.root/'automation.json').costs_confirmed)
    def test_missing_policy_status_armed_no_live(self):
        s=OperatorService(self.root,clock=lambda:AFTER).public_status()
        self.assertEqual(s['mode'],'ARMED'); self.assertFalse(s['live_approved']); self.assertFalse(s['trading_on']); self.assertFalse((self.root/'runtime/automation_policy.json').exists())
    def test_menu_at_1600(self):
        out=[]; s=OperatorService(self.root,clock=lambda:AFTER)
        self.assertEqual(menu(writer=out.append,service=s,argv=['--self-check']),0)
        self.assertTrue(any('운영시간 밖' in line for line in out)); self.assertFalse((self.root/'runtime/operator_error.json').exists())
    def test_local_1600_normal(self):
        r=diagnose(self.root,AFTER); self.assertEqual(r['status'],'NORMAL'); self.assertEqual(r['reason_code'],'OUTSIDE_TRADING_WINDOW')
        self.assertFalse(r['regression_failure']); self.assertEqual(r['network_calls'],0); self.assertEqual(r['real_orders_sent'],0)
    def test_local_heartbeat_updates_independent(self):
        r=diagnose(self.root,AFTER); later=diagnose(self.root,AFTER+timedelta(seconds=1))
        self.assertNotEqual(r['heartbeat'],later['heartbeat']); self.assertFalse((self.root/'runtime/operator_runner.json').exists())
    def test_diagnostics_does_not_change_policy_or_halt(self):
        policy=self.root/'runtime/automation_policy.json'; state=self.root/'runtime/automation-production/state.json'
        atomic_json(policy,{'mode':'STOP','live_authorized':True}); atomic_json(state,{'halt':'KEEP','positions':{},'orders':{}})
        before=(policy.read_bytes(),state.read_bytes()); self.assertTrue(diagnose(self.root,AFTER)['halt_present'])
        self.assertEqual(before,(policy.read_bytes(),state.read_bytes()))
    def test_afterhours_then_nextday_no_time_halt(self):
        diagnose(self.root,AFTER); r=diagnose(self.root,datetime(2026,10,2,9,10,tzinfo=KST))
        self.assertEqual(r['reason_code'],'LOCAL_DIAGNOSTICS'); self.assertFalse(r['halt_present']); self.assertFalse((self.root/'runtime/automation_policy.json').exists())
    def test_main_afterhours_no_credentials_or_rest(self):
        import main
        with patch.object(main,'ROOT',self.root),patch.object(main,'now',return_value=AFTER),patch.object(sys,'argv',['main.py']),patch.object(main,'load_credentials') as creds,patch.object(main,'Rest') as rest,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(),0); creds.assert_not_called(); rest.assert_not_called()
    def test_main_diagnostics_in_session_no_network(self):
        import main
        with patch.object(main,'ROOT',self.root),patch.object(main,'now',return_value=AFTER.replace(hour=10)),patch.object(sys,'argv',['main.py','--diagnostics']),patch.object(main,'load_credentials') as creds,patch.object(main,'Rest') as rest,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(),0); creds.assert_not_called(); rest.assert_not_called()
    def test_clone_does_not_load_legacy_env(self):
        import main
        with patch.object(main,'ROOT',self.root),patch.object(Path,'home',side_effect=AssertionError('legacy fallback forbidden')):
            main.load_credentials()
    def test_setup_clone_does_not_load_legacy_env(self):
        import main
        tool('setup_local').setup(self.root)
        with patch.object(main,'ROOT',self.root),patch.object(Path,'home',side_effect=AssertionError('explicit credentials required')):
            main.load_credentials()
    def test_local_config_takes_precedence(self):
        cfg=json.loads((self.root/'config.example.json').read_text()); cfg['max_positions']=2
        atomic_json(self.root/'config.json',cfg); self.assertEqual(load_config(self.root/'config.json').max_positions,2)
    def test_setup_never_overwrites_existing(self):
        setup=tool('setup_local'); self.assertEqual(len(setup.setup(self.root)),2)
        path=self.root/'config.json'; before=path.read_bytes(); self.assertEqual(setup.setup(self.root),[]); self.assertEqual(path.read_bytes(),before)
    def test_account_readonly_error_preserves_code_text(self):
        factory=Mock(side_effect=Rejected('API拒否',api='kt00001',response={'return_code':777,'return_msg':'정확한 응답문구'}))
        r=account_diagnostic(Config(),self.root,AFTER,factory)
        self.assertEqual(r['response']['return_code'],777); self.assertEqual(r['response']['return_msg'],'정확한 응답문구')
        self.assertEqual(r['verdict'],'INCONCLUSIVE'); self.assertFalse((self.root/'runtime/automation_policy.json').exists()); self.assertFalse(factory.call_args.args[0].live_mode)
    def test_readonly_api_cannot_send_order(self):
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'fake','KIWOOM_APP_SECRET':'fake'}),patch.object(Rest,'snapshot',lambda s:s._http('/api/dostk/ordr',{}, {'api-id':'kt10000'})),patch.object(Rest,'_http') as send:
            r=account_diagnostic(replace(Config(),live_mode=True),self.root,AFTER)
            self.assertEqual(r['message'],'READ_ONLY_DIAGNOSTIC_API_DENIED'); send.assert_not_called()
    def test_clone_cap0_order0(self):
        from test_automation import FakeBroker
        clock=AFTER.replace(hour=10); broker=FakeBroker(); policy=Policy(self.root/'runtime/automation_policy.json')
        with contextlib.redirect_stdout(io.StringIO()):
            e=AutonomousEngine(load_config(self.root/'config.json'),load_automation(self.root/'automation.json'),policy,self.root/'book',broker,lambda:clock)
            e.reconcile({'cash':1000000,'equity':1000000,'orders':[],'holdings':{}}); e.connected=True; e.market='3'
            sig={'symbol':'999990','limit':2000,'stop':1990,'pattern':'B'}
            e.enter(sig,Tick('999990',clock,2000,100,100,2001,1999))
        self.assertEqual(broker.orders,[]); self.assertFalse(e.s['positions']); self.assertEqual(e.auto_available_capital(),0)
    def test_1600_buy_block_no_halt_then_nextday(self):
        from test_automation import FakeBroker
        clock=[AFTER]; broker=FakeBroker(); policy=Policy(self.root/'runtime/policy.json')
        with contextlib.redirect_stdout(io.StringIO()):
            e=AutonomousEngine(Config(),AutomationConfig(),policy,self.root/'book',broker,lambda:clock[0])
            e.enter({'symbol':'999990','limit':2000,'stop':1990},Tick('999990',clock[0],2000,100,100,2001,1999))
        rows=[json.loads(x) for x in (self.root/'book/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(r.get('primary_reason_code')=='NO_BUY_OUTSIDE_SESSION' for r in rows)); self.assertFalse(e.s['halt']); self.assertEqual(broker.orders,[])
        clock[0]=datetime(2026,10,2,9,10,tzinfo=KST); self.assertNotEqual(e.new_entry_reason('999990'),'OUT_OF_SUPPORTED_SESSION'); self.assertFalse(e.s['halt'])
    def test_hard_locks_preserved(self): self.assertEqual(HARD_LOCKS,{'005930','017900'})
    def test_guard_local_config_rejected(self): self.assertIn('LOCAL_OR_UNREVIEWED_FILE',guard.inspect('config.json',b'{}'))
    def test_guard_runtime_rejected(self): self.assertTrue(guard.inspect('runtime/events.jsonl',b'{}'))
    def test_guard_placeholder_allowed(self): self.assertFalse(guard.inspect('.env.example',b'KIWOOM_APP_KEY=YOUR_APP_KEY\nKIWOOM_APP_SECRET=YOUR_APP_SECRET'))
    def test_guard_secret_literal_rejected(self):
        raw=('KIWOOM_APP_KEY='+('z'*32)).encode(); self.assertIn('CREDENTIAL_LITERAL',guard.inspect('main.py',raw))
    def test_guard_bearer_rejected(self): self.assertIn('BEARER_LITERAL',guard.inspect('README.md',('Bearer '+('a'*32)).encode()))
    def test_guard_github_token_rejected(self): self.assertIn('GITHUB_TOKEN',guard.inspect('README.md',('gh'+'p_'+('x'*32)).encode()))
    def test_guard_personal_path_rejected(self): self.assertIn('PERSONAL_WINDOWS_PATH',guard.inspect('README.md',b'C:/'+b'Users/person/Desktop/file'))
    def test_guard_unsafe_example_rejected(self): self.assertIn('UNSAFE_EXAMPLE_LIVE',guard.inspect('config.example.json',b'{"live_mode":true}'))
    def test_guard_binary_rejected(self): self.assertIn('BINARY_FILE',guard.inspect('README.md',b'\x00secret'))
    def test_guard_original_spec_rejected(self): self.assertTrue(guard.inspect('reference/contract.json',b'{"api":{"meta":{}}}'))
    def test_staged_scan_checks_index_not_worktree(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        path=self.root/'README.md'; path.write_text('Bearer '+('x'*32),encoding='utf-8')
        subprocess.run(['git','-C',str(self.root),'add','README.md'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        path.write_text('safe',encoding='utf-8')
        self.assertFalse(guard.scan(self.root,staged=True)['passed'])
    def test_force_staged_local_config_rejected(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        (self.root/'config.json').write_text('{}'); subprocess.run(['git','-C',str(self.root),'add','config.json'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertFalse(guard.scan(self.root,staged=True)['passed'])
    def test_history_scan_finds_deleted_secret(self):
        def run(*args): subprocess.run(['git','-C',str(self.root),'-c','user.name=Offline Test','-c','user.email=offline@example.invalid',*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        run('init','-q'); path=self.root/'README.md'; path.write_text('Bearer '+('x'*32)); run('add','README.md'); run('commit','-qm','synthetic secret')
        path.write_text('safe'); run('add','README.md'); run('commit','-qm','remove synthetic secret')
        self.assertFalse(guard.scan(self.root,history=True)['passed'])
