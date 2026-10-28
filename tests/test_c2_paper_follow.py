"""Synthetic events and FakeBroker only; no broker access or LIVE mutation."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from core.settings import KST
from research.c2_validation import C2ObservedSession
from research.evidence_collector import JsonTail
from tests.test_c2_box_retest import settings
from tools.c2_paper_follow import SampledC2Session, follow, MIN_FREE_BYTES, _write_json
from tools.observe_paper import follow as follow_abc, set_idle_priority


class C2FollowerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'source'
        self.folder = self.root/'runtime/automation-production/trade-review'
        self.folder.mkdir(parents=True)
        self.output = Path(self.tmp.name)/'output'
        self.cfg, self.auto = settings()
        self.disk = SimpleNamespace(free=MIN_FREE_BYTES+1024**3)

    def test_throttle_preserves_processing_and_marks_missing_evaluation(self):
        obj = SampledC2Session.__new__(SampledC2Session)
        obj.last_evaluation = {}; obj.counts = Counter()
        stamp = datetime(2026,10,29,9,0,tzinfo=KST)
        def frame(offset):
            return SimpleNamespace(tick=SimpleNamespace(symbol='999990'),available_at=stamp+timedelta(milliseconds=offset))
        seen = []
        with patch.object(C2ObservedSession, '_step', lambda self, f, retry, evaluate=True: seen.append(evaluate)):
            obj._step(frame(0),False); obj._step(frame(500),False); obj._step(frame(1000),False)
        self.assertEqual(seen,[True,False,True])
        self.assertEqual(obj.counts['C2_THROTTLED_EVALUATION_UNKNOWN'],1)

    def test_diagnostic_status_retries_transient_windows_share_violation(self):
        status=self.root/'STATUS.json';original=os.replace
        attempts=[]
        def replace_once(source,dest):
            attempts.append(1)
            if len(attempts)==1:raise PermissionError('transient reader lock')
            return original(source,dest)
        with patch('core.util.os.replace',side_effect=replace_once),patch('core.util.time.sleep'):
            _write_json(status,{'status':'RUNNING'})
        self.assertEqual(len(attempts),2)
        self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['status'],'RUNNING')

    @unittest.skipUnless(os.name=='nt','Windows priority contract')
    def test_actual_windows_process_is_idle_priority(self):
        code=('import ctypes; from tools.observe_paper import set_idle_priority; '
              'set_idle_priority(); k=ctypes.WinDLL("kernel32",use_last_error=True); '
              'k.GetCurrentProcess.restype=ctypes.c_void_p; '
              'k.GetPriorityClass.argtypes=[ctypes.c_void_p]; k.GetPriorityClass.restype=ctypes.c_uint32; '
              'print(k.GetPriorityClass(k.GetCurrentProcess()))')
        p=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(int(p.stdout.strip()),0x40)

    @unittest.skipUnless(os.name=='nt','Windows priority contract')
    def test_windows_priority_failure_is_not_silently_ignored(self):
        kernel=MagicMock();kernel.SetPriorityClass.return_value=0
        with patch('ctypes.WinDLL',return_value=kernel):
            with self.assertRaisesRegex(RuntimeError,'LOW_PRIORITY_SETUP_FAILED'):
                set_idle_priority()

    def test_existing_eof_new_row_fake_only_and_no_network(self):
        name = 'trade-review-'+datetime.now(KST).strftime('%Y%m%d')+'-001.jsonl'
        source = self.folder/name
        old = {'event':'STREAM_STATE','kind':'DISCONNECTED','at':datetime.now(KST).isoformat()}
        source.write_text(json.dumps(old)+'\n',encoding='utf-8')
        before = source.read_bytes()
        def append():
            time.sleep(.3)
            row = {'event':'STREAM_STATE','kind':'RECONNECTING','at':datetime.now(KST).isoformat()}
            with source.open('a',encoding='utf-8') as f: f.write(json.dumps(row)+'\n')
        writer=threading.Thread(target=append);writer.start()
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('tools.c2_paper_follow.shutil.disk_usage',return_value=self.disk):
            result = follow(self.root,self.output,self.cfg,self.auto,70420,1,poll=.1)
        writer.join()
        self.assertEqual(result['observer_rows'],1)
        self.assertEqual(result['source_counts']['INPUT:STREAM_STATE'],1)
        self.assertFalse(result['actual_orders'])
        self.assertFalse(result['live_activation_allowed'])
        self.assertEqual(result['opportunity_completeness'],'UNKNOWN_DUE_TO_SAMPLING_AND_SOURCE_GAPS')
        self.assertTrue(source.read_bytes().startswith(before))
        self.assertTrue((self.output/'RESULT.json').exists())
        self.assertEqual(json.loads((self.output/'STATUS.json').read_text())['status'],'FINISHED')

    def test_no_source_is_unknown_and_no_fake_order(self):
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('tools.c2_paper_follow.shutil.disk_usage',return_value=self.disk):
            result = follow(self.root,self.output,self.cfg,self.auto,70420,1,poll=.1)
        self.assertEqual(result['observer_rows'],0)
        self.assertFalse(result['actual_orders'])
        self.assertEqual(result['orders_submitted'],0)
        self.assertEqual(result['measured_live_orderability'],'UNKNOWN_UNLESS_EVERY_OBSERVED_CONTEXT_GATE_IS_KNOWN')

    def test_more_than_two_new_rotation_files_exposes_missing_bytes(self):
        prefix = 'trade-review-'+datetime.now(KST).strftime('%Y%m%d')
        row = {'event':'STREAM_STATE','kind':'DISCONNECTED','at':datetime.now(KST).isoformat()}
        def rotate():
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:
                    if json.loads((self.output/'STATUS.json').read_text())['status']=='RUNNING':break
                except (OSError,ValueError,KeyError):
                    pass
                time.sleep(.01)
            time.sleep(.3)
            for i in range(3):
                (self.folder/(prefix+f'-{i:03}.jsonl')).write_text(json.dumps(row)+'\n',encoding='utf-8')
        writer = threading.Thread(target=rotate);writer.start()
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('tools.c2_paper_follow.shutil.disk_usage',return_value=self.disk):
            result=follow(self.root,self.output,self.cfg,self.auto,70420,1,poll=.1)
        writer.join()
        self.assertGreater(result['source_counts']['OBSERVER_UNSEEN_ROTATION_GAP_BYTES_UNKNOWN'],0)
        self.assertEqual(result['observer_rows'],2)

    def test_abc_rapid_rotation_also_reports_unseen_file(self):
        prefix='trade-review-'+datetime.now(KST).strftime('%Y%m%d')
        (self.folder/(prefix+'-000.jsonl')).touch()
        self.output.mkdir()
        ready=threading.Event()
        class SignallingTail(JsonTail):
            def read(self,*args,**kwargs):
                ready.set()
                return super().read(*args,**kwargs)
        def rotate():
            if not ready.wait(5):return
            time.sleep(.05)
            for i in range(1,4):
                row={'event':'STREAM_STATE','kind':'DISCONNECTED','at':datetime.now(KST).isoformat()}
                (self.folder/(prefix+f'-{i:03}.jsonl')).write_text(json.dumps(row)+'\n',encoding='utf-8')
        writer=threading.Thread(target=rotate);writer.start()
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('tools.observe_paper.JsonTail',SignallingTail):
            result=follow_abc(self.root,self.output,self.cfg,self.auto,70420,1,poll=.2)
        writer.join()
        self.assertGreater(result['source_counts']['OBSERVER_UNSEEN_ROTATION_GAP_BYTES_UNKNOWN'],0)

    def test_c2_reads_known_daily_reference_only_after_watch_receipt(self):
        symbol='999990';at=datetime.now(KST)
        source=self.folder/('trade-review-'+at.strftime('%Y%m%d')+'-001.jsonl');source.touch()
        daily=self.root/'data/raw'/(symbol+'_daily.csv');daily.parent.mkdir(parents=True);daily.write_text('source',encoding='utf-8')
        bars=[dict(datetime=(at.date()-timedelta(days=80-i)).isoformat(),
                   open=9000,high=9500,low=8800,close=9000,volume=10000) for i in range(60)]
        def produce():
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:
                    if json.loads((self.output/'STATUS.json').read_text())['status']=='RUNNING':break
                except (OSError,ValueError,KeyError):pass
                time.sleep(.01)
            time.sleep(.3)
            row={'event':'WATCH_METADATA','symbol':symbol,'at':datetime.now(KST).isoformat(),
                 'watch':{'status':'WATCH','source_count':2}}
            with source.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
        writer=threading.Thread(target=produce);writer.start()
        cfg=replace(self.cfg,history_bars=60,min_history_bars=60)
        with patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN')), \
             patch('tools.c2_paper_follow.shutil.disk_usage',return_value=self.disk), \
             patch('candidate_builder.bars_from_csv',return_value=bars):
            result=follow(self.root,self.output,cfg,self.auto,70420,1,poll=.1)
        writer.join()
        self.assertEqual(result['observer_rows'],1)
        self.assertEqual(len(result['reference_observations']),1)
        self.assertIn('NOT_HISTORICAL',result['reference_observations'][0]['basis'])
        self.assertTrue((self.output/'reference-inputs'/result['reference_observations'][0]['snapshot']).exists())

    def test_source_root_output_and_storage_guard_rejected(self):
        with patch('tools.c2_paper_follow.shutil.disk_usage',return_value=self.disk):
            with self.assertRaisesRegex(ValueError,'OUTPUT_MUST_BE_NEW'):
                follow(self.root,self.root/'bad',self.cfg,self.auto,70420,1)
        with patch('tools.c2_paper_follow.shutil.disk_usage',return_value=SimpleNamespace(free=MIN_FREE_BYTES-1)):
            with self.assertRaisesRegex(ValueError,'32GiB'):
                follow(self.root,self.output,self.cfg,self.auto,70420,1)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
