"""Synthetic counterexamples prove code behavior, never a market entry."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import Mock
import unittest
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from research.initial_structure import InitialBaseResearch

AT=datetime(2026,10,23,9,6,tzinfo=KST)

def bar(at,o,h,l,c):
    return dict(time=at,open=o,high=h,low=l,close=c,volume=1000,observation_source='ACTUAL_RECEIVED',
                available_at=(at+timedelta(minutes=1)).isoformat(),first_second=0,gap_reason=None)

class InitialStructure(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=AutomationConfig();self.s=InitialBaseResearch(self.cfg,self.auto)
        self.base=[bar(AT-timedelta(minutes=3),991,1005,990,997),
                   bar(AT-timedelta(minutes=2),997,1004,994,1000),
                   bar(AT-timedelta(minutes=1),1000,1005,997,1003)]
        self.refs=[bar(AT-timedelta(minutes=6),1050,1080,1040,1070),
                   bar(AT-timedelta(minutes=5),1070,1120,1060,1100),
                   bar(AT-timedelta(minutes=4),1100,1105,1020,1040)]+deepcopy(self.base)
    def tick(self,offset=1,price=1006,cumulative=200):
        at=AT+timedelta(seconds=offset)
        return Tick('999990',at,price,100,cumulative,price,price-1,True,
            ask_qty=100,bid_qty=100,depth_available=True,depth_source='0B_FIELDS',
            book_time=at,received_at=at.isoformat(),book_received_at=at.isoformat())
    def evaluate(self,**kw):
        previous=self.tick(-1,1004,100)
        self.s.evaluate(previous,self.base,self.refs,previous.time)
        t=self.tick();return self.s.evaluate(t,self.base,self.refs,t.time,cash_budget=70000,risk_budget=1400,**kw)
    def test_target_is_a_preknown_peak_and_stop_is_the_local_base(self):
        signal,d=self.evaluate();self.assertIsNotNone(signal,d)
        self.assertEqual(signal['target'],1120);self.assertEqual(signal['research_structure']['support'],990)
        self.assertLess(signal['stop'],signal['invalidation_policy']['structural_stop'])
        self.assertGreaterEqual(signal['entry_economics']['net_rr'],1.5)
        self.assertTrue(signal['SHADOW_ONLY']);self.assertFalse(signal['actual_order_permission'])
        self.assertGreater(signal['planned_qty'],0)
        self.assertEqual(signal['research_structure']['formation_economics']['entry_basis'],
            'UNOBSERVED_FIRST_TRIGGER_TICK_FEASIBILITY_PROBE')
        self.assertEqual(signal['entry_economics']['entry_basis'],'OBSERVED_ASK_ORDER_LIMIT')
    def test_nearest_resistance_is_not_skipped_to_raise_the_target(self):
        for b,values in zip(self.refs[:3],[(1004,1008,1000,1006),(1006,1010,1002,1008),(1008,1009,1001,1006)]):
            b.update(zip(('open','high','low','close'),values))
        self.refs=[bar(AT-timedelta(minutes=9),1050,1080,1040,1070),
                   bar(AT-timedelta(minutes=8),1070,1120,1060,1100),
                   bar(AT-timedelta(minutes=7),1100,1105,1020,1040)]+self.refs
        _,d=self.evaluate();self.assertEqual(d['reason'],'COST_ZONE_EMPTY_BEFORE_TRIGGER')
        self.assertEqual(d['target'],1010);self.assertEqual(d['stage'],'ECONOMIC_FEASIBILITY')
    def test_future_reference_does_not_create_a_target(self):
        for b in self.refs[:3]:b['available_at']=(AT+timedelta(minutes=1)).isoformat()
        signal,d=self.evaluate();self.assertIsNone(signal);self.assertEqual(d['reason'],'NO_PREKNOWN_OVERHEAD_ABOVE_BASE')
    def test_rest_cannot_supply_signal_warmup(self):
        self.base[0]['observation_source']='REST_REFERENCE'
        self.assertIsNone(self.evaluate()[0])
    def test_missing_or_partial_bar_remains_unknown(self):
        for key,value in [('gap_reason','UNSUBSCRIBED'),('available_at',None)]:
            self.setUp();self.base[0][key]=value
            signal,d=self.evaluate();self.assertIsNone(signal);self.assertEqual(d['status'],'UNKNOWN')
    def test_price_already_above_the_level_is_not_a_retroactive_trigger(self):
        p=self.tick(-1,1007,100);self.s.evaluate(p,self.base,self.refs,p.time)
        t=self.tick();signal,d=self.s.evaluate(t,self.base,self.refs,t.time)
        self.assertIsNone(signal);self.assertEqual(d['reason'],'NO_OBSERVED_POST_STRUCTURE_CROSSING')
    def test_quote_retry_retains_the_actual_trigger_and_fixed_lines(self):
        p=self.tick(-1,1004,100);self.s.evaluate(p,self.base,self.refs,p.time)
        t=replace(self.tick(),depth_available=False)
        self.assertIsNone(self.s.evaluate(t,self.base,self.refs,t.time)[0])
        valid=replace(t,depth_available=True,book_time=t.time+timedelta(seconds=1),book_received_at=(t.time+timedelta(seconds=1)).isoformat())
        sig,d=self.s.evaluate(valid,self.base,self.refs,t.time+timedelta(seconds=1))
        self.assertIsNotNone(sig,d);self.assertEqual(sig['trigger_evidence']['trigger_at'],t.time.isoformat())
    def test_zero_cash_is_not_a_positive_order_quantity(self):
        previous=self.tick(-1,1004,100);self.s.evaluate(previous,self.base,self.refs,previous.time)
        t=self.tick();sig,d=self.s.evaluate(t,self.base,self.refs,t.time,cash_budget=0,risk_budget=1400)
        self.assertIsNone(sig);self.assertEqual(d['reason'],'ZERO_COMMON_RISK_OR_CASH_QUANTITY')
    def test_unobserved_account_does_not_get_an_invented_quantity(self):
        previous=self.tick(-1,1004,100);self.s.evaluate(previous,self.base,self.refs,previous.time)
        t=self.tick();sig,d=self.s.evaluate(t,self.base,self.refs,t.time)
        self.assertIsNotNone(sig,d);self.assertIsNone(sig['planned_qty'])
        self.assertFalse(d['actual_portfolio_orderable'])
    def test_noise_and_target_are_not_recomputed_to_match_a_later_ask(self):
        sig,d=self.evaluate();old=deepcopy(sig['research_structure'])
        t=self.tick(2,1007,300);sig2,d=self.s.evaluate(t,self.base,self.refs,t.time)
        self.assertIsNotNone(sig2,d)
        for k in ('target','structural_stop','emergency_stop','first_structure_at'):
            self.assertEqual(sig2['research_structure'][k],old[k])
    def test_live_engine_rejects_the_original_research_signal(self):
        from automation.engine import AutonomousEngine
        sig,d=self.evaluate();e=AutonomousEngine.__new__(AutonomousEngine);e.decisions=Mock()
        self.assertIsNone(e.enter(sig,self.tick()));self.assertEqual(e.decisions.emit.call_count,1)

if __name__=='__main__':unittest.main()
