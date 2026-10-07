"""Oct 8 single GUI click -> daily approval -> open evidence -> fake first fill."""
import contextlib,io,time,unittest
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.settings import KST
from core.strategy import Tick
from automation.policy import Policy
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.readiness import authorization_valid
from automation.runner import readiness_snapshot
from operator_gui import MainWindow
from tests import test_gui as gui
from tests import test_start_click_and_morning as morning
from tests import test_audit_followup as patterns
from tests.test_automation import FakeBroker

class NextDayFirstFillE2E(unittest.TestCase):
    def test_single_click_preopen_to_open_b_pattern_first_fill(self):
        app=gui.QApplication.instance() or gui.QApplication([])
        f=morning.NextMorningServiceTests();f.setUp();self.addCleanup(f.doCleanups)
        f.clock[0]=datetime(2026,10,8,8,0,tzinfo=KST);f.g.cash=20480
        f.s.confirm_costs('0.015','0.015','0.2',True);f.s.change_cap('20000',True)
        f.proof()  # Fake readiness backend evidence, same contract as standalone runner tests.
        w=MainWindow(f.s,auto_refresh=False);w.apply_status(f.s.snapshot())
        def cleanup():
            w.check_timer.stop();w.timer.stop();w.allow_close=True;w.close();app.processEvents()
        self.addCleanup(cleanup)
        def submit(op,callback=None,**kw):
            result=op()
            if callback:callback(result)
            return result
        w.submit=Mock(side_effect=submit);w.confirm=Mock(side_effect=AssertionError('extra approval dialog'))
        w.problem=Mock();w.show_result=Mock()
        with patch('operator_gui.QInputDialog.getText',side_effect=AssertionError('typed approval not permitted')):
            w.start()
        self.assertIn(('start',()),f.g.calls);self.assertEqual(f.g.orders,[]);w.problem.assert_not_called()
        cfg,auto,policy=f.s.settings();self.assertTrue(cfg.live_mode);self.assertTrue(policy['live_authorized'])
        self.assertEqual(auto.capital_limit,20000);self.assertEqual(cfg.risk_fraction,.02)
        receipt=(f.root/'runtime/operator_user_authorization.json').read_bytes()
        broker=FakeBroker();p=Policy(f.root/'runtime/automation_policy.json')
        with contextlib.redirect_stdout(io.StringIO()):
            e=AutonomousEngine(cfg,auto,p,f.root/'runtime/automation-production',broker,lambda:f.clock[0])
            e.reconcile({'cash':20480,'equity':20480,'holdings':{},'orders':[]});e.connected=True;e.market='UNKNOWN'
            e.market_status={'999990':{'pass':True}}
            detector=PatternDetector(cfg,auto,e.costs);detector.continuous_observation=True
            self.assertEqual(readiness_snapshot(e,{'subscription_acks':1},False,f.clock[0],detector)['state'],'PREOPEN_READY')
            self.assertIsNone(e.submit('BUY','999990',1,1002,{'stop':979}));self.assertEqual(broker.orders,[])
            f.clock[0]=f.clock[0].replace(hour=9);e.market='3';e.last_regular_tick_at=f.clock[0];e.last_radar_at=f.clock[0]
            first=Tick('999990',f.clock[0],1000,100,100,1001,999);detector.ingest(first,first.time)
            self.assertEqual(detector.buffers['999990'][0].time,f.clock[0]);self.assertEqual(detector.usable_bars('999990'),[])
            ready=readiness_snapshot(e,{'subscription_acks':1},True,f.clock[0],detector)
            self.assertEqual(ready['state'],'OPEN_READY');self.assertIsNone(detector.signal(first,{'status':'WATCH'}))
            self.assertTrue(authorization_valid(f.root,cfg,auto,p,f.clock[0])[0])
            self.assertEqual(receipt,(f.root/'runtime/operator_user_authorization.json').read_bytes())
            f.clock[0]+=timedelta(minutes=5);e.last_regular_tick_at=f.clock[0];e.last_radar_at=f.clock[0];e.last_account=time.monotonic()
            fixture=SimpleNamespace(cfg=cfg,auto=auto,e=e,clock=f.clock)
            detector,tick,watch=patterns.SetupReachabilityTests.build(fixture,'B');detector.ingest(tick,tick.time)
            signal=detector.signal(tick,watch);self.assertIsNotNone(signal);self.assertEqual(signal['pattern'],'B')
            self.assertGreaterEqual(detector.base_gates(tick,watch)['warmup']['values']['elapsed_seconds'],70)
            def guard():
                view=readiness_snapshot(e,{'subscription_acks':1},True,f.clock[0],detector)
                valid,reasons=authorization_valid(f.root,cfg,auto,p,f.clock[0],**e.authorization_context())
                return '' if valid and view['state']=='OPEN_READY' else 'READINESS_BLOCKED'
            e.readiness_reason=guard
            order=e.enter(signal,tick);self.assertEqual(order['status'],'ACCEPTED');self.assertEqual(e.s['entries'],{})
            self.assertEqual(len(broker.orders),1)
            e.cumulative_fill(order,1,order['limit']);self.assertEqual(e.s['entries'],{'999990':1})
            e.cumulative_fill(order,1,order['limit']);self.assertEqual(e.s['entry_diagnostics']['entry_count'],1)
            self.assertEqual(e.s['positions']['999990']['initial_stop'],signal['stop'])
        self.assertEqual(receipt,(f.root/'runtime/operator_user_authorization.json').read_bytes())

if __name__=='__main__':unittest.main()
