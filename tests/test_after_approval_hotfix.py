import ast,copy,json,os,sys,tempfile,unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
os.environ['QT_QPA_PLATFORM']='offscreen'
REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from core.settings import Config,KST
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation.decisions import BUILD_ID
from automation.readiness import digest,policy_hash,security_state
from operator_service import OperatorService,read_json
from operator_ui_service import UserOperations
from PySide6.QtWidgets import QApplication
import operator_gui as gui
from tests.test_gui import FakeService,synthetic_status

class AfterApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.stamp=datetime(2026,10,7,17,10,tzinfo=KST)
        self.cfg=replace(Config(),live_mode=True,after_enabled=True,risk_fraction=.02,
            max_daily_loss_fraction=.04,max_positions=1,max_entries_per_day=3,max_watchlist=12)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        atomic_json(self.root/'config.json',asdict(self.cfg));atomic_json(self.root/'automation.json',asdict(self.auto))
        self.policy=Policy(self.root/'runtime/automation_policy.json')
        self.policy.command('LIVE',confirmation='LIVE');self.policy.command('STOP')
        atomic_json(self.root/'runtime/operator_user_authorization.json',{
            'authorization_kind':'DAILY_PREOPEN_V13','trading_day':self.stamp.date().isoformat(),
            'build_id':BUILD_ID,'config_hash':self.cfg.fingerprint(),'auto_hash':self.auto.fingerprint(),
            'policy_hash':policy_hash(self.policy.state),'security_state_hash':digest([])})
        self.folder=self.root/'runtime'/('automation-'+self.cfg.broker_environment)
        self.state={'halt':'','positions':{},'orders':{},'automation':{'capital_snapshot':{
            'timestamp':self.stamp.isoformat(),'actual_orderable_cash':20420}}}
        self.hb={'time':self.stamp.isoformat(),'pid':555,'identity':'synthetic','connected':True,'account_ok':True,
            'build_id':BUILD_ID,'mode':'STOP','market':'3','readiness':{'state':'OPEN_READY','regular_tick_age':1,
                'gates':{'account_fresh':True,'fresh_regular_tick':True}}}
        self.s=OperatorService(self.root,clock=lambda:self.stamp,gateway=Mock())
        self.s.process_info=Mock(return_value={'running':True,'managed':True,'started':(self.stamp-timedelta(seconds=30)).isoformat()})
        self.save_state()
        self.before={p:p.read_bytes() for p in [self.root/'config.json',self.root/'automation.json',self.root/'runtime/operator_user_authorization.json']}
    def save_state(self):
        atomic_json(self.folder/'state.json',self.state);atomic_json(self.folder/'heartbeat.json',self.hb)
    def assert_unapproved(self):
        self.assertFalse(read_json(self.policy.path).get('after_live_authorized',False))
        self.s.gateway.snapshot.assert_not_called();self.s.gateway.start.assert_not_called()
    def test_after_approval_persists_using_existing_atomic_path(self):
        self.assertFalse(hasattr(self.policy,'save'))
        result=self.s.activate_after_live('20000',True)
        fresh=Policy(self.policy.path)
        self.assertTrue(fresh.state['after_live_authorized']);self.assertEqual(fresh.mode,'LIVE')
        self.assertEqual(result['verdict'],'SAVED')
        for p,b in self.before.items():self.assertEqual(p.read_bytes(),b)
        self.s.gateway.snapshot.assert_not_called();self.s.gateway.start.assert_not_called()
    def test_current_approved_cap_and_unlimited_count_accept_after_with_dated_budget(self):
        self.cfg=replace(self.cfg,max_entries_per_day=None,max_entries_per_symbol=None)
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=81300,max_consecutive_losses=None,
            daily_risk_override_day=self.stamp.date().isoformat(),daily_risk_override_budget_krw=3880.266)
        atomic_json(self.root/'config.json',asdict(self.cfg));atomic_json(self.root/'automation.json',asdict(self.auto))
        receipt=read_json(self.root/'runtime/operator_user_authorization.json')
        receipt.update(config_hash=self.cfg.fingerprint(),auto_hash=self.auto.fingerprint())
        atomic_json(self.root/'runtime/operator_user_authorization.json',receipt)
        before=(self.root/'automation.json').read_bytes()
        self.assertEqual(self.s.activate_after_live('81300',True)['verdict'],'SAVED')
        self.assertEqual((self.root/'automation.json').read_bytes(),before)
        self.assertTrue(Policy(self.policy.path).state['after_live_authorized'])
        self.s.gateway.snapshot.assert_not_called();self.s.gateway.start.assert_not_called()
    def test_after_amount_mismatch_cannot_change_approved_cap(self):
        with self.assertRaises(ValueError):self.s.activate_after_live('81300',True)
        self.assert_unapproved();self.assertEqual(read_json(self.root/'automation.json')['AUTO_TRADING_CAP_KRW'],20000)
    def test_17h_permission_can_be_on_after_click_and_refresh(self):
        self.s.activate_after_live('20000',True)
        with patch('operator_service.process_identity',return_value='synthetic'):
            status=self.s.public_status()
        self.assertTrue(status['trading_on']);self.assertTrue(status['after_live_approved'])
        self.assertEqual(status['mode'],'LIVE')
    def test_confirmation_requires_explicit_boolean_not_arbitrary_value(self):
        for value in [False,None,'yes','AFTER_실제매매시작',1]:
            with self.subTest(value=value),self.assertRaises(ValueError):self.s.activate_after_live('20000',value)
        self.assert_unapproved()
    def test_cutoffs_block_new_approval_and_existing_permission(self):
        self.s.activate_after_live('20000',True)
        for hour,minute in [(19,45),(20,0)]:
            self.stamp=self.stamp.replace(hour=hour,minute=minute)
            with self.subTest(time=self.stamp),self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
            with patch('operator_service.process_identity',return_value='synthetic'):
                self.assertFalse(self.s.public_status()['trading_on'])
    def test_halt_unknown_foreign_intent_block_without_persistence(self):
        for kind in ['HALT','UNKNOWN','INTENT']:
            self.state['halt']='test' if kind=='HALT' else ''
            self.state['orders']={} if kind=='HALT' else {'foreign':{'status':kind,'symbol':'999999','qty':1,'side':'BUY','limit':100,'filled':0}}
            self.save_state()
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
            self.assert_unapproved()
    def test_freshness_failure_table_blocks_approval(self):
        baseline=copy.deepcopy(self.hb)
        cases=[{'connected':False},{'account_ok':False},{'time':(self.stamp-timedelta(seconds=11)).isoformat()},
            {'readiness':{'state':'WAITING'}},{'readiness':{'state':'OPEN_READY','regular_tick_age':6,'gates':{'account_fresh':True,'fresh_regular_tick':True}}}]
        for update in cases:
            self.hb={**baseline,**update};self.save_state()
            with self.subTest(update=update),self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
            self.assert_unapproved()
    def test_stale_account_snapshot_blocks_approval(self):
        self.state['automation']['capital_snapshot']['timestamp']=(self.stamp-timedelta(seconds=self.cfg.reconcile_seconds*3+1)).isoformat()
        self.save_state()
        with self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
        self.assert_unapproved()
    def test_stopped_runner_and_kill_block_approval(self):
        self.s.process_info.return_value.update(running=False,managed=False)
        with self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
        self.s.process_info.return_value.update(running=True,managed=True)
        self.policy.command('KILL')
        with self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
        self.assert_unapproved()
    def test_policy_write_error_is_not_swallowed(self):
        real=atomic_json
        def denied(path,value):
            if Path(path)==self.policy.path:raise PermissionError('synthetic policy denied')
            return real(path,value)
        with patch('automation.policy.atomic_json',side_effect=denied),self.assertRaises(PermissionError):
            self.s.activate_after_live('20000',True)
        self.assert_unapproved()
    def test_policy_refresh_exposes_hot_approval_without_restart(self):
        running_policy=Policy(self.policy.path)
        self.assertFalse(running_policy.state.get('after_live_authorized',False))
        self.s.activate_after_live('20000',True);running_policy.refresh()
        self.assertTrue(running_policy.state['after_live_authorized']);self.assertEqual(running_policy.mode,'LIVE')
    def test_existing_engine_reads_hot_approval_without_override(self):
        from automation.engine import AutonomousEngine
        from tests.test_automation import FakeBroker
        import contextlib,io
        with contextlib.redirect_stdout(io.StringIO()):
            engine=AutonomousEngine(self.cfg,self.auto,Policy(self.policy.path),self.root/'isolated-engine',FakeBroker(),lambda:self.stamp)
        self.assertFalse(engine.after_live_authorized)
        self.s.activate_after_live('20000',True)
        engine.policy.refresh()
        self.assertTrue(engine.after_live_authorized);self.assertEqual(engine.policy.mode,'LIVE')
    def test_cap_cost_risk_and_hard_lock_guards_preserved(self):
        mutations=[('automation.json','AUTO_TRADING_CAP_KRW',0),('automation.json','costs_confirmed',False),
            ('config.json','risk_fraction',.01),('config.json','live_mode',False)]
        for name,key,value in mutations:
            path=self.root/name;baseline=path.read_bytes();raw=read_json(path);raw[key]=value;atomic_json(path,raw)
            with self.subTest(key=key),self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
            path.write_bytes(baseline);self.assert_unapproved()
        self.s.hard_locks_ok=Mock(return_value=False)
        with self.assertRaises(ValueError):self.s.activate_after_live('20000',True)
        self.assert_unapproved()

class ApprovalGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.s=FakeService();self.s.activate_after_live=Mock(return_value={'verdict':'SAVED'})
        self.w=gui.MainWindow(self.s,auto_refresh=False);self.w.status=synthetic_status(cap=20000)
        self.w.submit=Mock(side_effect=lambda f,*a,**k:f());self.w.confirm=Mock()
    def tearDown(self):
        self.w.check_timer.stop();self.w.timer.stop();self.w.allow_close=True;self.w.close();self.app.processEvents()
    def test_after_button_has_no_typed_or_secondary_confirmation(self):
        with patch('operator_gui.QInputDialog.getText',side_effect=AssertionError('unexpected typed input')):
            self.w.after_live_action()
        self.s.activate_after_live.assert_called_once_with('20000',True);self.w.confirm.assert_not_called()
    def test_general_live_approval_has_no_phrase_dialog(self):
        with patch('operator_gui.QInputDialog.getText',return_value=('20000',True)) as inputs:
            d=gui.SettingsDialog(self.w);d.approve();d.accept()
        self.assertEqual(inputs.call_count,1);self.assertIn(('authorize',('20000',True)),self.s.calls)
    def test_guided_live_approval_uses_clicked_existing_cap(self):
        self.s.start_user=Mock(return_value={'verdict':'STARTED'})
        with patch('operator_gui.QInputDialog.getText',side_effect=AssertionError('no start input dialog')) as inputs:
            self.w._prepared_start({'verdict':'NEEDS_LIVE_APPROVAL','cap':20000},clicked_cap=20000)
        inputs.assert_not_called();self.w.confirm.assert_not_called()
        self.assertIn(('authorize',('20000',True)),self.s.calls)
        self.s.start_user.assert_called_once_with(confirmed=True)
    def test_no_start_phrase_prompts_and_kill_reset_unchanged(self):
        tree=ast.parse((REPO/'operator_gui.py').read_text(encoding='utf-8'))
        for n in ast.walk(tree):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='getText':
                text=' '.join(str(a.value) for a in n.args if isinstance(a,ast.Constant))
                self.assertNotIn('정확히 입력',text)
        self.assertIn("reset_kill('RESET KILL')",(REPO/'operator_gui.py').read_text(encoding='utf-8'))
    def test_general_service_accepts_click_boolean_after_existing_preflight(self):
        from tests.test_gui import UserServiceTests
        fixture=UserServiceTests('test_authorization_and_explicit_start_confirmation')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.ready()
        result=fixture.s.authorize_capital('5000',True)
        self.assertEqual(result['verdict'],'SAVED')
        cfg,auto,policy=fixture.s.settings()
        self.assertTrue(cfg.live_mode);self.assertTrue(policy['live_authorized'])
        self.assertEqual(auto.capital_limit,5000);self.assertEqual(policy['mode'],'STOP')

if __name__=='__main__':unittest.main(verbosity=2)
