"""Real runner handoff/context paths; isolated reference-cache durability tests."""
import ast,importlib.util,json,os,queue,tempfile,threading,time,unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation import runner as installed_runner
from automation.price_context import PriceContext
from core.settings import KST
from tests import test_r6_delivery_fairness as fairness

runner=installed_runner
if os.environ.get('R6_CONTEXT_RUNNER_FILE'):
    spec=importlib.util.spec_from_file_location('before_context_runner',os.environ['R6_CONTEXT_RUNNER_FILE'])
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

AT=datetime(2026,10,21,10,30,tzinfo=KST)
SYM='999990';OTHER='999991'

def detector():
    return SimpleNamespace(_state_lock=threading.RLock(),price_context=PriceContext(),history={},buffers={},observation_start={})

def packet(symbol=SYM,at=AT):
    return {'symbol':symbol,'venue':'KRX','received_at':at.isoformat(),'source':'REST_REFERENCE',
            'bars':[{'time':AT-timedelta(minutes=2),'open':1000.,'high':1010.,'low':995.,'close':1005.,
                     'volume':100,'venue':'KRX','available_at':at.isoformat()}]}


class ActualRunnerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.main=detector();self.shadow_detector=detector()
        self.shadow=SimpleNamespace(observer=SimpleNamespace(detector=self.shadow_detector),runtime_view=lambda:{'evaluations':{}})
        self.engine=SimpleNamespace(event=lambda *a,**kw:None)
        self.worker=SimpleNamespace(q=queue.Queue(),set_targets=lambda t:None)
        self.checkpoint=None

    def consume(self,packets,write=None):
        for item in packets:self.worker.q.put(item)
        if hasattr(runner,'PriceContextCheckpoint'):
            self.checkpoint=runner.PriceContextCheckpoint(self.root/'runtime/price_context_cache.json')
            self.addCleanup(self.checkpoint.close)
        # Execute the actual context_worker block from run(), including its
        # real helper call after the fix. No alternate patch helper is used.
        tree=ast.parse(Path(runner.__file__).read_text(encoding='utf-8-sig'))
        block=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
            and isinstance(n.test,ast.Name) and n.test.id=='context_worker'
            and n.body and isinstance(n.body[0],ast.ImportFrom) and n.body[0].module=='automation.intraday_context')
        code=compile(ast.fix_missing_locations(ast.Module(body=[block],type_ignores=[])),'<actual runner context block>','exec')
        with patch('automation.intraday_context.context_targets',return_value={SYM:'KRX',OTHER:'KRX'}):
            namespace={**runner.__dict__,'context_worker':self.worker,'context_checkpoint':self.checkpoint,
                'detector':self.main,'shadow':self.shadow,'engine':self.engine,'lookup':{OTHER:{}},
                'effective':SimpleNamespace(after_default_venue='SOR'),'root':self.root,'in_session':True,
                'now':lambda:AT,'after_supported':lambda t:False}
            if write is not None:namespace['atomic_json']=write
            exec(code,namespace)

    def test_spent_history_deadline_preserves_fresh_evaluation_slice(self):
        fixture=fairness.DeliveryFairnessTests();fixture.setUp()
        tick=fixture.tick();fixture.enqueue(tick);seen=[];clock=[0.]
        real_ingest=fixture.detector.ingest
        def ingest(*args):
            result=real_ingest(*args);clock[0]=.016;return result
        with patch.object(fixture.detector,'ingest',side_effect=ingest),patch.object(runner.time,'monotonic',side_effect=lambda:clock[0]):
            runner.prewarm_handoff(fixture.engine,fixture.detector,fixture.feed,{fairness.SYM},set(),set(),0,
                on_opportunity=lambda t,r:seen.append(t))
        self.assertEqual(seen,[tick])

    def test_shadow_reference_reaches_both_contexts_without_tick_warmup(self):
        self.consume([packet()])
        self.assertIn(SYM,self.main.price_context.rows)
        self.assertIn(SYM,self.shadow_detector.price_context.rows)
        for target in (self.main,self.shadow_detector):
            row=target.price_context.rows[SYM]
            self.assertEqual(row['history_received_at'],AT.isoformat())
            self.assertEqual(row['bars'][0]['observation_source'],'REST_REFERENCE')
            self.assertEqual(row['bars'][0]['available_at'],AT.isoformat())
            self.assertEqual((target.history,target.buffers,target.observation_start),({},{},{}))

    def test_context_batch_has_one_off_thread_fsync_checkpoint(self):
        calls=[];real_write=runner.atomic_json;main_thread=threading.get_ident()
        def write(path,data,**kw):
            calls.append((threading.get_ident(),kw.get('fsync',True)))
            return real_write(path,data,**kw)
        with patch.object(runner,'atomic_json',side_effect=write):
            self.consume([packet(),packet(OTHER)],write=write)
            if self.checkpoint:self.assertTrue(self.checkpoint.close())
        self.assertEqual(len(calls),1)
        self.assertNotEqual(calls[0][0],main_thread)
        self.assertTrue(calls[0][1])
        saved=json.loads((self.root/'runtime/price_context_cache.json').read_text(encoding='utf-8'))
        self.assertEqual(set(saved['rows']),{SYM,OTHER})

    def test_future_reference_packet_is_not_evidence(self):
        self.consume([packet(at=AT+timedelta(seconds=1))])
        self.assertEqual(self.main.price_context.rows,{})
        self.assertEqual(self.shadow_detector.price_context.rows,{})

    def test_cache_reload_keeps_rest_availability_and_source(self):
        self.consume([packet()]);self.assertTrue(self.checkpoint.close())
        saved=json.loads((self.root/'runtime/price_context_cache.json').read_text(encoding='utf-8'))
        recovered=PriceContext()
        self.assertEqual(runner.restore_reference_cache(recovered,saved,AT+timedelta(seconds=1)),1)
        self.assertEqual(recovered.rows[SYM]['context_venue'],'KRX')
        self.assertEqual(recovered.rows[SYM]['history_received_at'],AT.isoformat())
        self.assertEqual(recovered.reference_bars(SYM,AT-timedelta(seconds=1),'KRX'),[])
        bar=recovered.reference_bars(SYM,AT,'KRX')[0]
        self.assertEqual(bar['observation_source'],'REST_REFERENCE')
        self.assertEqual(bar['available_at'],AT.isoformat())

    def test_cache_with_future_or_unknown_rest_availability_is_not_restored(self):
        reference=packet()['bars'][0]
        base={'day':AT.date().isoformat(),'rows':{SYM:{'context_venue':'KRX',
              'history_received_at':(AT+timedelta(seconds=1)).isoformat(),
              'bars':[{**reference,'time':reference['time'].isoformat(),'observation_source':'REST_REFERENCE'}]}}}
        self.assertEqual(runner.restore_reference_cache(PriceContext(),base,AT),0)
        base['rows'][SYM].pop('history_received_at');base['rows'][SYM]['bars'][0].pop('available_at')
        self.assertEqual(runner.restore_reference_cache(PriceContext(),base,AT),0)

    def test_legacy_per_bar_availability_sets_row_causal_boundary(self):
        bar=packet()['bars'][0]
        saved={'day':AT.date().isoformat(),'rows':{SYM:{'context_venue':'KRX',
            'bars':[{**bar,'time':bar['time'].isoformat(),'observation_source':'REST_REFERENCE'}]}}}
        restored=PriceContext()
        self.assertEqual(runner.restore_reference_cache(restored,saved,AT),1)
        self.assertEqual(restored.rows[SYM]['history_received_at'],AT.isoformat())
        self.assertEqual(restored.reference_bars(SYM,AT-timedelta(seconds=1),'KRX'),[])
        self.assertEqual(len(restored.reference_bars(SYM,AT,'KRX')),1)

    def test_checkpoint_snapshot_does_not_alias_live_peak_mutations(self):
        self.main.price_context.merge_history(SYM,packet()['bars'],AT,'KRX')
        peak={'price':1010.,'time':(AT-timedelta(minutes=2)).isoformat(),'confirmed_at':AT.isoformat()}
        self.main.price_context.rows[SYM]['peaks'].append(peak)
        frozen=runner.snapshot_reference_cache(self.main)
        peak['cleared_at']=AT.isoformat()
        self.main.price_context.rows[SYM]['bars'][0]['close']=1006.
        self.assertNotIn('cleared_at',frozen['rows'][SYM]['peaks'][0])
        self.assertEqual(frozen['rows'][SYM]['bars'][0]['close'],1005.)


@unittest.skipUnless(hasattr(runner,'PriceContextCheckpoint'),'Checkpoint class is post-fix implementation')
class CheckpointDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'price_context_cache.json'

    def test_one_pending_snapshot_and_clean_shutdown_flush_latest(self):
        entered=threading.Event();release=threading.Event();calls=[];real=runner.atomic_json
        def blocked(path,data,**kw):
            calls.append(data['generation'])
            if len(calls)==1:entered.set();release.wait(2)
            self.assertTrue(kw['fsync']);real(path,data,**kw)
        with patch.object(runner,'atomic_json',side_effect=blocked):
            writer=runner.PriceContextCheckpoint(self.path)
            try:
                writer.submit({'generation':0});self.assertTrue(entered.wait(1))
                for n in range(1,50):writer.submit({'generation':n})
                self.assertEqual(writer.snapshot()['pending'],1)
                release.set();self.assertTrue(writer.close())
            finally:release.set();writer.close()
        self.assertEqual(json.loads(self.path.read_text())['generation'],49)
        self.assertEqual(calls,[0,49])

    def test_failed_atomic_save_preserves_good_cache_and_close_retries(self):
        self.path.write_text('{"generation":0}',encoding='utf-8')
        failed=threading.Event();real=runner.atomic_json;calls=[]
        def unreliable(path,data,**kw):
            calls.append(kw['fsync'])
            if len(calls)==1:failed.set();raise PermissionError('synthetic cache sharing failure')
            real(path,data,**kw)
        with patch.object(runner,'atomic_json',side_effect=unreliable):
            writer=runner.PriceContextCheckpoint(self.path)
            try:
                writer.submit({'generation':1});self.assertTrue(failed.wait(1))
                with writer.condition:writer.condition.wait_for(lambda:not writer.inflight,timeout=1)
                self.assertEqual(json.loads(self.path.read_text())['generation'],0)
                self.assertEqual(writer.snapshot()['errors'],1)
                self.assertTrue(writer.close())
            finally:writer.close()
        self.assertEqual(calls,[True,True])
        self.assertEqual(json.loads(self.path.read_text())['generation'],1)

    def test_shutdown_timeout_is_bounded_and_reports_incomplete(self):
        entered=threading.Event();release=threading.Event();real=runner.atomic_json
        def blocked(path,data,**kw):entered.set();release.wait(2);real(path,data,**kw)
        with patch.object(runner,'atomic_json',side_effect=blocked):
            writer=runner.PriceContextCheckpoint(self.path)
            try:
                writer.submit({'generation':1});self.assertTrue(entered.wait(1))
                started=time.monotonic();self.assertFalse(writer.close(timeout=.01))
                self.assertLess(time.monotonic()-started,.2)
            finally:release.set();writer.close()


if __name__=='__main__':unittest.main()
