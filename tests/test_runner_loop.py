"""기본 실행 루프를 가짜 조회/수신으로 검증. 실제 연결을 만들지 않는다."""
import contextlib
import io
import queue
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST,ROOT
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation import runner

class FakeRest:
    identity='runner-offline-test'
    calls=[]
    def __init__(self,cfg,auth_session=None): self.cfg=cfg; self.auth_session=auth_session or {}
    def get_token(self): self.calls.append('auth'); raise AssertionError('real network')
    def submit(self,*a): self.calls.append('order'); raise AssertionError('real order')
    def cancel(self,*a): self.calls.append('cancel'); raise AssertionError('unexpected cancel')

class RunnerLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); atomic_json(self.root/'automation.json',asdict(AutomationConfig()))
        self.policy=Policy(self.root/'runtime/automation_policy.json'); FakeRest.calls=[]
        self.out=contextlib.redirect_stdout(io.StringIO()); self.out.__enter__(); self.addCleanup(self.out.__exit__,None,None,None)

    def test_default_after_hours_waits_without_starting_any_data_worker(self):
        radar=Mock(); account=Mock(); feed=Mock(); reporter=Mock()
        def stop_without_network(*a): self.policy.command('STOP')
        with patch.object(runner,'Rest',FakeRest),patch.object(runner,'RadarWorker',return_value=radar),\
             patch.object(runner,'AccountWorker',return_value=account),patch.object(runner,'Feed',return_value=feed),\
             patch.object(runner,'Reporter',return_value=reporter),\
             patch.object(runner,'now',return_value=datetime(2026,9,30,20,40,tzinfo=KST)),\
             patch.object(runner.time,'sleep',side_effect=stop_without_network):
            result=runner.run(Config(),self.root)
        radar.start.assert_not_called(); account.start.assert_not_called(); feed.start.assert_not_called()
        self.assertEqual(FakeRest.calls,[]); self.assertEqual(result['classification'],'OUT_OF_SUPPORTED_SESSION')
        self.assertIsNone(result['passed']); self.assertEqual(result['real_orders_sent'],0)

    def test_armed_loop_account_radar_tick_then_stop(self):
        stamp=datetime(2026,9,30,14,40,tzinfo=KST)
        account=Mock(); account.q=queue.Queue(); account.request=threading.Event()
        account.q.put({'snapshot':{'cash':9000000,'equity':100000000,'holdings':{},'orders':[]},
                       'fills':[],'started':time.monotonic(),'error':None})
        radar=Mock(); radar.q=queue.Queue(); radar.cache={}
        row={'code':'999990','name':'가짜 후보','status':'WATCH','resistance':11000}
        radar.q.put({'time':stamp.isoformat(),'rows':[row],'watch':[row],
                     'changes':{'added':['999990'],'removed':[]},'error':None})
        frames=[{'trnm':'REG','return_code':0},{'trnm':'REAL','data':[{'type':'0s','values':{'215':'3'}}]},
                {'trnm':'REAL','data':[{'type':'0B','item':'999990','values':{'20':'144000','10':'10000',
                    '15':'100','13':'100','27':'10010','28':'9990','290':'2'}}]}]
        pending=queue.Queue()
        for frame in frames: pending.put((stamp,frame))
        def receive(**kw):
            if pending.empty(): self.policy.command('STOP'); raise queue.Empty
            return pending.get_nowait()
        feed=Mock(); feed.q.get.side_effect=receive; feed.overflow.is_set.return_value=False
        with patch.object(runner,'Rest',FakeRest),patch.object(runner,'RadarWorker',return_value=radar),\
             patch.object(runner,'AccountWorker',return_value=account),patch.object(runner,'Feed',return_value=feed),\
             patch.object(runner,'Reporter',return_value=Mock()),patch.object(runner,'now',return_value=stamp):
            result=runner.run(Config(),self.root)
        self.assertEqual(result['counts']['account_checks'],1); self.assertEqual(result['counts']['radar_cycles'],1)
        self.assertEqual(result['counts']['ticks'],1); self.assertEqual(result['real_orders_sent'],0)
        self.assertEqual(FakeRest.calls,[]); self.assertEqual(result['approved_cap'],0)
        feed.update_symbols.assert_called_with(['999990']); self.assertTrue(result['passed'])

if __name__=='__main__': unittest.main()
