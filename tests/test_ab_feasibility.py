import unittest
from automation.capital import Costs
from research.ab_feasibility import (assess,cost_scenarios,pretrigger_headroom,distribution,
    IMPOSSIBLE,LATE,MISSING,ORDERABLE)

COSTS=Costs(.00015,.00015,.002,.002,.003)
AT='2026-10-23T09:27:00+09:00'

def row(**kw):
    return dict({'entry':1000,'trigger':999,'structural_stop':980,'emergency_stop':970,
        'target':1100,'proof_status':'PASS','at':AT,'structure_key':'fixed'},**kw)

class FeasibilityTests(unittest.TestCase):
    def test_positive_prices_alone_cannot_establish_orderability(self):
        self.assertEqual(assess(row(),COSTS)['category'],MISSING)
    def test_known_target_below_trigger_cannot_be_rescued_by_costs_or_rr(self):
        for cost in cost_scenarios(COSTS).values():
            for rr in (1.,1.25,1.5,2.):
                self.assertEqual(assess(row(target=998),cost,rr)['category'],IMPOSSIBLE)
    def test_missing_proof_is_not_failure_even_with_numeric_obstruction(self):
        a=assess(row(target=998,proof_status='UNKNOWN'),COSTS)
        self.assertEqual(a['category'],MISSING);self.assertTrue(a['numeric_obstruction_even_if_proof_missing'])
    def test_hypothetical_earlier_price_is_not_observed_delay(self):
        a=assess(row(target=1010,prior_viable_observation={'ask':980}),COSTS)
        self.assertNotEqual(a['category'],LATE)
    def test_real_prior_cost_zone_supports_delay_only_with_causal_qualification(self):
        p={'at':'2026-10-23T09:26:50+09:00','ask':985,'actual_received':True,'qualification_known':True,
            'quote_fresh':True,'structure_key':'fixed','structure_confirmed':True,'trigger_pending':True}
        self.assertEqual(assess(row(target=1040,entry=1020,prior_viable_observation=p),COSTS)['category'],LATE)
        p['qualification_known']=False
        self.assertNotEqual(assess(row(target=1040,entry=1020,prior_viable_observation=p),COSTS)['category'],LATE)
    def test_future_prior_evidence_does_not_establish_delay(self):
        p={'at':'2026-10-23T09:28:00+09:00','ask':985,'actual_received':True,'qualification_known':True,
            'quote_fresh':True,'structure_key':'fixed','structure_confirmed':True,'trigger_pending':True}
        self.assertNotEqual(assess(row(prior_viable_observation=p),COSTS)['category'],LATE)
    def test_real_orderable_requires_all_depth_quantity_and_engine_evidence(self):
        x=dict(actual_received=True,qualification_known=True,quote_fresh=True,depth_confirmed=True,engine_gates_pass=True,quantity=1)
        self.assertEqual(assess(row(execution_evidence=x),COSTS)['category'],ORDERABLE)
        x['quantity']=0;self.assertEqual(assess(row(execution_evidence=x),COSTS)['category'],MISSING)
    def test_diagnostic_classification_never_grants_order_permission(self):
        self.assertFalse(assess(row(),COSTS)['actual_order_permission'])
    def test_minimum_trigger_uses_real_tick_grid(self):
        a=assess(row(entry=2010,trigger=2005,structural_stop=1971,emergency_stop=1936,target=2050),COSTS)
        self.assertEqual(a['zone']['minimum_trigger_entry'],2010);self.assertEqual(a['category'],IMPOSSIBLE)
    def test_unformed_reference_can_only_report_room(self):
        quote={'ask':1000,'received_at':AT,'actual_received':True}
        reference={'price':1100,'available_at':AT,'confirmed_at':AT}
        a=pretrigger_headroom(quote,reference,AT)
        self.assertEqual(a['room_fraction'],.1)
        self.assertIsNone(a['structural_stop']);self.assertIsNone(a['target']);self.assertIsNone(a['maximum_entry'])
        reference['available_at']='2026-10-23T09:28:00+09:00'
        self.assertEqual(pretrigger_headroom(quote,reference,AT)['status'],'UNKNOWN')
    def test_stale_pretrigger_quote_cannot_define_current_ranking_room(self):
        q={'ask':1000,'received_at':'2026-10-23T09:26:00+09:00','actual_received':True}
        r={'price':1100,'available_at':AT,'confirmed_at':AT}
        self.assertEqual(pretrigger_headroom(q,r,AT)['status'],'UNKNOWN')
    def test_spread_distribution_excludes_unknown(self):
        self.assertEqual(distribution([None,1,2,3])['median'],2)

if __name__=='__main__':unittest.main()
