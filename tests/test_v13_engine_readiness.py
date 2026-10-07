"""합성 시각·계좌·수신만 사용한다. 실계좌 조회와 실제 주문은 금지한다."""
import contextlib
import io
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from core.util import atomic_json
from automation.session import SessionPlan,control_supported,supported,assessment
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation import runner
from tests.test_automation import FakeBroker
from tests.test_runner_loop import FakeRest

DAY=datetime(2026,10,7,9,0,tzinfo=KST)
SYM='999990'

class V13ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.clock=DAY;self.cfg=Config();self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        self.policy=Policy(self.root/'runtime/automation_policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.broker=FakeBroker();self.engine=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'runtime/automation-production',self.broker,lambda:self.clock,shadow=True)
        self.detector=PatternDetector(self.cfg,self.auto,self.engine.costs);self.detector.continuous_observation=True
        self.engine.connected=True;self.engine.market='3';self.engine.account_ok=True;self.engine.last_account=time.monotonic();self.engine.last_radar_at=DAY
        self.engine.market_status[SYM]={'pass':True};self.counts={'subscription_acks':1,'account_checks':1,'radar_cycles':1,'ticks':1}
        self.output=contextlib.redirect_stdout(io.StringIO());self.output.__enter__();self.addCleanup(self.output.__exit__,None,None,None)
    def tick(self,stamp=None,regular=True,cumulative=100):
        return Tick(SYM,stamp or self.clock,10000,100,cumulative,10010,9990,regular)
    def ready(self,radar=True):return runner.readiness_snapshot(self.engine,self.counts,radar,self.clock,self.detector)
    def test_session_control_boundaries_and_regular_epoch(self):
        self.assertFalse(control_supported(DAY.replace(hour=7,minute=59,second=59)))
        self.assertTrue(control_supported(DAY.replace(hour=8)))
        self.assertFalse(supported(DAY.replace(hour=8)))
        self.clock=DAY.replace(hour=8,minute=10);self.assertEqual(self.ready()['regular_epoch'],DAY.isoformat())
        self.assertEqual(assessment(self.clock)['phase'],'PREOPEN_CONTROL')
    def test_preopen_ignores_regular_tick_and_market_three(self):
        self.clock=DAY.replace(hour=8);self.engine.market='UNKNOWN';self.counts['ticks']=0;self.counts['radar_cycles']=0
        view=self.ready(False);self.assertEqual(view['state'],'PREOPEN_READY');self.assertEqual(view['missing'],[])
        self.assertNotIn('fresh_regular_tick',view['gates']);self.assertTrue(view['market_pending'])
    def test_preopen_missing_account_and_buy_zero_invariants(self):
        self.clock=DAY.replace(hour=8);self.engine.account_ok=False
        self.assertEqual(self.ready()['state'],'WAITING');self.assertIn('ACCOUNT_FRESH_NOT_READY',self.ready()['missing'])
        self.clock=DAY.replace(hour=8,minute=59,second=59)
        self.assertEqual(self.engine.new_entry_reason(SYM),'OUT_OF_SUPPORTED_SESSION')
        self.assertIsNone(self.engine.submit('BUY',SYM,1,10000,{'stop':9900}));self.assertEqual(self.broker.orders,[])
    def test_first_0900_regular_tick_is_preserved(self):
        pre=self.tick(DAY-timedelta(seconds=1));self.assertFalse(self.detector.ingest(pre,pre.time));self.assertEqual(self.detector.last,{})
        first=self.tick(DAY,cumulative=101);self.assertTrue(self.detector.ingest(first,DAY))
        self.assertEqual(self.detector.buffers[SYM][0].time,DAY);self.assertEqual(self.detector.current[SYM]['first_second'],0)
        self.assertEqual(self.detector.usable_bars(SYM),[])
    def test_readiness_immediately_passes_without_strategy_warmup(self):
        self.assertTrue(self.detector.ingest(self.tick(),DAY));view=self.ready()
        self.assertEqual(view['state'],'OPEN_READY');self.assertEqual(view['missing'],[])
        self.assertEqual(len(self.detector.buffers[SYM]),1);self.assertEqual(self.detector.usable_bars(SYM),[])
        self.assertIsNone(self.detector.signal(self.tick(),{'status':'WATCH'}))
    def test_tick_freshness_and_staleness_reasons(self):
        view=self.ready();self.assertEqual(view['state'],'WAITING');self.assertIn('FRESH_REGULAR_TICK_NOT_READY',view['missing'])
        self.detector.ingest(self.tick(),DAY);self.clock+=timedelta(seconds=self.cfg.stale_seconds+1)
        self.assertIn('FRESH_REGULAR_TICK_NOT_READY',self.ready()['missing'])
    def test_preopen_radar_not_valid_open_evidence(self):
        self.engine.last_radar_at=DAY-timedelta(seconds=1);self.detector.ingest(self.tick(),DAY)
        self.assertIn('RADAR_PIPELINE_NOT_READY',self.ready()['missing'])
    def test_disconnect_and_stale_account_revoke_readiness(self):
        self.detector.ingest(self.tick(),DAY);self.assertEqual(self.ready()['state'],'OPEN_READY')
        self.engine.disconnect();self.assertIn('WEBSOCKET_REG_ACK_NOT_READY',self.ready()['missing'])
        self.engine.connected=True
        self.engine.last_account=time.monotonic()-self.cfg.reconcile_seconds*3-1
        self.assertIn('ACCOUNT_FRESH_NOT_READY',self.ready()['missing'])
    def test_runtime_callback_is_actual_buy_gate(self):
        self.engine.readiness_reason=lambda:'OPEN_READINESS_FRESH_REGULAR_TICK_NOT_READY'
        self.assertEqual(self.engine.new_entry_reason(SYM),'OPEN_READINESS_FRESH_REGULAR_TICK_NOT_READY')
        self.assertIsNone(self.engine.submit('BUY',SYM,1,10000,{'stop':9900}));self.assertEqual(self.broker.orders,[])
    def test_halt_or_unknown_never_ready(self):
        self.detector.ingest(self.tick(),DAY);self.engine.s['halt']='test'
        self.assertIn('SAFE_STATE_NOT_READY',self.ready()['missing']);self.engine.s['halt']=''
        self.engine.s['orders']['test']={'status':'UNKNOWN','symbol':SYM,'side':'BUY'}
        self.assertIn('SAFE_STATE_NOT_READY',self.ready()['missing'])
    def test_new_entry_0900_without_redundant_fixed_delay(self):
        self.assertEqual(SessionPlan().new_entry_start,'09:00:00');self.assertTrue(SessionPlan().is_entry_window(DAY))
        self.assertFalse(SessionPlan().is_entry_window(DAY-timedelta(microseconds=1)))
    def test_actual_engine_cutoff_flatten_support_end(self):
        self.assertEqual(self.engine.session_plan.effective_new_entry_end,'14:55:00')
        self.clock=DAY.replace(hour=14,minute=54,second=59);self.assertTrue(self.engine.entry_window())
        self.clock+=timedelta(seconds=1);self.assertFalse(self.engine.entry_window())
        self.clock=DAY.replace(hour=15,minute=10);self.assertTrue(self.engine.session_ending())
        self.assertTrue(control_supported(DAY.replace(hour=15,minute=19,second=59)))
        self.assertFalse(control_supported(DAY.replace(hour=15,minute=20)));self.assertFalse(supported(DAY.replace(hour=15,minute=20)))
    def test_strategy_buffer_and_minimum_ABC_bars_unchanged(self):
        self.assertEqual(self.cfg.baseline_seconds+self.cfg.acceleration_seconds,70)
        self.assertEqual(self.auto.box_min_bars,5)
        self.assertIsNone(self.detector.pullback([{'low':9900,'high':10000,'close':9990,'volume':100}]*3))
        tick=self.tick();self.detector.prior_high[SYM]=9990
        bars=[{'low':9900,'high':10000,'open':9990,'close':9990,'volume':100}]
        self.assertFalse(any(s['pattern']=='A' for s in self.detector.candidates(tick,bars)))
        self.assertTrue(any(s['pattern']=='A' for s in self.detector.candidates(tick,bars*2)))

class V13RunnerReadinessLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        atomic_json(self.root/'automation.json',asdict(AutomationConfig()));self.policy=Policy(self.root/'runtime/automation_policy.json')
        self.out=contextlib.redirect_stdout(io.StringIO());self.out.__enter__();self.addCleanup(self.out.__exit__,None,None,None);FakeRest.calls=[]
    def run_ready(self,stamp,with_tick):
        account=Mock();account.q=queue.Queue();account.request=threading.Event();radar=Mock();radar.q=queue.Queue();radar.cache={};radar.request=threading.Event()
        row={'code':SYM,'name':'합성','status':'WATCH','source_count':2,'resistance':11000}
        radar.q.put({'time':stamp.isoformat(),'rows':[row],'watch':[row],'changes':{'added':[SYM],'removed':[]},'error':None})
        frames=[{'trnm':'REG','return_code':0}]
        if with_tick:frames.append({'trnm':'REAL','data':[{'type':'0B','item':SYM,'values':{'20':stamp.strftime('%H%M%S'),'10':'10000','15':'100','13':'100','27':'10010','28':'9990','290':'2'}}]})
        pending=queue.Queue()
        for frame in frames:pending.put((stamp,frame))
        calls=[]
        def receive(**kwargs):
            calls.append(1)
            if len(calls)>5:raise AssertionError('조건이 충족됐는데 고정 대기 중입니다.')
            if not pending.empty():
                result=pending.get_nowait()
                if result[1]['trnm']=='REG':account.q.put({'snapshot':{'cash':20000,'equity':20000,'holdings':{},'orders':[]},'fills':[],'started':time.monotonic(),'error':None})
                return result
            raise queue.Empty
        feed=Mock();feed.q.get.side_effect=receive;feed.overflow.is_set.return_value=False
        with patch.object(runner,'Rest',FakeRest),patch.object(runner,'RadarWorker',return_value=radar),patch.object(runner,'AccountWorker',return_value=account),patch.object(runner,'Feed',return_value=feed),patch.object(runner,'Reporter',return_value=Mock()),patch.object(runner,'now',return_value=stamp):
            result=runner.run(Config(),self.root,readiness_check=True)
        self.assertLessEqual(len(calls),3);self.assertEqual(FakeRest.calls,[]);self.assertEqual(result['real_orders_sent'],0)
        return result,feed,account,radar
    def test_runner_readiness_finishes_on_evidence_not_fixed_delay(self):
        result,feed,account,radar=self.run_ready(DAY.replace(hour=8),False)
        self.assertTrue(result['passed']);self.assertEqual(result['readiness']['state'],'PREOPEN_READY');self.assertEqual(result['counts']['ticks'],0)
        feed.start.assert_called_once();account.start.assert_called_once();feed.close.assert_called_once()
        result2,*_=self.run_ready(DAY,True)
        self.assertTrue(result2['passed']);self.assertEqual(result2['readiness']['state'],'OPEN_READY');self.assertEqual(result2['counts']['ticks'],1)

if __name__=='__main__':unittest.main()
