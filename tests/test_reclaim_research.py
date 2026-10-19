"""A distinct completed local pattern is research, never a weakened LIVE A/B."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import Mock
import unittest

from automation.reclaim_research import reclaim_trial
from automation.capital import Costs
from automation.policy import AutomationConfig
from core.settings import Config,KST
from core.strategy import Tick

AT=datetime(2026,10,19,9,5,tzinfo=KST)


class ReclaimResearchTests(unittest.TestCase):
    def setUp(self):
        values=[(950,960,945,958,1000),(958,1000,956,995,5000),
                (994,996,980,984,2000),(985,1001,982,999,4000)]
        self.bars=[dict(time=AT-timedelta(minutes=4-i),open=o,high=h,low=l,close=c,volume=v)
                   for i,(o,h,l,c,v) in enumerate(values)]
        self.t=Tick('999990',AT,1003,2000,100000,1003,1002,ask_qty=1000,bid_qty=1000,
                    depth_available=True,book_time=AT)
        self.cfg=Config();self.auto=AutomationConfig();self.costs=Costs(.00015,.00015,.002,.002,.003)
        self.watch={'status':'WATCH','source_count':2,'resistance':1120,'resistance_asof':AT.isoformat(),
                    'market_status':{'pass':True,'checked_at':AT.isoformat()}}
        self.gates={k:{'result':'PASS','values':{}} for k in ('warmup','spread','structure')}
        self.gates['liquidity']={'result':'PASS','values':{'window_turnover':30000000}}

    def trial(self,**kw):
        return reclaim_trial(self.t,self.bars,self.watch,self.cfg,self.auto,self.costs,AT,self.gates,
                             quote_reason=kw.pop('quote_reason',None),**kw)

    def test_confirmed_reclaim_has_structural_lines_real_ceiling_and_costs(self):
        signal,trial=self.trial()
        self.assertIsNotNone(signal,trial);self.assertTrue(trial['entry_ready'])
        self.assertEqual(signal['support'],980);self.assertEqual(signal['stop'],979)
        self.assertLess(signal['emergency'],signal['stop']);self.assertEqual(signal['target'],1120)
        self.assertGreaterEqual(signal['net_reward_risk'],self.cfg.min_reward_risk)
        self.assertFalse(signal['actual_order_permission']);self.assertTrue(signal['SHADOW_ONLY'])
        self.assertIsNone(signal['planned_qty']);self.assertIsNone(signal['actual_net_pnl'])

    def test_future_or_missing_confirmed_bar_cannot_generate_pattern(self):
        for kind in ('missing','future','gap'):
            old=deepcopy(self.bars)
            if kind=='missing':self.bars.pop()
            elif kind=='future':self.bars[-1]['time']=AT
            else:self.bars[-1]['gap_reason']='QUEUE_GAP'
            with self.subTest(kind=kind):self.assertIsNone(self.trial()[0])
            self.bars=old

    def test_downward_bounce_or_broken_higher_low_is_not_a_reclaim(self):
        for changes in ({'low':940},{'close':990},{'volume':1}):
            old=dict(self.bars[-1]);self.bars[-1].update(changes)
            with self.subTest(changes=changes):self.assertIsNone(self.trial()[0])
            self.bars[-1]=old

    def test_known_nearby_ceiling_cannot_be_ignored_to_inflate_rr(self):
        signal,trial=self.trial(context={'nearest_confirmed_peak':{'price':1010,'confirmed_at':AT.isoformat()}})
        self.assertIsNone(signal);self.assertEqual(trial['setup_geometry']['target'],1010)
        self.assertIn('RECLAIM_NET_REWARD_RISK_FAIL',trial['failed_reasons'])

    def test_future_or_missing_target_is_unknown_not_a_two_r_projection(self):
        for value in (None,(AT+timedelta(seconds=1)).isoformat()):
            self.watch['resistance_asof']=value
            with self.subTest(asof=value):self.assertIsNone(self.trial()[0])

    def test_failed_quote_market_status_warmup_or_turnover_is_never_bypassed(self):
        self.assertIsNone(self.trial(quote_reason='CURRENT_TRADE_STALE_OR_FUTURE')[0])
        self.watch['market_status']['checked_at']=(AT-timedelta(seconds=181)).isoformat()
        self.assertIsNone(self.trial()[0]);self.watch['market_status']['checked_at']=AT.isoformat()
        for key in ('warmup','spread','structure'):
            self.gates[key]['result']='FAIL'
            with self.subTest(gate=key):self.assertIsNone(self.trial()[0])
            self.gates[key]['result']='PASS'
        self.gates['liquidity']['values']['window_turnover']=1
        self.assertIsNone(self.trial()[0])

    def test_research_signal_is_rejected_before_any_order_objects_are_needed(self):
        from automation.engine import AutonomousEngine
        signal,_=self.trial();engine=AutonomousEngine.__new__(AutonomousEngine);engine.decisions=Mock()
        self.assertIsNone(engine.enter(signal,self.t))

    def test_policy_does_not_modify_live_settings_or_source_evidence(self):
        before=deepcopy((self.bars,self.watch,self.gates));cfg=self.cfg;auto=self.auto
        self.trial();self.assertEqual((self.bars,self.watch,self.gates),before)
        self.assertEqual((self.cfg,self.auto),(cfg,auto))
