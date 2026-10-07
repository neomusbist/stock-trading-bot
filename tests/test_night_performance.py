"""Lossless logs, snapshot safety and threaded offline hot-path regression."""
import contextlib,io,json,math,os,tempfile,threading,time,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock,patch
from core.event_log import append_jsonl,rotate_if_large,HumanConsole
from core.util import atomic_json,read_json_retry
from local_diagnostics import redact
from automation.decisions import safe_values
from automation.policy import Policy
from operator_service import tail_events
from tests import test_audit_followup as fixtures
from tests import test_gui as gui_fixtures

class NightPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def test_lossless_rotation_and_tail_read_across_archives(self):
        path=self.root/'events.jsonl'
        for i in range(24):append_jsonl(path,{'seq':i,'reason':'한글 근거'},max_bytes=100)
        rows=tail_events(path,100)
        self.assertEqual([r['seq'] for r in rows],list(range(24)))
        self.assertGreater(len(list((self.root/'archive').iterdir())),1)
    def test_rotation_permission_denied_is_bounded_and_preserves_old_bytes(self):
        path=self.root/'events.jsonl';path.write_text('old audit\n',encoding='utf-8');old=path.read_bytes()
        with patch('core.event_log.os.replace',side_effect=PermissionError(13,'synthetic',str(path))) as rename,patch('core.event_log.time.sleep'):
            with self.assertRaises(PermissionError):append_jsonl(path,{'new':True},max_bytes=1)
        self.assertEqual(rename.call_count,6);self.assertEqual(path.read_bytes(),old)
    def test_rotation_transient_denial_retries_without_loss(self):
        path=self.root/'events.jsonl';append_jsonl(path,{'seq':0});original=os.replace
        with patch('core.event_log.os.replace',side_effect=[PermissionError(),None]) as rename,patch('core.event_log.time.sleep'):
            def attempt(src,dst):
                if rename.call_count==1:raise PermissionError()
                return original(src,dst)
            rename.side_effect=attempt
            append_jsonl(path,{'seq':1},max_bytes=1)
        self.assertEqual([r['seq'] for r in tail_events(path)],[0,1])
    def test_console_startup_rotation_keeps_exact_old_log(self):
        path=self.root/'operator_console.txt';payload=('기존 감사 기록\n'*8).encode('utf-8');path.write_bytes(payload)
        archive=rotate_if_large(path,max_bytes=1)
        self.assertEqual(archive.read_bytes(),payload);self.assertFalse(path.exists())
        self.assertIsNone(rotate_if_large(path))
    def test_machine_records_unthrottled_human_gate_changes_immediate(self):
        path=self.root/'events.jsonl';console=HumanConsole();out=io.StringIO()
        row={'event':'ENTRY_EVAL','symbol':'999990','decision':'NO_BUY','primary_reason_code':'WAIT','gates':{'warmup':{'result':'FAIL'}}}
        with contextlib.redirect_stdout(out):
            for stamp in (0,1,2):append_jsonl(path,row);console.emit(row,stamp)
            ready={**row,'gates':{'warmup':{'result':'PASS'}}};append_jsonl(path,ready);console.emit(ready,3)
            changed={**ready,'primary_reason_code':'RISK_ZERO'};append_jsonl(path,changed);console.emit(changed,4)
            console.emit({'event':'ORDER_UNKNOWN','symbol':'999990'},4.1)
            console.emit({'event':'STOP_RAISED','symbol':'999990','stop':1010},4.2)
        self.assertEqual(len(tail_events(path)),5);self.assertEqual(len(out.getvalue().splitlines()),5)
    def test_redaction_has_no_cross_snapshot_secret_cache(self):
        obj={'authorization':'private','items':['Bearer abc123','KEY_ONE','KEY_ONE']}
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'KEY_ONE','KIWOOM_APP_SECRET':''}):
            value=redact(obj);self.assertEqual(value['authorization'],'[REDACTED]');self.assertEqual(value['items'],['Bearer [REDACTED]','[REDACTED]','[REDACTED]'])
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'KEY_TWO','KIWOOM_APP_SECRET':''}):
            self.assertEqual(redact(['KEY_TWO','KEY_ONE']),['[REDACTED]','KEY_ONE'])
    def test_serialization_memo_keeps_values_and_does_not_cache_next_call(self):
        shared={'n':math.nan,'p':math.inf,'arr':(1,False)};value=safe_values({'a':shared,'b':shared})
        self.assertEqual(value,{'a':{'n':'NON_FINITE:nan','p':'NON_FINITE:inf','arr':[1,False]},'b':{'n':'NON_FINITE:nan','p':'NON_FINITE:inf','arr':[1,False]}})
        shared['n']=10;self.assertEqual(safe_values(shared)['n'],10)
    def test_cached_windows_api_never_caches_kill_or_revocation_content(self):
        path=self.root/'policy.json';atomic_json(path,{'mode':'LIVE','live_authorized':True})
        self.assertTrue(read_json_retry(path)['live_authorized'])
        atomic_json(path,{'mode':'KILL','live_authorized':False})
        self.assertEqual(read_json_retry(path),{'mode':'KILL','live_authorized':False})
        p=Policy(path);p.refresh();self.assertEqual(p.mode,'KILL')
    def test_repeated_menu_snapshot_concurrent_atomic_writer_is_readonly(self):
        f=gui_fixtures.UserServiceTests();f.setUp();self.addCleanup(f.doCleanups)
        path=f.root/'runtime/automation-production/heartbeat.json';atomic_json(path,{'time':f.clock[0].isoformat(),'seq':0})
        errors=[];done=threading.Event()
        def writer():
            try:
                for n in range(60):atomic_json(path,{'time':f.clock[0].isoformat(),'seq':n})
            except Exception as e:errors.append(e)
            finally:done.set()
        worker=threading.Thread(target=writer);worker.start()
        for _ in range(15):f.s.snapshot()
        worker.join(10);self.assertTrue(done.is_set());self.assertEqual(errors,[]);self.assertEqual(read_json_retry(path)['seq'],59)
        self.assertEqual(f.g.calls,[])
    def test_gui_snapshot_reads_event_tail_once(self):
        f=gui_fixtures.UserServiceTests();f.setUp();self.addCleanup(f.doCleanups)
        with patch('operator_ui_service.tail_events',wraps=tail_events) as ui_tail,patch('operator_service.tail_events',side_effect=AssertionError('duplicate tail')):
            f.s.snapshot()
        self.assertEqual(ui_tail.call_count,1)
    def test_slow_radar_rest_does_not_block_already_watch_tick(self):
        f=fixtures.SetupReachabilityTests();f.setUp();self.addCleanup(f.doCleanups)
        from automation.radar import RadarWorker
        from automation.runner import process_message
        started=threading.Event();release=threading.Event();rest=Mock();rest.profile={}
        def slow_collect(_):started.set();release.wait(5);return []
        worker=RadarWorker(f.cfg,f.auto,f.policy,rest,f.root)
        detector,tick,watch=f.build('A');f.policy.command('ARMED')
        frame={'trnm':'REAL','data':[{'type':'0B','item':'999990','values':{'20':f.clock[0].strftime('%H%M%S'),'10':str(tick.price),'15':str(tick.volume),'13':str(tick.cumulative),'27':str(tick.ask),'28':str(tick.bid),'290':'2'}}]}
        with patch('automation.radar.collect',side_effect=slow_collect),patch('automation.runner.now',side_effect=lambda:f.clock[0]):
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                process_message(f.e,detector,frame,f.clock[0],{'999990'},{'999990':watch},Mock(cache={}),{},Mock(),True)
                self.assertFalse(release.is_set());self.assertEqual(detector.last['999990'].time,tick.time)
                self.assertIn('999990',detector.last_eval);self.assertEqual(f.broker.orders,[])
            finally:worker.stop.set();release.set();worker.request.set();worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())

if __name__=='__main__':unittest.main()
