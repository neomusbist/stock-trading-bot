"""Regression for frozen C's cost-inclusive box economics, not profitability."""
import unittest
from dataclasses import replace
from automation.capital import signal_economics
from automation.policy import AutomationConfig
from core.settings import Config

class FrozenCBoxCostBound(unittest.TestCase):
    def test_widest_box_best_price_and_no_buffer_still_fail(self):
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,min_reward_risk=1.5)
        auto=replace(AutomationConfig(),box_max_range=.015,commission_buy=.00015,
                     commission_sell=.00015,sell_tax=.002)
        # Optimistic synthetic upper bound: entry exactly at top, stop exactly
        # at low, target at top+2 widths; real buffer/chase only worsen it.
        low=10000;top=10150;target=10450
        result=signal_economics(cfg,auto,{'pattern':'C','limit':top,'stop':low,'target':target})
        self.assertAlmostEqual(result['net_rr'],1.004645747741495)
        self.assertFalse(result['rr_pass'])
        self.assertLess(result['net_rr'],cfg.min_reward_risk)

    def test_adverse_entry_or_lower_ceiling_cannot_fix_the_bound(self):
        cfg=replace(Config(),min_reward_risk=1.5)
        auto=replace(AutomationConfig(),commission_buy=.00015,commission_sell=.00015,sell_tax=.002)
        for entry,stop,target in ((10150,10000,10450),(10160,10000,10450),(10150,9990,10450),(10150,10000,10400)):
            with self.subTest(entry=entry,stop=stop,target=target):
                self.assertFalse(signal_economics(cfg,auto,{'pattern':'C','limit':entry,'stop':stop,'target':target})['rr_pass'])
