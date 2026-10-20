"""Reaction-path regressions: synthetic ticks and local metadata, no broker orders."""
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.observation_worker import ObservationWorker
from automation.shadow_observation import ShadowObserver

START=datetime(2026,10,8,13,0,tzinfo=KST)
SYM='999990'

class ObservationReactionTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=AutomationConfig();self.costs=Costs(.00015,.00015,.002,.002,.003)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def detector(self):
        d=PatternDetector(self.cfg,self.auto,self.costs);d.continuous_observation=True;return d
    def tick(self,sec,cumulative):
        return Tick(SYM,START+timedelta(seconds=sec),10000,100,cumulative,10010,9990)
    def warm(self,d,start=0,end=125,per_second=5):
        for i in range(int(start*per_second),int(end*per_second)+1):
            t=self.tick(i/per_second,100*(i+1));self.assertTrue(d.ingest(t,t.time))
    def row(self,symbol=SYM):
        return {'code':symbol,'status':'RAW','reference_price':10000,'source_count':1,
            'affordability':{'entry_eligible':True},'discovery_score':20}
    def test_busy_promotion_keeps_entire_time_window_and_acceleration(self):
        source=self.detector();target=self.detector();self.warm(source)
        t=source.last[SYM];watch={'status':'WATCH','market_status':{'pass':True}}
        before=source.base_gates(t,watch)
        self.assertGreater(len(source.buffers[SYM]),300)
        self.assertTrue(target.handoff_from(source,SYM))
        self.assertEqual(list(target.buffers[SYM]),list(source.buffers[SYM]))
        self.assertIsNone(target.buffers[SYM].maxlen)
        self.assertEqual(target.base_gates(t,watch),before)
        self.assertEqual(before['warmup']['result'],'PASS')
        self.assertEqual(target.minutes[SYM],source.minutes[SYM])
        self.assertEqual(list(target.completed[SYM]),list(source.completed[SYM]))
    def test_older_equal_bar_source_cannot_overwrite_newer_trade_or_high(self):
        source=self.detector();target=self.detector();self.warm(source,end=124);self.warm(target,end=125)
        before=target.last[SYM];buffer=target.buffers[SYM];start=target.observation_start[SYM]
        self.assertFalse(target.handoff_from(source,SYM))
        self.assertIs(target.last[SYM],before);self.assertIs(target.buffers[SYM],buffer)
        self.assertEqual(target.observation_start[SYM],start)
    def test_later_observation_start_cannot_make_warmup_go_backwards(self):
        source=self.detector();target=self.detector();self.warm(target,end=125);self.warm(source,start=4,end=126)
        start=target.observation_start[SYM]
        self.assertFalse(target.handoff_from(source,SYM))
        self.assertEqual(target.observation_start[SYM],start)
    def test_handoff_owns_mutable_bars_and_invalidates_cached_evaluation(self):
        source=self.detector();target=self.detector();self.warm(source)
        target.last_eval[SYM]={'old':True};target._eval_keys[SYM]='old'
        self.assertTrue(target.handoff_from(source,SYM))
        self.assertNotIn(SYM,target.last_eval);self.assertNotIn(SYM,target._eval_keys)
        current=deepcopy(target.current[SYM]);history=deepcopy(list(target.history[SYM]))
        source.current[SYM]['volume']+=100;source.history[SYM][0]['low']=1
        self.assertEqual(target.current[SYM],current);self.assertEqual(list(target.history[SYM]),history)
    def test_actual_stale_reset_is_not_hidden_by_handoff(self):
        source=self.detector();target=self.detector();self.warm(source);target.handoff_from(source,SYM)
        target.forget(SYM);source.forget(SYM)
        t=self.tick(200,999999);source.ingest(t,t.time);target.handoff_from(source,SYM)
        self.assertEqual(target.base_gates(t,{'status':'WATCH'})['warmup']['result'],'FAIL')
        self.assertEqual(target.observation_start[SYM],t.time)
    def test_bidirectional_handoff_has_no_lock_order_deadlock(self):
        a=self.detector();b=self.detector();self.warm(a);self.warm(b)
        errors=[]
        def copy_many(target,source):
            try:
                for _ in range(100):target.handoff_from(source,SYM)
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=copy_many,args=pair,daemon=True) for pair in ((a,b),(b,a))]
        for t in threads:t.start()
        for t in threads:t.join(timeout=3)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
    def test_diagnostic_snapshot_survives_handoff_before_log_and_never_orders(self):
        records=[]
        observer=ShadowObserver(self.cfg,self.auto,self.costs,lambda kind,**data:records.append((kind,data)))
        row=self.row();row.update(status='WATCH',market_status={'pass':True})
        observer.update([row],START);observer.detector.continuous_observation=True
        self.warm(observer.detector);source=self.detector();self.warm(source,end=127)
        snapshot=observer.detector.evaluation_snapshot
        def concurrent_handoff(t,view):
            result=snapshot(t,view)
            self.assertTrue(observer.detector.handoff_from(source,SYM))
            self.assertNotIn(SYM,observer.detector.last_eval)
            return result
        t=self.tick(126,100000)
        with patch.object(observer.detector,'evaluation_snapshot',side_effect=concurrent_handoff):observer.tick(t,t.time)
        evaluation=next(data for kind,data in records if kind=='SHADOW_EVAL')
        # AG added an independent LB research trial after this ABC handoff
        # regression was written. Preserve all three ABC assertions and check
        # the extra diagnostic separately instead of counting it as an order.
        abc=[trial for trial in evaluation['setup_trials'] if trial['setup'] in {'A','B','C'}]
        lb=[trial for trial in evaluation['setup_trials'] if trial['setup']=='LB']
        self.assertEqual(len(abc),3)
        self.assertEqual({trial['setup'] for trial in abc},{'A','B','C'})
        self.assertEqual(len(lb),1)
        self.assertFalse(lb[0]['entry_ready'])
        self.assertTrue(lb[0]['failed_reasons'])
        self.assertFalse(evaluation['actual_order_permission'])
        self.assertTrue(evaluation['SHADOW_ONLY'])
    def test_verified_candidate_published_before_blocking_history_fetch(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);worker.clock=lambda:START
        row=self.row();row['market_status']={'pass':True};published=[]
        def publish(rows,stamp,capital):
            worker.publish(rows,stamp,capital);published.append(rows)
        def chart(*args):
            self.assertIn(SYM,worker.runtime_view()['symbols'])
            self.assertEqual(worker.observer.rows[SYM]['shadow_view']['status'],'UNKNOWN')
            self.assertFalse(worker.state()['actual_order_permission'])
            t=self.tick(0,100);worker.observer.tick(t,t.time)
            self.assertIn(SYM,worker.observer.detector.last)
            return []
        with patch.object(worker,'daily_bars',side_effect=chart):worker.prepare([row],START,{},progress=publish)
        self.assertTrue(published)
        # Metadata cannot mutate a snapshot already handed to the observation thread.
        self.assertEqual(published[0][0]['shadow_view']['status'],'UNKNOWN')
    def test_cached_normal_candidate_not_delayed_by_other_status_query(self):
        rest=Mock();worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root,rest);worker.clock=lambda:START
        worker.metadata[SYM]={'checked':START,'status':{'pass':True}}
        def check(symbol):
            self.assertIn(SYM,worker.runtime_view()['symbols']);return {'pass':False}
        rest.stock_status.side_effect=check
        with patch.object(worker,'daily_bars',return_value=[]),patch('automation.observation_worker.fetch_bars',return_value=[]):
            worker.prepare([self.row('999980'),self.row()],START,{},progress=worker.publish)
        self.assertEqual(rest.stock_status.call_count,1)
        self.assertNotIn('999980',worker.runtime_view()['symbols'])
    def test_unknown_unaffordable_and_protected_candidates_not_early_admitted(self):
        worker=ObservationWorker(self.cfg,self.auto,self.costs,self.root);worker.clock=lambda:START
        protected=self.row('005930');protected['market_status']={'pass':True}
        unaffordable=self.row('999980');unaffordable['affordability']['entry_eligible']=False
        unaffordable['market_status']={'pass':True}
        worker.prepare([self.row(),protected,unaffordable],START,{},progress=worker.publish)
        self.assertEqual(worker.runtime_view()['symbols'],())

if __name__=='__main__':unittest.main()
