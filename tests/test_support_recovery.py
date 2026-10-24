from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import unittest
from core.settings import Config
from automation.policy import AutomationConfig
from research.support_recovery import SupportRecoveryResearch
from tests import test_initial_structure as common
AT=common.AT

class Recovery(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=AutomationConfig();self.s=SupportRecoveryResearch(self.cfg,self.auto)
        self.base=[common.bar(AT-timedelta(minutes=3),1080,1090,1050,1060),
                   common.bar(AT-timedelta(minutes=2),1060,1065,1030,1040),
                   common.bar(AT-timedelta(minutes=1),1040,1045,1010,1020)]
        self.refs=[common.bar(AT-timedelta(minutes=6),980,1020,950,1010),
                   common.bar(AT-timedelta(minutes=5),1010,1120,1000,1100),
                   common.bar(AT-timedelta(minutes=4),1100,1105,1040,1080)]+deepcopy(self.base)
        self.helper=common.InitialStructure();self.helper.setUp()
    def run_path(self,prices=(1015,1012,1016)):
        result=None
        for i,price in enumerate(prices,1):
            t=self.helper.tick(i,price,i*100)
            result=self.s.evaluate(t,self.base,self.refs,t.time,cash_budget=70000,risk_budget=1400)
        return result
    def test_support_defense_then_actual_recovery_has_no_completed_recovery_bar(self):
        sig,d=self.run_path();self.assertIsNotNone(sig,d)
        self.assertEqual(sig['limit'],1016);self.assertEqual(sig['target'],1120)
        self.assertEqual(sig['research_structure']['support'],1010)
        self.assertEqual(sig['research_structure']['micro_defense']['rows'][0]['price'],1015)
        self.assertGreaterEqual(sig['entry_economics']['net_rr'],1.5)
        self.assertFalse(sig['actual_order_permission']);self.assertTrue(sig['SHADOW_ONLY'])
    def test_only_two_real_trades_cannot_confirm_a_recovery(self):
        sig,d=self.run_path((1015,1012));self.assertIsNone(sig)
        self.assertEqual(d['reason'],'POST_TOUCH_RECEIVED_PATH_INSUFFICIENT')
    def test_no_recovery_or_broken_support_never_creates_a_signal(self):
        for prices in ((1015,1014,1013),(1015,1009,1016)):
            self.setUp();self.assertIsNone(self.run_path(prices)[0])
    def test_a_downleg_peak_cannot_be_called_a_defended_upleg(self):
        self.refs[1]['close']=1005
        sig,d=self.run_path();self.assertIsNone(sig);self.assertEqual(d['reason'],'PREKNOWN_UPLEG_NOT_DEFENDED')
    def test_old_trades_before_touch_close_are_not_reused(self):
        for i,p in enumerate((1015,1012,1016),-3):
            t=self.helper.tick(i,p,100*(i+4))
            sig,d=self.s.evaluate(t,self.base,self.refs,t.time)
        self.assertIsNone(sig)
    def test_gap_clears_actual_micro_defense(self):
        sig,d=self.run_path();self.assertIsNotNone(sig,d)
        self.s.reset('999990');t=self.helper.tick(4,1017,400)
        self.assertIsNone(self.s.evaluate(t,self.base,self.refs,t.time)[0])

class ReceivedPrefixNoise(unittest.TestCase):
    def setUp(self):
        fixture=Recovery();fixture.setUp();self.f=fixture
        self.s=SupportRecoveryResearch(fixture.cfg,fixture.auto,noise_policy='received_prefix')
    def feed(self,offset,price=1018,**changes):
        t=replace(self.f.helper.tick(offset,price,(offset+100)*100),**changes)
        return self.s.evaluate(t,self.f.base,self.f.refs,t.time,cash_budget=70000,risk_budget=1400)
    def prefix(self):
        for i in range(-59,1):self.feed(i)
    def recovery(self):
        for i,p in enumerate((1015,1012,1016),1):result=self.feed(i,p)
        return result
    def test_full_received_prefix_freezes_noise_before_the_recovery(self):
        self.prefix();sig,d=self.recovery();self.assertIsNotNone(sig,d)
        p=sig['research_structure'];self.assertEqual(p['noise_band'],3)
        self.assertLess(p['noise_evidence']['cutoff'],sig['trigger_evidence']['trigger_at'])
        old,detail=self.f.run_path();self.assertIsNotNone(old,detail)
        for key in ('target','structural_stop','support','level'):
            self.assertEqual(p[key],old['research_structure'][key])
        self.feed(4,1060)
        self.assertEqual(self.s.cache['999990']['emergency_stop'],p['emergency_stop'])
    def test_missing_received_minute_cannot_use_a_smaller_emergency_band(self):
        sig,d=self.recovery();self.assertIsNone(sig)
        self.assertEqual(d['reason'],'PREFIX_TOO_FEW_ACTUAL_PRINTS')
    def test_a_gap_in_the_prefix_remains_unknown(self):
        for i in list(range(-59,-30))+list(range(-20,1)):self.feed(i)
        sig,d=self.recovery();self.assertIsNone(sig)
        self.assertEqual(d['reason'],'PREFIX_COVERAGE_INCOMPLETE')
    def test_unknown_quote_in_prefix_is_not_fabricated(self):
        self.prefix();self.s.prefix['999990'][10]['quote_fresh']=False
        sig,d=self.recovery();self.assertIsNone(sig)
        self.assertEqual(d['reason'],'PREFIX_QUOTE_HISTORY_UNKNOWN')
    def test_observed_wide_jump_can_widen_the_band(self):
        self.prefix();self.s.prefix['999990'][10]['price']=1100
        sig,d=self.recovery();self.assertIsNone(sig)
        self.assertEqual(d['noise_band'],82)
        self.assertEqual(d['reason'],'COST_ZONE_EMPTY_BEFORE_TRIGGER')
    def test_quote_arrival_preserves_trade_knowledge_timestamp(self):
        self.feed(1,1015);before=dict(self.s.recent['999990'][-1])
        t=self.f.helper.tick(1,1015,10100);now=t.time+timedelta(milliseconds=100)
        self.s.evaluate(replace(t,bid=1015),self.f.base,self.f.refs,now)
        after=self.s.recent['999990'][-1]
        self.assertEqual(after['known_at'],before['known_at'])
        self.assertEqual(after['quote_known_at'],now)
        self.assertEqual(self.s.prefix['999990'][-1]['bid'],before['bid'])
    def test_capture_gap_clears_the_received_noise_prefix(self):
        self.prefix();self.s.reset('999990')
        sig,d=self.recovery();self.assertIsNone(sig)
        self.assertEqual(d['reason'],'PREFIX_TOO_FEW_ACTUAL_PRINTS')

if __name__=='__main__':unittest.main()
