"""Explicit start-click UX and next-day readiness: fake gateway only."""
import copy, unittest
from dataclasses import asdict,replace
from datetime import datetime
from unittest.mock import Mock,patch
from core.settings import KST
from core.util import atomic_json
from operator_gui import MainWindow
from automation.decisions import BUILD_ID
from automation.readiness import authorization_valid
from tests import test_gui as fixtures
from tests import test_v13_engine_readiness as readiness_fixtures
from tests import test_setup_acceleration as setup_fixtures

class StartClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=fixtures.QApplication.instance() or fixtures.QApplication([])
    def setUp(self):
        self.s=fixtures.FakeService();self.s.status=fixtures.synthetic_status(cap=20000)
        self.s.prepare_start=Mock(return_value={'verdict':'READY_LIVE','cap':20000})
        self.s.start_user=Mock(return_value={'verdict':'STARTED'})
        self.s.authorize_capital=Mock(return_value={'verdict':'SAVED'})
        self.w=MainWindow(self.s,auto_refresh=False);self.w.apply_status(self.s.status)
        self.w.problem=Mock();self.w.show_result=Mock();self.w.confirm=Mock(side_effect=AssertionError('redundant confirmation'))
        def submit(op,callback=None,**kwargs):
            result=op()
            if callback:callback(result)
            return result
        self.w.submit=Mock(side_effect=submit)
        self.no_input=patch('operator_gui.QInputDialog.getText',side_effect=AssertionError('redundant amount input'))
        self.no_input.start();self.addCleanup(self.no_input.stop)
    def tearDown(self):
        self.w.check_timer.stop();self.w.timer.stop();self.w.allow_close=True;self.w.close();self.app.processEvents()
    def test_ready_live_click_starts_without_secondary_dialog(self):
        self.w.start();self.s.start_user.assert_called_once_with(confirmed=True)
        self.s.authorize_capital.assert_not_called();self.w.confirm.assert_not_called();self.w.show_result.assert_not_called()
    def test_needs_approval_click_authorizes_only_displayed_existing_cap(self):
        self.s.prepare_start.return_value={'verdict':'NEEDS_LIVE_APPROVAL','cap':20000}
        self.w.start();self.s.authorize_capital.assert_called_once_with('20000',True)
        self.s.start_user.assert_called_once_with(confirmed=True)
    def test_failed_readiness_never_authorizes_or_starts(self):
        for verdict in ['FAIL','INCONCLUSIVE']:
            self.s.prepare_start.return_value={'verdict':verdict}
            self.w.start()
        self.s.authorize_capital.assert_not_called();self.s.start_user.assert_not_called()
        self.assertEqual(self.w.show_result.call_count,2)
    def test_changed_cap_while_preparing_requires_new_start_click(self):
        self.s.prepare_start.return_value={'verdict':'NEEDS_LIVE_APPROVAL','cap':10000}
        self.w.start();self.s.authorize_capital.assert_not_called();self.s.start_user.assert_not_called()
        self.w.problem.assert_called_once()
    def test_no_click_intent_cannot_authorize_or_start(self):
        for verdict in ['NEEDS_LIVE_APPROVAL','READY_LIVE']:
            self.w._prepared_start({'verdict':verdict,'cap':20000})
        self.s.authorize_capital.assert_not_called();self.s.start_user.assert_not_called()
    def test_zero_cap_starts_observation_without_live_approval(self):
        status=fixtures.synthetic_status(cap=0,mode='ARMED',live_mode=False,live_approved=False)
        self.w.apply_status(status);self.s.prepare_start.return_value={'verdict':'READY_OBSERVE'}
        self.w.start();self.s.start_user.assert_called_once_with(confirmed=False)
        self.s.authorize_capital.assert_not_called()
    def test_halt_and_unknown_block_before_prepare(self):
        for update in [{'halt':'synthetic HALT'},{'uncertain':1}]:
            self.w.apply_status(fixtures.synthetic_status(cap=20000,**update));self.w.start()
        self.s.prepare_start.assert_not_called();self.s.authorize_capital.assert_not_called();self.s.start_user.assert_not_called()
    def test_backend_authorization_rejection_never_starts_runner(self):
        self.s.prepare_start.return_value={'verdict':'NEEDS_LIVE_APPROVAL','cap':20000}
        self.s.authorize_capital.side_effect=ValueError('settings hash changed')
        with self.assertRaises(ValueError):self.w.start()
        self.s.start_user.assert_not_called()

class NextMorningServiceTests(unittest.TestCase):
    def setUp(self):
        fixtures.UserServiceTests.setUp(self)
        cfg,auto,p=self.s.settings()
        # Production operating shape, with fake account/gateway and temporary files only.
        cfg=replace(cfg,risk_fraction=.02,max_daily_loss_fraction=.04,max_positions=1,
                    max_entries_per_day=3,max_watchlist=12,after_enabled=True)
        atomic_json(self.s.root/'config.json',asdict(cfg))
    def proof(self):
        cfg,auto,p=self.s.settings()
        return self.s.save_result('pilot',{'verdict':'PASS','build_id':BUILD_ID,'requalification':True,
            'operational_requalification':True,'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),'policy_revision':p['revision']})
    def test_oct8_preopen_start_path_uses_new_daily_click_and_never_orders(self):
        self.clock[0]=datetime(2026,10,8,8,0,tzinfo=KST)
        self.s.confirm_costs('0.015','0.015','0.2',True);self.s.change_cap('20000',True)
        self.proof();result=self.s.prepare_start()
        self.assertEqual(result['verdict'],'NEEDS_LIVE_APPROVAL')
        self.assertFalse(self.s.settings()[0].live_mode)
        self.s.authorize_capital('20000',True)
        cfg,auto,p=self.s.settings()
        valid,reasons=authorization_valid(self.s.root,cfg,auto,p,self.clock[0])
        self.assertTrue(valid,reasons);self.assertEqual(auto.capital_limit,20000)
        self.assertEqual(self.s.start_user(True)['verdict'],'STARTED')
        self.assertIn(('start',()),self.g.calls);self.assertEqual(self.g.orders,[])
    def test_oct7_approval_is_expired_on_oct8_before_any_new_click(self):
        self.clock[0]=datetime(2026,10,7,8,0,tzinfo=KST)
        self.s.confirm_costs('0.015','0.015','0.2',True);self.s.change_cap('20000',True)
        self.proof();self.s.prepare_capital();self.s.authorize_capital('20000',True)
        self.clock[0]=datetime(2026,10,8,8,0,tzinfo=KST)
        cfg,auto,p=self.s.settings();valid,reasons=authorization_valid(self.s.root,cfg,auto,p,self.clock[0])
        self.assertFalse(valid);self.assertIn('DAILY_LIVE_APPROVAL_EXPIRED',reasons)
        with self.assertRaises(ValueError):self.s.start_user(True)
        self.assertEqual(self.g.orders,[])
    def test_preopen_0759_blocked_without_network_or_order(self):
        self.clock[0]=datetime(2026,10,8,7,59,59,tzinfo=KST)
        with self.assertRaises(ValueError):self.s.prepare_start()
        self.assertEqual(self.g.calls,[]);self.assertEqual(self.g.orders,[])

class NextMorningRunnerTests(unittest.TestCase):
    def test_today_three_entries_reset_on_next_day_boot_but_hard_locks_remain(self):
        from automation.engine import AutonomousEngine
        from automation.policy import Policy
        from tests.test_automation import FakeBroker
        fixture=setup_fixtures.SetupAccelerationTests('test_zero_cap_blocks_valid_B_C')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.e.s['entries']={'003350':1,'038500':1,'012210':1}
        fixture.e.s['daily_loss']=30
        fixture.e.save()
        fixture.clock=fixture.clock.replace(day=8,hour=8,minute=0,second=0)
        broker=FakeBroker()
        engine=AutonomousEngine(fixture.cfg,fixture.auto,Policy(fixture.policy.path),
            fixture.e.folder,broker,lambda:fixture.clock)
        self.assertEqual(engine.s['day'],'2026-10-08');self.assertEqual(engine.s['entries'],{})
        self.assertEqual(engine.s['daily_loss'],0)
        fixture.clock=fixture.clock.replace(hour=9)
        for symbol in ['005930','017900']:
            self.assertEqual(engine.new_entry_reason(symbol),'HARD_LOCK')
        self.assertEqual(broker.orders,[])
    def test_oct8_first_open_tick_and_account_evidence_complete_without_fixed_wait(self):
        fixture=readiness_fixtures.V13RunnerReadinessLoopTests('test_runner_readiness_finishes_on_evidence_not_fixed_delay')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        result,*_=fixture.run_ready(datetime(2026,10,8,9,0,tzinfo=KST),True)
        self.assertTrue(result['passed']);self.assertEqual(result['readiness']['state'],'OPEN_READY')
        self.assertEqual(result['counts']['ticks'],1);self.assertEqual(result['real_orders_sent'],0)

if __name__=='__main__':unittest.main()
