"""Exit-plan/runner/book regressions, candidate counterexamples and read-only UI."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from automation.exit_plan import build_plan, state_for_position, display_state, invalidation_lines
from automation.exit_candidates import observe_candidates
from automation.policy import AutomationConfig
from automation.position_health import PositionDataHealth
from core.settings import Config, KST
from core.strategy import Tick
from tests import test_automation as fixture
from tests import test_subscription_refresh as wire

AT = datetime(2026, 10, 13, 10, 10, tzinfo=KST)
CFG = Config()
AUTO = replace(AutomationConfig(), confirmed_invalidation_enabled=True)


def position():
    sig = {'pattern': 'B', 'session': 'REGULAR', 'venue': 'KRX', 'limit': 10000,
           'stop': 9600, 'target': 11000, 'target_basis': '확정 저항으로 제한한 패턴 가설',
           'invalidation_policy': {'enabled': True, 'structural_stop': 9700, 'noise_band': 100}}
    p = dict(symbol='999990', opened=(AT-timedelta(minutes=6)).isoformat(),
             avg=10000, qty=3, high=10300, low=9900, initial_r=400, initial_stop=9600,
             stop=9600, signal=sig, ownership='BOT', exit_requested=False, qty_blocked=False,
             last_trade_time=AT.isoformat(), last_trade_price=10200,
             last_quote={'time': AT.isoformat(), 'bid': 10190, 'ask': 10200})
    p['exit_plan'] = build_plan(CFG, AUTO, sig, 10000, 3, True)
    return p


def bars(values, end=AT):
    return [dict(time=(end-timedelta(minutes=len(values)-i)).isoformat(),
                 open=o, high=h, low=l, close=c, volume=1000, first_second=0)
            for i, (o,h,l,c) in enumerate(values)]


class ExitPlanPureTests(unittest.TestCase):
    def test_target_is_not_resting_sell_or_automatic_take_profit(self):
        p=position();plan=p['exit_plan']
        self.assertFalse(plan['resting_target_order']);self.assertEqual(plan['profit_policy'],'AE3')
        self.assertIn('자동매도하지 않음',plan['target_action'])
    def test_separates_structural_stop_emergency_and_risk_sized_one_r(self):
        p=position();v=state_for_position(CFG,AUTO,p,AT)
        self.assertEqual(v['lines']['active_structure'],9700)
        self.assertEqual(v['lines']['emergency'],9600)
        self.assertEqual(v['plan']['one_r_price'],10400)
    def test_missing_target_is_unknown_not_zero_or_a_fake_projection(self):
        for target in (None, float('nan'), float('inf'), 9990):
            sig={**position()['signal'],'target':target}
            plan=build_plan(CFG,AUTO,sig)
            self.assertIsNone(plan['target']);self.assertEqual(plan['target_status'],'UNKNOWN')
            self.assertFalse(plan['reward_guard_pass'])
    def test_costs_reduce_rr_and_fill_removes_estimated_entry_slippage(self):
        sig=position()['signal'];a=build_plan(CFG,AUTO,sig);b=build_plan(CFG,AUTO,sig,10000,3,True)
        self.assertLess(a['net_reward_risk'],(11000-10000)/(10000-9600))
        self.assertGreater(b['net_reward_risk'],a['net_reward_risk'])
    def test_one_r_and_configured_activation_multiple_are_not_confused(self):
        p=build_plan(replace(CFG,protect_at_r=2),AUTO,position()['signal'])
        self.assertEqual(p['one_r_price'],10400);self.assertEqual(p['profit_activation_price'],10800)
    def test_holding_horizon_is_not_a_forced_exit(self):
        p=position();p['signal']['efficiency']={'inputs':{'planned_minutes':10}}
        self.assertFalse(p['exit_plan']['holding_horizon_is_deadline'])
        self.assertEqual(p['exit_plan']['flatten_at'],'15:10:00')
    def test_after_deadline_and_failure_scope_are_exact(self):
        sig={**position()['signal'],'session':'AFTER','pattern':'A','venue':'SOR'}
        a=replace(AUTO,after_momentum_failure_enabled=True)
        p=build_plan(CFG,a,sig)
        self.assertTrue(p['after_failed_rebound']);self.assertEqual(p['flatten_at'],'19:55:00')
        sig['pattern']='B';self.assertFalse(build_plan(CFG,a,sig)['after_failed_rebound'])
    def test_continuation_does_not_inherit_after_burst_failure(self):
        sig={**position()['signal'],'session':'AFTER','pattern':'A',
             'evaluation_gates':{'acceleration':{'values':{'mode':'CONFIRMED_CONTINUATION'}}}}
        self.assertFalse(build_plan(CFG,replace(AUTO,after_momentum_failure_enabled=True),sig)['after_failed_rebound'])
    def test_net_pnl_uses_fresh_bid_and_labels_estimate(self):
        v=state_for_position(CFG,AUTO,position(),AT)
        expected=(10190*(1-AUTO.commission_sell-AUTO.sell_tax)-10000*(1+AUTO.commission_buy))*3
        self.assertAlmostEqual(v['estimated_net_pnl'],expected);self.assertIn('추정',v['net_pnl_basis'])
    def test_stale_quote_and_trade_invalidate_display_even_without_engine_tick(self):
        saved=state_for_position(CFG,AUTO,position(),AT,order_allowed=True)
        v=display_state(saved,AT+timedelta(seconds=6),5)
        self.assertIsNone(v['estimated_net_pnl']);self.assertIsNone(v['market_data']['current'])
        self.assertFalse(v['order_allowed']);self.assertEqual(v['trend_status'],'판단 불가')
        self.assertIsNotNone(saved['estimated_net_pnl'])
    def test_future_other_day_and_naive_quote_never_supply_net_pnl(self):
        for time in ((AT+timedelta(seconds=1)).isoformat(),(AT-timedelta(days=1)).isoformat(),AT.replace(tzinfo=None).isoformat()):
            p=position();p['last_quote']['time']=time
            self.assertIsNone(state_for_position(CFG,AUTO,p,AT)['estimated_net_pnl'])
    def test_standing_quote_confirmation_keeps_original_market_timestamp(self):
        p=position();p['last_quote'].update(time=(AT-timedelta(minutes=10)).isoformat(),
            confirmation='MATCHED_STANDING_BOOK',snapshot_confirmed_at=AT.isoformat())
        v=state_for_position(CFG,AUTO,p,AT)
        self.assertIsNotNone(v['estimated_net_pnl']);self.assertTrue(v['market_data']['standing_fresh'])
        self.assertEqual(v['market_data']['quote_time'],p['last_quote']['time'])
    def test_one_r_not_reached_does_not_pretend_profit_protection_is_active(self):
        v=state_for_position(CFG,AUTO,position(),AT)
        self.assertFalse(v['profit_armed']);self.assertEqual(v['protection_status'],'미발동')
    def test_one_r_reached_without_confirmed_pivot_is_waiting_not_raised(self):
        p=position();p['high']=10410
        self.assertEqual(state_for_position(CFG,AUTO,p,AT)['protection_status'],'1R 도달·확정 지지 대기')
    def test_raised_lines_never_retreat_or_use_hypothetical_soft_value(self):
        p=position();p['stop']=10200
        v=state_for_position(CFG,AUTO,p,AT)
        self.assertEqual(v['lines']['emergency'],10100);self.assertEqual(v['protection_status'],'구조 보호 중')
    def test_missing_or_malformed_bar_is_unknown_not_a_crash(self):
        p=position();p['last_invalidation_bar']={'time':'not-a-timestamp'}
        self.assertEqual(state_for_position(CFG,AUTO,p,AT)['trend_status'],'판단 불가')
    def test_new_reward_guard_defaults_off_and_requires_a_boolean(self):
        self.assertFalse(AutomationConfig().exit_plan_reward_guard_enabled)
        with self.assertRaises(ValueError):replace(AUTO,exit_plan_reward_guard_enabled='true').validate()
    def test_profit_report_never_mixes_unconfirmed_costs_into_broker_expectancy(self):
        from automation.performance import report
        base=dict(symbol='999990',opened=(AT-timedelta(minutes=6)).isoformat(),closed=AT.isoformat(),
                  qty=2,entry=10000,exit=10300,pnl_estimate=400,MFE=500,MAE=50,completed=True,
                  holding_seconds=360,signal={'target':11000},pattern='B',pnl_source='BROKER_QUERY')
        modeled={**base,'symbol':'999980','pnl_estimate':99999,'pnl_source':'ESTIMATED'}
        stats=report([base,modeled])
        self.assertEqual(stats['all']['broker_confirmed_count'],1)
        self.assertEqual(stats['all']['broker_net_expectancy_krw'],400)
        self.assertAlmostEqual(stats['completed_trades'][0]['mfe_capture_fraction'],.4)
        self.assertIsNone(stats['all']['target_reach_rate'])
    def test_mixed_partial_fills_are_not_mislabeled_broker_confirmed(self):
        from automation.performance import completed_trades
        a=dict(symbol='999990',opened=(AT-timedelta(minutes=6)).isoformat(),closed=AT.isoformat(),
               trade_id='synthetic',qty=1,entry=10000,exit=10300,pnl_estimate=280,MFE=500,MAE=50,
               completed=False,pnl_source='ESTIMATED')
        b={**a,'completed':True,'pnl_source':'BROKER_QUERY'}
        self.assertEqual(completed_trades([a,b])[0]['pnl_source'],'MIXED_OR_ESTIMATED')


class CandidatePolicyTests(unittest.TestCase):
    def test_one_share_cannot_fake_partial_profit(self):
        p=position();p['qty']=1;p['target_touch']={'time':AT.isoformat(),'price':11000}
        row=observe_candidates(CFG,AUTO,p,AT)['policies']['TARGET_PARTIAL']
        self.assertEqual(row['action'],'NOT_APPLICABLE');self.assertIsNone(row['quantity'])
    def test_two_and_three_share_partial_quantities_are_integer_and_leave_remainder(self):
        for qty in (2,3):
            p=position();p['qty']=qty;p['target_touch']={'time':AT.isoformat(),'price':11000}
            row=observe_candidates(CFG,AUTO,p,AT)['policies']['TARGET_PARTIAL']
            self.assertEqual(row['quantity'],1);self.assertEqual(row['remainder_quantity'],qty-1)
    def test_stored_high_alone_does_not_invent_time_of_target_touch(self):
        p=position();p['high']=12000
        self.assertEqual(observe_candidates(CFG,AUTO,p,AT)['policies']['TARGET_FULL']['action'],'HOLD')
    def test_target_touch_without_quote_or_fill_never_has_realized_profit(self):
        p=position();p['last_quote']={};p['target_touch']={'time':AT.isoformat(),'price':11000}
        rows=observe_candidates(CFG,AUTO,p,AT)['policies']
        for r in rows.values():self.assertFalse(r['orders_enabled']);self.assertIsNone(r['realized_net_pnl'])
        self.assertFalse(rows['TARGET_FULL']['executable_quote_known'])
    def test_confirmed_profitable_higher_low_can_be_observed_before_one_r(self):
        p=position();p['protection_bars']=bars([(10130,10160,10120,10140),(10140,10150,10110,10120),(10140,10180,10130,10170)])
        v=observe_candidates(CFG,AUTO,p,AT)
        self.assertLess(p['high']-p['avg'],p['initial_r']);self.assertGreater(v['pivot_stop'],p['avg'])
        self.assertEqual(p['stop'],9600);self.assertFalse(v['experimental_orders_enabled'])
    def test_first_pullback_does_not_exit_without_confirmed_recovery(self):
        p=position();p['protection_bars']=bars([(10210,10240,10200,10220),(10220,10220,10120,10140),(10140,10150,10100,10120)])
        v=observe_candidates(CFG,AUTO,p,AT)
        self.assertIsNone(v.get('pivot_stop'));self.assertEqual(v['policies']['STRUCTURAL_TRAIL']['action'],'HOLD')
    def test_broken_pivot_can_exit_before_one_r_but_is_observation_only(self):
        p=position();p['protection_bars']=bars([(10130,10160,10120,10140),(10140,10150,10110,10120),(10140,10180,10130,10170)])
        old=observe_candidates(CFG,AUTO,p,AT)
        p['protection_bars']+=bars([(10170,10180,10070,10080)],AT+timedelta(minutes=1))
        v=observe_candidates(CFG,AUTO,p,AT+timedelta(minutes=1),old)
        self.assertEqual(v['policies']['STRUCTURAL_TRAIL']['action'],'EXIT_ALL')
        self.assertFalse(p['exit_requested'])
    def test_normal_recovery_counterexample_can_outperform_early_pivot_exit(self):
        p=position();p['protection_bars']=bars([(10130,10160,10120,10140),(10140,10150,10110,10120),(10140,10180,10130,10170)])
        old=observe_candidates(CFG,AUTO,p,AT)
        p['protection_bars']+=bars([(10170,10180,10070,10080)],AT+timedelta(minutes=1))
        early=observe_candidates(CFG,AUTO,p,AT+timedelta(minutes=1),old)
        p['protection_bars']+=bars([(10080,10480,10080,10450)],AT+timedelta(minutes=2))
        later=observe_candidates(CFG,AUTO,p,AT+timedelta(minutes=2),early)
        self.assertEqual(early['policies']['STRUCTURAL_TRAIL']['action'],'EXIT_ALL')
        self.assertEqual(later['policies']['AE3']['action'],'HOLD')
        self.assertGreater(p['protection_bars'][-1]['close'],10400)
    def test_stale_gapped_partial_or_bad_volume_is_unknown(self):
        p=position();p['protection_bars']=bars([(10130,10160,10120,10140),(10140,10150,10110,10120),(10140,10180,10130,10170)])
        for mode in ('old','gap','partial','volume'):
            q=copy.deepcopy(p);at=AT
            if mode=='old':at+=timedelta(minutes=1)
            elif mode=='gap':del q['protection_bars'][1]
            elif mode=='partial':q['protection_bars'][-1]['first_second']=20
            else:q['protection_bars'][-1]['volume']=None
            self.assertEqual(observe_candidates(CFG,AUTO,q,at)['policies']['STRUCTURAL_TRAIL']['action'],'UNKNOWN')


class ExitPlanEngineTests(unittest.TestCase):
    setUp=fixture.AutomationTests.setUp
    make_engine=fixture.AutomationTests.make_engine
    signal=fixture.AutomationTests.signal
    tick=fixture.AutomationTests.tick
    buy=fixture.AutomationTests.buy
    def test_plan_created_before_order_and_rebound_to_actual_fill(self):
        o=self.buy();p=self.engine.s['positions']['999990']
        self.assertEqual(o['signal']['exit_plan']['entry_basis'],'ORDER_LIMIT')
        self.assertEqual(p['exit_plan']['entry_basis'],'ACTUAL_AVERAGE_FILL')
        self.assertEqual(p['exit_plan']['quantity'],p['qty'])
    def test_optional_unknown_target_guard_refuses_without_real_order(self):
        self.auto=replace(self.auto,exit_plan_reward_guard_enabled=True);self.engine=self.make_engine()
        self.engine.reconcile(self.snapshot);self.engine.connected=True;self.engine.market='3'
        sig=self.signal();sig['target']=None
        self.assertIsNone(self.engine.enter(sig,self.tick()));self.assertEqual(self.broker.orders,[])
    def test_optional_reward_guard_is_rechecked_at_final_market_boundary(self):
        self.engine.a=replace(self.auto,exit_plan_reward_guard_enabled=True)
        sig=self.signal();sig['target']=None
        self.assertEqual(self.engine.entry_market_reason(sig,1,self.tick(),phase='PRE_SUBMIT'),'EXIT_PLAN_REWARD_UNKNOWN_OR_LOW')
        self.assertEqual(self.broker.orders,[])
    def test_target_touch_never_submits_new_profit_order_by_itself(self):
        self.auto=replace(self.auto,confirmed_invalidation_enabled=True);self.engine=self.make_engine()
        self.engine.reconcile(self.snapshot);self.engine.connected=True;self.engine.market='3'
        sig=self.signal();sig['invalidation_policy']={'enabled':True,'structural_stop':9900,'noise_band':100}
        o=self.engine.enter(sig,self.tick());self.engine.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        self.engine.actual={'999990':{'qty':o['qty'],'available':o['qty'],'avg':o['limit']}}
        self.engine.on_tick(self.tick(10610))
        p=self.engine.s['positions']['999990'];self.assertFalse(p['exit_requested'])
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(p['profit_policy_observation']['policies']['TARGET_FULL']['action'],'EXIT_ALL')
    def test_new_hard_line_is_identical_in_quote_and_trade_paths(self):
        self.buy();p=self.engine.s['positions']['999990']
        self.engine.a=replace(self.auto,confirmed_invalidation_enabled=True)
        p['signal']['invalidation_policy']={'enabled':True,'structural_stop':9900,'noise_band':100}
        p['stop']=10100
        health=PositionDataHealth(self.engine,Mock(),SimpleNamespace(position_symbols=frozenset(),position_context={}),Mock())
        book={'time':self.clock[0].isoformat(),'best_bid':10050,'best_ask':10060}
        self.assertTrue(health.accept_book('999990',book,self.clock[0]));self.assertFalse(p['exit_requested'])
        book.update(best_bid=10000,best_ask=10010)
        health.accept_book('999990',book,self.clock[0]);self.assertTrue(p['exit_requested'])
    def test_old_book_does_not_trigger_new_close_based_emergency(self):
        self.buy();p=self.engine.s['positions']['999990']
        self.engine.a=replace(self.auto,confirmed_invalidation_enabled=True)
        p['signal']['invalidation_policy']={'enabled':True,'structural_stop':9900,'noise_band':100}
        health=PositionDataHealth(self.engine,Mock(),SimpleNamespace(position_symbols=frozenset(),position_context={}),Mock())
        health.accept_book('999990',{'time':(self.clock[0]-timedelta(seconds=10)).isoformat(),'best_bid':9700,'best_ask':9710},self.clock[0])
        self.assertFalse(p['exit_requested'])


class ExitPlanWireTests(unittest.TestCase):
    setUp=wire.SubscriptionRefreshTests.setUp
    frame=wire.SubscriptionRefreshTests.frame
    send=wire.SubscriptionRefreshTests.send
    def test_live_runner_delivers_completed_close_even_when_old_structure_flag_is_off(self):
        self.e.a=replace(self.auto,confirmed_invalidation_enabled=True,confirmed_structure_protection=False)
        p=position();p.update(opened=wire.START.isoformat(),avg=10000,initial_stop=9800,stop=9800,
                              initial_r=200,high=10000,low=10000,exit_attempts=0,last_tick=wire.START.isoformat())
        p['signal']['invalidation_policy']={'enabled':True,'structural_stop':9950,'noise_band':50}
        self.e.s['positions'][wire.SYM]=p
        self.e.actual={wire.SYM:{'qty':3,'available':3,'avg':10000}}
        for sec in range(61):
            self.clock=wire.START+timedelta(seconds=sec)
            t=Tick(wire.SYM,self.clock,9920,100,100+sec,9930,9910)
            self.send(self.frame(t))
        self.assertTrue(p['exit_requested'])
        self.assertEqual(p['exit_reason'],'확정봉 종가의 구조적 무효화')
        self.assertEqual(len([o for o in self.broker.orders if o[0]=='SELL']),1)
    def test_delayed_frames_do_not_trigger_past_bar_exit_or_make_fresh_pnl(self):
        self.e.a=replace(self.auto,confirmed_invalidation_enabled=True)
        p=position();p.update(opened=wire.START.isoformat(),initial_stop=9800,stop=9800,exit_attempts=0,last_tick=wire.START.isoformat())
        self.e.s['positions'][wire.SYM]=p;self.e.actual={wire.SYM:{'qty':3,'available':3,'avg':10000}}
        self.clock=wire.START+timedelta(minutes=3)
        from automation.runner import process_message
        for sec in range(61):
            at=wire.START+timedelta(seconds=sec);t=Tick(wire.SYM,at,9600,100,100+sec,9610,9590)
            process_message(self.e,self.d,self.frame(t),at,{wire.SYM},{},self.radar,self.prices,self.account,True)
        self.assertFalse(p['exit_requested']);self.assertEqual(self.broker.orders,[])

    def test_after_failure_uses_actual_ohlcv_receiver_route_without_one_r(self):
        from tests.test_after_momentum import OPEN,START,OBSERVED
        self.e.c=replace(self.cfg,after_enabled=True);self.e.after_enabled=True
        self.e.a=replace(self.auto,confirmed_invalidation_enabled=True,after_momentum_failure_enabled=True,
                         confirmed_structure_protection=False)
        p=position();p.update(symbol=wire.SYM,opened=OPEN.isoformat(),avg=17460,initial_stop=17180,
            stop=17180,initial_r=280,high=17460,low=17460,exit_attempts=0,last_tick=OPEN.isoformat())
        p['signal'].update(pattern='A',session='AFTER',venue='SOR',limit=17460,stop=17180,target=18200,
                           invalidation_policy={'enabled':True,'structural_stop':17180,'noise_band':30})
        p['exit_plan']=build_plan(self.e.c,self.e.a,p['signal'],17460,3,True)
        self.e.s['positions'][wire.SYM]=p;self.e.actual={wire.SYM:{'qty':3,'available':3,'avg':17460}}
        count=0
        for i,(opening,high,low,close,volume) in enumerate(OBSERVED):
            for sec,price in ((0,opening),(1,high),(2,low),(3,close)):
                self.clock=START+timedelta(minutes=i,seconds=sec);count+=100
                t=Tick(wire.SYM,self.clock,price,100,count,price+10,price-10,venue='SOR')
                self.send(self.frame(t))
                self.assertFalse(p['exit_requested'])
        self.clock=START+timedelta(minutes=len(OBSERVED));count+=100
        self.send(self.frame(Tick(wire.SYM,self.clock,17450,100,count,17460,17440,venue='SOR')))
        self.assertTrue(p['exit_requested']);self.assertEqual(p['exit_signal_at'],self.clock.isoformat())
        self.assertEqual(p['exit_reason'],'AFTER 급등 후 반등 실패 확인')
        self.assertFalse(p['exit_state']['profit_armed'])
        self.assertEqual(len([o for o in self.broker.orders if o[0]=='SELL']),1)


if __name__=='__main__':unittest.main()
