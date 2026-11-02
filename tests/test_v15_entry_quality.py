"""Dedicated V1.5 experiments: synthetic prices and isolated fake brokers only."""
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from core.settings import Config,KST
from automation.policy import AutomationConfig
from automation.trade_quality import fresh_box_basis
from automation.focus_selection import calculate_tier
from automation.radar import DynamicRadar

class BoxOriginTests(unittest.TestCase):
    def setUp(self):self.at=datetime(2026,10,2,12,tzinfo=KST)
    def bars(self,prices):
        return [{'time':self.at-timedelta(minutes=len(prices)-i),'open':v,'close':v,'high':v+1,'low':v-1,'volume':10} for i,v in enumerate(prices)]
    def check(self,bars):return fresh_box_basis(bars,self.at,5,.015)
    def test_first_live_breakout_does_not_require_an_extra_base(self):
        self.assertTrue(self.check(self.bars([1000]*5))[0])
    def test_completed_breakout_inside_claimed_base_rejected(self):
        passed,proof=self.check(self.bars([1000]*5+[1003,1004]))
        self.assertFalse(passed);self.assertEqual(proof['reason'],'BOX_CONTAINS_COMPLETED_BREAKOUT')
        self.assertLessEqual(datetime.fromisoformat(proof['confirmed_at']),self.at)
    def test_rearms_only_after_five_independent_completed_bars(self):
        self.assertFalse(self.check(self.bars([1000]*5+[1003]+[1003]*4))[0])
        self.assertTrue(self.check(self.bars([1000]*5+[1003]+[1003]*5))[0])
    def test_old_floor_failure_retires_old_base(self):
        self.assertTrue(self.check(self.bars([1000]*5+[1003,998,999]))[0])
    def test_future_close_cannot_change_past_decision(self):
        bars=self.bars([1000]*5+[1003,1004]);before=self.check(bars)
        bars.append({'time':self.at,'open':900,'close':900,'high':901,'low':899})
        self.assertEqual(self.check(bars),before)
    def test_gap_duplicate_partial_or_stale_history_unknown(self):
        for mutation in ('gap','duplicate','partial','stale'):
            bars=self.bars([1000]*7)
            if mutation=='gap':bars[3]['time']-=timedelta(minutes=1)
            elif mutation=='duplicate':bars[3]['time']=bars[2]['time']
            elif mutation=='partial':bars[3]['first_second']=10
            else:
                for b in bars:b['time']-=timedelta(minutes=1)
            with self.subTest(mutation=mutation):self.assertIsNone(self.check(bars)[0])
    def test_flags_default_off_and_only_strict_boolean(self):
        auto=AutomationConfig();self.assertFalse(auto.fresh_box_basis_enabled);self.assertFalse(auto.setup_aware_watch_priority)
        for flag in ('fresh_box_basis_enabled','setup_aware_watch_priority'):
            with self.assertRaises(ValueError):replace(auto,**{flag:1}).validate()

class SetupAwareSelectionTests(unittest.TestCase):
    def test_c_without_acceleration_beats_rank_only_when_price_gates_pass(self):
        at=datetime(2026,10,2,12,tzinfo=KST)
        cfg=replace(Config(),max_watchlist=1)
        auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000)
        raw=[{'code':s,'source_count':2,'source_details':{'one':{'observed_rank':i,'snapshot_at':at.isoformat(),'values':{}},'two':{'observed_rank':i,'snapshot_at':at.isoformat(),'values':{}}},'reference_price':1000} for i,s in enumerate(('000010','000020'),1)]
        gates={k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status','liquidity')};gates['acceleration']={'result':'FAIL'}
        trial={'setup':'C','acceleration_required':False,'candidate_generated':True,'entry_ready':True,'gates':{k:{'result':'PASS'} for k in ('liquidity','risk_reward','breakout','invalidation','chase','economic','order_price')}}
        def run(enabled,failed=False,age=0):
            radar=DynamicRadar(cfg,replace(auto,setup_aware_watch_priority=enabled),SimpleNamespace(locked=lambda s:False))
            t=deepcopy(trial)
            if failed:t['gates']['box_origin']={'result':'FAIL'}
            radar.momentum={'000020':{'timestamp':(at-timedelta(seconds=age)).isoformat(),'gates':gates,'setup_trials':[t]}}
            return radar.update(deepcopy(raw),lambda row:{'status':'ELIGIBLE'},at)[1][0]['code']
        self.assertEqual(run(False),'000010');self.assertEqual(run(True),'000020')
        self.assertEqual(run(True,failed=True),'000010');self.assertEqual(run(True,age=16),'000010')
    def test_invalid_origin_cannot_claim_geometry_readiness(self):
        gates={k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status','liquidity')};gates['acceleration']={'result':'FAIL'}
        t={'setup':'C','acceleration_required':False,'candidate_generated':True,'gates':{'liquidity':{'result':'PASS'},'box_origin':{'result':'FAIL'}}}
        self.assertEqual(calculate_tier(gates,[t]),4)

class DetectorIntegrationTests(unittest.TestCase):
    def test_origin_changes_c_price_decision_but_not_a(self):
        from automation.patterns import PatternDetector
        from automation.capital import Costs
        from core.strategy import Tick
        at=datetime(2026,10,2,12,tzinfo=KST);helper=BoxOriginTests();helper.at=at
        bars=helper.bars([1000]*5+[1003,1004]);tick=Tick('999990',at,1007,100,100,1007,1006)
        costs=Costs(.00015,.00015,.002,.0005,.005)
        auto=AutomationConfig();watch={'status':'WATCH','source_details':{'one':{},'two':{}},'resistance':2000}
        base=PatternDetector(Config(),auto,costs);new=PatternDetector(Config(),replace(auto,fresh_box_basis_enabled=True),costs)
        setup=base.box(bars);old=base.setup_gates(tick,watch,setup,bars);revised=new.setup_gates(tick,watch,setup,bars)
        self.assertNotIn('box_origin',old);self.assertEqual(revised['box_origin']['result'],'FAIL')
        setup['pattern']='A';self.assertEqual(base.setup_gates(tick,watch,setup,bars),new.setup_gates(tick,watch,setup,bars))
    def test_first_c_signal_and_engine_proof_recompute_agree(self):
        from tests import test_audit_followup as fixtures
        from automation.engine import AutonomousEngine
        fixture=fixtures.SetupReachabilityTests('runTest');fixture.setUp()
        try:
            detector,tick,watch=fixture.build('C')
            detector.cfg=replace(detector.cfg,entry_slippage_fraction=0,exit_slippage_fraction=0)
            detector.a=replace(detector.a,fresh_box_basis_enabled=True,commission_buy=0,commission_sell=0,sell_tax=0)
            fixture.e.c=detector.cfg;fixture.e.a=detector.a
            detector.ingest(tick,tick.time);signal=detector.signal(tick,watch)
            self.assertIsNotNone(signal);self.assertEqual(signal['evaluation_gates']['box_origin']['result'],'PASS')
            fixture.e.a=detector.a
            self.assertNotEqual(fixture.e.entry_market_reason(signal,1,tick),'BOX_ORIGIN_PROOF_MISSING_OR_INVALID')
            signal['decision_bar_snapshot'][0]['high']+=10
            self.assertEqual(fixture.e.entry_market_reason(signal,1,tick),'BOX_ORIGIN_PROOF_MISSING_OR_INVALID')
        finally:fixture.doCleanups()

class OriginOrderBoundaryTests(unittest.TestCase):
    def test_missing_or_tampered_proof_blocked_before_broker_access(self):
        from automation.engine import AutonomousEngine
        engine=SimpleNamespace(a=replace(AutomationConfig(),fresh_box_basis_enabled=True))
        at=datetime(2026,10,2,12,tzinfo=KST)
        signal={'pattern':'C','time':at.isoformat(),'decision_bar_snapshot':[]}
        self.assertEqual(AutonomousEngine.entry_market_reason(engine,signal,1),'BOX_ORIGIN_PROOF_MISSING_OR_INVALID')
        bars=BoxOriginTests();bars.at=at;signal['decision_bar_snapshot']=bars.bars([1000]*5)
        signal['evaluation_gates']={'box_origin':{'result':'PASS','values':{'enabled':True,'asof':at.isoformat()}}}
        self.assertEqual(AutonomousEngine.entry_market_reason(engine,signal,1),'BOX_ORIGIN_PROOF_MISSING_OR_INVALID')

if __name__=='__main__':unittest.main()
