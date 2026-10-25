"""Offline synthetic contracts; these fixtures are not historical fills."""
from copy import deepcopy
from dataclasses import replace
import unittest

from automation.capital import Costs, entry_economics, entry_quantity
from core.settings import Config
from research.recovery_stress import stress_sizing, entry_stresses, protection_stress

COSTS = Costs(.00015, .00015, .002, .002, .003)
RISK = 70847 * .02


def economic(stop=2010, entry=2045):
    return entry_economics(entry, 2025, stop, 2140, COSTS, 1.5)


def case():
    return {'recorded_entry_ask': 2045, 'scenario_cash': 70847,
        'scenario_per_trade_risk': RISK, 'prefix': {'max_adjacent_price_change': 15},
        'unchanged_geometry': {'level': 2040, 'structural_stop': 2025, 'target': 2140}}


class BoundaryAndSizing(unittest.TestCase):
    def test_one_entry_tick_loses_RR_even_with_positive_diagnostic_quantity(self):
        # This frozen historical cash scenario used fraction=1; Config's
        # example default=.20 must not silently substitute for that evidence.
        rows = entry_stresses(case(), {'emergency': 2010}, replace(Config(), max_position_fraction=1), COSTS)
        self.assertTrue(rows[0]['price_RR_pass'])
        self.assertEqual(rows[0]['economics']['max_entry_price'], 2045)
        self.assertEqual(rows[1]['price'], 2050)
        self.assertFalse(rows[1]['price_RR_pass'])
        self.assertEqual(rows[1]['diagnostic_sizing']['quantity'], 25)
        self.assertFalse(rows[1]['historically_received_quote'])
        self.assertFalse(rows[1]['actual_order_permission'])

    def test_cap_reuses_common_model_without_changing_geometry_or_costs(self):
        e = economic(); before = deepcopy(e)
        cap = stress_sizing(e, 70847, RISK, 15, COSTS)
        self.assertEqual(e, before)
        self.assertEqual(cap['ordinary_quantity'], 28)
        self.assertEqual(cap['quantity'], 21)
        self.assertEqual(cap['emergency_stop'], 2010)
        self.assertEqual(cap['target'], 2140)
        self.assertAlmostEqual(cap['stressed_loss_with_reserves'], 1358.091)
        self.assertTrue(cap['within_scenario_budget'])
        self.assertFalse(cap['live_enabled'])

    def test_stress_cap_never_increases_size_and_stronger_jump_never_increases_it(self):
        e = economic(); quantities = [stress_sizing(e, 70847, RISK, m, COSTS)['quantity'] for m in (5, 15, 50)]
        self.assertEqual(quantities, sorted(quantities, reverse=True))
        self.assertLessEqual(max(quantities), entry_quantity(e, 70847, RISK)['quantity'])

    def test_missing_cash_or_risk_or_prefix_is_unknown_not_zero_or_orderable(self):
        for cash, risk, jump in ((None, RISK, 15), (70847, None, 15), (70847, RISK, None),
                                  (70847, RISK, 0), (70847, RISK, float('nan'))):
            with self.subTest(cash=cash, risk=risk, jump=jump):
                cap = stress_sizing(economic(), cash, risk, jump, COSTS)
                self.assertEqual(cap['status'], 'UNKNOWN')
                self.assertIsNone(cap['quantity'])

    def test_cap_respects_cash_and_daily_remaining_risk_instead_of_assuming_two_percent(self):
        cap = stress_sizing(economic(), 4100, 80, 15, COSTS)
        self.assertEqual(cap['quantity'], 1)
        self.assertLessEqual(cap['stressed_loss_with_reserves'], 80)
        self.assertEqual(stress_sizing(economic(), 70847, 0, 15, COSTS)['quantity'], 0)

    def test_exact_integer_boundary_uses_shared_decimal_sizing(self):
        cap = stress_sizing(economic(), 70847, 64.671 * 20, 15, COSTS)
        self.assertEqual(cap['quantity'], 20)

    def test_stress_price_below_zero_is_unknown(self):
        cap = stress_sizing(economic(), 70847, RISK, 2020, COSTS)
        self.assertIsNone(cap['quantity'])


class ProtectionEvidence(unittest.TestCase):
    def model(self):
        return {'sizing_scenario': {'quantity': 28}, 'path': {
            'first_eligible_complete_close_failure': {'available_at': '2026-10-23T13:37:00+09:00'},
            'first_actionable_emergency_at': '2026-10-23T13:42:20+09:00',
            'first_fixed_line_protection_event': {'at': '2026-10-23T13:37:00+09:00', 'reason': 'CONFIRMED_COMPLETE_CLOSE'},
            'liquidation_sensitivity': [{'quote_at': '2026-10-23T13:37:00+09:00', 'bid': 2025,
                'available_bid_qty': 200, 'full_quantity_L1_observed': True, 'actual_fill': False}]}}

    def test_close_before_emergency_is_not_an_emitted_order_or_actual_fill(self):
        result = protection_stress(case(), self.model(), Config(), COSTS, {'quantity': 21})
        self.assertTrue(result['structural_close_precedes_emergency'])
        self.assertEqual(result['actual_protection_request_at'], 'UNKNOWN_NONEXECUTED_RESEARCH_HYPOTHESIS')
        self.assertFalse(result['stop_stability_validated'])
        self.assertAlmostEqual(result['latency_scenarios'][0]['loss_reserve_scenarios']['stress_capped_scenario'], 731.3355)

    def test_quote_gap_cannot_be_filled_with_a_last_trade_price(self):
        model = self.model(); model['path']['liquidation_sensitivity'][0]['quote_at'] = None
        result = protection_stress(case(), model, Config(), COSTS, {'quantity': 21})
        self.assertIsNone(result['latency_scenarios'][0]['loss_reserve_scenarios']['ordinary_scenario'])
        self.assertFalse(result['latency_scenarios'][0]['actual_fill'])

    def test_insufficient_observed_depth_does_not_certify_liquidation(self):
        model = self.model(); model['path']['liquidation_sensitivity'][0]['available_bid_qty'] = 10
        result = protection_stress(case(), model, Config(), COSTS, {'quantity': 21})
        self.assertIsNone(result['latency_scenarios'][0]['loss_reserve_scenarios']['stress_capped_scenario'])


if __name__ == '__main__':
    unittest.main()
