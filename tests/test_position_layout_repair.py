"""Actual visible widgets stay readable when a position expands the cockpit."""
import unittest
from PySide6.QtCore import QPoint
from tests import test_gui as fixture
from tests import test_exit_plan_gui as plan_fixture

class PositionLayoutRepair(unittest.TestCase):
    setUpClass=classmethod(fixture.GuiTests.setUpClass.__func__)
    setUp=fixture.GuiTests.setUp
    tearDown=fixture.GuiTests.tearDown
    def test_buy_expansion_scrolls_details_instead_of_painting_over_the_position_row(self):
        row=plan_fixture.ProfitPlanGuiTests.row(self)
        self.w.resize(1280,900);self.w.show();self.app.processEvents()
        self.w.apply_status({**self.s.status,'bot_positions':[row],'positions':1,'trading_on':True})
        self.app.processEvents()
        table=self.w.bot_board;rect=table.visualItemRect(table.item(0,0))
        self.assertGreater(rect.height(),15)
        self.assertTrue(table.viewport().rect().contains(rect))
        self.assertGreater(self.w.bot_scroll.verticalScrollBar().maximum(),0)
        lower=self.w.exit_plan_panel.mapTo(table.parentWidget(),QPoint(0,0)).y()
        self.assertGreaterEqual(lower,table.geometry().bottom())
        self.assertEqual(self.s.calls,[])
    def test_position_disappearing_restores_candidate_space_without_any_start(self):
        self.w.show();row=plan_fixture.ProfitPlanGuiTests.row(self)
        self.w.apply_status({**self.s.status,'bot_positions':[row],'positions':1})
        self.app.processEvents();self.w.apply_status(self.s.status);self.app.processEvents()
        self.assertTrue(self.w.bot_board.isHidden());self.assertTrue(self.w.exit_plan_panel.isHidden())
        self.assertFalse(self.w.bot_empty_label.isHidden());self.assertEqual(self.s.calls,[])

if __name__=='__main__':unittest.main()
