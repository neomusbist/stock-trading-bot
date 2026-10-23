"""Synthetic isolated feature/defect tests, never actual execution evidence."""
from dataclasses import replace,asdict
from datetime import datetime,timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch
from core.settings import Config,KST
from core.strategy import Tick
from automation.capital import Costs
from automation.entry_timing import ObservationTimeline
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig,Policy,load_automation
from automation.radar import DynamicRadar
from automation.runner import _quote_blocked,DiagnosticSnapshotWriter
from automation.structural_breakout import structural_breakout

AT=datetime(2026,10,23,10,15,tzinfo=KST)

class SelectiveIntegrationTests(unittest.TestCase):
    def test_research_options_default_off_and_b_expansion_remains_off(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'automation.json';p.write_text(json.dumps({'structural_pullback_enabled':True}))
            a=load_automation(p)
        self.assertFalse(a.pullback_expansion_target_enabled)
        for key in ('a_short_reference_enabled','a_sustained_turnover_enabled','b_sustained_turnover_enabled','early_surge_priority_enabled'):
            self.assertFalse(getattr(a,key))

    def test_each_option_is_independent_and_strictly_boolean(self):
        a=AutomationConfig()
        for key in ('a_short_reference_enabled','a_sustained_turnover_enabled','b_sustained_turnover_enabled','early_surge_priority_enabled','single_source_eligibility_fix_enabled','abc_quote_retry_enabled','timeline_async_save_enabled'):
            changed=replace(a,**{key:not getattr(a,key)})
            self.assertEqual(sum(asdict(a)[k]!=asdict(changed)[k] for k in asdict(a)),1)
            with self.assertRaises(ValueError):replace(a,**{key:1}).validate()

    def test_regular_quote_block_is_retried_but_rr_reject_is_not(self):
        trial={'candidate_generated':True,'gates':{'spread':{'result':'FAIL'},'order_price':{'result':'PASS'},'risk_reward':{'result':'PASS'}}}
        d=SimpleNamespace(last_eval={'X':{'trials':[trial]}})
        e=SimpleNamespace(a=AutomationConfig())
        self.assertTrue(_quote_blocked(e,d,'X'))
        e.a=replace(e.a,abc_quote_retry_enabled=False);self.assertFalse(_quote_blocked(e,d,'X'))
        e.a=AutomationConfig();trial['gates']['risk_reward']['result']='FAIL'
        self.assertFalse(_quote_blocked(e,d,'X'))
        trial['gates']['risk_reward']['result']='PASS'
        trial['gates']['box_structure']={'result':'UNKNOWN','values':{'reason':'BOX_QUOTE_UNAVAILABLE'}}
        self.assertTrue(_quote_blocked(e,d,'X'))
        trial['gates']['box_structure']['values']['reason']='BOX_STRUCTURE_DATA_UNAVAILABLE'
        self.assertFalse(_quote_blocked(e,d,'X'))

    def test_missing_quote_can_recompute_provisional_valuation_but_not_bypass_other_gates(self):
        trial={'candidate_generated':True,'gates':{'spread':{'result':'FAIL'},
            'breakout_structure':{'result':'UNKNOWN','values':{'reason':'INVALID_EXECUTABLE_QUOTE'}},
            'risk_reward':{'result':'FAIL'},'invalidation':{'result':'FAIL'},'economic':{'result':'FAIL'},
            'liquidity':{'result':'PASS'}}}
        d=SimpleNamespace(last_eval={'X':{'trials':[trial]}});e=SimpleNamespace(a=AutomationConfig())
        self.assertTrue(_quote_blocked(e,d,'X'))
        trial['gates']['liquidity']['result']='FAIL';self.assertFalse(_quote_blocked(e,d,'X'))
        trial['gates']['liquidity']['result']='PASS'
        trial['gates']['breakout_structure']['values']['reason']='INDEPENDENT_STRUCTURE_UNCONFIRMED'
        self.assertFalse(_quote_blocked(e,d,'X'))

    def test_single_source_permission_survives_reserve_and_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            policy=Policy(Path(folder)/'policy.json');a=AutomationConfig()
            def sample(enabled):
                values={**asdict(a),'capital_limit':a.capital_limit,'single_source_eligibility_fix_enabled':enabled}
                r=DynamicRadar(replace(Config(),max_watchlist=1),SimpleNamespace(**values),policy,fallback_source1=True)
                rows=[dict(code=str(900001+i),source_details={'ka10023':{'observed_rank':i+1,'values':{'flu_rt':2.}}},reference_price=1000.) for i in range(2)]
                progress=[]
                result=r.update(rows,lambda x:dict(status='ELIGIBLE',resistance=2000.),AT,progress=lambda rs:progress.extend(rs))
                return result[0],progress
            rows,progress=sample(True)
            self.assertTrue(next(r for r in rows if r['status']=='RESERVE').get('fallback_source1',False))
            self.assertTrue(all(r.get('fallback_source1') for r in progress))
            rows,_=sample(False);self.assertFalse(next(r for r in rows if r['status']=='RESERVE').get('fallback_source1',False))

    def test_early_priority_opt_in_never_changes_watch_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            policy=Policy(Path(folder)/'policy.json')
            def sample(enabled):
                r=DynamicRadar(replace(Config(),max_watchlist=1),replace(AutomationConfig(),early_surge_priority_enabled=enabled),policy,fallback_source1=True)
                rows=[dict(code='900001',source_details={k:{'observed_rank':20,'values':{'flu_rt':12.}} for k in ('ka10027','ka10030')},reference_price=1000.),
                      dict(code='900002',source_details={'ka10023':{'observed_rank':1,'values':{'flu_rt':2.,'sdnin_rt':60.}}},reference_price=1000.)]
                return r.update(rows,lambda x:dict(status='ELIGIBLE',resistance=2000.),AT)[1]
            self.assertEqual([r['code'] for r in sample(False)],['900001'])
            self.assertEqual([r['code'] for r in sample(True)],['900002'])
            self.assertEqual(len(sample(True)),1)

    def test_short_reference_option_preserves_target_and_confirmation(self):
        bars=[dict(time=AT-timedelta(minutes=4-i),open=o,high=h,low=l,close=c,volume=1000) for i,(o,h,l,c) in enumerate(
            [(1000,1040,990,1030),(1030,1035,1000,1010),(1010,1045,1005,1042),(1042,1050,1020,1048)])]
        setup=dict(breakout=1050,support=1020,projection=1110)
        old,old_ev=structural_breakout(bars,AT,setup,Config(),1051,1050,1051)
        new,ev=structural_breakout(bars,AT,setup,Config(),1051,1050,1051,auto=replace(AutomationConfig(),a_short_reference_enabled=True))
        self.assertIsNone(old);self.assertTrue(new,ev)
        self.assertEqual(ev['projection'],1050+2*(1050-ev['support']))
        passed,_=structural_breakout(bars[-3:],AT,setup,Config(),1051,1050,1051,auto=replace(AutomationConfig(),a_short_reference_enabled=True))
        self.assertIsNone(passed)

    def test_sustained_liquidity_uses_real_ticks_not_bar_volume(self):
        d=PatternDetector(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        d.observation_start['X']=AT-timedelta(seconds=70)
        d.buffers['X']=[Tick('X',AT-timedelta(seconds=s),10000,100,1000+s,10000,9990,True) for s in range(60)]
        ok,proof=d.sustained_turnover(d.buffers['X'][0],30000000,5000000)
        self.assertTrue(ok);self.assertEqual(proof['sustained_turnover'],60000000)
        ok,_=d.sustained_turnover(d.buffers['X'][0],30000000,4999999);self.assertFalse(ok)
        d.mark_coverage_gap('X',AT,'TEST_GAP')
        ok,_=d.sustained_turnover(Tick('X',AT,10000,1,2000,10000,9990,True),30000000,5000000);self.assertFalse(ok)

    def test_timeline_publishes_copied_diagnostic_only_snapshot_without_caller_io(self):
        with tempfile.TemporaryDirectory() as folder:
            timeline=ObservationTimeline(Path(folder)/'timeline.json');timeline.day=AT.date();timeline.rows={'X':{'first_raw_at':AT.isoformat()}}
            sent=[];timeline.writer=lambda p,v:sent.append(v)
            with patch('core.util.atomic_json',side_effect=AssertionError('MAIN_THREAD_DISK_IO')):timeline.save(AT)
            timeline.rows['X']['first_raw_at']='CHANGED'
            self.assertEqual(sent[0]['symbols']['X']['first_raw_at'],AT.isoformat())
            self.assertTrue(sent[0]['diagnostic_only']);self.assertFalse(sent[0]['actual_order_permission'])

    def test_writer_never_accepts_ledger_as_optional_diagnostic_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaises(ValueError):DiagnosticSnapshotWriter([root/'candidate_summary.json',root/'heartbeat.json'],timeline_path=root/'state.json')

    def test_slow_diagnostic_worker_cannot_block_submit_or_mutate_pending_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);entered=threading.Event();release=threading.Event();written=[]
            def write(p,v,**kw):entered.set();release.wait(2);written.append((p,v,kw))
            w=DiagnosticSnapshotWriter([root/'candidate_summary.json',root/'heartbeat.json'],timeline_path=root/'observation_timeline.json',write=write)
            try:
                snapshot={'symbols':{'X':1}};w.submit(root/'observation_timeline.json',snapshot)
                self.assertTrue(entered.wait(1));snapshot['symbols']['X']=99
                w.submit(root/'heartbeat.json',{'order_ready':False})
            finally:release.set();self.assertTrue(w.close())
            self.assertEqual(written[0][1]['symbols']['X'],1)
            self.assertFalse(written[0][2]['fsync'])

if __name__=='__main__':unittest.main()
