import tempfile,unittest
from pathlib import Path
from datetime import datetime
from dataclasses import asdict,replace
from unittest.mock import patch
from core.settings import Config,KST
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from operator_ui_service import UserOperations
class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.time=datetime(2026,10,2,10,0,tzinfo=KST);self.calls=[];self.orders=[]
  atomic_json(self.root/'config.json',asdict(Config()));atomic_json(self.root/'automation.json',asdict(replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=10000,costs_confirmed=True)))
  self.p=Policy(self.root/'runtime/automation_policy.json');self.s=UserOperations(self.root,clock=lambda:self.time,gateway=self)
 def snapshot(self):self.calls.append('account');return {'cash':10420,'holdings':{},'orders':self.orders}
 def command(self,args,timeout=0):
  self.calls.append(args);stamp=self.time.isoformat();folder=self.root/'runtime/automation-smoke'
  atomic_json(self.root/'automation_check.json',{'started':stamp,'finished':stamp,'counts':{'account_checks':1,'subscription_acks':1,'radar_cycles':1,'ticks':1}})
  atomic_json(folder/'heartbeat.json',{'market':'3','connected':True,'account_ok':True});atomic_json(folder/'state.json',{'orders':{},'positions':{}})
  (folder/'events.jsonl').write_text('{"event":"ENTRY_EVAL","timestamp":"'+stamp+'"}\n',encoding='utf-8');atomic_json(self.root/'watchlist.json',{'asof':self.time.date().isoformat(),'items':[{'code':'111111'}]});return 0
 def test_recovery_pass_preserves_settings_no_live(self):
  before=self.s.settings();result=self.s.recover_pilot(True);self.assertEqual(result['verdict'],'PASS');self.assertEqual(before,self.s.settings());self.assertEqual(result['orders'],0);self.assertEqual(result['cap'],10000);self.assertTrue(self.s._approval_basis(*before));self.assertIn('--recover-pilot',self.calls[-1])
 def test_initial_still_rejects(self):self.assertEqual(self.s.pilot()['phase'],'PREFLIGHT_REJECTED');self.assertEqual(self.calls,[])
 def test_confirmation_required(self):
  with self.assertRaises(ValueError):self.s.recover_pilot()
  self.assertEqual(self.calls,[])
 def test_live_rejected(self):
  self.p.command('LIVE',confirmation='LIVE');self.assertEqual(self.s.recover_pilot(True)['verdict'],'FAIL');self.assertEqual(self.calls,[])
 def test_outside_rejected(self):
  self.time=self.time.replace(hour=20);self.assertEqual(self.s.recover_pilot(True)['verdict'],'FAIL');self.assertEqual(self.calls,[])
 def test_running_rejected(self):
  with patch.object(self.s,'running',return_value=True):self.assertEqual(self.s.recover_pilot(True)['verdict'],'FAIL')
  self.assertEqual(self.calls,[])
 def test_account_open_rejected(self):
  self.orders=[{}];self.assertEqual(self.s.recover_pilot(True)['phase'],'PREFLIGHT_REJECTED');self.assertEqual(self.calls,['account'])
 def test_changed_settings_not_qualified(self):
  self.s.recover_pilot(True);atomic_json(self.root/'automation.json',asdict(replace(self.s.settings()[1],AUTO_TRADING_CAP_KRW=20000)));self.assertFalse(self.s._approval_basis(*self.s.settings()))
 def test_runner_live_rejected_before_network(self):
  from automation.runner import run
  with self.assertRaises(ValueError):run(replace(Config(),live_mode=True),self.root,150,recover_pilot=True)
