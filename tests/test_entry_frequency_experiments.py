"""Offline alternatives and causal cohort boundaries, no broker access."""
import copy
from dataclasses import replace
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch,Mock
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.opening_frequency_research import OpeningPrint
from automation.entry_frequency_experiments import SixBarContinuationDetector,replay_variants,research_variants
from automation.patterns import PatternDetector
from automation.capital import Costs

AT=datetime(2026,10,16,10,6,tzinfo=KST)

class FrequencyExperimentsTests(unittest.TestCase):
    def bars(self):
        return [{'time':AT-timedelta(minutes=6-i),'open':100+i*2,'high':104+i*2,'low':98+i*2,
            'close':103+i*2,'volume':100 if i<3 else 150,'complete':True} for i in range(6)]

    def trial(self):
        return {'setup':'A','candidate_generated':True,'failed_reasons':['ACCELERATION_FAIL'],
            'entry_ready':False,'gates':{'breakout_structure':{'result':'PASS'},
                'acceleration':{'result':'FAIL','values':{'ratio':1.2,'min_ratio':3.}}}}

    def detector(self):
        return SixBarContinuationDetector(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))

    def test_continuation_alternative_requires_prior_bars_and_keeps_structure(self):
        d=self.detector();t=Tick('999990',AT,120,1,10,120,119,True)
        trial=self.trial();saved=copy.deepcopy(trial)
        with patch.object(PatternDetector,'setup_trials',return_value=[trial]):
            result=d.setup_trials(t,{},_bars=self.bars())
        self.assertTrue(result[0]['entry_ready']);self.assertEqual(trial,saved)
        self.assertFalse(result[0]['gates']['acceleration']['values']['orders_enabled'])

    def test_structure_failure_is_never_relaxed(self):
        trial=self.trial();trial['gates']['breakout_structure']['result']='FAIL'
        d=self.detector();t=Tick('999990',AT,120,1,10,120,119,True)
        with patch.object(PatternDetector,'setup_trials',return_value=[trial]):
            self.assertFalse(d.setup_trials(t,{},_bars=self.bars())[0]['entry_ready'])

    def test_missing_future_gapped_and_weak_volume_bars_cannot_confirm(self):
        for mutate in ('missing','future','gap','volume'):
            bars=self.bars()
            if mutate=='missing':bars.pop()
            elif mutate=='future':bars[-1]['time']=AT
            elif mutate=='gap':bars[3]['time']-=timedelta(seconds=30)
            else:
                for b in bars[3:]:b['volume']=1
            with self.subTest(mutate=mutate),patch.object(PatternDetector,'setup_trials',return_value=[self.trial()]):
                self.assertFalse(self.detector().setup_trials(Tick('999990',AT,120,1,10,120,119,True),{},_bars=bars)[0]['entry_ready'])

    def test_other_failed_gates_remain_failed(self):
        trial=self.trial();trial['failed_reasons'].append('LIQUIDITY_FAIL')
        with patch.object(PatternDetector,'setup_trials',return_value=[trial]):
            result=self.detector().setup_trials(Tick('999990',AT,120,1,10,120,119,True),{},_bars=self.bars())[0]
        self.assertFalse(result['entry_ready']);self.assertEqual(result['failed_reasons'],['LIQUIDITY_FAIL'])

    def test_same_prefix_never_uses_future_prints_or_mutates_live_configuration(self):
        cfg=Config();auto=AutomationConfig();before=(cfg,auto)
        tick=Tick('999990',AT+timedelta(seconds=1),120,1,10,120,119,True)
        result=replay_variants([OpeningPrint(tick,tick.time,{},'SIMULATED')],cfg,auto,AT)
        self.assertTrue(all(v['accepted_prints']==0 and v['detector_episodes']==0 for v in result['variants']))
        self.assertFalse(result['orders_enabled']);self.assertEqual((cfg,auto),before)
        self.assertTrue(all(v['actual_trades'] is None and v['net_expectancy'] is None for v in result['variants']))

    def test_variant_parameters_are_isolated_and_existing_bc_independence_remains(self):
        cfg=Config();variants=research_variants(cfg,AutomationConfig())
        self.assertEqual(cfg.min_turnover_ratio,3.)
        self.assertEqual(variants[1][1].min_bc_window_turnover,cfg.min_bc_window_turnover)
        self.assertEqual(variants[-1][1],cfg)

    def test_experimental_detector_signal_cannot_cross_engine_order_boundary(self):
        from automation.engine import AutonomousEngine
        t=Tick('999990',AT,120,1,10,120,119,True)
        with patch.object(PatternDetector,'signal',return_value={'symbol':t.symbol,'pattern':'A'}):
            signal=self.detector().signal(t,{'status':'WATCH'})
        engine=object.__new__(AutonomousEngine)
        engine.decisions=Mock()
        self.assertTrue(signal['SHADOW_ONLY']);self.assertTrue(signal['diagnostic_only'])
        self.assertFalse(signal['actual_order_permission'])
        # No broker, account or risk objects exist: the order boundary must
        # return before consulting them, even when the class is used directly.
        self.assertIsNone(engine.enter(signal,t))
