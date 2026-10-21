"""Discovery and diagnostics must use the same bounded capital as order sizing."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
import unittest

from automation.entry_funnel import EntryFunnel
from automation.policy import AutomationConfig
from automation.radar import DynamicRadar
from core.settings import Config, KST

AT = datetime(2026, 10, 22, 9, 5, tzinfo=KST)


class EntryCapacityContractTests(unittest.TestCase):
    def radar(self, context):
        r = DynamicRadar(replace(Config(), max_position_fraction=1.),
                         replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300),
                         SimpleNamespace())
        r.available_capital = 71760
        r.capital_context = context
        return r

    def test_actual_bot_book_budget_not_broker_cash_determines_one_share(self):
        r = self.radar({'effective_auto_cap':71760, 'entry_budget':71334.7759, 'account_fresh':True})
        row = {'reference_price':71500}
        self.assertFalse(r.affordable(row))
        self.assertEqual(row['affordability']['budget'], 71334.7759)
        self.assertEqual(row['affordability']['reason_code'], 'ONE_SHARE_UNAFFORDABLE')

    def test_explicit_position_fraction_budget_must_not_be_overridden(self):
        r = self.radar({'effective_auto_cap':71760, 'entry_budget':14266.95518, 'account_fresh':True})
        self.assertFalse(r.affordable({'reference_price':20000}))

    def test_explicit_zero_entry_budget_is_not_missing_budget(self):
        r = self.radar({'effective_auto_cap':71760, 'entry_budget':0, 'account_fresh':True})
        self.assertFalse(r.affordable({'reference_price':1000}))

    def test_unknown_or_invalid_supplied_budget_does_not_admit_candidate(self):
        for value in (None, float('nan'), -1, True):
            with self.subTest(value=value):
                r = self.radar({'effective_auto_cap':71760, 'entry_budget':value, 'account_fresh':True})
                row = {'reference_price':1000}
                self.assertFalse(r.affordable(row))
                self.assertEqual(row['affordability']['reason_code'], 'ACCOUNT_CAPITAL_UNAVAILABLE')

    def test_legacy_observation_context_without_entry_budget_is_supported(self):
        r = self.radar({'effective_auto_cap':71760, 'account_fresh':True})
        self.assertTrue(r.affordable({'reference_price':1000}))

    def test_stale_capital_never_grants_affordability(self):
        r = self.radar({'effective_auto_cap':71760, 'entry_budget':71334, 'account_fresh':False})
        self.assertFalse(r.affordable({'reference_price':1000}))

    def test_capital_zero_and_risk_positive_are_distinct(self):
        f = EntryFunnel()
        f.boundary('999990', AT, 'ONE_SHARE_UNAFFORDABLE', {'risk_quantity':{
            'values':{'quantity':0, 'capital_quantity':0, 'risk_quantity':8}}}, {'pattern':'FAST'})
        s = f.snapshot(AT)['stages']
        self.assertEqual(s.get('SIZING_ZERO'), 1)
        self.assertNotIn('SIZING_POSITIVE', s)
        self.assertEqual(s.get('RISK_QUANTITY_POSITIVE'), 1)
        self.assertEqual(s.get('CAPITAL_QUANTITY_ZERO'), 1)

    def test_planned_zero_quantity_does_not_become_positive_final_quantity(self):
        f = EntryFunnel()
        f.boundary('999990', AT, 'STRATEGY_ZERO_QUANTITY', {'risk_quantity':{
            'values':{'quantity':0, 'capital_quantity':5, 'risk_quantity':8}}}, {'pattern':'FAST'})
        self.assertEqual(f.snapshot(AT)['stages'].get('SIZING_ZERO'), 1)

    def test_missing_final_quantity_is_unknown_even_with_positive_risk(self):
        f = EntryFunnel()
        f.boundary('999990', AT, 'UNKNOWN', {'risk_quantity':{'values':{'risk_quantity':8}}}, {'pattern':'B'})
        s = f.snapshot(AT)['stages']
        self.assertEqual(s.get('SIZING_UNKNOWN'), 1)
        self.assertNotIn('SIZING_POSITIVE', s)


if __name__ == '__main__':
    unittest.main()
