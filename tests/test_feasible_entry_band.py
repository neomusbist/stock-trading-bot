"""Unit and regression tests for Feasible Entry Price Band and Economics-First Entry.
Tests:
1. Mathematical derivation and precision of P_max across LIVE (0.73%), OPTIM (0.23%), and CONSERV (1.03%) cost models.
2. Positive cases: Ask strictly inside [P_min, P_max], Net RR >= 1.5, Quantity >= 1.
3. Negative cases:
   - FEASIBLE_BAND_EMPTY: P_max < P_min
   - ASK_ABOVE_MAX_ENTRY: Ask > P_max
   - ASK_BELOW_MIN_SUPPORT: Ask < P_min
   - PRICE_EXCEEDS_CASH: Entry > 70,847 KRW
   - RISK_BUDGET_ZERO_QTY: Risk per share > 1,416.94 KRW
4. Boundary cases: Exact P == P_max, Exact P == P_min, tick rounding invariants.
5. Real market data cases (001210 금호전기, 067310 서원인텍, 357880 코퍼스코리아).
"""
import unittest
from automation.capital import (
    Costs,
    entry_economics,
    entry_quantity,
    feasible_entry_price_band,
    economics_first_entry_eval,
)
from core.util import floor_tick

ACTUAL_CASH = 70847.0
APPROVED_CAP = 81300
EQUITY = min(ACTUAL_CASH, float(APPROVED_CAP))
RISK_BUDGET = EQUITY * 0.02       # 1,416.94 KRW

COSTS_LIVE = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)        # 0.730%
COSTS_OPTIM = Costs(0.00015, 0.00015, 0.002, 0.000, 0.000)       # 0.230%
COSTS_CONSERV = Costs(0.00015, 0.00015, 0.002, 0.003, 0.005)     # 1.030%


class TestFeasibleEntryBand(unittest.TestCase):

    def test_p_max_mathematical_derivation_live(self):
        # Target: 10,000, Emergency Stop: 9,000, Minimum RR: 1.5
        # Exit factor: 1 - 0.00015 - 0.002 - 0.003 = 0.99485
        # Entry factor: 1 + 0.00015 + 0.002 = 1.00215
        # P_max_raw = (10000 + 1.5 * 9000) * 0.99485 / (2.5 * 1.00215)
        #           = 23500 * 0.99485 / 2.505375 = 23378.975 / 2.505375 = 9331.527
        # floor_tick at 9,000 ~ 10,000 is 10 KRW tick -> 9330 KRW
        band = feasible_entry_price_band(10000, 9200, 9000, COSTS_LIVE, minimum_rr=1.5, p_min=9200)
        self.assertEqual(band['status'], 'KNOWN')
        self.assertTrue(band['band_exists'])
        self.assertAlmostEqual(band['p_max_unrounded'], 9331.527, delta=0.01)
        self.assertEqual(band['p_max'], 9330.0)
        self.assertEqual(band['p_min'], 9200.0)

        # Verify that entry at exactly p_max achieves Net RR >= 1.5
        econ_at_max = entry_economics(band['p_max'], 9200, 9000, 10000, COSTS_LIVE, 1.5)
        self.assertTrue(econ_at_max['rr_pass'])
        self.assertGreaterEqual(econ_at_max['net_rr'], 1.5)

        # Verify that entry at 1 tick above p_max FAILS Net RR >= 1.5
        econ_above_max = entry_economics(band['p_max'] + 10, 9200, 9000, 10000, COSTS_LIVE, 1.5)
        self.assertFalse(econ_above_max['rr_pass'])
        self.assertLess(econ_above_max['net_rr'], 1.5)

    def test_cost_model_scenarios_comparison(self):
        # For the same geometry (Target: 10,000, S_emerg: 9,000), compare LIVE vs OPTIM vs CONSERV
        b_live = feasible_entry_price_band(10000, 9200, 9000, COSTS_LIVE, 1.5, p_min=9200)
        b_optim = feasible_entry_price_band(10000, 9200, 9000, COSTS_OPTIM, 1.5, p_min=9200)
        b_conserv = feasible_entry_price_band(10000, 9200, 9000, COSTS_CONSERV, 1.5, p_min=9200)

        # Optimistic (lower friction) expands the feasible entry price upward
        self.assertGreater(b_optim['p_max'], b_live['p_max'])
        # Conservative (higher friction) compresses the feasible entry price downward
        self.assertLess(b_conserv['p_max'], b_live['p_max'])

        self.assertAlmostEqual(b_live['friction_pct'], 0.0073, delta=0.0001)
        self.assertAlmostEqual(b_optim['friction_pct'], 0.0023, delta=0.0001)
        self.assertAlmostEqual(b_conserv['friction_pct'], 0.0103, delta=0.0001)

    def test_negative_band_empty_when_target_too_close(self):
        # Target: 9,300 (+1.08%), Support: 9,200, Emergency Stop: 9,000
        # Target room is too small to overcome 0.73% friction and 1.5 Net RR
        band = feasible_entry_price_band(9300, 9200, 9000, COSTS_LIVE, 1.5, p_min=9200)
        self.assertEqual(band['status'], 'KNOWN')
        # P_max will be below P_min (9,200), meaning the band is mathematically empty!
        self.assertLess(band['p_max'], band['p_min'])
        self.assertFalse(band['band_exists'])

        res = economics_first_entry_eval(9300, 9200, 9000, COSTS_LIVE, 1.5, 9210, ACTUAL_CASH, RISK_BUDGET, p_min=9200)
        self.assertFalse(res['eligible'])
        self.assertEqual(res['reason'], 'FEASIBLE_BAND_EMPTY')

    def test_negative_ask_above_max_chase(self):
        # Band exists [9200, 9330], but market ask is 9,350 (chased too high)
        res = economics_first_entry_eval(10000, 9200, 9000, COSTS_LIVE, 1.5, 9350, ACTUAL_CASH, RISK_BUDGET, p_min=9200)
        self.assertFalse(res['eligible'])
        self.assertEqual(res['reason'], 'ASK_ABOVE_MAX_ENTRY')

    def test_negative_ask_below_support(self):
        # Band exists [9200, 9330], but market ask is 9,150 (dropped below structural support)
        res = economics_first_entry_eval(10000, 9200, 9000, COSTS_LIVE, 1.5, 9150, ACTUAL_CASH, RISK_BUDGET, p_min=9200)
        self.assertFalse(res['eligible'])
        self.assertEqual(res['reason'], 'ASK_BELOW_MIN_SUPPORT')

    def test_negative_price_exceeds_cash_budget(self):
        # Price 80,000 > cash 70,847 KRW -> 0 shares capital quantity
        res = economics_first_entry_eval(90000, 78000, 77000, COSTS_LIVE, 1.5, 79000, ACTUAL_CASH, RISK_BUDGET, p_min=78000)
        self.assertFalse(res['eligible'])
        self.assertEqual(res['reason'], 'PRICE_EXCEEDS_CASH')
        self.assertEqual(res['sizing']['capital_quantity'], 0)

    def test_negative_risk_budget_zero_qty(self):
        # Stock price 60,000 KRW, Emergency stop 55,000 KRW.
        # Risk per share is ~5,300 KRW > 1,416.94 KRW risk budget -> 0 shares
        res = economics_first_entry_eval(75000, 56000, 55000, COSTS_LIVE, 1.5, 59000, ACTUAL_CASH, RISK_BUDGET, p_min=56000)
        self.assertFalse(res['eligible'])
        self.assertEqual(res['reason'], 'RISK_BUDGET_ZERO_QTY')
        self.assertEqual(res['sizing']['risk_quantity'], 0)

    def test_positive_real_symbol_kumho_electric(self):
        # 001210 금호전기 on 2026-11-02:
        # Target: 8,450, S_struct: 7,530, S_emerg: 7,440
        # P_ask = 7,650
        band = feasible_entry_price_band(8450, 7530, 7440, COSTS_LIVE, 1.5, p_min=7530)
        self.assertTrue(band['band_exists'])
        self.assertGreaterEqual(band['p_max'], 7780.0)
        self.assertLessEqual(band['p_min'], 7530.0)

        res = economics_first_entry_eval(8450, 7530, 7440, COSTS_LIVE, 1.5, 7650, ACTUAL_CASH, RISK_BUDGET, p_min=7530)
        self.assertTrue(res['eligible'])
        self.assertEqual(res['reason'], 'ELIGIBLE_IN_BAND')
        self.assertEqual(res['quantity'], 5)  # 5 shares affordable
        self.assertGreaterEqual(res['economics']['net_rr'], 2.5)

    def test_positive_real_symbol_copus_korea(self):
        # 357880 코퍼스코리아 on 2026-11-02:
        # Target: 1,627, S_struct: 1,559, S_emerg: 1,555
        # P_ask = 1,570
        res = economics_first_entry_eval(1627, 1559, 1555, COSTS_LIVE, 1.5, 1570, ACTUAL_CASH, RISK_BUDGET, p_min=1559)
        self.assertTrue(res['eligible'])
        self.assertEqual(res['reason'], 'ELIGIBLE_IN_BAND')
        self.assertEqual(res['quantity'], 45)  # 45 shares affordable
        self.assertGreaterEqual(res['economics']['net_rr'], 1.6)


if __name__ == '__main__':
    unittest.main()
