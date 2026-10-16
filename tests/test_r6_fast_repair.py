"""Causal FAST counterexamples; synthetic ticks test safety, not profitability."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from automation.fast_momentum import FastMomentumStrategy, MicroTape
from automation.policy import AutomationConfig
from core.settings import Config, KST
from core.strategy import Tick

BASE = datetime(2026, 10, 14, 9, 1, tzinfo=KST)
CAUSAL = [984, 988, 992, 996, 1000, 990, 992, 994, 996, 998, 1001]


def ticks(values=CAUSAL, start=BASE, symbol='999990', initial_cumulative=0):
    return [Tick(symbol, start+timedelta(seconds=i), price, 1000, initial_cumulative+1000*(i+1),
                 price, price-1, ask_qty=10000, bid_qty=10000, depth_available=True,
                 book_time=start+timedelta(seconds=i), depth_source='SIMULATED_QUOTE')
            for i, price in enumerate(values)]


def watch(resistance=1045, at=BASE):
    return dict(name='Synthetic causal support', status='WATCH', reference_price=984,
                resistance=resistance, resistance_asof=at.isoformat(), source_count=2)


class FastRepairTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(Config(), risk_fraction=.02, max_daily_loss_fraction=.04)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def seed(self, data=None):
        data = data or ticks()
        for t in data:self.assertTrue(self.strategy.ingest_tick(t, t.time))
        return data[-1]

    def signal(self):
        t=self.seed()
        sig=self.strategy.evaluate(t,watch(),capital_limit=81300,risk_budget=1626,ingest=False,received=t.time)
        self.assertIsNotNone(sig, self.strategy.last_evaluation.get(t.symbol))
        return t,sig

    def position(self):
        t,sig=self.signal()
        return t,dict(symbol=t.symbol,avg=sig.extra['geometry']['modeled_entry'],qty=sig.quantity,
            stop=sig.stop,initial_stop=sig.stop,emergency=sig.emergency,risk_budget=1626,opened=t.time.isoformat())

    def exit_tick(self, position, values, start_seconds=11):
        data=ticks(values,start=BASE+timedelta(seconds=start_seconds),initial_cumulative=11000)
        for t in data:
            self.assertTrue(self.strategy.ingest_tick(t,t.time))
            outcome=self.strategy.evaluate_exit(position,t,t.time)
        return outcome

    def test_causal_support_and_hard_line_precede_cost_sizing(self):
        _,sig=self.signal();g=sig.extra['geometry']
        self.assertEqual(g['structural_evidence']['support'],990)
        self.assertEqual(sig.stop,989)
        self.assertEqual(sig.emergency,985)
        self.assertEqual(sig.target,1045)
        self.assertLess(g['structural_evidence']['confirmation_at'],sig.created_at.isoformat())
        self.assertGreaterEqual(sig.net_reward_risk,1.5)
        self.assertLessEqual(sig.quantity*sig.risk_net_per_share,1626)
        self.assertFalse(sig.extra['actual_order_permission'])

    def test_monotonic_rise_has_no_independent_floor_and_does_not_create_stop(self):
        t=self.seed(ticks([984,986,988,990,992,994,996,998,1000,1001]))
        self.assertIsNone(self.strategy.evaluate(t,watch(),ingest=False))
        self.assertEqual(self.strategy.last_evaluation[t.symbol]['reason'],'CONFIRMED_MICRO_SUPPORT_UNKNOWN')

    def test_decision_print_cannot_confirm_its_own_unfinished_floor(self):
        t=self.seed(ticks([984,988,992,996,1000,990,1001]))
        geometry=self.strategy.calculate_invalidation_and_target(t,self.strategy.get_tape(t.symbol).metrics(t.time),watch())
        self.assertFalse(geometry['pass'])
        self.assertEqual(geometry['reason'],'CONFIRMED_MICRO_SUPPORT_UNKNOWN')

    def test_missing_or_future_resistance_is_unknown_not_rr_synthesized_target(self):
        t=self.seed();m=self.strategy.get_tape(t.symbol).metrics(t.time)
        for row in [dict(watch(),resistance=None),dict(watch(),resistance_asof=None),watch(at=t.time+timedelta(seconds=1))]:
            g=self.strategy.calculate_invalidation_and_target(t,m,row)
            self.assertFalse(g['pass']);self.assertEqual(g['reason'],'KNOWN_OVERHEAD_TARGET_UNAVAILABLE')

    def test_local_receipt_decision_time_is_distinct_from_exchange_trade_time(self):
        t=self.seed();received=t.time+timedelta(seconds=3)
        row=watch(at=t.time+timedelta(seconds=2))
        sig=self.strategy.evaluate(t,row,ingest=False,received=received)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.created_at,received)
        self.assertEqual(sig.extra['geometry']['structural_evidence']['asof'],received.isoformat())

    def test_nonregular_print_cannot_create_entry_or_emergency_exit(self):
        t,pos=self.position();nonregular=replace(t,regular=False,price=984,bid=983,ask=984)
        self.assertIsNone(self.strategy.evaluate(nonregular,watch(),ingest=False))
        outcome=self.strategy.evaluate_exit(pos,nonregular,t.time)
        self.assertFalse(outcome[0]);self.assertEqual(outcome[1],'NONREGULAR_TRADE_NOT_EXECUTABLE')

    def test_near_resistance_rejects_cost_rr_without_lifting_the_ceiling(self):
        t=self.seed();g=self.strategy.calculate_invalidation_and_target(t,self.strategy.get_tape(t.symbol).metrics(t.time),watch(1010))
        self.assertFalse(g['pass']);self.assertEqual(g['reason'],'NET_REWARD_RISK_LOW')
        self.assertEqual(g['target'],1010)

    def test_costs_and_risk_use_emergency_line_and_apply_slippage_once(self):
        _,sig=self.signal();c=self.strategy.costs
        buy=sig.limit*(1+c['entry_slippage'])*(1+c['buy'])
        sell=sig.target*(1-c['exit_slippage'])*(1-c['sell']-c['tax'])
        hard=sig.emergency*(1-c['exit_slippage'])*(1-c['sell']-c['tax'])
        self.assertAlmostEqual(sig.reward_net_per_share,sell-buy)
        self.assertAlmostEqual(sig.risk_net_per_share,buy-hard)
        self.assertGreater(sig.risk_net_per_share,buy-sig.stop*(1-c['exit_slippage'])*(1-c['sell']-c['tax']))

    def test_flat_prints_are_neutral_and_never_become_buying_pressure(self):
        tape=MicroTape()
        for t in ticks([1000]*12):self.assertTrue(tape.ingest(t,t.time))
        m=tape.metrics(BASE+timedelta(seconds=11))
        self.assertEqual(m['uptick_ratio_30'],0)
        self.assertEqual(m['downtick_ratio_30'],0)
        self.assertEqual(m['neutral_volume_30'],11000)

    def test_duplicate_and_reverse_cumulative_prints_do_not_inflate_tape(self):
        tape=MicroTape();data=ticks([1000,1001])
        self.assertTrue(tape.ingest(data[0]));self.assertFalse(tape.ingest(data[0]))
        self.assertTrue(tape.ingest(data[1]));self.assertFalse(tape.ingest(replace(data[1],time=BASE)))
        self.assertEqual(len(tape.ticks),2)

    def test_stale_future_and_invalid_trade_timestamps_are_rejected(self):
        tape=MicroTape();t=ticks([1000])[0]
        # Same one-second clock-lead boundary as the main receiver. An excess
        # lead is invalid; bounded resolution skew is covered separately.
        for receipt in [t.time-timedelta(seconds=1.001),t.time+timedelta(seconds=6)]:self.assertFalse(tape.ingest(t,receipt))
        self.assertFalse(tape.ingest(replace(t,time=t.time.replace(tzinfo=None)),t.time))
        self.assertEqual(len(tape.ticks),0)

    def test_unsubscribe_queue_gap_and_new_date_do_not_reuse_old_proof(self):
        t,_=self.signal();self.strategy.forget(t.symbol,'UNSUBSCRIBE_GAP')
        self.assertEqual(len(self.strategy.get_tape(t.symbol).ticks),0)
        self.seed();later=replace(t,time=t.time+timedelta(seconds=1),book_time=t.time+timedelta(seconds=1),cumulative=12000)
        self.assertTrue(self.strategy.ingest_tick(later,later.time,coverage_gap=True))
        self.assertEqual(len(self.strategy.get_tape(t.symbol).ticks),1)
        tomorrow=replace(later,time=later.time+timedelta(days=1),book_time=later.time+timedelta(days=1),cumulative=1)
        self.assertTrue(self.strategy.ingest_tick(tomorrow,tomorrow.time))
        self.assertEqual(len(self.strategy.get_tape(t.symbol).ticks),1)

    def test_missing_stale_crossed_or_shallow_book_cannot_be_signal(self):
        for change in [dict(book_time=None),dict(book_time=BASE-timedelta(seconds=1)),
                       dict(depth_available=False),dict(ask_qty=0),dict(ask=999,bid=1000)]:
            self.strategy=FastMomentumStrategy(self.cfg,self.auto);t=self.seed()
            current=replace(t,**change)
            self.assertIsNone(self.strategy.evaluate(current,watch(),ingest=False,received=current.time))

    def test_quantity_sized_to_capital_risk_and_full_depth(self):
        t=self.seed();g=self.strategy.calculate_invalidation_and_target(t,self.strategy.get_tape(t.symbol).metrics(t.time),watch(),risk_budget=100)
        self.assertTrue(g['pass']);self.assertLessEqual(g['quantity']*g['risk_net_per_share'],100)
        shallow=replace(t,ask_qty=2,bid_qty=2)
        rejected=self.strategy.calculate_invalidation_and_target(shallow,self.strategy.get_tape(t.symbol).metrics(t.time),watch())
        self.assertFalse(rejected['pass']);self.assertEqual(rejected['reason'],'FULL_QUANTITY_DEPTH_INSUFFICIENT')

    def test_one_normal_stop_touch_is_held_but_two_distinct_breaches_exit(self):
        _,pos=self.position()
        first=self.exit_tick(pos,[988]);self.assertFalse(first[0])
        t=replace(ticks([987],start=BASE+timedelta(seconds=12),initial_cumulative=12000)[0],bid=986)
        self.assertTrue(self.strategy.ingest_tick(t,t.time))
        second=self.strategy.evaluate_exit(pos,t,t.time)
        self.assertTrue(second[0]);self.assertEqual(second[1],'INVALIDATION_STOP_EXIT')

    def test_fresh_executable_emergency_bid_remains_immediate(self):
        _,pos=self.position();outcome=self.exit_tick(pos,[985])
        self.assertTrue(outcome[0]);self.assertEqual(outcome[1],'EMERGENCY_PLUNGE_EXIT')

    def test_thin_ask_side_does_not_block_a_full_quantity_emergency_sell_bid(self):
        _,pos=self.position()
        t=replace(ticks([985],start=BASE+timedelta(seconds=11),initial_cumulative=11000)[0],ask_qty=0)
        self.assertTrue(self.strategy.ingest_tick(t,t.time))
        outcome=self.strategy.evaluate_exit(pos,t,t.time)
        self.assertTrue(outcome[0]);self.assertEqual(outcome[1],'EMERGENCY_PLUNGE_EXIT')

    def test_account_allowance_is_saved_actual_budget_not_fixed_1626(self):
        _,pos=self.position();pos['risk_budget']=100
        outcome=self.exit_tick(pos,[1000])
        self.assertTrue(outcome[0]);self.assertEqual(outcome[1],'ACCOUNT_RISK_LIMIT_EXIT')
        self.assertEqual(outcome[2]['risk_budget'],100)

    def test_stale_or_unknown_exit_book_does_not_generate_fake_exit(self):
        t,pos=self.position();bad=replace(t,time=t.time+timedelta(seconds=1),price=984,bid=983,ask=984,book_time=None,cumulative=12000)
        self.assertTrue(self.strategy.ingest_tick(bad,bad.time))
        outcome=self.strategy.evaluate_exit(pos,bad,bad.time)
        self.assertFalse(outcome[0]);self.assertEqual(outcome[1],'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')

    def test_fixed_one_point_eight_percent_or_future_mfe_does_not_move_stop(self):
        _,pos=self.position();pos['highest_price']=1000000
        original=pos['stop'];outcome=self.exit_tick(pos,[1045])
        self.assertFalse(outcome[0]);self.assertEqual(pos['stop'],original)
        self.assertNotIn('protection_stop',pos)

    def test_real_confirmed_higher_low_can_protect_cost_positive_gain(self):
        _,pos=self.position();outcome=self.exit_tick(pos,[1020,1030,1022,1035])
        self.assertFalse(outcome[0]);self.assertTrue(pos.get('profit_locked'))
        self.assertGreater(pos['protection_stop'],pos['avg'])

    def test_exit_decision_does_not_start_cooldown_until_complete_fill(self):
        t,pos=self.position();self.exit_tick(pos,[985])
        self.assertNotIn(t.symbol,self.strategy.last_exit_by_symbol)
        fill=BASE+timedelta(seconds=12);self.strategy.record_exit(t.symbol,fill)
        self.assertEqual(self.strategy.last_exit_by_symbol[t.symbol],fill)

    def test_forgotten_or_stale_data_cannot_create_next_signal(self):
        t=self.seed();later=t.time+timedelta(seconds=6)
        self.assertIsNone(self.strategy.evaluate(t,watch(),ingest=False,received=later))
        self.strategy.reset('WEBSOCKET_DISCONNECT')
        self.assertEqual(self.strategy.tapes,{})


if __name__=='__main__':unittest.main()
