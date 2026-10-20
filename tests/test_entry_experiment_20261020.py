import copy
from dataclasses import replace
from datetime import datetime,timedelta
import unittest

from automation.capital import Costs
from automation.policy import AutomationConfig
from core.settings import Config,KST
from research.r6_capture_audit import audit,gate_pass
from research.r6_entry_experiment import compare_confirmed_range_target

AT=datetime(2026,10,20,11,tzinfo=KST)


class OfflineEntryExperimentTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=replace(AutomationConfig(),costs_confirmed=True)
        self.costs=Costs(.00015,.00015,.002,.002,.003)
        proof={'asof':AT.isoformat(),'projection':1100,'target_ceiling':1090,
               'structural_stop':970,'emergency_stop':960}
        gates={k:{'result':'PASS','values':{}} for k in ('warmup','spread','structure','liquidity','breakout','chase','invalidation')}
        gates.update(box_structure={'result':'PASS','values':proof},
            risk_reward={'result':'FAIL','values':{'target':1010}},
            economic={'result':'FAIL','values':{'score':{'inputs':{'confidence':.6,'planned_minutes':20}}}})
        self.trial={'setup':'C','candidate_generated':True,'virtual_entry':1000,'gates':gates}
        self.bars=[{'time':AT-timedelta(minutes=5-i),'open':990,'high':999,'low':980,'close':995,
                    'available_at':AT.isoformat(),'observation_source':'ACTUAL_RECEIVED'} for i in range(5)]

    def compare(self):return compare_confirmed_range_target(self.trial,self.bars,AT,self.cfg,self.auto,self.costs)

    def test_target_hypothesis_is_separate_and_capped_by_existing_known_ceiling(self):
        result=self.compare()
        self.assertEqual(result['status'],'OBSERVE',result)
        self.assertEqual(result['candidate_target'],1090)
        self.assertFalse(result['actual_order_permission'])
        self.assertFalse(result['profitability_proven'])
        self.assertEqual(result['structural_stop'],970)
        self.assertEqual(result['emergency_stop'],960)
        self.assertEqual(self.trial['gates']['risk_reward']['values']['target'],1010)

    def test_missing_original_bars_are_unknown_not_a_success(self):
        self.bars=[];self.assertEqual(self.compare()['status'],'UNKNOWN')

    def test_future_bar_receipt_cannot_be_used(self):
        self.bars[0]['available_at']=(AT+timedelta(milliseconds=1)).isoformat()
        self.assertEqual(self.compare()['status'],'UNKNOWN')

    def test_reference_bars_cannot_replace_real_observation(self):
        self.bars[0]['observation_source']='REST_REFERENCE'
        self.assertEqual(self.compare()['status'],'UNKNOWN')

    def test_incomplete_or_nonconsecutive_bars_cannot_validate_policy(self):
        self.bars=self.bars[1:]
        self.assertEqual(self.compare()['status'],'UNKNOWN')

    def test_breakout_must_already_have_happened(self):
        self.trial['gates']['breakout']['result']='FAIL'
        self.assertEqual(self.compare()['status'],'WAIT')

    def test_original_liquidity_failure_is_not_relaxed(self):
        self.trial['gates']['liquidity']['result']='FAIL'
        self.assertEqual(self.compare()['status'],'REJECT')

    def test_costs_and_final_net_rr_can_still_reject_the_candidate(self):
        self.trial['gates']['box_structure']['values']['target_ceiling']=1020
        self.assertEqual(self.compare()['status'],'REJECT')

    def test_ablations_never_create_a_shape_from_a_missing_shape(self):
        t={'candidate_generated':False,'gates':{'liquidity':{'result':'FAIL','values':{'window_turnover':1}}}}
        self.assertFalse(gate_pass(t,omit={'liquidity'}))

    def test_ablations_keep_other_failed_gates(self):
        t={'candidate_generated':True,'gates':{'liquidity':{'result':'FAIL','values':{'window_turnover':10_000_000}},
                                             'spread':{'result':'FAIL'}}}
        self.assertFalse(gate_pass(t,turnover=5_000_000))

    def test_repeated_frames_are_one_episode_and_no_order_is_inferred(self):
        t=copy.deepcopy(self.trial);t['setup_geometry']={'breakout':999,'support':980}
        rows=[{'event':'ENTRY_EVAL','symbol':'999990','setup_trials':[t],'details':{}} for _ in range(5)]
        result=audit(rows)
        self.assertEqual(result['setups']['C']['episodes'],1)
        self.assertEqual(result['setups']['C']['formed_frames'],5)
        self.assertEqual(result['setups']['C']['entry_ready_frames'],0)
        self.assertIsNone(result['net_pnl'])


if __name__=='__main__':unittest.main()
