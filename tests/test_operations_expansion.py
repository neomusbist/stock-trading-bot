"""Explicit opt-out of counts; bounded discovery and snapshot/render efficiency."""
import unittest,tempfile
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock,patch
from core.settings import Config
from automation.policy import AutomationConfig
from automation.observation_settings import ObservationSettings
from automation.radar import collect,RANKS
from tests.test_audit_followup import AuditFixture

class CountBudgetTests(AuditFixture):
    def no_count_caps(self):
        from tests.test_unlimited_daily_entries import UnlimitedAutonomousTests
        UnlimitedAutonomousTests.unlimited(self)
        self.e.c=replace(self.e.c,max_entries_per_symbol=None)
        self.e.a=replace(self.e.a,max_consecutive_losses=None)
        self.e.s['entries']['999990']=7
        self.e.s['automation']['consecutive_losses']=7
    def test_repeat_after_many_losses_reaches_existing_order_boundary(self):
        self.no_count_caps()
        order=self.e.enter(self.signal(),self.tick())
        self.assertIsNotNone(order)
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(self.e.s['entries']['999990'],7) # first fill counts, not signal
        self.assertIsNone(self.e.enter(self.signal(),self.tick())) # active order still blocks
        self.assertEqual(len(self.broker.orders),1)
    def test_exhausted_daily_budget_still_blocks_without_counts(self):
        self.no_count_caps();self.e.s['daily_loss']=self.e.risk_equity()*self.e.c.max_daily_loss_fraction
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])
    def test_reentry_cooldown_and_halt_are_not_bypassed(self):
        self.no_count_caps();self.e.s['automation']['last_exit']['999990']=self.clock[0].isoformat()
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.e.s['automation']['last_exit'].clear();self.e.s['halt']='UNKNOWN'
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])

class BoundedDiscoveryTests(unittest.TestCase):
    def test_wider_first_pages_exactly_five_queries_no_continuation(self):
        rest=Mock();rest.radar_observation_settings=ObservationSettings(rank_limit_multiplier=2)
        def query(api,payload,**kwargs):
            key=next(x[1] for x in RANKS if x[0]==api)
            return {key:[{'stk_cd':f'{10000+i:05d}0','stk_nm':'합성','cur_prc':'35000'} for i in range(100)]}
        rest.query.side_effect=query
        rows=collect(rest)
        self.assertEqual(rest.query.call_count,5);rest.pages.assert_not_called()
        self.assertEqual(len(rows),100)
        self.assertEqual(rows[-1]['reference_price'],35000)
        self.assertEqual(len(rows[0]['source_details']),5)
    def test_bounded_union_preserves_all_source_evidence_before_truncation(self):
        rest=Mock();rest.radar_observation_settings=ObservationSettings(rank_limit_multiplier=2)
        def query(api,payload,**kwargs):
            index=[x[0] for x in RANKS].index(api);key=RANKS[index][1]
            return {key:[{'stk_cd':f'{10000+index*100+i:05d}0','stk_nm':'합성','cur_prc':'400'} for i in range(100)]}
        rest.query.side_effect=query
        rows=collect(rest)
        self.assertEqual(len(rows),300);self.assertEqual(rest.query.call_count,5)
        self.assertTrue(all(r['reference_price']==400 for r in rows))
    def test_legacy_page_interface_unchanged_when_expansion_off(self):
        rest=Mock();rest.pages.return_value=([],{})
        self.assertEqual(collect(rest),[])
        self.assertEqual(rest.pages.call_count,5);rest.query.assert_not_called()
    def test_settings_reject_unbounded_or_wrong_types(self):
        for changes in ({'rank_limit_multiplier':3},{'rank_limit_multiplier':True},{'max_raw_symbols':1000},{'max_symbols':65}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):replace(ObservationSettings(),**changes).validate()
        self.assertEqual(replace(ObservationSettings(),max_symbols=64,rank_limit_multiplier=2).validate().max_symbols,64)
    def test_only_count_fields_accept_null(self):
        self.assertIsNone(replace(Config(),max_entries_per_symbol=None).validate().max_entries_per_symbol)
        self.assertIsNone(replace(AutomationConfig(),max_consecutive_losses=None).validate().max_consecutive_losses)
        for v in (0,-1,False,'none',1.5):
            with self.subTest(v=v),self.assertRaises(ValueError):replace(AutomationConfig(),max_consecutive_losses=v).validate()
        with self.assertRaises(ValueError):replace(AutomationConfig(),max_scan_charts=None).validate()

class UiEfficiencyTests(unittest.TestCase):
    def test_hidden_log_view_deferred_but_switch_displays_latest_snapshot(self):
        from tests.test_gui import GuiTests
        import copy
        GuiTests.setUpClass();f=GuiTests();f.setUp()
        try:
            before=f.w.logs.toPlainText()
            current=copy.deepcopy(f.s.status);current['log_rows']=[{'event':'LATEST_HIDDEN_EVENT'}]
            f.w.apply_status(current)
            self.assertEqual(f.w.logs.toPlainText(),before)
            f.w.tabs.setCurrentIndex(3)
            self.assertIn('LATEST_HIDDEN_EVENT',f.w.logs.toPlainText())
            self.assertEqual(f.w.status['mode'],current['mode'])
        finally:f.tearDown()
    def test_single_snapshot_reads_latest_once_and_revocation_next_snapshot(self):
        from tests.test_gui import UserServiceTests
        from core.util import atomic_json
        f=UserServiceTests();f.setUp()
        try:
            with patch.object(f.s,'latest',wraps=f.s.latest) as latest:
                first=f.s.snapshot();self.assertEqual(latest.call_count,1)
            cfg,auto,policy=f.s.settings();policy['mode']='KILL'
            atomic_json(f.s.root/'runtime/automation_policy.json',policy)
            second=f.s.snapshot();self.assertEqual(second['mode'],'KILL');self.assertFalse(second['trading_on'])
        finally:f.tearDown()
    def test_identical_table_render_preserves_item_changed_value_replaces(self):
        import operator_gui as gui
        from PySide6.QtWidgets import QApplication,QTableWidget
        app=QApplication.instance() or QApplication([])
        w=QTableWidget(0,2)
        gui.rows_to_table(w,[['A',1]])
        before=w.item(0,1);gui.rows_to_table(w,[['A',1]])
        self.assertIs(w.item(0,1),before)
        gui.rows_to_table(w,[['A',2]])
        self.assertEqual(w.item(0,1).text(),'2');w.close()

if __name__=='__main__':unittest.main()
