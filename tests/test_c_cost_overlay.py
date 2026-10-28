import unittest
from dataclasses import replace
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs,entry_economics,entry_quantity,loss_reserve
from automation.exit_plan import build_plan
from core.settings import Config
from tests.test_causal_strategy_adapters import make_frame,AT,SYM
from research.causal_account import PaperAccount

class CostC(unittest.TestCase):
 def setUp(self):
  self.cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,max_position_fraction=1.,max_positions=1,max_entries_per_day=None,max_entries_per_symbol=None)
  self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,max_consecutive_losses=3)
  self.cost=Costs(.00015,.00015,.002,.002,.003)
  self.sig=dict(symbol=SYM,pattern='C',limit=17180,stop=16930,target=17610)
 def test_c_net_rr_rejects_gross_only_pass(self):
  tick=make_frame(price=17180,ask=17180,bid=17170).tick
  det=PatternDetector(self.cfg,self.auto,self.cost)
  gate=det.setup_gates(tick,{'status':'WATCH','source_count':2},dict(pattern='C',breakout=17170,support=16950,projection=17610),[])['risk_reward']
  self.assertGreater(gate['values']['gross_rr'],1.5)
  self.assertLess(gate['values']['net_rr'],1.5)
  self.assertEqual(gate['result'],'FAIL')
 def test_c_quantity_budget_uses_same_cost_model(self):
  account=PaperAccount(AT,initial_cash=70420,cfg=self.cfg,auto=self.auto); self.addCleanup(account.close)
  account.step(make_frame(price=17180,ask=17180,bid=17170),market_status={'pass':True,'checked_at':AT.isoformat()})
  econ=entry_economics(17180,16930,16930,17610,self.cost,1.5)
  expected=entry_quantity(econ,70420,1408.4);actual=account.e.quantity_details(self.sig,70420,70420,2816.8)
  self.assertEqual(actual['risk_quantity'],expected['risk_quantity'])
  self.assertEqual(actual['quantity'],expected['quantity'])
  self.assertAlmostEqual(actual['risk_per_share'],econ['risk_per_share'])
 def test_c_exit_plan_must_apply_net_rr_without_optional_guard(self):
  plan=build_plan(self.cfg,self.auto,self.sig,quantity=1)
  self.assertTrue(plan['reward_guard_enabled'])
  self.assertEqual(plan['entry_economics']['status'],'KNOWN')
  self.assertFalse(plan['reward_guard_pass'])
 def test_c_rejects_unknown_economics_for_sizing(self):
  account=PaperAccount(AT,initial_cash=70420,cfg=self.cfg,auto=self.auto);self.addCleanup(account.close)
  account.step(make_frame(price=17180,ask=17180,bid=17170),market_status={'pass':True,'checked_at':AT.isoformat()})
  missing={k:v for k,v in self.sig.items() if k!='target'}
  self.assertEqual(account.e.quantity_details(missing,70420,70420,2816.8)['quantity'],0)
 def test_c_partial_pending_buy_reserves_cost_adjusted_remaining_quantity(self):
  account=PaperAccount(AT,initial_cash=70420,cfg=self.cfg,auto=self.auto);self.addCleanup(account.close)
  account.step(make_frame(price=17180,ask=17180,bid=17170),market_status={'pass':True,'checked_at':AT.isoformat()})
  sig=dict(self.sig);order=dict(signal=sig,limit=17180,qty=4,filled=1)
  econ=entry_economics(17180,16930,16930,17610,self.cost,1.5)
  self.assertAlmostEqual(account.e.risk_used([order],execution=True),econ['risk_per_share']*3)
 def test_c_unknown_pending_order_uses_full_risk_budget(self):
  account=PaperAccount(AT,initial_cash=70420,cfg=self.cfg,auto=self.auto);self.addCleanup(account.close)
  account.step(make_frame(price=17180,ask=17180,bid=17170),market_status={'pass':True,'checked_at':AT.isoformat()})
  sig={k:v for k,v in self.sig.items() if k!='target'}
  self.assertEqual(account.e.risk_used([dict(signal=sig,limit=17180,qty=2,filled=0)],execution=True),account.e.daily_risk_budget())
if __name__=='__main__': unittest.main()
