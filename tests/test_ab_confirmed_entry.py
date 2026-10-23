"""Disabled A/B entry revisions. Synthetic prices are not recovered market trades."""
from copy import deepcopy
from dataclasses import fields,replace
from datetime import timedelta
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch,Mock
import unittest
import json,tempfile
from pathlib import Path
from core.settings import Config
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.decisions import gate
from automation.patterns import PatternDetector
from automation.structural_pullback import structural_pullback
from automation.shadow_observation import ShadowObserver
from automation.observation_retention import observation_residence_seconds
from tests.test_structural_pullback import bars,AT

COSTS=Costs(.00015,.00015,.002,.002,.003)
MEASUREMENTS=[]
B_RECLAIM=bars([(1000,1005,1000,1004,100),(1004,1170,1004,1160,400),
 (1110,1112,1060,1068,100),(1068,1070,1060,1065,80),(1065,1090,1061,1072,60)])
A_RETEST=bars([(1000,1030,1000,1020,100),(1020,1050,1010,1040,100),
 (1040,1045,1020,1030,100),(1030,1160,1030,1150,400),
 (1060,1060,1050,1055,80),(1055,1064,1052,1062,60)])

def settings(**extra):
    base=dict(structural_breakout_enabled=True,structural_pullback_enabled=True,
              confirmed_invalidation_enabled=True,fast_momentum_enabled=False)
    names={f.name for f in fields(AutomationConfig)}
    auto=replace(AutomationConfig(),**{k:v for k,v in {**base,**extra}.items() if k in names})
    # A baseline capsule predates these disabled switches. Let its unchanged
    # implementation ignore them, so earlier-path tests fail on behavior.
    for k,v in extra.items():
        if k not in names:object.__setattr__(auto,k,v)
    return auto

def evaluation(name,raw,entry,**flags):
    cfg=Config();auto=settings(**flags);d=PatternDetector(cfg,auto,COSTS)
    d.history['999990']=deque(deepcopy(raw),maxlen=30)
    d.price_context.merge_history('999990',raw,AT,'KRX');d.prior_high['999990']=max(b['high'] for b in raw)
    t=Tick('999990',AT,entry,1000,100000,entry,entry-1,True,100000,100000,
           depth_available=True,book_time=AT,depth_source='SYNTHETIC')
    d.buffers[t.symbol]=deque([replace(t,time=AT-timedelta(seconds=1),price=entry-1,cumulative=99900),t])
    watch=dict(status='WATCH',resistance=2000,source_count=2,source_details={'one':{},'two':{}},market_status={'pass':True})
    gates=dict(warmup=gate(True,elapsed_seconds=360),spread=gate(True,bid=entry-1,ask=entry),structure=gate(True,status='WATCH'),
               liquidity=gate(True,window_turnover=40000000),acceleration=gate(False,ratio=1.,min_ratio=3.,gain=.001),market_status=gate(True))
    trials=d.setup_trials(t,watch,gates,_bars=raw)
    return next(x for x in trials if x['setup']==name),d,t,watch,gates

class ConfirmedEntryTests(unittest.TestCase):
    def test_revisions_default_off_and_strict_boolean(self):
        auto=AutomationConfig()
        for name in ('a_breakout_retest_entry_enabled','b_support_reclaim_entry_enabled'):
            self.assertIs(getattr(auto,name,None),False)
            with self.assertRaises(ValueError):replace(auto,**{name:1}).validate()

    def test_B_completed_reclaim_does_not_wait_for_its_upper_wick(self):
        old,_,_,_,_=evaluation('B',B_RECLAIM,1073)
        new,_,_,_,_=evaluation('B',B_RECLAIM,1073,b_support_reclaim_entry_enabled=True)
        self.assertFalse(old['entry_ready']);self.assertIn('NO_REBREAKOUT',old['failed_reasons'])
        self.assertTrue(new['entry_ready'],new['failed_reasons'])
        self.assertEqual(new['setup_geometry']['breakout'],1070)
        for key in ('stop','target'):
            self.assertEqual(old['gates']['risk_reward' if key=='target' else 'invalidation']['values'][key],
                             new['gates']['risk_reward' if key=='target' else 'invalidation']['values'][key])
        self.assertEqual(new['gates']['risk_reward']['values']['target'],1170)
        self.assertGreaterEqual(new['gates']['risk_reward']['values']['net_rr'],1.5)

    def test_B_unconfirmed_or_still_forming_recovery_cannot_arm(self):
        for raw in (B_RECLAIM[:-1], [{**b,'time':b['time']+timedelta(minutes=1)} for b in B_RECLAIM]):
            trial,_,_,_,_=evaluation('B',raw,1073,b_support_reclaim_entry_enabled=True)
            self.assertFalse(trial['entry_ready'])

    def test_B_wrong_reaction_level_cannot_cross_order_boundary(self):
        trial,_,_,_,_=evaluation('B',B_RECLAIM,1073,b_support_reclaim_entry_enabled=True)
        setup={**trial['setup_geometry'],'breakout':1065}
        ok,proof=structural_pullback(B_RECLAIM,AT,setup,Config(),1073,1072,1073,True,
            settings(b_support_reclaim_entry_enabled=True))
        self.assertFalse(ok);self.assertEqual(proof['reason'],'PULLBACK_RECLAIM_TRIGGER_PROOF_INVALID')

    def test_A_confirmed_retest_has_its_own_trigger_and_invalidation(self):
        old,_,_,_,_=evaluation('A',A_RETEST,1063)
        new,_,_,_,_=evaluation('A',A_RETEST,1063,a_breakout_retest_entry_enabled=True)
        self.assertFalse(old['entry_ready'])
        self.assertTrue(new['entry_ready'],new['failed_reasons'])
        setup=new['setup_geometry'];proof=new['gates']['breakout_structure']['values']
        self.assertEqual(setup['breakout'],1060);self.assertEqual(setup['old_ceiling'],1050)
        self.assertEqual(proof['support'],1050)
        self.assertEqual(new['gates']['risk_reward']['values']['target'],1150)
        self.assertEqual(setup['projection'],1050+2*(1050-1000))
        self.assertEqual(new['gates']['acceleration']['values']['mode'],'CONFIRMED_BREAKOUT_RETEST')
        self.assertFalse(new['acceleration_required'])
        self.assertGreaterEqual(new['gates']['risk_reward']['values']['net_rr'],1.5)

    def test_A_current_tick_cannot_confirm_retest_or_clear_overhead(self):
        for raw in (A_RETEST[:-1], [{**b,'time':b['time']+timedelta(minutes=1)} for b in A_RETEST]):
            trial,_,_,_,_=evaluation('A',raw,1063,a_breakout_retest_entry_enabled=True)
            self.assertFalse(trial['entry_ready'])
        raw=deepcopy(A_RETEST);raw[4]['low']=1049
        trial,_,_,_,_=evaluation('A',raw,1063,a_breakout_retest_entry_enabled=True)
        self.assertFalse(trial['entry_ready'])

    def test_A_does_not_replace_a_known_near_ceiling_with_projection(self):
        raw=deepcopy(A_RETEST);raw[3].update(high=1090,close=1080)
        trial,_,_,_,_=evaluation('A',raw,1063,a_breakout_retest_entry_enabled=True)
        candidates=[trial]+trial.get('entry_alternatives',[])
        retest=next(x for x in candidates if (x.get('setup_geometry') or {}).get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST')
        self.assertFalse(retest['entry_ready'])
        self.assertLessEqual(retest['gates']['risk_reward']['values']['target'],1090)

    def test_A_original_projection_does_not_drift_as_older_history_rolls_out(self):
        from automation.structural_breakout import breakout_retest_setup
        prefix=[dict(time=AT-timedelta(minutes=8),open=900,high=1000,low=900,close=990,volume=100,first_second=0),
                dict(time=AT-timedelta(minutes=7),open=990,high=1000,low=950,close=999,volume=100,first_second=0)]
        extended=breakout_retest_setup(prefix+A_RETEST,AT,Config())
        bounded=breakout_retest_setup(A_RETEST,AT,Config())
        for key in ('old_ceiling','original_range_support','projection','breakout','support','range_anchor_at'):
            self.assertEqual(extended[key],bounded[key])
        self.assertEqual(bounded['projection'],1150)

    def test_revision_never_suppresses_an_already_ready_baseline(self):
        from tests.test_structural_pullback import VIABLE_B
        old,_,_,_,_=evaluation('B',VIABLE_B,1051)
        new,_,_,_,_=evaluation('B',VIABLE_B,1051,b_support_reclaim_entry_enabled=True)
        self.assertTrue(old['entry_ready']);self.assertTrue(new['entry_ready'])
        self.assertEqual(old['setup_geometry'],new['setup_geometry'])

    def test_revised_entry_does_not_buy_a_falling_or_stale_reaction(self):
        trial,d,t,watch,gates=evaluation('A',A_RETEST,1063,a_breakout_retest_entry_enabled=True)
        self.assertTrue(trial['entry_ready'])
        for prior in (replace(t,time=AT-timedelta(seconds=1),price=t.price+1,cumulative=99900),
                      replace(t,time=AT-timedelta(seconds=Config().stale_seconds+1),price=t.price-1,cumulative=99900)):
            d.buffers[t.symbol]=deque([prior,t])
            current=next(x for x in d.setup_trials(t,watch,gates,_bars=A_RETEST) if x['setup']=='A')
            self.assertFalse(current['entry_ready'])

    def test_quote_retry_reaches_a_revised_alternative_without_waiving_rr(self):
        from automation.runner import _quote_blocked
        alternative={'candidate_generated':True,'gates':{'spread':gate(False),
            'pullback_structure':gate(None,reason='PULLBACK_QUOTE_UNAVAILABLE'),
            'risk_reward':gate(False),'invalidation':gate(False),'economic':gate(False),'reclaim_tick':gate(True)}}
        trial={'candidate_generated':False,'gates':{'breakout':gate(False)},'entry_alternatives':[alternative]}
        d=SimpleNamespace(last_eval={'999990':{'trials':[trial]}});engine=SimpleNamespace(a=settings())
        self.assertTrue(_quote_blocked(engine,d,'999990'))
        alternative['gates']['pullback_structure']=gate(True)
        self.assertFalse(_quote_blocked(engine,d,'999990'))

class ObservationResidenceTests(unittest.TestCase):
    def row(self,sym,score=10,status='SHADOW'):
        return dict(code=sym,status=status,source_count=1,discovery_score=score,affordability={'entry_eligible':True},market_status={'pass':True})
    def test_rank_only_refresh_cannot_evict_resident_before_bar_budget(self):
        observer=ShadowObserver(Config(),AutomationConfig(),COSTS,Mock(),limit=1)
        observer.update([self.row('999990')],AT)
        observer.update([self.row('999990'),self.row('999991',100)],AT+timedelta(seconds=1))
        self.assertEqual(set(observer.rows),{'999990'})
        observer.update([self.row('999990'),self.row('999991',100)],AT+timedelta(seconds=observation_residence_seconds(observer.auto)+1))
        self.assertEqual(set(observer.rows),{'999991'})
    def test_watch_and_hard_invalidity_preempt_residence(self):
        observer=ShadowObserver(Config(),AutomationConfig(),COSTS,Mock(),limit=1)
        observer.update([self.row('999990',100)],AT)
        observer.update([self.row('999990',100),self.row('999991',1,'WATCH')],AT+timedelta(seconds=1))
        self.assertEqual(set(observer.rows),{'999991'})
        invalid=self.row('999991',100);invalid['affordability']['entry_eligible']=False
        observer.update([invalid,self.row('999990')],AT+timedelta(seconds=2))
        self.assertEqual(set(observer.rows),{'999990'})

    def test_fresh_viable_geometry_preempts_residence_but_disabled_fast_does_not(self):
        observer=ShadowObserver(Config(),replace(AutomationConfig(),setup_aware_watch_priority=True),COSTS,Mock(),limit=1)
        observer.update([self.row('999990',100)],AT)
        observer.latest['999991']={'timestamp':AT.isoformat(),'fast_signal_ready':True,'gates':{},'setup_trials':[]}
        observer.update([self.row('999990',100),self.row('999991')],AT+timedelta(seconds=1))
        self.assertEqual(set(observer.rows),{'999990'})
        gates={k:gate(True) for k in ('warmup','spread','structure','market_status','liquidity')}
        observer.latest['999991']={'timestamp':AT.isoformat(),'gates':gates,'setup_trials':[
            {'setup':'B','candidate_generated':True,'acceleration_required':False,'entry_ready':True,
             'gates':{'liquidity':gate(True),'risk_reward':gate(True),'breakout':gate(True)}}]}
        observer.update([self.row('999990',100),self.row('999991')],AT+timedelta(seconds=2))
        self.assertEqual(set(observer.rows),{'999991'})

class ReceiptTraceTests(unittest.TestCase):
    def test_quote_recheck_links_the_original_trade_and_new_book_without_renewing_it(self):
        from automation.decisions import tick_source_identity
        t=Tick('999990',AT,1063,100,10000,1063,1062,received_at=AT.isoformat(),received_monotonic=100.,
            book_received_at=AT.isoformat(),book_received_monotonic=100.)
        quoted=replace(t,ask=1064,book_received_at=(AT+timedelta(milliseconds=10)).isoformat(),book_received_monotonic=100.01)
        old,new=tick_source_identity(t),tick_source_identity(quoted)
        self.assertEqual(old['trade_receipt_id'],new['trade_receipt_id'])
        self.assertNotEqual(old['book_receipt_id'],new['book_receipt_id'])
        unknown=tick_source_identity(replace(t,received_at=None))
        self.assertIsNone(unknown['trade_receipt_id']);self.assertEqual(unknown['identity_status'],'UNKNOWN_RECEIPT_ID')

    def test_existing_sample_carries_price_zone_and_receipt_without_more_samples(self):
        from automation.trade_review_capture import TradeReviewCapture
        trial,d,t,watch,_=evaluation('A',A_RETEST,1063,a_breakout_retest_entry_enabled=True)
        t=replace(t,received_at=AT.isoformat(),received_monotonic=100.)
        with tempfile.TemporaryDirectory() as folder:
            c=TradeReviewCapture(Path(folder),start=False,min_free_bytes=0,continuous_watch=True,clock=lambda:AT)
            try:
                c.set_scope({t.symbol:watch},{},AT);c.tick(t,AT);c.tick(t,AT,AT)
                for _ in range(5):c.set_scope({t.symbol:watch},{},AT)
                for _ in range(10):c.evaluation(t,watch,{'trials':[trial]},None,AT)
                c.flush()
                rows=[json.loads(line) for f in Path(folder).glob('trade-review-*.jsonl') for line in f.read_text(encoding='utf-8').splitlines()]
                sampled=[r for r in rows if r['event']=='OPPORTUNITY_SAMPLE']
                self.assertEqual(len(sampled),1)
                self.assertEqual(len([r for r in rows if r['event']=='WATCH_METADATA']),1)
                sample=sampled[0];stage=sample['trials'][0]['entry_stage']
                self.assertEqual(stage['maximum_entry'],trial['gates']['risk_reward']['values']['economics']['max_entry_price'])
                self.assertFalse(stage['actual_order_permission'])
                self.assertEqual(stage['structure_observed_by'],AT.isoformat())
                identities=[r['receipt_evidence']['trade_receipt_id'] for r in rows if r['event'] in {'TRADE','OPPORTUNITY_SAMPLE'}]
                self.assertEqual(len(set(identities)),1)
            finally:c.close()
class RevisedFakeLifecycle(unittest.TestCase):
    def lifecycle(self,name,raw,entry):
        from tests.test_live_ready_20261021 import LiveReadyLifecycle,settings as legacy_settings,AT as origin
        cfg,auto=legacy_settings();auto=replace(auto,fast_momentum_enabled=False,
            a_breakout_retest_entry_enabled=True,b_support_reclaim_entry_enabled=True)
        h=LiveReadyLifecycle()
        with patch('tests.test_live_ready_20261021.settings',return_value=(cfg,auto)):h.setUp()
        self.addCleanup(h.doCleanups);h.watch('B')
        for i,b in enumerate(raw):
            start=origin-timedelta(minutes=len(raw)-i)
            for second,price in ((0,b['open']),(15,b['high']),(30,b['low']),(59,b['close'])):
                h.feed(start+timedelta(seconds=second),price,int(b['volume']))
        h.feed(origin,raw[-1]['close'],100)
        h.feed(origin+timedelta(seconds=5),entry,100000)
        buys=[o for o in h.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1,[(k,v.get('primary_reason_code') or v.get('reason')) for k,v in h.events if k in {'ENTRY_EVAL','ORDER_REJECTED','NO_TRADE'}][-4:])
        buy=buys[0];self.assertEqual(buy['signal']['pattern'],name);self.assertEqual(buy['status'],'ACCEPTED')
        self.assertGreater(buy['qty'],1);self.assertTrue(h.broker.intent_checks[0]['durable_intent'])
        h.fill(buy,1,buy['limit']);h.fill(buy,1,buy['limit']);h.fill(buy,buy['qty'],buy['limit'])
        self.assertEqual(h.e.s['positions']['999990']['qty'],buy['qty'])
        hard=buy['signal']['invalidation_policy']['emergency_stop']
        h.feed(h.clock+timedelta(seconds=1),hard-1,100)
        sells=[o for o in h.e.s['orders'].values() if o['side']=='SELL']
        self.assertEqual(len(sells),1);h.fill(sells[0],buy['qty'],hard-1)
        self.assertEqual(h.e.s['positions'],{});self.assertEqual(len(h.broker.intent_checks),2)
        MEASUREMENTS.append(dict(basis='SYNTHETIC_REVISED_AB_NOT_MARKET_EXECUTION',setup=name,entry=buy['limit'],
            structural_stop=buy['signal']['invalidation_policy']['structural_stop'],emergency=hard,
            target=buy['signal']['target'],quantity=buy['qty'],net_rr=buy['signal']['entry_economics']['net_rr'],
            durable_intent=True,fake_filled=True,emergency_exit_completed=True,actual_orders=0))
    def test_A_receive_watch_intent_fill_emergency_exit(self):self.lifecycle('A',A_RETEST,1063)
    def test_B_receive_watch_intent_fill_emergency_exit(self):self.lifecycle('B',B_RECLAIM,1073)

if __name__=='__main__':unittest.main()
