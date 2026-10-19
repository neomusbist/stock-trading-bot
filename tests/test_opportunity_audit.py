from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from automation.opportunity_audit import audit_records
from core.settings import KST

AT=datetime(2026,10,19,9,10,tzinfo=KST)


class OpportunityAuditTests(unittest.TestCase):
    def row(self):
        return {'symbol':'079650','time':AT.isoformat(),'setup_trials':[{'setup':'B',
            'candidate_generated':True,'entry_ready':False,'setup_geometry':{'breakout':8000,'support':7900},
            'failed_reasons':['NO_REBREAKOUT','RISK_REWARD_FAIL']}]}

    def test_repeated_prints_and_minutes_are_one_observed_geometry(self):
        rows=[]
        for i in range(120):
            row=self.row();row['time']=(AT+timedelta(seconds=i)).isoformat();rows.append(row)
        result=audit_records(rows,AT+timedelta(minutes=2))
        self.assertEqual(result['evaluations_observed'],120)
        self.assertEqual(result['patterns']['B']['unique_geometry_episodes'],1)
        self.assertEqual(result['patterns']['B']['failure_episode_counts']['RISK_REWARD_FAIL'],1)
        self.assertIsNone(result['actual_market_opportunities']);self.assertIsNone(result['actual_completed_trades'])

    def test_future_signal_is_never_available_at_earlier_cutoff(self):
        row=self.row();row['time']=(AT+timedelta(seconds=1)).isoformat();row['setup_trials'][0]['entry_ready']=True
        result=audit_records([row],AT)
        self.assertEqual(result['patterns'],{});self.assertEqual(result['future_records_excluded'],1)

    def test_correlated_blockers_are_distinguished_from_one_sole_blocker(self):
        row=self.row();second=deepcopy(row);second['setup_trials'][0]['failed_reasons']=['RISK_REWARD_FAIL']
        result=audit_records([row,second],AT)['patterns']['B']
        self.assertEqual(result['sole_blocker_episode_counts'],{'RISK_REWARD_FAIL':1})
        self.assertEqual(result['ready_geometry_episodes'],0)

    def test_observational_acceleration_is_not_counted_as_bc_entry_failure(self):
        row=self.row();trial=row['setup_trials'][0];trial.pop('failed_reasons')
        trial['gates']={'acceleration':{'result':'FAIL','required':False},'risk_reward':{'result':'FAIL'}}
        result=audit_records([row],AT)['patterns']['B']
        self.assertNotIn('ACCELERATION_FAIL',result['failure_episode_counts'])

    def test_new_sampling_format_and_missing_time_are_explicit(self):
        row={'symbol':'079650','asof':AT.isoformat(),'trials':[{'setup':'C','formed':True,
              'ready':False,'entry':8000,'stop':7900,'failures':['LIQUIDITY_FAIL']}],
             'fast':{'reason':'EXECUTABLE_DEPTH_UNKNOWN'}}
        result=audit_records([row,{'symbol':'999990'}],AT)
        self.assertEqual(result['patterns']['C']['unique_geometry_episodes'],1)
        self.assertEqual(result['unknown_timestamp_records'],1)
        self.assertEqual(result['fast_rejection_evaluations']['FAST_EXECUTABLE_DEPTH_UNKNOWN'],1)
