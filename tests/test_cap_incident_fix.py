"""10/02 한도 변경 사건 회귀: 합성 계좌/Feed만 사용, 실제 주문 없음."""
import ast,contextlib,io,json,queue,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch,Mock
from core.settings import ROOT,Config,KST
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from operator_ui_service import UserOperations,start_block,presentation
from tests.test_gui import ServiceGateway,synthetic_status,FakeService
import operator_gui as gui
from PySide6.QtWidgets import QApplication
import main

class CapIncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.clock=[datetime(2026,10,2,9,34,tzinfo=KST)]
        atomic_json(self.root/'config.json',asdict(Config())); atomic_json(self.root/'automation.json',asdict(AutomationConfig()))
        self.p=Policy(self.root/'runtime/automation_policy.json'); self.g=ServiceGateway(self)
        self.s=UserOperations(self.root,clock=lambda:self.clock[0],gateway=self.g)
        self.s.save_result('pilot',{'verdict':'PASS'}); self.s.confirm_costs('0.015','0.015','0.2',True)
    def approved_then_changed(self):
        self.s.change_cap('3000',True); self.s.prepare_capital(); self.s.authorize_capital('3000','실제매매승인')
        self.s.change_cap('10000',True)
    def test_cap_change_full_reapproval_start_path(self):
        self.approved_then_changed(); cfg,a,p=self.s.settings()
        self.assertFalse(cfg.live_mode); self.assertFalse(p['live_authorized']); self.assertEqual(a.capital_limit,10000)
        self.s.prepare_capital(); self.s.authorize_capital('10000','실제매매승인')
        self.assertEqual(start_block(self.s.snapshot()),''); self.assertEqual(self.s.start_user(True)['verdict'],'STARTED'); self.assertEqual(self.g.orders,[])
    def test_harmless_stop_armed_retains_qualification_not_live(self):
        self.approved_then_changed(); self.p.command('STOP'); self.p.command('ARMED')
        self.assertEqual(self.s.prepare_capital()['verdict'],'PASS'); self.assertFalse(self.s.settings()[0].live_mode)
        self.s.authorize_capital('10000','실제매매승인'); self.assertEqual(self.s.settings()[2]['mode'],'STOP')
    def test_kill_revokes_qualification_chain_even_after_stop(self):
        self.approved_then_changed(); self.p.command('KILL'); self.p.command('STOP')
        self.assertFalse(self.s._approval_basis(*self.s.settings()))
        with self.assertRaises(ValueError): self.s.prepare_capital()
    def test_incomplete_revision_or_changed_safety_config_rejects(self):
        self.approved_then_changed(); self.p.command('STOP'); self.p.command('ARMED')
        p=self.p.refresh(); p['commands'].pop(-2); atomic_json(self.p.path,p)
        self.assertFalse(self.s._approval_basis(*self.s.settings()))
        self.setUp()
        self.approved_then_changed(); cfg=json.loads((self.root/'config.json').read_text(encoding='utf-8')); cfg['risk_fraction']=.004; atomic_json(self.root/'config.json',cfg)
        self.assertFalse(self.s._approval_basis(*self.s.settings()))
    def test_rejected_initial_pilot_does_not_erase_prior_pass(self):
        self.approved_then_changed(); before=self.s.pilot_proof()
        result=self.s.pilot(); self.assertEqual(result['verdict'],'FAIL'); self.assertEqual(result['phase'],'PREFLIGHT_REJECTED')
        self.assertEqual(self.s.pilot_proof(),before); self.assertEqual(self.s.prepare_capital()['verdict'],'PASS')
    def test_failed_or_missing_pilot_proof_returns_empty(self):
        self.s.save_result('pilot',{'verdict':'FAIL','reasons':['synthetic connection failure']})
        self.assertEqual(self.s.pilot_proof(),{})
        self.assertEqual(self.s.pilot()['phase'],'PREFLIGHT_REJECTED')
        (self.root/'runtime/operator_pilot.json').unlink()
        self.assertEqual(self.s.pilot_proof(),{})
    def test_readonly_snapshot_cannot_clear_kill(self):
        self.p.command('KILL'); before=self.p.path.read_bytes(); self.s.snapshot()
        self.assertEqual(self.p.path.read_bytes(),before); self.assertTrue(start_block(self.s.snapshot()))
    def test_error_traceback_and_history_preserved(self):
        try: raise PermissionError(13,'synthetic denied',str(self.root/'state.json'))
        except PermissionError as error: self.s.save_error(error)
        r=json.loads((self.root/'runtime/operator_error.json').read_text(encoding='utf-8'))
        self.assertEqual(r['type'],'PermissionError'); self.assertIn('state.json',r['error']); self.assertIn('traceback',r)
        history=(self.root/'runtime/operator_error_history.jsonl').read_text(encoding='utf-8'); self.s.save_error(ValueError('second reason'))
        self.assertTrue((self.root/'runtime/operator_error_history.jsonl').read_text(encoding='utf-8').startswith(history))
        self.assertIn('second reason',self.s.snapshot()['current_error'])
    def test_fresh_market_heartbeat_drives_display_only(self):
        self.approved_then_changed(); folder=self.root/'runtime/automation-production'; atomic_json(folder/'state.json',{'positions':{},'orders':{},'trades':[],'halt':''})
        hb={'time':self.clock[0].isoformat(),'market':'3','session':'SUPPORTED_SESSION','connected':True,'account_ok':True,'counts':{'subscription_acks':1}}
        atomic_json(folder/'heartbeat.json',hb)
        with patch.object(self.s,'process_info',return_value={'running':True,'managed':False}):
            s=self.s.snapshot(); self.assertTrue(s['market_confirmed']); self.assertIn('정규장 확인',presentation(s)[1]); self.assertFalse(s['trading_on']); self.assertEqual(start_block(s),'이미 실행 중입니다.')
            self.clock[0]+=timedelta(seconds=11); self.assertFalse(self.s.snapshot()['market_confirmed'])
    def test_armed_running_gui_explains_reapproval(self):
        app=QApplication.instance() or QApplication([]); gui.configure_font(app); w=gui.MainWindow(FakeService(),auto_refresh=False)
        try:
            w.apply_status(synthetic_status(mode='ARMED',live_mode=False,live_approved=False,cap=10000,process={'running':True}))
            self.assertFalse(w.buttons['start'].isEnabled()); self.assertIn('정상 중지',w.notice.text()); self.assertIn('명시 승인',w.notice.text())
        finally: w.allow_close=True; w.close()

class ConnectionProbeTests(unittest.TestCase):
    def run_probe(self,items=None,ack=True):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            if items is not None: atomic_json(root/'watchlist.json',{'items':items})
            stamp=datetime(2026,10,2,9,34,tzinfo=KST); feed=Mock(); feed.q=queue.Queue()
            feed.q.put((stamp,{'trnm':'REG','return_code':0 if ack else 1})); feed.q.put((stamp,{'trnm':'REAL','data':[{'type':'0B'}]}))
            with patch.object(main,'ROOT',root),patch.object(main,'now',return_value=stamp),patch.object(main.time,'monotonic',side_effect=[0,0,0,31]),patch.object(main,'Rest') as rest,patch.object(main,'Engine') as engine,patch('core.realtime.Feed',return_value=feed) as factory,patch('core.realtime.parse_tick',return_value=SimpleNamespace(regular=True,time=stamp)),contextlib.redirect_stdout(io.StringIO()):
                if ack: main.check_connection(Config())
                else:
                    with self.assertRaises(RuntimeError): main.check_connection(Config())
                result=json.loads((root/'connection_check.json').read_text(encoding='utf-8')); engine.assert_not_called(); rest.return_value.buy.assert_not_called(); rest.return_value.sell.assert_not_called(); feed.close.assert_called_once()
                return result,factory.call_args.args[1]
    def test_connection_probe_watchlist_scenarios_table(self):
        # 1. empty candidates uses 005930 readonly probe
        res1,syms1=self.run_probe([])
        self.assertTrue(res1['passed']); self.assertTrue(res1['diagnostic_only']); self.assertEqual(syms1,['005930']); self.assertEqual(res1['orders_sent'],0)
        # 2. missing watchlist
        self.assertTrue(self.run_probe()[0]['passed'])
        # 3. existing candidates preserved
        res3,syms3=self.run_probe([{'code':'111111'}])
        self.assertEqual(syms3,['111111']); self.assertFalse(res3['diagnostic_only'])
        # 4. missing reg ack fails
        self.assertFalse(self.run_probe([],False)[0]['passed'])
