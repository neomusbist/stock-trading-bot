"""Regression contracts for the external benchmark's false STRICT results."""
import unittest
from dataclasses import replace
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs, entry_economics, entry_quantity
from core.settings import Config
from tests.test_causal_strategy_adapters import make_frame, AT, SYM
from research.causal_account import PaperAccount


class CommonCostRegressionTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,
                         max_position_fraction=1.,max_positions=1,
                         max_entries_per_day=None,max_entries_per_symbol=None)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,
                          costs_confirmed=True,max_consecutive_losses=3)
        self.costs=Costs(.00015,.00015,.002,.002,.003)

    def test_C_and_D_must_not_pass_gross_RR_when_net_RR_fails(self):
        t=make_frame(price=17180,ask=17180,bid=17170).tick
        for pattern in ('C','D'):
            with self.subTest(pattern=pattern):
                det=PatternDetector(self.cfg,self.auto,self.costs)
                setup=dict(pattern=pattern,breakout=17170,support=16950,projection=17610)
                gates=det.setup_gates(t,{'status':'WATCH','source_count':2},setup,[])
                rr=gates['risk_reward']
                self.assertGreater(rr['values']['gross_rr'],1.5)
                self.assertLess(rr['values']['net_rr'],1.5)
                self.assertEqual(rr['result'],'FAIL')

    def test_C_and_D_sizing_uses_the_same_cash_flow_as_entry_review(self):
        with self.subTest():
            account=PaperAccount(AT,initial_cash=70420,cfg=self.cfg,auto=self.auto)
            self.addCleanup(account.close)
            account.step(make_frame(price=17180,ask=17180,bid=17170),
                         market_status={'pass':True,'checked_at':AT.isoformat()})
            for pattern in ('C','D'):
                sig=dict(symbol=SYM,pattern=pattern,limit=17180,stop=16930,target=17610)
                econ=entry_economics(17180,16930,16930,17610,self.costs,1.5)
                expected=entry_quantity(econ,70420,1408.4)
                actual=account.e.quantity_details(sig,70420,70420,2816.8)
                self.assertEqual(actual['risk_quantity'],expected['risk_quantity'])
                self.assertEqual(actual['quantity'],expected['quantity'])
                self.assertAlmostEqual(actual['risk_per_share'],econ['risk_per_share'])

    def test_missing_pullback_open_is_unknown_not_a_crash_or_invented_open(self):
        det=PatternDetector(self.cfg,self.auto,self.costs)
        bars=[dict(low=10000,high=10200,close=10100,volume=1000) for _ in range(4)]
        setup,reason,evidence=det.pullback_analysis(bars)
        self.assertIsNone(setup)
        self.assertEqual(reason,'PULLBACK_BAR_DATA_UNKNOWN')


if __name__=='__main__':unittest.main()
