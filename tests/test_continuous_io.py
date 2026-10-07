"""Offline Windows lifecycle and atomic I/O regressions. No broker or feed."""
import json,os,sys,time,tempfile,threading,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timedelta
from core.settings import ROOT,KST
from core.util import atomic_json,exception_details,read_json_retry
from operator_process import busy
from operator_service import read_json
from tests import test_continuous as fixtures

class AtomicIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.json'
    def test_unique_temps_parallel_writers_and_reader(self):
        atomic_json(self.path,{'n':0}); errors=[]
        def writer(n):
            try:
                for i in range(30): atomic_json(self.path,{'n':n,'i':i,'payload':'x'*2000})
            except Exception as e: errors.append(exception_details(e))
        threads=[threading.Thread(target=writer,args=(n,)) for n in range(3)]
        for t in threads: t.start()
        try:
            while any(t.is_alive() for t in threads): read_json_retry(self.path)
        finally:
            for t in threads: t.join()
        self.assertEqual(errors,[]); self.assertEqual(list(self.path.parent.glob('*.tmp')),[])
    def test_permission_retry_is_bounded_and_preserves_target(self):
        atomic_json(self.path,{'old':1})
        error=PermissionError(13,'denied',str(self.path))
        with patch('core.util.os.replace',side_effect=error) as replace,patch('core.util.time.sleep'):
            with self.assertRaises(PermissionError): atomic_json(self.path,{'new':1})
        self.assertEqual(replace.call_count,6); self.assertEqual(read_json(self.path),{'old':1})
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])
    def test_transient_permission_recovers(self):
        original=os.replace; calls=[]
        def replace(src,dst):
            calls.append(str(src))
            if len(calls)<3: raise PermissionError(13,'sharing violation',str(dst))
            return original(src,dst)
        with patch('core.util.os.replace',side_effect=replace): atomic_json(self.path,{'ok':True})
        self.assertEqual(read_json(self.path),{'ok':True}); self.assertEqual(len(calls),3)
    def test_error_details_preserve_path_message_and_traceback(self):
        from automation.runner import error_record
        try: raise PermissionError(13,'actual message',str(self.path))
        except PermissionError as e: record=error_record(e)
        self.assertEqual(record['filename'],str(self.path)); self.assertIn('actual message',record['exception_message'])
        self.assertIn('test_error_details',record['traceback']); self.assertEqual(record['error'],'PermissionError')
    def test_status_probe_does_not_create_or_change_lock_files(self):
        root=self.path.parent
        self.assertFalse(busy(root)); self.assertFalse((root/'runtime').exists())
        (root/'runtime').mkdir(); lock=root/'runtime/process.lock'; lock.write_bytes(b'0')
        before=lock.stat().st_mtime_ns
        for _ in range(100): self.assertFalse(busy(root))
        self.assertEqual(lock.read_bytes(),b'0'); self.assertEqual(lock.stat().st_mtime_ns,before)
    def test_windows_open_reader_blocks_replace_then_retry_recovers(self):
        atomic_json(self.path,{'n':0}); errors=[]; started=threading.Event()
        def writer():
            started.set()
            try: atomic_json(self.path,{'n':1})
            except Exception as e: errors.append(e)
        with self.path.open('rb') as reader:
            # Reproduce the Windows no-delete-share mechanism, not a claim
            # about the historical error whose traceback was not recorded.
            other=self.path.with_suffix('.probe'); other.write_text('{}')
            if os.name=='nt':
                with self.assertRaises(PermissionError): os.replace(other,self.path)
            if other.exists(): other.unlink()
            t=threading.Thread(target=writer); t.start(); started.wait(2); time.sleep(.07)
        t.join(3); self.assertFalse(t.is_alive()); self.assertEqual(errors,[])
        self.assertEqual(read_json(self.path),{'n':1})

class LifecycleIOTests(unittest.TestCase):
    setUp=fixtures.ContinuousTests.setUp
    record=fixtures.ContinuousTests.record
    def test_child_heartbeat_market_and_repeated_menu_status(self):
        stop=self.root/'end_offline_stub'
        script='''import sys,time
from pathlib import Path
from datetime import datetime,timedelta
from core.settings import KST
from core.util import ProcessLock,atomic_json
from automation.policy import Policy
import operator_runner
root=Path(sys.argv[1]); base=time.monotonic()
def clock(): return datetime(2026,10,1,9,30,tzinfo=KST)+timedelta(seconds=time.monotonic()-base)
def stub():
    with ProcessLock(root/'runtime/supervisor.lock'),ProcessLock(root/'runtime/process.lock'):
        while not (root/'end_offline_stub').exists():
            atomic_json(root/'runtime/automation-production/heartbeat.json',{'time':clock().isoformat(),'connected':True,'account_ok':True})
            time.sleep(.03)
        Policy(root/'runtime/automation_policy.json').command('STOP')
    return 0
operator_runner.run(root,clock,stub,token='offline-heartbeat')
'''
        child=subprocess.Popen([sys.executable,'-X','utf8','-c',script,str(self.root)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        heartbeats=set(); market=set(); checks=0
        try:
            deadline=time.monotonic()+10
            while not (self.root/'runtime/operator_runner.json').exists() and time.monotonic()<deadline: time.sleep(.02)
            start=time.monotonic()
            while time.monotonic()-start<2:
                self.clock[0]=datetime(2026,10,1,9,30,tzinfo=KST)+timedelta(seconds=1+time.monotonic()-start)
                status=self.s.public_status(); info=status['process']
                self.assertTrue(info['running']); self.assertTrue(info['process_heartbeat_fresh'])
                heartbeats.add(info['heartbeat']); self.assertFalse(status['trading_on'])
                hb_m=read_json(self.root/'runtime/automation-production/heartbeat.json',{})
                if hb_m.get('time'): market.add(hb_m['time'])
                self.assertEqual(self.s.start_continuous()['verdict'],'ALREADY_RUNNING'); checks+=1
                time.sleep(.01)
        finally:
            stop.touch(); _,err=child.communicate(timeout=10)
        self.assertEqual(child.returncode,0,err.decode()); self.assertEqual(err,b'')
        self.assertGreaterEqual(len(heartbeats),3); self.assertGreaterEqual(len(market),3); self.assertGreater(checks,5)
        self.assertFalse(self.s.running()); record=read_json(self.root/'runtime/operator_runner.json')
        record['status']='RUNNING'; atomic_json(self.root/'runtime/operator_runner.json',record)
        self.clock[0]+=timedelta(seconds=10)
        self.assertTrue(self.s.process_info()['stale']); self.assertFalse(self.s.process_info()['process_heartbeat_fresh'])

if __name__=='__main__': unittest.main()
