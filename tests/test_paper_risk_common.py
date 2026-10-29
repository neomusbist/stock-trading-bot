"""Research-only D/E/E2 cash-flow sizing and order-seal regression."""
from unittest import TestCase
from automation.capital import signal_economics
from automation.exit_plan import build_plan
from tests import test_subscription_refresh as fixture


class PaperRiskCommonTests(TestCase):
    def setUp(self):
        self.h=fixture.SubscriptionRefreshTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def signal(self,pattern):
        return {'symbol':fixture.SYM,'pattern':pattern,'limit':10020,'stop':9800,
                'target':11000,'paper_only':True}

    def test_research_quantity_uses_the_same_cash_flow_risk_as_A(self):
        e=self.h.e
        for name in ('D','E','E2'):
            with self.subTest(pattern=name):
                sig=self.signal(name);econ=signal_economics(e.c,e.a,sig)
                q=e.quantity_details(sig,1000000,1000000,3000)
                self.assertEqual(econ['status'],'KNOWN')
                self.assertAlmostEqual(q['risk_per_share'],econ['risk_per_share'])
                self.assertLessEqual(q['quantity']*q['risk_per_share'],q['risk_budget'])
                self.assertFalse(e.research_paper_allowed(sig))

    def test_existing_or_reserved_paper_risk_uses_the_same_model(self):
        e=self.h.e
        for name in ('D','E','E2'):
            with self.subTest(pattern=name):
                sig=self.signal(name);econ=signal_economics(e.c,e.a,sig)
                order={'symbol':fixture.SYM,'side':'BUY','qty':2,'filled':0,'limit':10020,
                       'status':'ACCEPTED','signal':sig}
                self.assertAlmostEqual(e.risk_used([order]),2*econ['risk_per_share'])

    def test_exit_plan_records_and_requires_the_same_cost_model(self):
        e=self.h.e
        for name in ('D','E','E2'):
            with self.subTest(pattern=name):
                sig=self.signal(name);plan=build_plan(e.c,e.a,sig,quantity=1)
                self.assertTrue(plan['reward_guard_enabled'])
                self.assertEqual(plan['entry_economics']['status'],'KNOWN')
                self.assertAlmostEqual(plan['net_reward_risk'],sig_econ(e,sig)['net_rr'])

def sig_econ(engine,signal):
    return signal_economics(engine.c,engine.a,signal)
