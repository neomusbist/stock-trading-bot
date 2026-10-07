"""오프라인 Qt 화면/서비스 검사. 실제 API·주문 대신 격리 파일과 가짜 Gateway만 사용."""
import ast,copy,json,os,sys,tempfile,time,unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from core.settings import ROOT,Config,KST
from core.util import atomic_json,ProcessLock
from automation.policy import AutomationConfig,Policy
from operator_service import STRATEGY_VERSION,read_json
from operator_ui_service import UserOperations,PUBLIC_MENUS,presentation,start_block,human_decision,result_text
import gui_launch
import operator_gui as gui
from PySide6.QtCore import QThread,QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication,QPushButton
from PySide6.QtTest import QTest

def synthetic_status(**changes):
    s={'time':'2026-10-01T09:30:00+09:00','supported':True,'mode':'STOP','live_mode':True,
       'live_approved':True,'live_configured':False,'trading_on':False,'halt':'','halt_info':{},
       'hard_ok':True,'uncertain':0,'positions':0,'position_count':0,'pending':0,'cap':5000,'cash':7500,
       'available':5000,'cash_time':'2026-10-01T09:29:59+09:00','account':'최근 조회 기록만 있음',
       'account_fresh':False,'cash_fresh':True,'websocket':'미연결/오래된 기록 — 현재 정상 미확인',
       'process':{'running':False,'state':'중지','started':'2026-10-01T09:25:00+09:00'},
       'entry_cutoff':'15:05:00','costs_confirmed':True,'strategy_version':STRATEGY_VERSION,
       'candidate_summary':{'timestamp':'2026-10-01T09:29:50+09:00','cycle_started':'2026-10-01T09:29:00+09:00',
           'raw_candidates':24,'affordable':8,'unaffordable':10,'affordability_unknown_or_observation':6,
           'liquidity_fail':2,'spread_fail':1,'structure_fail':3,'setup_not_ready':2,'risk_zero_qty':1,'final_entry_eligible':0},
       'decision_rows':[{'timestamp':'2026-10-01T09:29:50+09:00','symbol':'111111','name':'합성 후보 A',
          'decision':'NO_BUY','primary_reason_code':'ONE_SHARE_UNAFFORDABLE'}],
       'log_rows':[{'event':'ENTRY_EVAL','reason':'ONE_SHARE_UNAFFORDABLE'}],
       'orders':[{'timestamp':'2026-10-01T09:22:00+09:00','symbol':'111111','side':'BUY','qty':1,'filled':1,
          'limit':2400,'reason':'SYNTHETIC','execution':'PAPER','status':'FILLED','strategy_version':STRATEGY_VERSION}],
       'trades':[],'advanced':{'risk_fraction':.003,'max_positions':1},'active_error':False,
       'protection_rows':[{'symbol':'005930','policy':'HARD_LOCK / 자동매매 금지'},
          {'symbol':'017900','policy':'HARD_LOCK / 자동매매 금지'},
          {'symbol':'052260','policy':'BREAKEVEN_EXIT','breakeven':5834}],
       'last_decision':'합성 후보 A → 조건 미완성 → 매수 안 함','current_error':'현재 오류 미확인 / 과거 기록은 상세보기',
       'error':'과거 시험 오류 (현재 오류 아님)'}
    s.update(changes); return s

class FakeService:
    def __init__(self):
        self.status=synthetic_status(); self.calls=[]; self.snapshot_calls=0
    def snapshot(self): self.snapshot_calls+=1; return copy.deepcopy(self.status)
    def running(self): return self.status['process']['running']
    def start_user(self,confirmed=False): self.calls.append(('start',confirmed)); return {'verdict':'STARTED'}
    def stop(self,command): self.calls.append(command); return {'verdict':'REQUESTED','command':command}
    def save_error(self,error): self.calls.append(('error',str(error)))
    def change_cap(self,*args,**kw): self.calls.append(('cap',args,kw)); return {'verdict':'SAVED'}
    def prepare_capital(self): self.calls.append('prepare'); return {'verdict':'PASS'}
    def authorize_capital(self,*args): self.calls.append(('authorize',args)); return {'verdict':'SAVED'}
    def local_diagnostic(self): self.calls.append('local'); return {'network_calls':0}
    def pilot(self): self.calls.append('pilot'); return {'verdict':'PASS','orders':0}

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]); gui.configure_font(cls.app)
    def setUp(self):
        self.s=FakeService(); self.w=gui.MainWindow(self.s,auto_refresh=False); self.w.apply_status(self.s.status)
        self.w.confirm=Mock(return_value=True); self.w.details=Mock(); self.w.problem=Mock(); self.w.show_result=Mock()
    def tearDown(self):
        self.w.read_pool.waitForDone(2000); self.w.command_pool.waitForDone(2000); self.w.emergency_pool.waitForDone(2000)
        self.app.processEvents(); self.w.allow_close=True; self.w.close()
    def wait(self,predicate):
        deadline=time.monotonic()+2
        while not predicate() and time.monotonic()<deadline: self.app.processEvents(); QTest.qWait(5)
        self.assertTrue(predicate())
    def click(self,key): QTest.mouseClick(self.w.buttons[key],gui.Qt.MouseButton.LeftButton)
    def test_stop_initial_screen(self): self.assertEqual(self.w.cards['operation'].text(),'정지')
    def test_live_running_text(self):
        s=synthetic_status(mode='LIVE',trading_on=True,process={'running':True,'state':'실행 중'})
        self.w.apply_status(s); self.assertEqual(self.w.cards['operation'].text(),'자동매매 중')
    def test_armed_observation_not_live(self):
        self.w.apply_status(synthetic_status(mode='ARMED',live_mode=False,live_approved=False,cap=0,available=0,process={'running':True,'state':'실행 중'}))
        self.assertIn('관찰',self.w.cards['operation'].text()); self.assertIn('OFF',self.w.notice.text())
    def test_outside_hours_ui_state(self):
        self.w.apply_status(synthetic_status(supported=False,time='2026-10-01T18:00:00+09:00'))
        self.assertIn('운영시간 밖',self.w.cards['market'].text()); self.assertFalse(self.w.buttons['start'].isEnabled())
        self.assertTrue(self.w.buttons['diagnostic'].isEnabled()); self.assertTrue(self.w.buttons['logs'].isEnabled())
    def test_start_delegates_service(self):
        self.click('start'); self.wait(lambda:('start',True) in self.s.calls); self.w.confirm.assert_called_once()
    def test_start_confirmation_cancel_no_call(self):
        self.w.confirm.return_value=False; self.click('start'); self.assertEqual(self.s.calls,[])
    def test_emergency_buttons_dispatch_commands(self):
        # 1. STOP
        self.w.apply_status(synthetic_status(process={'running':True,'state':'실행 중'}))
        self.click('stop'); self.wait(lambda:'STOP' in self.s.calls)
        # 2. STOP_FLAT
        self.w.apply_status(synthetic_status(positions=1,position_count=1))
        self.click('flat'); self.wait(lambda:'STOP_FLAT' in self.s.calls)
        # 3. KILL (with confirm and cancel)
        self.w.confirm.return_value=False; self.click('kill'); self.assertNotIn('KILL',self.s.calls)
        self.w.confirm.return_value=True; self.click('kill'); self.wait(lambda:'KILL' in self.s.calls)
    def test_dynamic_cards_display_updates(self):
        # 1. Cap & cash dynamic
        self.assertEqual(self.w.cards['cap'].text(),'5,000원'); self.w.apply_status(synthetic_status(cap=10000,available=7500))
        self.assertEqual(self.w.cards['cap'].text(),'10,000원'); self.assertEqual(self.w.cards['available'].text(),'7,500원')
        # 2. Unknown cash
        self.assertEqual(self.w.cards['cash'].text(),'7,500원'); self.w.apply_status(synthetic_status(cash=None,available=None))
        self.assertEqual(self.w.cards['cash'].text(),'미확인'); self.assertEqual(self.w.cards['available'].text(),'미확인')
        # 3. Old cash
        self.w.apply_status(synthetic_status())
        self.assertIn('최근 조회값만 있음',self.w.cards['account'].text())
    def test_halt_reason_and_start_block(self):
        self.w.apply_status(synthetic_status(halt='ACCOUNT_RECONCILE_STALE',halt_info={'occurred_at':'09:20','classification':'MANUAL_REVIEW'}))
        self.assertIn('HALT',self.w.cards['safety'].text()); self.assertIn('ACCOUNT_RECONCILE_STALE',self.w.errors.toPlainText()); self.assertFalse(self.w.buttons['start'].isEnabled())
    def test_running_process_warning_states(self):
        self.w.apply_status(synthetic_status(process={'running':True,'state':'실행 중'}))
        self.assertIn('주의',self.w.cards['safety'].text())
        self.w.apply_status(synthetic_status(process={'running':True,'state':'실행 중','managed':True,'process_heartbeat_fresh':False}))
        self.assertIn('프로세스 응답 지연',self.w.cards['safety'].text()); self.assertFalse(self.w.buttons['start'].isEnabled()); self.assertEqual(self.s.calls,[])
    def test_hard_locks_readonly(self):
        text=self.w.protection.text(); self.assertIn('삼성전자 005930',text); self.assertIn('광전자 017900',text); self.assertEqual(text.count('HARD_LOCK'),2)
        self.assertNotIn('해제', ' '.join(b.text() for b in self.w.findChildren(QPushButton)))
    def test_candidate_all_fields(self):
        self.assertEqual(len(self.w.stats),10); self.assertIn('24',self.w.stats['raw_candidates'].text()); self.assertIn('1',self.w.stats['risk_zero_qty'].text())
    def test_decision_full_stamp_actual_reason(self):
        self.assertEqual(self.w.decisions.rowCount(),1); self.assertIn('2026-10-01',self.w.decisions.item(0,0).text())
        self.assertIn('ONE_SHARE_UNAFFORDABLE',self.w.decisions.item(0,2).text()); self.assertIn('매수 안 함',self.w.decisions.item(0,2).text())
    def test_trade_paper_and_fill(self): self.assertEqual(self.w.trades.item(0,6).text(),'PAPER'); self.assertEqual(self.w.trades.item(0,7).text(),'FILLED')
    def test_logs_separate_from_decisions(self): self.assertIn('ENTRY_EVAL',self.w.logs.toPlainText()); self.assertEqual(self.w.tabs.count(),4)
    def test_unknown_orders_start_disabled(self): self.w.apply_status(synthetic_status(uncertain=1)); self.assertFalse(self.w.buttons['start'].isEnabled())
    def test_market_boundary_start_rules_table(self):
        for stamp,enabled in [('15:04:59',True),('15:05:00',False),('15:10:00',False)]:
            self.w.apply_status(synthetic_status(time='2026-10-01T'+stamp+'+09:00')); self.assertEqual(self.w.buttons['start'].isEnabled(),enabled)
        self.w.apply_status(synthetic_status(time='2026-10-01T15:10:00+09:00',positions=1,position_count=1))
        self.assertTrue(self.w.buttons['start'].isEnabled())
        self.w.apply_status(synthetic_status(supported=False,time='2026-10-01T15:20:00+09:00')); self.assertFalse(self.w.buttons['start'].isEnabled())
    def test_close_event_choices_table(self):
        # 1. window only
        self.s.status['process']['running']=True; self.w.close_choice=Mock(return_value='window'); event=QCloseEvent(); self.w.closeEvent(event)
        self.assertTrue(event.isAccepted()); self.assertEqual(self.s.calls,[]); self.w.close_choice.assert_called_once()
        # 2. cancel
        self.s.status['process']['running']=True; self.w.close_choice=Mock(return_value='cancel'); event=QCloseEvent(); self.w.closeEvent(event)
        self.assertFalse(event.isAccepted()); self.assertEqual(self.s.calls,[])
        # 3. stopped process close
        self.s.status['process']['running']=False; event=QCloseEvent(); self.w.closeEvent(event); self.assertTrue(event.isAccepted()); self.assertEqual(self.s.calls,[])
    def test_stop_close_waits_existing_protection(self):
        self.s.status['process']['running']=True; self.w.close_choice=Mock(return_value='stop'); event=QCloseEvent(); self.w.closeEvent(event)
        self.wait(lambda:'STOP' in self.s.calls); self.assertFalse(event.isAccepted()); self.assertTrue(self.w.wait_close)
        self.w._read_done(synthetic_status(positions=1)); self.assertFalse(self.w.allow_close)
        self.w._read_done(synthetic_status()); self.assertTrue(self.w.allow_close)
    def test_refresh_service_readonly(self): self.w.refresh(); self.wait(lambda:not self.w.reading); self.assertEqual(self.s.snapshot_calls,1); self.assertEqual(self.s.calls,[])
    def test_refresh_coalesces_requests(self):
        self.s.snapshot=Mock(side_effect=lambda:(time.sleep(.1),synthetic_status())[1]); self.w.refresh(); self.w.refresh(); self.wait(lambda:not self.w.reading)
        self.assertEqual(self.s.snapshot.call_count,1)
    def test_long_task_responsive_and_main_thread_delivery(self):
        ticks=[]; delivered=[]; timer=QTimer(); timer.setInterval(5); timer.timeout.connect(lambda:ticks.append(1)); timer.start()
        self.w.submit(lambda:(time.sleep(.1),{'verdict':'PASS'})[1],lambda r:delivered.append(QThread.currentThread()==self.app.thread()))
        self.wait(lambda:bool(delivered)); timer.stop(); self.assertGreater(len(ticks),3); self.assertEqual(delivered,[True])
    def test_emergency_works_during_long_work(self):
        self.w.submit(lambda:(time.sleep(.15),{})[1]); self.click('kill'); self.wait(lambda:'KILL' in self.s.calls)
        self.assertTrue(self.w.working)
    def test_settings_no_auto_live_and_confirmation(self):
        d=gui.SettingsDialog(self.w); d.amount.setText('10000'); self.w.confirm.return_value=False; d.change_cap(); self.assertEqual(self.s.calls,[])
        self.w.confirm.return_value=True; d.change_cap(); self.wait(lambda:any(isinstance(x,tuple) and x[0]=='cap' for x in self.s.calls))
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='authorize' for x in self.s.calls)); d.accept()
    def test_human_result_has_no_internal_json(self):
        text=result_text({'verdict':'REQUESTED','command':'STOP','note':'기존 봇 보호를 유지합니다.'})
        self.assertIn('요청을 저장',text); self.assertIn('기존 봇 보호',text); self.assertNotIn('REQUESTED',text); self.assertNotIn('{',text)
    def test_settings_result_is_human_readable(self):
        d=gui.SettingsDialog(self.w); d.output({'verdict':'SAVED','cap':10000})
        self.assertIn('10,000원',d.result.toPlainText()); self.assertNotIn('SAVED',d.result.toPlainText()); d.accept()
    def test_invalid_settings_amount(self):
        d=gui.SettingsDialog(self.w); d.amount.setText('LIVE'); d.change_cap(); self.w.problem.assert_called_once(); self.assertEqual(self.s.calls,[]); d.accept()
    def test_cost_unconfirmed_disable_ready(self):
        self.w.apply_status(synthetic_status(costs_confirmed=False)); d=gui.SettingsDialog(self.w)
        self.assertFalse(d.ready.isEnabled()); self.assertFalse(d.authorize.isEnabled()); d.accept()
    def test_no_ui_broker_or_hardcoded_trial_amount(self):
        src=(ROOT/'operator_gui.py').read_text(encoding='utf-8'); tree=ast.parse(src)
        imports=[node.module or '' for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
        self.assertFalse(any('broker' in x or 'engine' in x or 'strategy' in x for x in imports))
        self.assertFalse(any(isinstance(x,ast.Constant) and isinstance(x.value,str) and ('3000' in x.value or '3,000' in x.value or '나인테크' in x.value) for x in ast.walk(tree)))
    def test_state_read_failure_disables_start(self): self.w._read_failed('PermissionError: sample.json'); self.assertFalse(self.w.buttons['start'].isEnabled()); self.assertIn('PermissionError',self.w.notice.text())
    def test_console_fallback_on_missing_gui_or_qt(self):
        fallback=Mock(return_value=0)
        def missing(): raise ImportError('synthetic missing Qt')
        self.assertEqual(gui_launch.launch(gui=missing,console=fallback),0)
        with patch.dict(sys.modules,{'operator_gui':None}):
            self.assertEqual(gui_launch.launch(console=fallback),0)
        self.assertEqual(fallback.call_count,2)
    def test_risk_reason_is_korean_specific(self):
        text=human_decision({'decision':'NO_BUY','primary_reason_code':'NO_BUY_MIN_QTY_ZERO',
            'gates':{'risk_quantity':{'result':'FAIL','values':{'reason_code':'RISK_BUDGET_ZERO_QTY'}}}})
        self.assertIn('거래당 위험예산으로 1주 매수 불가',text)
    def test_refresh_handles_summary_without_cycle_stamp(self):
        self.w.apply_status(synthetic_status(candidate_summary={'cycle_started':None})); self.assertIn('미확인',self.w.summary_stamp.text())
    def test_console_emergency_has_no_qt_import(self):
        tree=ast.parse((ROOT/'operator_console.py').read_text(encoding='utf-8'))
        self.assertFalse(any(isinstance(x,ast.ImportFrom) and x.module and x.module.startswith('PySide6') for x in ast.walk(tree)))
        from operator_console import main
        fake=Mock(); fake.stop.return_value={'verdict':'REQUESTED'}
        self.assertEqual(main(service=fake,reader=lambda _: '',writer=lambda _:None,argv=['--emergency','KILL']),0); fake.stop.assert_called_once_with('KILL')
    def test_console_public_menu_generic_and_smoke_hidden(self):
        self.assertEqual(len(PUBLIC_MENUS),8); self.assertFalse(any('3000' in x or '3,000' in x or '150' in x for x in PUBLIC_MENUS.values()))

class ServiceGateway:
    def __init__(self,t): self.t=t; self.calls=[]; self.orders=[]; self.cash=7500; self.open=[]
    def snapshot(self): self.calls.append('snapshot'); return {'cash':self.cash,'holdings':{},'orders':copy.deepcopy(self.open)}
    def command(self,args,timeout=60):
        self.calls.append(tuple(args)); atomic_json(self.t.root/'connection_check.json',{'started':self.t.clock[0].isoformat(),
            'passed':True,'counts':{'valid_regular_ticks':3}}); return 0
    def start(self,args): self.calls.append(('start',tuple(args))); return Mock()

class UserServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.clock=[datetime(2026,10,1,9,30,tzinfo=KST)]
        atomic_json(self.root/'config.json',asdict(Config())); atomic_json(self.root/'automation.json',asdict(AutomationConfig()))
        self.p=Policy(self.root/'runtime/automation_policy.json'); self.g=ServiceGateway(self)
        self.s=UserOperations(self.root,clock=lambda:self.clock[0],gateway=self.g); self.s.save_result('pilot',{'verdict':'PASS'})
    def ready(self,cap=5000):
        self.s.confirm_costs('0.015','0.015','0.2',True); self.s.change_cap(str(cap),True); return self.s.prepare_capital()
    def state(self,**values):
        d={'orders':{},'positions':{},'trades':[],'halt':''}; d.update(values); atomic_json(self.root/'runtime/automation-production/state.json',d)
    def hashes(self): return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
    def test_snapshot_readonly_repeated(self):
        before=self.hashes()
        for _ in range(12): self.s.snapshot()
        self.assertEqual(before,self.hashes()); self.assertEqual(self.g.calls,[])
    def test_outside_start_no_gateway(self):
        self.clock[0]=self.clock[0].replace(hour=18)
        with self.assertRaises(ValueError): self.s.start_user()
        self.assertEqual(self.g.calls,[])
    def test_armed_cap0_start_observation_only(self):
        self.assertEqual(self.s.start_user()['verdict'],'STARTED'); cfg,auto,p=self.s.settings()
        self.assertFalse(cfg.live_mode); self.assertEqual(auto.capital_limit,0); self.assertEqual(p['mode'],'ARMED'); self.assertEqual(self.g.orders,[])
    def test_stop_flat_kill_preserve_settings(self):
        cfg=self.s.settings()[:2]
        for command in ('STOP','STOP_FLAT','KILL'):
            self.s.stop(command); self.assertEqual(self.s.settings()[2]['mode'],command); self.assertEqual(self.s.settings()[:2],cfg)
        self.assertEqual(self.g.calls,[])
    def test_cap_changes_explicit_not_auto_live(self):
        self.s.change_cap('10000',True); cfg,a,p=self.s.settings()
        self.assertEqual(a.capital_limit,10000); self.assertFalse(cfg.live_mode); self.assertFalse(p['live_authorized']); self.assertEqual(p['mode'],'ARMED'); self.assertEqual(self.g.calls,[])
    def test_cap_rejects_invalid_values_and_unconfirmed(self):
        before=self.s.settings()
        for value in ('-1','1.5','LIVE','３０００','1e4',''):
            with self.assertRaises(ValueError): self.s.change_cap(value,True)
        with self.assertRaises(ValueError): self.s.change_cap('5000',False)
        self.assertEqual(self.s.settings(),before)
    def test_cap_confirmation_stale_rejected(self):
        with self.assertRaises(ValueError): self.s.change_cap('5000',True,expected_cap=123)
        self.assertEqual(self.s.settings()[1].capital_limit,0)
    def test_cap_change_blocked_when_active_or_unsafe_table(self):
        # 1. running lock
        with ProcessLock(self.root/'runtime/operator_runner.lock'):
            with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        # 2. live pid running
        with patch.object(self.s,'running',return_value=True):
            with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        # 3. positions present
        self.state(positions={'111111':{'qty':1}})
        with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        # 4. unknown orders present
        self.state(positions={},orders={'x':{'status':'UNKNOWN'}})
        with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        # 5. halt present
        self.state(orders={},halt='ACCOUNT_RECONCILE_STALE')
        with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        self.assertEqual(self.s.settings()[1].capital_limit,0)
    def test_cost_not_confirmed_no_network(self):
        self.s.change_cap('5000',True)
        with self.assertRaisesRegex(ValueError,'비용'): self.s.prepare_capital()
        self.assertEqual(self.g.calls,[])
    def test_capital_ready_generic_cash_min(self):
        result=self.ready(10000); self.assertEqual(result['available'],7500); self.assertEqual(result['cap'],10000)
        self.assertEqual(self.s.settings()[2]['mode'],'ARMED'); self.assertEqual(self.g.orders,[])
    def test_authorization_and_explicit_start_confirmation(self):
        self.ready(); result=self.s.authorize_capital('5000','실제매매승인')
        cfg,a,p=self.s.settings(); self.assertTrue(cfg.live_mode); self.assertTrue(p['live_authorized']); self.assertEqual(p['mode'],'STOP')
        self.assertEqual(result['cap'],5000); self.assertNotIn(('start',()),self.g.calls)
        # without explicit confirmation
        with self.assertRaises(ValueError): self.s.start_user(False)
        self.assertNotIn(('start',()),self.g.calls)
        # with explicit confirmation
        self.assertEqual(self.s.start_user(True)['verdict'],'STARTED')
        self.assertIn(('start',()),self.g.calls); self.assertEqual(self.s.settings()[2]['mode'],'LIVE')
    def test_approval_basis_lifecycle_and_daily_expiry(self):
        # 1. reapproval retains valid prior generic basis
        self.ready(); self.s.authorize_capital('5000','실제매매승인'); self.s.change_cap('10000',True)
        self.assertEqual(self.s.prepare_capital()['verdict'],'PASS'); self.s.authorize_capital('10000','실제매매승인')
        self.assertEqual(self.s.settings()[1].capital_limit,10000)
        # 2. existing approved basis allows fresh recheck next day
        self.clock[0]+=timedelta(days=1)
        self.s.change_cap('10000',True); self.assertEqual(self.s.prepare_capital()['verdict'],'PASS')
    def test_initial_approval_basis_does_not_last_to_next_day(self):
        self.ready(); self.clock[0]+=timedelta(days=1)
        with self.assertRaises(ValueError): self.s.prepare_capital()
        self.assertFalse(self.s.settings()[0].live_mode)
    def test_authorization_halt_added_after_proof_rejected(self):
        self.ready(); self.state(halt='MANUAL_REVIEW')
        with self.assertRaises(ValueError): self.s.authorize_capital('5000','실제매매승인')
        self.assertFalse(self.s.settings()[0].live_mode)
    def test_cap_transaction_failure_rolls_back(self):
        before=self.s.settings(); real_atomic=atomic_json
        def fail_once(path,data):
            if Path(path).name=='operator_cap_approval_basis.json': raise PermissionError('synthetic denied receipt')
            return real_atomic(path,data)
        with patch('operator_ui_service.atomic_json',side_effect=fail_once):
            with self.assertRaises(PermissionError): self.s.change_cap('5000',True)
        self.assertEqual(self.s.settings(),before)
    def test_authorization_invalid_conditions_table(self):
        self.ready()
        # 1. wrong amount or phrase
        for amount,phrase in [('5001','실제매매승인'),('5000','yes'),('','')]:
            with self.assertRaises(ValueError): self.s.authorize_capital(amount,phrase)
        # 2. expired basis (61s)
        self.clock[0]+=timedelta(seconds=61)
        with self.assertRaises(ValueError): self.s.authorize_capital('5000','실제매매승인')
        # 3. changed settings
        self.clock[0]-=timedelta(seconds=61)
        self.s.change_cap('10000',True)
        with self.assertRaises(ValueError): self.s.authorize_capital('10000','실제매매승인')
        self.assertFalse(self.s.settings()[0].live_mode)
    def test_prepare_capital_prerequisites_table(self):
        # 1. open orders block
        self.s.confirm_costs('0','0','0',True); self.s.change_cap('5000',True); self.g.open=[{'id':'synthetic'}]
        with self.assertRaises(ValueError): self.s.prepare_capital()
        self.assertEqual(self.g.calls,['snapshot'])
        # 2. missing initial proof blocks
        self.g.open=[]; (self.root/'runtime/operator_pilot.json').unlink()
        with self.assertRaises(ValueError): self.s.prepare_capital()
        self.assertEqual(self.g.calls,['snapshot'])
    def test_protection_release_blocks_ui(self):
        self.p.command('RELEASE',symbol='005930',confirmation='RELEASE')
        with self.assertRaises(ValueError): self.s.start_user()
        with self.assertRaises(ValueError): self.s.change_cap('5000',True)
        self.assertEqual(self.g.calls,[])
    def test_old_errors_not_current_halt(self):
        atomic_json(self.root/'runtime/operator_error.json',{'timestamp':(self.clock[0]-timedelta(days=1)).isoformat(),'error':'past rejection'})
        s=self.s.snapshot(); self.assertFalse(s['active_error']); self.assertIn('past rejection',s['error'])
    def test_cash_increase_does_not_raise_cap(self):
        self.s.change_cap('5000',True); self.s.remember_account({'cash':12000,'holdings':{},'orders':[]})
        self.assertEqual(self.s.snapshot()['available'],5000); self.assertEqual(self.s.settings()[1].capital_limit,5000)
    def test_core_orders_still_guard_cap0_and_hard_locks(self):
        from automation.engine import AutonomousEngine
        from tests.test_automation import FakeBroker
        cfg,auto,p=self.s.settings(); broker=FakeBroker(); engine=AutonomousEngine(cfg,auto,self.p,self.root/'runtime/automation-production',broker,lambda:self.clock[0])
        engine.connected=True; engine.market='3'; engine.account_ok=True
        for symbol in ('005930','017900'):
            for reason in ('SELL_STOP_FLAT','SELL_EMERGENCY','SELL_SESSION_FLATTEN','SELL_PROFIT_PROTECTION_HIT'):
                engine.submit('SELL',symbol,1,1000,{'reason':reason,'ownership':'EXISTING_POLICY'})
        engine.submit('BUY','111111',1,1000,{})
        self.assertEqual(broker.orders,[])
    def test_local_diagnostic_no_network_or_state_changes(self):
        before=self.s.settings(); d=self.s.local_diagnostic(); self.assertEqual(d['network_calls'],0); self.assertEqual(self.s.settings(),before)

if __name__=='__main__': unittest.main()
