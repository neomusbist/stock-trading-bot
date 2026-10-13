"""Profit-plan display contracts; isolated state and mock services only."""
import copy
import unittest
from dataclasses import replace
from datetime import timedelta
from automation.exit_plan import build_plan, state_for_position, display_state
from operator_ui_service import exit_plan_presentation, exit_plan_text
from tests.test_exit_plan import CFG, AUTO, AT, position
from tests import test_gui as fixture


def view(p=None, auto=AUTO, **kw):
    return state_for_position(CFG, auto, p or position(), AT, **kw)


def row(p=None, **kw):
    p=p or position()
    return dict(name='합성 검증 종목',symbol=p['symbol'],quantity=p['qty'],setup=p['signal']['pattern'],
                entry=p['avg'],current=999999,invalidation=p['initial_stop'],protection=p['stop'],
                exit_state=view(p,**kw))


class DisplayProjectionTests(unittest.TestCase):
    def test_initial_target_comes_from_signal_not_mutable_current_plan(self):
        p=position();p['exit_plan'].update(target=11200,target_change_reason='저장된 변경 사유 예시')
        target=view(p)['presentation']['target']
        self.assertEqual(target['initial'],11000);self.assertEqual(target['current'],11200)
        self.assertEqual(target['change_reason'],'저장된 변경 사유 예시')
        self.assertEqual(p['signal']['target'],11000)

    def test_unchanged_target_is_explicit_and_never_a_resting_sell(self):
        v=view();target=v['presentation']['target']
        self.assertEqual(target['change_reason'],'최초 목표 유지');self.assertFalse(target['resting_sell_order'])
        self.assertIn('목표만으로 매도·목표 상향하지 않음',target['reassessment'])

    def test_missing_change_evidence_is_unknown_not_fabricated(self):
        p=position();p['exit_plan']['target']=11200
        self.assertIn('UNKNOWN',view(p)['presentation']['target']['change_reason'])

    def test_initial_target_below_actual_fill_is_still_preserved(self):
        p=position();p['avg']=11100;p['initial_r']=1500
        self.assertEqual(view(p)['presentation']['target']['initial'],11000)

    def test_missing_target_is_unknown_with_no_guessed_take_profit(self):
        p=position();p['signal']['target']=None;p['exit_plan']=build_plan(CFG,AUTO,p['signal'])
        t=view(p)['presentation']['target'];self.assertIsNone(t['initial']);self.assertIsNone(t['current'])

    def test_original_recorded_rr_not_replaced_by_current_cost_estimate(self):
        p=position();p['signal']['evaluation_gates']={'risk_reward':{'values':{'calculated_rr':2.5}}}
        v=view(p);self.assertEqual(v['presentation']['target']['initial_price_rr'],2.5)
        self.assertNotEqual(v['plan']['net_reward_risk'],2.5)
        self.assertIn('가격 손익비: 2.50',exit_plan_text({'exit_state':v}))

    def test_fractional_fill_activation_uses_exact_engine_initial_r(self):
        p=position();p.update(avg=9600.25,initial_r=1)
        v=view(p)
        self.assertEqual(v['plan']['one_r_price'],9601.25)
        self.assertEqual(v['presentation']['profit']['activation_price'],9601.25)

    def test_one_r_below_threshold_does_not_claim_profit_is_protected(self):
        profit=view()['presentation']['profit']
        self.assertFalse(profit['armed']);self.assertFalse(profit['protecting'])
        self.assertEqual(profit['status'],'익절 보호 미발동');self.assertIsNone(profit['protect_price'])

    def test_one_r_without_higher_low_is_not_an_active_profit_line(self):
        p=position();p['high']=10450
        profit=view(p)['presentation']['profit']
        self.assertTrue(profit['armed']);self.assertFalse(profit['protecting'])
        self.assertIn('연속 3개 확정봉',profit['rule'])

    def test_profitable_raised_structure_uses_actual_line_and_cost_model(self):
        p=position();p.update(high=10500,stop=10300)
        p['protection_evidence']={'pivot_low':10320,'confirmation_time':AT.isoformat(),'reason':'CONFIRMED_HIGHER_LOW_RAISED_STOP'}
        profit=view(p)['presentation']['profit']
        self.assertTrue(profit['protecting']);self.assertEqual(profit['protect_price'],10300)
        self.assertGreater(profit['modeled_net_at_protect_price'],0)
        self.assertIn('완성봉 종가',profit['expected_condition'])
        self.assertIn('체결 수익 보장 아님',profit['expected_condition'])

    def test_small_raised_stop_that_does_not_cover_costs_is_not_profit_protection(self):
        p=position();p['stop']=10010
        self.assertFalse(view(p)['presentation']['profit']['protecting'])

    def test_setup_c_reports_legacy_trail_and_actual_giveback(self):
        p=position();p['signal'].update(pattern='C',invalidation_policy=None)
        p['exit_plan']=build_plan(CFG,AUTO,p['signal'],p['avg'],p['qty'],True)
        info=view(p)['presentation']
        self.assertEqual(info['profit']['path'],'LEGACY_TRAIL_GIVEBACK')
        self.assertIn('40%',info['profit']['rule']);self.assertIn('1.5%',info['profit']['rule'])
        self.assertIn('신선한 체결가',info['profit']['expected_condition'])

    def test_after_failure_needs_real_policy_and_scope_not_only_after_clock(self):
        p=position();p['signal'].update(session='AFTER',pattern='A',invalidation_policy=None)
        info=view(p,replace(AUTO,after_momentum_failure_enabled=True))['presentation']
        self.assertFalse(info['after']['enabled'])
        self.assertIn('미적용',next(c for c in info['conditions'] if c['key']=='after')['detail'])

    def test_after_condition_without_bars_is_data_insufficient(self):
        p=position();p['signal'].update(session='AFTER',pattern='A')
        info=view(p,replace(AUTO,after_momentum_failure_enabled=True))['presentation']
        self.assertTrue(info['after']['enabled']);self.assertFalse(info['after']['ready'])
        self.assertEqual(next(c for c in info['conditions'] if c['key']=='after')['status'],'데이터 부족')

    def test_after_confirmation_displays_recorded_floor_closes_and_readiness(self):
        p=position();p['signal'].update(session='AFTER',pattern='A')
        p['after_momentum_evidence']={'asof':AT.isoformat(),'reason':'AFTER_BURST_FAILED_REBOUND',
                                    'rebound_floor':10100,'decline_times':['a','b','c'],'closes':[10120,10100,10090]}
        v=view(p,replace(AUTO,after_momentum_failure_enabled=True));info=v['presentation']
        self.assertTrue(info['after']['failed']);self.assertTrue(info['after']['ready'])
        text=exit_plan_text({'exit_state':v})
        self.assertIn('10,100원',text);self.assertIn('[10120, 10100, 10090]',text)

    def test_future_or_expired_after_evidence_cannot_claim_current_failure(self):
        for at in (AT+timedelta(seconds=1),AT-timedelta(seconds=66)):
            p=position();p['signal'].update(session='AFTER',pattern='A')
            p['after_momentum_evidence']={'asof':at.isoformat(),'reason':'AFTER_BURST_FAILED_REBOUND','decline_times':['a','b','c']}
            self.assertFalse(view(p,replace(AUTO,after_momentum_failure_enabled=True))['presentation']['after']['failed'])

    def test_target_touch_is_a_record_not_exit_request(self):
        p=position();p['target_touch']={'time':AT.isoformat(),'price':11000,'basis':'RECEIVED_TRADE'}
        v=view(p);self.assertTrue(v['presentation']['target']['touch']);self.assertFalse(v['exit_requested'])
        self.assertEqual(p['stop'],9600)

    def test_future_target_touch_is_not_shown_at_earlier_point_in_time(self):
        p=position();p['target_touch']={'time':(AT+timedelta(minutes=1)).isoformat(),'price':11000}
        self.assertIsNone(view(p)['presentation']['target']['touch'])

    def test_user_exit_is_distinct_from_structural_failure(self):
        p=position();p.update(exit_requested=True,exit_reason='사용자 STOP_FLAT')
        v=view(p);text=exit_plan_text({'exit_state':v})
        self.assertNotIn('실패 확정',v['trend_status']);self.assertIn('사용자 STOP_FLAT',text)

    def test_stale_prices_keep_sell_order_and_request_without_reauthorizing(self):
        p=position();p.update(exit_requested=True,exit_reason='사용자 STOP_FLAT')
        orders=[{'side':'SELL','status':'ACCEPTED','qty':3,'filled':1,'signal':{'reason':'사용자 STOP_FLAT'}}]
        saved=view(p,pending=1,order_allowed=True,orders=orders)
        expired=display_state(saved,AT+timedelta(seconds=10),5)
        panel=exit_plan_presentation({'exit_state':expired},True)
        values={k:v for k,_,v in panel['summary']}
        self.assertEqual(values['current'],'미확인');self.assertEqual(values['net'],'미확인')
        self.assertIn('체결 1주',panel['orders']);self.assertIn('주문 차단',panel['orders'])
        self.assertIn('사용자 STOP_FLAT',panel['next_exit'])
        self.assertEqual(saved['market_data']['current'],10200)

    def test_stale_condition_status_never_implies_off_policy_is_running(self):
        v=display_state(view(),AT+timedelta(seconds=6),5)
        cs={c['key']:c for c in v['presentation']['conditions']}
        self.assertEqual(cs['structure']['status'],'데이터 부족')
        self.assertEqual(cs['after']['status'],'미발동')

    def test_observation_candidates_are_explicitly_nonexecuting(self):
        c=next(c for c in view()['presentation']['conditions'] if c['key']=='experimental')
        self.assertEqual(c['status'],'관찰 전용');self.assertIn('실제 매도 주문에 적용하지 않음',c['detail'])

    def test_initial_stop_history_not_fabricated_as_an_update(self):
        p=position();p['stop_history']=[{'time':AT.isoformat(),'stop':9600}]
        self.assertIn('상향 변경 기록 없음',view(p)['presentation']['history_note'])

    def test_projection_is_pure_and_does_not_mutate_plan_position_or_orders(self):
        p=position();orders=[{'side':'SELL','status':'UNKNOWN','qty':3}]
        before=copy.deepcopy((p,orders));view(p,orders=orders)
        self.assertEqual((p,orders),before)


class ProfitSummaryWidgetTests(unittest.TestCase):
    setUpClass=classmethod(fixture.GuiTests.setUpClass.__func__)
    setUp=fixture.GuiTests.setUp
    tearDown=fixture.GuiTests.tearDown

    def test_summary_conditions_and_details_are_separate_read_only_widgets(self):
        r=row();self.w.apply_status({**self.s.status,'bot_positions':[r]})
        self.assertIn('11,000원',self.w.exit_plan_summary['initial_target'].text())
        self.assertIn('최초 목표 유지',self.w.exit_plan_summary['current_target'].text())
        self.assertEqual(self.w.exit_plan_summary['current'].text(),'10,200원')
        self.assertIn('익절 보호 미발동',self.w.exit_plan_expected.text())
        self.assertEqual(self.w.exit_conditions.rowCount(),9)
        self.assertEqual(self.w.exit_plan_tabs.tabText(1),'상세 근거 / 변경 이력')
        self.assertTrue(self.w.exit_plan_details.isReadOnly());self.assertEqual(self.s.calls,[])

    def test_no_position_clears_summary_and_does_not_leave_old_target(self):
        self.w.apply_status({**self.s.status,'bot_positions':[row()]})
        self.w.apply_status(self.s.status)
        self.assertTrue(self.w.exit_plan_panel.isHidden());self.assertEqual(self.w.exit_plan_summary['initial_target'].text(),'')
        self.assertEqual(self.w.exit_conditions.rowCount(),0)

    def test_multiple_positions_selection_reads_correct_engine_plan(self):
        a=row();p=position();p.update(symbol='999980');p['signal']['target']=12000;p['exit_plan']['target']=12000
        b=row(p);self.w.apply_status({**self.s.status,'bot_positions':[a,b]})
        self.w.bot_board.selectRow(1)
        self.assertIn('12,000원',self.w.exit_plan_summary['initial_target'].text());self.assertEqual(self.s.calls,[])


class JournalProjectionTests(unittest.TestCase):
    setUp=fixture.UserServiceTests.setUp
    state=fixture.UserServiceTests.state

    def test_saved_engine_state_flows_through_real_service_without_network_or_writes(self):
        self.clock[0]=AT;p=position();p['exit_state']=view(p)
        self.state(positions={p['symbol']:p});before=(self.root/'runtime/automation-production/state.json').read_bytes()
        result=self.s.snapshot();r=result['bot_positions'][0]
        self.assertEqual(r['exit_state']['presentation']['target']['initial'],11000)
        self.assertEqual(r['current'],10200);self.assertEqual(self.g.calls,[])
        self.assertEqual((self.root/'runtime/automation-production/state.json').read_bytes(),before)

    def test_journal_orders_newer_than_projection_are_retained_after_price_expiry(self):
        self.clock[0]=AT+timedelta(seconds=10);p=position();p['exit_state']=view(p)
        p.update(exit_requested=True,exit_reason='사용자 STOP_FLAT')
        order={'symbol':p['symbol'],'side':'SELL','qty':3,'filled':1,'status':'ACCEPTED','signal':{'reason':'사용자 STOP_FLAT'}}
        self.state(positions={p['symbol']:p},orders={'synthetic':order})
        r=self.s.snapshot()['bot_positions'][0];panel=exit_plan_presentation(r,True)
        self.assertIsNone(r['current']);self.assertIn('체결 1주',panel['orders'])
        self.assertIn('사용자 STOP_FLAT',panel['next_exit']);self.assertEqual(self.g.calls,[])

    def test_protected_and_external_positions_are_not_bot_exit_plans(self):
        p=position();p['ownership']='EXISTING_POLICY';protected=position();protected['ownership']='BOT'
        self.state(positions={p['symbol']:p,'005930':protected})
        self.assertEqual(self.s.snapshot()['bot_positions'],[]);self.assertEqual(self.g.orders,[])


if __name__=='__main__':unittest.main()
