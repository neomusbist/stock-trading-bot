"""Independent context alternatives do not relax or authorize LIVE orders."""
from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from unittest.mock import Mock
from core.settings import KST,Config
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.fast_entry_context import observe_fast_entry_context,attach_fast_entry_observation

AT=datetime(2026,10,16,14,42,34,tzinfo=KST)


class FastEntryContextTests(unittest.TestCase):
    def signal(self):
        return {'symbol':'452280','pattern':'FAST','time':AT.isoformat(),'limit':16810,
            'stop':16760,'emergency':16750,'target':17850,
            'fast_entry_evidence':{'geometry':{'costs':dict(buy=.00015,sell=.00015,tax=.002,entry_slippage=.002,exit_slippage=.003)}}}

    def context(self):
        return {'asof':AT.isoformat(),'nearest_confirmed_peak':{'price':16880,
            'time':AT.replace(minute=27,second=0).isoformat(),
            'confirmed_at':AT.replace(minute=29,second=0).isoformat()},
            'trend_5m':{'status':'MIXED'},'coverage':'OBSERVED_BOUNDED_HISTORY_ONLY',
            'missing_earlier_history':'UNKNOWN'}

    def test_hansun_geometry_with_a_previously_confirmed_nearby_ceiling_has_negative_net_reward(self):
        # Only the prices are incident-derived. The fixture does not assert
        # that the original LIVE decision retained this context after restart.
        signal=self.signal();before=deepcopy(signal)
        result=observe_fast_entry_context(signal,self.context(),AT,1.5)
        self.assertEqual(result['status'],'REJECT_CANDIDATE')
        self.assertEqual(result['candidate_target'],16880)
        self.assertLess(result['candidate_net_rr'],0)
        self.assertEqual(signal,before);self.assertFalse(result['actual_order_permission'])

    def test_causal_uptrend_with_ample_reward_is_only_an_observation(self):
        ctx=self.context();ctx['nearest_confirmed_peak']['price']=17800;ctx['trend_5m']['status']='UP'
        result=observe_fast_entry_context(self.signal(),ctx,AT,1.5)
        self.assertEqual(result['status'],'OBSERVE_CANDIDATE')
        self.assertTrue(result['SHADOW_ONLY']);self.assertFalse(result['live_policy_changed'])

    def test_completed_downtrend_remains_separate_from_micro_rebound(self):
        ctx=self.context();ctx['nearest_confirmed_peak']['price']=17800;ctx['trend_5m']['status']='DOWN'
        self.assertEqual(observe_fast_entry_context(self.signal(),ctx,AT,1.5)['reason'],'COMPLETED_TREND_DOWN')

    def test_unknown_context_is_not_assumed_to_be_free_of_resistance(self):
        ctx=self.context();ctx['nearest_confirmed_peak']=None
        result=observe_fast_entry_context(self.signal(),ctx,AT,1.5)
        self.assertEqual(result['status'],'UNKNOWN');self.assertIsNone(result['candidate_target'])
        self.assertIsNone(result['actual_net_pnl'])

    def test_future_naive_and_cleared_peak_evidence_is_unknown(self):
        for name in ('future_confirmation','late_arrival','future_context','naive','cleared'):
            ctx=self.context();peak=ctx['nearest_confirmed_peak']
            if name=='future_confirmation':peak['confirmed_at']=(AT+timedelta(seconds=1)).isoformat()
            elif name=='late_arrival':peak['available_at']=(AT+timedelta(seconds=1)).isoformat()
            elif name=='future_context':ctx['asof']=(AT+timedelta(seconds=1)).isoformat()
            elif name=='naive':peak['time']=AT.replace(tzinfo=None).isoformat()
            else:peak['cleared_at']=(AT-timedelta(seconds=1)).isoformat()
            with self.subTest(name=name):
                self.assertEqual(observe_fast_entry_context(self.signal(),ctx,AT,1.5)['status'],'UNKNOWN')

    def test_missing_nonfinite_costs_do_not_create_reward(self):
        for costs in ({},dict(buy=float('nan'),sell=0,tax=0,entry_slippage=0,exit_slippage=0)):
            signal=self.signal();signal['fast_entry_evidence']['geometry']['costs']=costs
            self.assertEqual(observe_fast_entry_context(signal,self.context(),AT,1.5)['status'],'UNKNOWN')

    def test_attaching_existing_reference_data_does_not_create_real_time_warmup(self):
        detector=PatternDetector(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        reference=[{'time':AT.replace(minute=26+i,second=0),'open':16810,'low':16800,
            'high':h,'close':16820,'volume':100,'source':'REST_REFERENCE','venue':'KRX'}
            for i,h in enumerate((16820,16880,16870))]
        detector.price_context.merge_history('452280',reference,AT-timedelta(seconds=1),'KRX')
        signal=self.signal();saved=deepcopy(signal)
        attach_fast_entry_observation(signal,detector,Tick('452280',AT,16810,1,1,16810,16800,True),AT,1.5)
        self.assertEqual(detector.usable_bars('452280'),[])
        self.assertNotIn('452280',detector.observation_start)
        self.assertEqual(signal['fast_context_observation']['candidate_target'],16880)
        self.assertEqual({k:signal[k] for k in saved},saved)
        import json
        json.dumps(signal,allow_nan=False)

    def test_observation_result_cannot_cross_the_engine_order_boundary(self):
        from automation.engine import AutonomousEngine
        signal=self.signal();signal.update(observe_fast_entry_context(signal,self.context(),AT,1.5))
        engine=object.__new__(AutonomousEngine);engine.decisions=Mock()
        self.assertIsNone(engine.enter(signal,Tick('452280',AT,16810,1,1,16810,16800,True)))
