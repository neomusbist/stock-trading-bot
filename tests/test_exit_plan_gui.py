"""Real widgets show only engine-authored profit plan and current freshness."""
import unittest
from datetime import timedelta
from automation.exit_plan import state_for_position, display_state
from operator_ui_service import exit_plan_text
from tests import test_gui as fixture
from tests.test_exit_plan import CFG,AUTO,AT,position


class ProfitPlanGuiTests(unittest.TestCase):
    setUpClass=classmethod(fixture.GuiTests.setUpClass.__func__)
    setUp=fixture.GuiTests.setUp
    tearDown=fixture.GuiTests.tearDown
    def row(self):
        p=position();v=state_for_position(CFG,AUTO,p,AT,order_allowed=True)
        return dict(name='합성 청산 계획 예시',symbol='999990',entry=10000,current=10200,
                    quantity=3,setup='B',invalidation=9700,protection=9600,
                    hold_reason=v['hold_reason'],next_exit=v['next_exit'],exit_state=v)
    def test_goal_activation_emergency_and_conditions_are_distinct_and_visible(self):
        row=self.row();s={**self.s.status,'bot_positions':[row],'trading_on':True}
        self.w.apply_status(s);text=self.w.exit_plan_details.toPlainText()
        for phrase in ('예상 목표가 11,000원','1R 가격 10,400원','최초 구조 무효화 9,700원',
                       '즉시 비상선 9,600원','다음 매도 조건','예상 순손익','15:10:00','매도 예약 주문 없음'):
            self.assertIn(phrase,text)
        self.assertTrue(self.w.exit_plan_details.isReadOnly());self.assertEqual(self.s.calls,[])
    def test_missing_target_not_presented_as_a_take_profit_order(self):
        row=self.row();row['exit_state']['plan'].update(target=None,target_status='UNKNOWN')
        self.assertIn('UNKNOWN / 가격구조 근거 부족',exit_plan_text(row))
        self.assertIn('매도 예약 주문 없음',exit_plan_text(row))
    def test_stale_bid_displays_unknown_net_pnl_and_blocks_freshness_claim(self):
        row=self.row();row['exit_state']=display_state(row['exit_state'],AT+timedelta(seconds=6),5)
        text=exit_plan_text(row,True)
        self.assertIn('예상 순손익 미확인',text);self.assertIn('호가 오래됨',text)
        self.assertIn('주문 차단',text)
    def test_no_position_has_no_synthetic_live_plan(self):
        self.w.apply_status(self.s.status)
        self.assertTrue(self.w.exit_plan_details.isHidden())
        self.assertEqual(self.w.exit_plan_details.toPlainText(),'')
    def test_gui_does_not_recompute_engine_plan_from_a_different_entry(self):
        row=self.row();row['entry']=999999
        self.assertIn('진입 평균가 10,000원',exit_plan_text(row))
    def test_legacy_position_without_saved_plan_is_waiting_not_a_fake_target(self):
        row=self.row();row.pop('exit_state')
        self.assertIn('엔진의 새 청산 상태 기록 대기',exit_plan_text(row))


if __name__=='__main__':unittest.main()
