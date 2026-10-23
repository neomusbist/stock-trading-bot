"""Price/cost contracts only. Synthetic cases are not market performance."""
from dataclasses import replace
from datetime import datetime
import unittest
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.exit_plan import build_plan
from automation.patterns import PatternDetector
from automation.structural_pullback import structural_pullback
from tests.test_structural_a_exit import price_gates
from tests.test_structural_pullback import NORMAL,AT

COSTS=Costs(.00015,.00015,.002,.002,.003)

class ABPriceContracts(unittest.TestCase):
    def test_planned_loss_reserves_the_same_entry_cost_as_reward(self):
        signal=dict(pattern='B',limit=10000,stop=9800,target=11000)
        plan=build_plan(Config(),AutomationConfig(),signal)
        expected=10000-9800+10000*(.00015+.002)+9800*(.00015+.002+.003)
        self.assertAlmostEqual(plan['risk_after_cost_per_share'],expected)

    def test_A_gross_pass_is_not_a_net_reward_risk_pass(self):
        gates,_,_=price_gates()
        self.assertEqual(gates['risk_reward']['result'],'FAIL')
        self.assertEqual(gates['risk_reward']['values']['rr_basis'],'AB_COMMON_NET_REWARD_RISK')

    def test_B_known_peak_does_not_disappear_when_ask_crosses_it(self):
        auto=replace(AutomationConfig(),structural_pullback_enabled=True,confirmed_invalidation_enabled=True)
        d=PatternDetector(Config(),auto,COSTS);setup=d.pullback(NORMAL)
        _,proof=structural_pullback(NORMAL,AT,setup,Config(),1041,1040,1041,close_policy=True,auto=auto)
        self.assertEqual(proof['target_ceiling'],setup['peak'])

    def test_A_resistance_is_fixed_by_structure_trigger_not_decision_price(self):
        d=PatternDetector(Config(),AutomationConfig(),COSTS)
        tick=Tick('999990',AT,1050,100,10000,1050,1049)
        setup=dict(pattern='A',breakout=1030,support=1000,projection=1090)
        self.assertEqual(d.target(tick,{'resistance':1040},setup,1050),1040)

    def test_A_costs_use_the_observed_ask_even_when_chase_blocks_it(self):
        d=PatternDetector(Config(),AutomationConfig(),COSTS)
        tick=Tick('999990',AT,1100,100,10000,1100,1099)
        setup=dict(pattern='A',breakout=1000,support=970,projection=1060)
        gates=d.setup_gates(tick,{'status':'WATCH','source_details':{}},setup,[])
        self.assertEqual(gates['invalidation']['values']['entry'],1100)
        self.assertEqual(gates['chase']['result'],'FAIL')

    def test_common_zone_tick_boundary_and_integer_budget(self):
        from automation.capital import entry_economics,viable_entry_zone,entry_quantity
        zero=Costs(0.,0.,0.,0.,0.)
        e=entry_economics(1000,900,900,1150,zero,1.5)
        self.assertTrue(e['rr_pass']);self.assertEqual(e['max_entry_price'],1000)
        self.assertFalse(entry_economics(1001,900,900,1150,zero,1.5)['rr_pass'])
        zone=viable_entry_zone(e,950,.08)
        self.assertTrue(zone['trigger_compatible_zone']);self.assertEqual(zone['minimum_trigger_entry'],951)
        qty=entry_quantity(e,10000,250)
        self.assertEqual(qty['quantity'],2);self.assertLessEqual(qty['quantity']*e['risk_per_share'],250)

    def test_invalid_nonfinite_or_missing_evidence_is_unknown(self):
        from automation.capital import entry_economics
        for entry in (None,float('nan'),float('inf'),True,0):
            self.assertEqual(entry_economics(entry,900,890,1200,COSTS,1.5)['status'],'UNKNOWN')

    def test_integer_quantity_keeps_exact_decimal_budget_boundary(self):
        from automation.capital import entry_quantity
        economics=dict(status='KNOWN',one_share_cash=1.1,risk_per_share=.1)
        self.assertEqual(entry_quantity(economics,11.,1.)['quantity'],10)
        self.assertEqual(entry_quantity(economics,10.99,1.)['quantity'],9)

    def test_actual_exit_uses_confirmed_fees_without_double_slippage(self):
        from automation.capital import entry_economics
        actual=entry_economics(998,950,940,1200,COSTS,1.5,filled=True,
            actual_entry_fee_per_share=.2,actual_exit_price=1050,actual_exit_fee_per_share=2.4)
        self.assertTrue(actual['realized_costs_confirmed'])
        self.assertAlmostEqual(actual['realized_net_per_share'],49.4)

    def test_filled_price_removes_estimated_entry_slippage_and_accepts_actual_fee(self):
        from automation.capital import entry_economics
        planned=entry_economics(1000,950,940,1200,COSTS,1.5)
        actual=entry_economics(998,950,940,1200,COSTS,1.5,filled=True,actual_entry_fee_per_share=.2)
        self.assertEqual(actual['entry_slippage_reserve'],0)
        self.assertAlmostEqual(actual['buy_fee_per_share'],.2)
        self.assertLess(actual['risk_per_share'],planned['risk_per_share'])

    def test_quote_zone_is_not_a_trigger_or_order_permission(self):
        from automation.capital import entry_economics,viable_entry_zone
        e=entry_economics(1000,950,940,1250,COSTS,1.5)
        zone=viable_entry_zone(e,990,.008)
        self.assertFalse(zone['actual_order_permission'])
        self.assertFalse(zone['trigger_confirmed'])

    def test_same_economics_drives_score_and_exit_plan(self):
        from automation.capital import entry_economics
        from automation.allocation import efficiency
        e=entry_economics(1000,950,940,1250,COSTS,1.5)
        score=efficiency(1000,940,1250,.6,10,COSTS,economics=e)
        signal=dict(pattern='A',limit=1000,stop=940,target=1250,
                    invalidation_policy=dict(structural_stop=950,emergency_stop=940))
        plan=build_plan(Config(),replace(AutomationConfig(),confirmed_invalidation_enabled=True),signal)
        self.assertAlmostEqual(plan['net_reward_risk'],e['net_rr'])
        self.assertEqual(score['risk_per_share'],plan['risk_after_cost_per_share'])
        self.assertTrue(plan['reward_guard_enabled'])

if __name__=='__main__':unittest.main()
