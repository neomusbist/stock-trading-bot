"""Real ObservationWorker TTL/budget regressions; synthetic metadata and ticks only."""
from datetime import datetime,timedelta
from pathlib import Path
import tempfile,unittest

from automation.capital import Costs
from automation.observation_worker import ObservationWorker
from automation.policy import AutomationConfig
from core.settings import Config,KST
from core.strategy import Tick

AT=datetime(2026,10,21,9,0,tzinfo=KST)
SYMS=[f'990{i:03d}' for i in range(1,13)]


class StatusSource:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.blocked=set()
    def stock_status(self,symbol):
        self.calls.append(symbol)
        return {'pass':symbol not in self.blocked,'checked_at':self.clock[0].isoformat(),
                'reason_code':'SYNTHETIC_BLOCKED' if symbol in self.blocked else 'SYNTHETIC_OK'}


class ShadowTTLTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=[AT]
        folder=self.root/'data/raw';folder.mkdir(parents=True)
        lines=['datetime,open,high,low,close,volume']
        for i in range(800):
            day=AT.date()-timedelta(days=800-i)
            lines.append(f'{day.isoformat()},1000,1200,980,1010,2000000')
        text='\n'.join(lines)+'\n'
        for sym in SYMS:(folder/f'{sym}_daily.csv').write_text(text,encoding='utf-8')
        self.source=StatusSource(self.clock)
        self.worker=ObservationWorker(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003),
                                      self.root,rest=self.source)
        self.worker.clock=lambda:self.clock[0]
        self.events=[];self.worker.event=lambda kind,**kw:self.events.append((kind,kw))
        self.worker.observer.emit=self.worker.event

    def rows(self):
        return [{'code':sym,'name':'SYNTHETIC_ONLY','status':'RAW','source_count':2,
                 'source_details':{'one':{},'two':{}},'discovery_score':100-i,
                 'reference_price':1020.,'affordability':{'entry_eligible':True}}
                for i,sym in enumerate(SYMS)]

    def prepare(self,seconds=0,rows=None,allow_metadata=True):
        self.clock[0]=AT+timedelta(seconds=seconds)
        self.source.calls.clear()
        self.worker.prepare(self.rows() if rows is None else rows,self.clock[0],{'entry_budget':80000},
                            allow_metadata=allow_metadata)
        self.assertLessEqual(len(self.source.calls),self.worker.settings.metadata_budget)
        return list(self.source.calls)

    def warm(self,until=180):
        detector=self.worker.observer.detector;sym=SYMS[0]
        for second in range(0,until+1,5):
            stamp=AT+timedelta(seconds=second)
            self.assertTrue(detector.ingest(Tick(sym,stamp,1020,100,10000+second*100,1020,1019,True),stamp))
        return detector

    def test_expired_active_shadow_refreshes_before_unqueried_raw_within_same_budget(self):
        self.assertEqual(self.prepare(),SYMS[:4])
        self.assertEqual(set(self.worker.observer.rows),set(SYMS[:4]))
        self.assertEqual(self.prepare(181),SYMS[:4])
        self.assertTrue(set(SYMS[:4])<=set(self.worker.observer.rows))
        for sym in SYMS[:4]:
            self.assertEqual(self.worker.observer.rows[sym]['shadow_view']['market_status']['checked_at'],
                             self.clock[0].isoformat())

    def test_new_raw_discovery_continues_after_incumbent_refresh(self):
        self.prepare();self.prepare(181)
        self.assertEqual(self.prepare(182),SYMS[4:8])
        self.assertTrue(set(SYMS[:8])<=set(self.worker.observer.rows))

    def test_exact_180_second_boundary_retains_current_status_and_uses_budget_for_raw(self):
        self.prepare()
        self.assertEqual(self.prepare(180),SYMS[4:8])
        self.assertTrue(set(SYMS[:4])<=set(self.worker.observer.rows))
        self.assertEqual(self.worker.metadata[SYMS[0]]['checked'],AT)
        self.assertEqual(self.prepare(181),SYMS[:4])

    def test_new_watch_keeps_first_claim_on_status_budget(self):
        self.prepare();rows=self.rows();rows[-1]['status']='WATCH'
        calls=self.prepare(181,rows)
        self.assertEqual(calls,[SYMS[-1],*SYMS[:3]])
        self.assertIn(SYMS[-1],self.worker.observer.rows)

    def test_ttl_refresh_preserves_accepted_ticks_and_completed_bar_continuity(self):
        self.prepare();detector=self.warm()
        history=detector.history[SYMS[0]];start=detector.observation_start[SYMS[0]]
        completed=list(history);self.assertEqual(len(detector.usable_bars(SYMS[0])),3)
        self.prepare(181)
        self.assertIn(SYMS[0],self.worker.runtime_view()['symbols'])
        self.assertIs(detector.history[SYMS[0]],history)
        self.assertEqual(list(history),completed)
        self.assertEqual(detector.observation_start[SYMS[0]],start)
        self.assertNotIn(SYMS[0],self.worker.observer.detached)
        self.prepare(182)
        self.assertEqual(detector.observation_start[SYMS[0]],start)
        self.assertFalse(any(k in {'SHADOW_ENDED','SHADOW_DATA_RESET'} and d.get('symbol')==SYMS[0]
                             for k,d in self.events))

    def test_incremental_publication_cannot_temporarily_detach_pending_active_refresh(self):
        self.prepare();detector=self.warm();start=detector.observation_start[SYMS[0]]
        self.clock[0]=AT+timedelta(seconds=181)
        self.worker.prepare(self.rows(),self.clock[0],{'entry_budget':80000},
                            progress=self.worker.publish)
        self.assertTrue(set(SYMS[:4])<=set(self.worker.runtime_view()['symbols']))
        self.assertEqual(detector.observation_start[SYMS[0]],start)
        self.assertFalse(any(k=='SHADOW_ENDED' and d.get('symbol') in SYMS[:4] for k,d in self.events))

    def test_expired_status_cannot_be_retained_when_metadata_queries_are_disabled(self):
        self.prepare()
        self.assertEqual(self.prepare(181,allow_metadata=False),[])
        self.assertFalse(self.worker.observer.rows)
        self.assertFalse(self.worker.runtime_view()['symbols'])

    def test_negative_refresh_evicts_symbol_and_true_detach_gap_resets_history(self):
        self.prepare();detector=self.warm();self.source.blocked.add(SYMS[0])
        self.prepare(181)
        self.assertNotIn(SYMS[0],self.worker.observer.rows)
        self.assertNotIn(SYMS[0],self.worker.runtime_view()['symbols'])
        self.prepare(187)
        self.assertNotIn(SYMS[0],detector.history)
        self.assertTrue(any(k=='SHADOW_DATA_RESET' and d.get('symbol')==SYMS[0] for k,d in self.events))

    def test_removed_candidate_still_unsubscribes_and_invalidates_real_gap(self):
        self.prepare();detector=self.warm()
        rows=[r for r in self.rows() if r['code']!=SYMS[0]]
        self.prepare(181,rows)
        self.assertNotIn(SYMS[0],self.worker.runtime_view()['symbols'])
        self.prepare(187,rows)
        self.assertNotIn(SYMS[0],detector.history)


class DiagnosticHandoffBoundary(unittest.TestCase):
    def test_actual_abc_snapshot_survives_handoff_without_research_trial_dependency(self):
        from unittest.mock import patch
        from tests.test_observation_reaction import ObservationReactionTests,START,SYM
        from automation.shadow_observation import ShadowObserver
        case=ObservationReactionTests(methodName='runTest');case.setUp();self.addCleanup(case.doCleanups)
        records=[]
        observer=ShadowObserver(case.cfg,case.auto,case.costs,lambda kind,**data:records.append((kind,data)))
        row=case.row();row.update(status='WATCH',market_status={'pass':True})
        observer.update([row],START);observer.detector.continuous_observation=True
        case.warm(observer.detector);source=case.detector();case.warm(source,end=127)
        snapshot=observer.detector.evaluation_snapshot
        def concurrent_handoff(t,view):
            result=snapshot(t,view)
            self.assertTrue(observer.detector.handoff_from(source,SYM))
            self.assertNotIn(SYM,observer.detector.last_eval)
            return result
        tick=case.tick(126,100000)
        with patch.object(observer.detector,'evaluation_snapshot',side_effect=concurrent_handoff):
            observer.tick(tick,tick.time)
        evaluation=next(data for kind,data in records if kind=='SHADOW_EVAL')
        abc=[trial for trial in evaluation['setup_trials'] if trial['setup'] in {'A','B','C'}]
        self.assertEqual(len(abc),3);self.assertEqual({trial['setup'] for trial in abc},{'A','B','C'})
        self.assertFalse(evaluation['actual_order_permission']);self.assertTrue(evaluation['SHADOW_ONLY'])


if __name__=='__main__':unittest.main()
