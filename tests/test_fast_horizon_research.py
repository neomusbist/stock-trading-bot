"""Recorded Lunit geometry and causal negative controls; no synthetic profits."""
import copy
import unittest
from automation.fast_horizon_research import compare_horizon
from automation.fast_momentum import FastSignal
from core.settings import KST
from datetime import datetime

AT='2026-10-19T11:37:39.577174+09:00'


def geometry():
    return {'entry':12890.,'stop':12840.,'emergency':12820.,'target':17510.,
        'target_known_at':'2026-10-19T11:37:15.551977+09:00',
        'costs':{'buy':.00015,'sell':.00015,'tax':.002,'entry_slippage':.002,'exit_slippage':.003},
        'structural_evidence':{'asof':AT,'support':12870.,'breakout':12880.,'origin':12840.,
            'support_at':'2026-10-19T11:37:36+09:00','breakout_at':'2026-10-19T11:37:36+09:00',
            'origin_at':'2026-10-19T11:36:08+09:00','confirmation_at':'2026-10-19T11:37:36+09:00'}}


class HorizonTests(unittest.TestCase):
    def test_lunit_high_rr_does_not_prove_near_horizon_net_reward(self):
        r=compare_horizon(geometry(),AT)
        self.assertAlmostEqual(r['daily_ceiling']['net_rr'],27.5099777,places=6)
        self.assertEqual(r['reaction_projection']['target'],12930)
        self.assertLess(r['reaction_projection']['net_reward_per_share'],0)
        self.assertGreater(r['minimum_exit_bid_for_existing_rr'],13200)
        self.assertFalse(r['actual_order_permission']);self.assertIsNone(r['net_pnl'])

    def test_future_structure_and_resistance_cannot_make_research_pass(self):
        for key in ('target_known_at','asof','confirmation_at','origin_at'):
            g=geometry()
            if key=='target_known_at':g[key]='2026-10-19T11:38:00+09:00'
            else:g['structural_evidence'][key]='2026-10-19T11:38:00+09:00'
            self.assertEqual(compare_horizon(g,AT)['status'],'UNKNOWN')

    def test_missing_geometry_is_unknown_not_an_assumed_opportunity(self):
        for key in ('costs','target_known_at','structural_evidence','emergency'):
            g=geometry();g.pop(key)
            self.assertEqual(compare_horizon(g,AT)['status'],'UNKNOWN')

    def test_high_local_range_can_pass_hypothesis_without_altering_risk(self):
        g=geometry();g['structural_evidence']['origin']=12000.
        original=copy.deepcopy(g);r=compare_horizon(g,AT)
        self.assertTrue(r['reaction_projection']['meets_existing_rr'])
        self.assertEqual(r['daily_ceiling']['net_risk_per_share'],r['reaction_projection']['net_risk_per_share'])
        self.assertEqual(g,original);self.assertFalse(r['profitability_proven'])

    def test_invalid_proportional_costs_fail_closed(self):
        for v in (float('nan'),float('inf'),-1,1,'bad'):
            g=geometry();g['costs']['exit_slippage']=v
            self.assertEqual(compare_horizon(g,AT)['status'],'UNKNOWN')

    def test_fast_cost_field_is_drag_and_preserves_net_rr_and_reward(self):
        sig=FastSignal('328130','Lunit',12890,12840,17510,12820,5,
            27.509977701748657,66.,4502.2190725,163.657678,'A','REGULAR',
            datetime(2026,10,19,11,37,39,tzinfo=KST))
        out=sig.to_engine_signal();eff=out['efficiency']
        self.assertAlmostEqual(eff['cost_per_share'],117.7809275)
        self.assertAlmostEqual(eff['cost_per_share']+eff['reward_after_cost'],17510-12890)
        self.assertEqual(eff['score'],sig.net_reward_risk)
        self.assertEqual(eff['risk_per_share'],sig.risk_net_per_share)
        self.assertEqual((out['limit'],out['stop'],out['emergency'],out['planned_qty']),
                         (12890,12840,12820,5))


if __name__=='__main__':unittest.main()
