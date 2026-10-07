"""조회 검사를 시작 버튼에 연결하되 명시적 LIVE 승인을 보존한다."""
import copy
import time
import unittest
from unittest.mock import Mock,patch
try:
    import test_gui as fixtures
except ImportError:
    from tests import test_gui as fixtures
from automation.decisions import BUILD_ID
from operator_gui import MainWindow
from operator_ui_service import candidate_wait_reason,board_freshness
from datetime import datetime,timedelta

class GuidedServiceTests(unittest.TestCase):
    def setUp(self):fixtures.UserServiceTests.setUp(self)
    def proof(self):
        cfg,auto,p=self.s.settings()
        return self.s.save_result('pilot',{'verdict':'PASS','build_id':BUILD_ID,'requalification':True,'operational_requalification':True,
            'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),'policy_revision':p['revision']})
    def test_missing_proof_runs_readonly_requalification_preserves_cap_cost_mode(self):
        fixtures.UserServiceTests.ready(self,20000);before=self.s.settings()
        def pilot(progress,_recovery=False,_operational=False):
            self.assertTrue(_recovery);self.assertTrue(_operational);self.proof();return {'verdict':'PASS'}
        with patch.object(self.s,'pilot',side_effect=pilot) as smoke:
            result=self.s.prepare_start();smoke.assert_called_once()
        self.assertEqual(result['verdict'],'NEEDS_LIVE_APPROVAL');self.assertEqual(before,self.s.settings());self.assertEqual(self.g.orders,[])
    def test_pilot_fail_cannot_run_prepare_authorize_or_start(self):
        with patch.object(self.s,'pilot',return_value={'verdict':'INCONCLUSIVE'}),patch.object(self.s,'prepare_capital') as ready:
            result=self.s.prepare_start();self.assertEqual(result['verdict'],'INCONCLUSIVE');ready.assert_not_called()
        self.assertEqual(self.g.orders,[]);self.assertFalse(self.s.settings()[0].live_mode)
    def test_cap_zero_and_cost_unconfirmed_block_live_authorization(self):
        self.proof();result=self.s.prepare_start();self.assertEqual(result['verdict'],'READY_OBSERVE')
        self.assertFalse(self.s.settings()[0].live_mode);self.assertEqual(self.g.orders,[])
        self.s.change_cap('20000',True);self.proof()
        with patch.object(self.s,'pilot') as smoke,patch.object(self.s,'prepare_capital') as ready:
            result=self.s.prepare_start();self.assertEqual(result['verdict'],'COST_CONFIRMATION_REQUIRED');smoke.assert_not_called();ready.assert_not_called()
        self.assertFalse(self.s.settings()[0].live_mode)
        with self.assertRaises(ValueError):self.s.authorize_capital('20000','실제매매승인')
    def test_cap_phrase_required_after_ready_then_start_uses_existing_runner(self):
        fixtures.UserServiceTests.ready(self,20000);self.proof();ready=self.s.prepare_start()
        self.assertEqual(ready['verdict'],'NEEDS_LIVE_APPROVAL');self.assertEqual(self.g.orders,[])
        for amount,phrase in [('10000','실제매매승인'),('20000','LIVE'),('20000','')]:
            with self.assertRaises(ValueError):self.s.authorize_capital(amount,phrase)
        self.s.authorize_capital('20000','실제매매승인');self.assertEqual(self.s.settings()[2]['mode'],'STOP')
        with self.assertRaises(ValueError):self.s.start_user(False)
        result=self.s.start_user(True);self.assertEqual(result['verdict'],'STARTED');self.assertIn(('start',()),self.g.calls)
        self.assertEqual(self.g.orders,[])
    def test_reuse_new_build_approval_checks_connection_without_requalifying(self):
        fixtures.UserServiceTests.ready(self,20000);self.proof();self.s.prepare_capital();self.s.authorize_capital('20000','실제매매승인')
        with patch.object(self.s,'pilot') as smoke:
            result=self.s.prepare_start();smoke.assert_not_called();self.assertEqual(result['verdict'],'READY_LIVE')
        self.assertEqual(self.g.orders,[])
    def test_stop_kill_after_hours_state_transitions(self):
        for mode,hour in [('KILL',9),('ARMED',20)]:
            self.p.command(mode);self.clock[0]=self.clock[0].replace(hour=hour)
            with patch.object(self.s,'pilot') as smoke:
                with self.assertRaises(ValueError):self.s.prepare_start()
                smoke.assert_not_called()
        self.assertEqual(self.g.calls,[])
        self.setUp()
        fixtures.UserServiceTests.ready(self,20000);self.proof();self.s.prepare_capital();self.s.authorize_capital('20000','실제매매승인')
        self.p.command('LIVE',confirmation='LIVE')
        def pilot(*args,**kw):self.proof();return {'verdict':'PASS'}
        with patch.object(self.s,'pilot',side_effect=pilot):result=self.s.prepare_start()
        self.assertEqual(self.s.settings()[2]['mode'],'STOP');self.assertEqual(result['verdict'],'READY_LIVE');self.assertEqual(self.g.orders,[])

class GuidedGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=fixtures.QApplication.instance() or fixtures.QApplication([])
    def setUp(self):
        self.s=fixtures.FakeService();self.w=MainWindow(self.s,auto_refresh=False);self.w.apply_status(self.s.status)
        self.w.confirm=Mock(return_value=True);self.w.show_result=Mock();self.w.problem=Mock()
    def tearDown(self):
        self.w.check_timer.stop();self.w.timer.stop()
        for pool in (self.w.read_pool,self.w.command_pool,self.w.emergency_pool):pool.waitForDone(3000)
        self.w.allow_close=True;self.w.close();self.app.processEvents()
    def test_candidate_board_thirty_rows_and_bot_explanation(self):
        state=copy.deepcopy(self.s.status)
        state['candidate_board']=[{'symbol':f'{i:06}','name':'합성','state':'SHADOW','source_count':1,'current_price':400,'gates':{'acceleration':{'result':'FAIL'}},'timestamp':state['time']} for i in range(30)]
        state['bot_positions']=[{'name':'합성 봇','entry':1000,'current':1100,'quantity':1,'setup':'A','invalidation':980,'protection':1060,'high':1100,'MFE':100,'MAE':10,'risk':0,'hold_reason':'현재 보호선 위','next_exit':'보호선 또는 15:10'}]
        self.w.apply_status(state);self.assertEqual(self.w.board.rowCount(),30);self.assertIn('가속',self.w.board.item(0,4).text())
        self.assertEqual(self.w.bot_board.rowCount(),1);self.assertIn('15:10',self.w.bot_board.item(0,7).text())
    def test_stop_flat_kill_buttons_explain_actual_semantics(self):
        self.assertIn('신규매수',self.w.buttons['stop'].text());self.assertIn('전량청산',self.w.buttons['flat'].text())
        self.assertIn('보호',self.w.buttons['flat'].toolTip())
    def test_gui_diagnostics_and_candidate_labeling_invariants(self):
        self.w._check_stage('150초 안전검사');self.w.check_started=time.monotonic()-151;self.w._check_elapsed()
        self.assertIn('결과 정리 중',self.w.notice.text());self.w.show_result.assert_not_called();self.assertEqual(self.s.calls,[])
        state=copy.deepcopy(self.s.status);state['candidate_board']=[{'symbol':'999990','state':'SHADOW','source_count':1,
            'setup_trials':[{'setup':'A','candidate_generated':False,'gates':{}}],'gates':{},'data_fresh':False,'data_note':'틱 판단 대기 / 오래된 기록'}]
        self.w.apply_status(state);self.assertNotIn('SETUP',self.w.board.item(0,1).text());self.assertIn('틱 미확인',self.w.board.item(0,1).text())
    def test_dialog_cancellations_prevent_preparation_or_authorization(self):
        with patch('operator_gui.QInputDialog.getText',return_value=('',False)):
            self.w._prepared_start({'verdict':'COST_CONFIRMATION_REQUIRED'})
            self.w._prepared_start({'verdict':'NEEDS_LIVE_APPROVAL','cap':20000})
        self.assertEqual(self.s.calls,[])
    def test_old_tick_gate_not_presented_as_current(self):
        clock=datetime.fromisoformat(self.s.status['time'])
        row={'timestamp':(clock-timedelta(seconds=16)).isoformat(),'gates':{'acceleration':{'result':'PASS'}}}
        self.assertFalse(board_freshness(row,clock)['data_fresh'])
        row['timestamp']=clock.isoformat();self.assertTrue(board_freshness(row,clock)['data_fresh'])
    def test_wait_summary_is_saved_counts_not_new_strategy(self):
        state=copy.deepcopy(self.s.status);state['cap']=20000;state['available']=20000
        state['candidate_summary'].update(affordable=0,watch=0,setup_candidates=0)
        self.assertIn('1주 가능한 후보가 없습니다',candidate_wait_reason(state))
        state['candidate_summary'].update(affordable=10,watch=5)
        self.assertIn('Setup 형상',candidate_wait_reason(state));self.assertEqual(self.s.calls,[])

if __name__=='__main__':unittest.main()
