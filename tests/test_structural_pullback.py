"""Synthetic structural analogues; frozen broker evidence remains outside Git."""
import copy
import unittest
from datetime import datetime,timedelta
from dataclasses import replace
from types import SimpleNamespace
from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.structural_pullback import structural_pullback
from automation.capital import Costs
from automation.engine import AutonomousEngine
from core.settings import Config,KST
from core.strategy import Tick
from collections import deque
from tests import test_audit_followup as fixture

AT=datetime(2026,9,30,11,tzinfo=KST)


def bars(rows):
    return [dict(time=AT-timedelta(minutes=len(rows)-i),open=o,high=h,low=l,close=c,volume=v,first_second=0)
            for i,(o,h,l,c,v) in enumerate(rows)]


INCIDENT=bars([(1000,1008,1000,1006,100),(1006,1018,1004,1016,600),
    (1016,1019,1016,1019,300),(1019,1028,1018,1024,700),
    (1024,1025,1015,1016,500),(1016,1030,1016,1019,500),
    (1020,1020,1016,1017,200),(1017,1018,1015,1015,100),(1015,1018,1015,1018,50)])
NORMAL=bars([(1000,1005,1000,1004,100),(1004,1030,1004,1028,400),
    (1028,1040,1020,1038,500),(1036,1036,1030,1032,100),(1032,1037,1031,1037,80)])


class StructuralPullbackTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=replace(AutomationConfig(),structural_pullback_enabled=True)
        self.costs=Costs(.00015,.00015,.002,.002,.003)
        self.d=PatternDetector(self.cfg,self.auto,self.costs)
    def gates(self,raw,entry,bid=None,enabled=True):
        d=PatternDetector(self.cfg,replace(self.auto,structural_pullback_enabled=enabled),self.costs)
        setup=d.pullback(raw);self.assertIsNotNone(setup)
        tick=Tick('999990',AT,entry,100,10000,entry,bid if bid is not None else entry-1)
        return d.setup_gates(tick,{'status':'WATCH','resistance':entry*2,'source_details':{'a':{},'b':{}}},setup,raw),setup
    def test_incident_unconfirmed_floor_and_overhead_inflate_original_rr(self):
        old,_=self.gates(INCIDENT,1022,1018,False);new,_=self.gates(INCIDENT,1022,1018)
        self.assertTrue(all(g['result']=='PASS' for g in old.values()))
        self.assertGreater(old['risk_reward']['values']['calculated_rr'],4)
        self.assertEqual(new['pullback_structure']['values']['reason'],'PULLBACK_SUPPORT_NOT_CONFIRMED')
        self.assertEqual(new['risk_reward']['values']['target'],1028)
        self.assertEqual(new['risk_reward']['result'],'FAIL')
        self.assertEqual(new['economic']['result'],'FAIL')
    def test_normal_shallow_pullback_does_not_need_twenty_percent_or_fixed_stop_width(self):
        gates,_=self.gates(NORMAL,1041)
        self.assertTrue(all(g['result']=='PASS' for g in gates.values()),gates)
        self.assertEqual(gates['pullback_structure']['values']['support'],1030)
    def test_normal_narrow_structural_stop_is_allowed_when_confirmed_and_rr_sufficient(self):
        raw=bars([(9750,9810,9750,9800,100),(9800,9920,9790,9900,300),
            (9900,10300,9890,10280,800),(10270,10270,10210,10220,100),
            (10220,10220,10150,10160,100),(10160,10160,10110,10120,100),
            (10120,10120,10090,10110,60),(10110,10130,10100,10130,50)])
        gates,_=self.gates(raw,10140,10130)
        self.assertTrue(all(g['result']=='PASS' for g in gates.values()),gates)
        self.assertLess(gates['invalidation']['values']['distance_pct'],.012)
        self.assertGreaterEqual(gates['risk_reward']['values']['calculated_rr'],1.5)
    def test_new_low_without_completed_recovery_cannot_be_support(self):
        raw=copy.deepcopy(NORMAL);raw[-1].update(low=1028,close=1030)
        gates,_=self.gates(raw,1041)
        self.assertEqual(gates['pullback_structure']['result'],'FAIL')
    def test_single_shallow_floor_and_completed_breakout_reuse_are_rejected(self):
        for raw in (NORMAL[:4],bars([(1000,1020,998,1010,300),(1010,1019,1005,1012,100),
            (1008,1030,1002,1030,400),(1027,1040,1009,1035,500),(1034,1039,1034,1038,100)])):
            # Retimestamp truncation: its final candle must be the latest full minute.
            raw=bars([(b['open'],b['high'],b['low'],b['close'],b['volume']) for b in raw])
            setup=self.d.pullback(raw);self.assertIsNotNone(setup)
            passed,proof=structural_pullback(raw,AT,setup,self.cfg,setup['peak']+2,setup['peak']+1,setup['peak']+2)
            self.assertFalse(passed);self.assertEqual(proof['reason'],'PULLBACK_SUPPORT_NOT_CONFIRMED')
    def test_future_partial_gap_duplicate_and_unknown_quotes_never_confirm_support(self):
        setup=self.d.pullback(NORMAL)
        variants=[]
        for change in ('future','partial','gap','duplicate','missing_volume'):
            raw=copy.deepcopy(NORMAL)
            if change=='future':raw[-1]['time']=AT
            elif change=='partial':raw[-1]['first_second']=20
            elif change=='gap':raw[-1]['gap_reason']='LOCAL_DROP'
            elif change=='duplicate':raw[-1]['time']=raw[-2]['time']
            else:raw[-1].pop('volume')
            variants.append(raw)
        for raw in variants:self.assertIsNot(structural_pullback(raw,AT,setup,self.cfg,1041,1040,1041)[0],True)
        self.assertIsNot(structural_pullback(NORMAL,AT,setup,self.cfg,1041,0,1041)[0],True)
    def test_valid_support_can_still_fail_execution_noise_without_widening_stop(self):
        setup=self.d.pullback(NORMAL)
        passed,proof=structural_pullback(NORMAL,AT,setup,replace(self.cfg,exit_slippage_fraction=.02),1041,1040,1041)
        self.assertFalse(passed);self.assertEqual(proof['reason'],'PULLBACK_RISK_WITHIN_OBSERVED_NOISE')
    def test_final_boundary_recomputes_proof_and_rejects_stop_target_and_bar_tampering(self):
        gates,setup=self.gates(NORMAL,1041);proof=gates['pullback_structure']['values']
        signal={**setup,'symbol':'999990','time':AT.isoformat(),'limit':1041,'stop':gates['invalidation']['values']['stop'],
                'target':gates['risk_reward']['values']['target'],'evaluation_gates':gates,
                'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in NORMAL]}
        e=SimpleNamespace(a=self.auto,c=self.cfg,clock=lambda:AT,current_session=lambda:'REGULAR')
        # Passing the structural boundary proceeds to existing market checks.
        tick=Tick('999990',AT,1041,10,1000,1041,1040)
        self.assertNotEqual(AutonomousEngine.entry_market_reason(e,signal,1,tick),'PULLBACK_STRUCTURE_PROOF_MISSING_OR_INVALID')
        for change in ('bar','stop','target','proof'):
            bad=copy.deepcopy(signal)
            if change=='bar':bad['decision_bar_snapshot'][0]['close']-=1
            elif change=='stop':bad['stop']+=1
            elif change=='target':bad['target']=bad['projection']+1
            else:bad['evaluation_gates']['pullback_structure']['values']['buffer_amount']+=1
            self.assertEqual(AutonomousEngine.entry_market_reason(e,bad,1,tick),'PULLBACK_STRUCTURE_PROOF_MISSING_OR_INVALID')
    def test_a_c_gates_unchanged_and_boolean_flag_is_strict(self):
        setup=self.d.pullback(NORMAL);setup['pattern']='A'
        t=Tick('999990',AT,1041,10,1000,1041,1040);watch={'status':'WATCH','resistance':2000}
        old=PatternDetector(self.cfg,AutomationConfig(),self.costs)
        self.assertEqual(old.setup_gates(t,watch,setup,NORMAL),self.d.setup_gates(t,watch,setup,NORMAL))
        for bad in (1,'true',None):
            with self.assertRaises(ValueError):replace(self.auto,structural_pullback_enabled=bad).validate()
    def test_final_boundary_uses_fresh_book_when_trade_quote_has_become_old(self):
        gates,setup=self.gates(NORMAL,1041)
        signal={**setup,'symbol':'999990','time':AT.isoformat(),'limit':1041,
            'stop':gates['invalidation']['values']['stop'],'target':gates['risk_reward']['values']['target'],
            'evaluation_gates':gates,'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in NORMAL]}
        tick=Tick('999990',AT,1041,10,1000,1041,1040)
        book={'time':AT.isoformat(),'best_bid':1040,'best_ask':1041}
        e=SimpleNamespace(a=self.auto,c=self.cfg,clock=lambda:AT,current_session=lambda:'REGULAR',entry_quote_provider=lambda s:(tick,book))
        self.assertIsNone(AutonomousEngine.entry_market_reason(e,signal,1))
        book['best_ask']=1042
        self.assertEqual(AutonomousEngine.entry_market_reason(e,signal,1),'ENTRY_QUOTE_ABOVE_LIMIT')
        book['best_ask']=1041;book['time']=(AT-timedelta(seconds=6)).isoformat()
        self.assertEqual(AutonomousEngine.entry_market_reason(e,signal,1),'PULLBACK_ENTRY_BOOK_STALE')
        book.clear()
        self.assertEqual(AutonomousEngine.entry_market_reason(e,signal,1),'PULLBACK_ENTRY_BOOK_UNAVAILABLE')


class StructuralOrderIntegrationTests(unittest.TestCase):
    setUp=fixture.AuditFixture.setUp
    build=fixture.SetupReachabilityTests.build
    def prepare(self,raw,price):
        self.clock[0]=AT
        self.e.s['day']=AT.date().isoformat()
        d,t,w=self.build('B');d.a=replace(d.a,structural_pullback_enabled=True);self.e.a=d.a
        d.history['999990']=deque(copy.deepcopy(raw));d.observation_start['999990']=AT-timedelta(minutes=len(raw))
        d.observation_session['999990']=(AT.date(),'REGULAR')
        t=replace(t,price=price,ask=price,bid=price-1)
        self.assertTrue(d.ingest(t,AT))
        return d,t,w
    def test_confirmed_normal_support_reaches_actual_fake_order_and_initial_stop(self):
        d,t,w=self.prepare(NORMAL,1041);signal=d.signal(t,w)
        self.assertIsNotNone(signal);self.assertEqual(signal['pattern'],'B')
        order=self.e.enter(signal,t);self.assertIsNotNone(order)
        self.assertEqual(len(self.broker.orders),1)
        self.e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
        self.assertEqual(self.e.s['positions']['999990']['initial_stop'],signal['evaluation_gates']['pullback_structure']['values']['buffered_stop'])
    def test_incident_b_trial_cannot_reach_order_and_changed_spread_fails_final_boundary(self):
        d,t,w=self.prepare(INCIDENT,1022);d.signal(t,w)
        trial=next(x for x in d.last_eval['999990']['trials'] if x['setup']=='B')
        self.assertFalse(trial['entry_ready']);self.assertEqual(self.broker.orders,[])
        d,t,w=self.prepare(NORMAL,1041);signal=d.signal(t,w)
        self.assertEqual(self.e.entry_market_reason(signal,1,replace(t,bid=t.ask-3)),'PULLBACK_STRUCTURE_SPREAD_CHANGED')
        bad=copy.deepcopy(signal);bad['stop']+=1
        self.assertIsNone(self.e.enter(bad,t));self.assertEqual(self.broker.orders,[])


if __name__=='__main__':unittest.main()
