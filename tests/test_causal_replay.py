"""Causal counterexamples and real engine calls, all isolated PAPER fixtures."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest
import json
from pathlib import Path
import tempfile

from core.settings import Config,KST
from core.strategy import Tick
from core.broker import Rejected
from automation.policy import AutomationConfig
from automation.capital import Costs
from research.causal_data import Frame,bar_evidence,known_signal_bars,read_frames,captured_bars
from research.causal_account import PaperAccount
from research.retest_strategy import ConfirmedRetestStrategy,paper_engine_signal

AT=datetime(2026,10,20,10,13,tzinfo=KST)
SYM='999990'

def bars():
    rows=[]
    for i in range(12):
        start=AT-timedelta(minutes=12-i)
        rows.append({'time':start,'open':995,'high':1000,'low':990,'close':997,'volume':1000,
            'observation_source':'ACTUAL_RECEIVED','available_at':(start+timedelta(minutes=1)).isoformat(),
            'first_second':0,'gap_reason':None})
    rows[-2].update(open=999,high=1005,low=996,close=1003)
    rows[-1].update(open=1003,high=1003,low=1000,close=1002)
    return rows

def frame(at=AT,price=1004,depth=1000,source='0D'):
    return Frame(Tick(SYM,at,price,100,1000,price,price-1,True,
        ask_qty=depth,bid_qty=depth,depth_available=True,book_time=at,depth_source=source,
        received_at=at.isoformat(),book_received_at=at.isoformat()),at,at,'SYNTHETIC_TEST_QUOTE','test')


class AvailabilityTests(unittest.TestCase):
    def test_bar_start_is_not_close_availability(self):
        b=bars()[-1]
        self.assertEqual(known_signal_bars([b],b['time'],1)[1],'FUTURE_OR_UNAVAILABLE_BAR')
    def test_unknown_cached_receipt_is_not_inferred_from_end(self):
        b=bars()[0];b.pop('available_at')
        self.assertEqual(bar_evidence(b)['reason'],'BAR_AVAILABILITY_UNKNOWN')
    def test_rest_receipt_gates_reference_but_cannot_warm_signal(self):
        b=bars()[0];b.pop('available_at');b['observation_source']='REST_REFERENCE'
        ev=bar_evidence(b,row={'history_received_at':AT.isoformat()})
        self.assertTrue(ev['valid']);self.assertFalse(ev['signal_source_verified'])
    def test_impossibly_early_receipt_rejected(self):
        b=bars()[0];b['available_at']=b['time'].isoformat()
        self.assertEqual(bar_evidence(b)['reason'],'BAR_AVAILABILITY_BEFORE_CLOSE')
    def test_future_values_cannot_change_current_signal(self):
        b=bars();b[-1]['available_at']=(AT+timedelta(seconds=1)).isoformat()
        self.assertEqual(known_signal_bars(b,AT,7)[1],'FUTURE_OR_UNAVAILABLE_BAR')
    def test_nonconsecutive_signal_history_rejected(self):
        b=bars();b.pop(5)
        self.assertEqual(known_signal_bars(b,AT,7)[1],'NONCONSECUTIVE_KNOWN_BARS')
    def test_paired_quote_requires_recorded_depth(self):
        self.assertEqual(frame(source='BOOK_PRICE_MISMATCH').quote_reason(AT),'MEASURED_DEPTH_UNKNOWN')
    def test_stale_quote_does_not_get_a_new_timestamp(self):
        f=frame();self.assertEqual(f.quote_reason(AT+timedelta(seconds=6)),'TRADE_STALE_OR_FUTURE')
    def test_abnormally_future_trade_and_book_are_rejected(self):
        f=frame(AT+timedelta(seconds=3))
        self.assertEqual(f.quote_reason(AT),'TRADE_STALE_OR_FUTURE')
    def test_last_minute_missing_does_not_reuse_old_structure(self):
        self.assertEqual(known_signal_bars(bars(),AT+timedelta(minutes=1),7)[1],'LATEST_COMPLETED_BAR_MISSING')
    def test_frozen_input_prefix_excludes_appended_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'tape.jsonl'
            r={'event':'TRADE','symbol':SYM,'time':AT.isoformat(),'received':AT.isoformat(),
               'processed_at':AT.isoformat(),'price':1004,'bid':1003,'ask':1004}
            p.write_text(json.dumps(r)+'\n',encoding='utf-8');limit=p.stat().st_size
            with p.open('a',encoding='utf-8') as f:f.write(json.dumps(r)+'\n')
            self.assertEqual(len(list(read_frames([p],'2026-10-20',limits={str(p):limit}))),1)
    def test_legacy_capture_processing_proves_late_availability_not_bar_end(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'bars.jsonl';b=bars()[-1]
            r={'event':'COMPLETED_BAR','symbol':SYM,'source':'ACTUAL_RECEIVED_DETECTOR_HISTORY',
               'bar':{**b,'time':b['time'].isoformat()},'confirmed_at':AT.isoformat(),
               'processed_at':(AT+timedelta(seconds=15)).isoformat()}
            r['bar'].pop('available_at')
            p.write_text(json.dumps(r)+'\n',encoding='utf-8')
            ev,st=captured_bars([p],'2026-10-20')
            self.assertEqual(ev[0][0],AT+timedelta(seconds=15))
            self.assertEqual(known_signal_bars([ev[0][2]],AT,1)[1],'FUTURE_OR_UNAVAILABLE_BAR')


class PaperEngineTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(Config(),max_position_fraction=1.,max_positions=1,after_enabled=False,
            max_entries_per_day=None,max_entries_per_symbol=None,risk_fraction=.02,max_daily_loss_fraction=.04)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,confirmed_invalidation_enabled=True,
            costs_confirmed=True,exit_plan_reward_guard_enabled=True,max_consecutive_losses=None)
        self.account=PaperAccount(AT,initial_cash=71822,cfg=self.cfg,auto=self.auto)
        self.addCleanup(self.account.close)
        self.strategy=ConfirmedRetestStrategy(self.cfg,self.auto,self.account.e.costs)
        self.watch={'name':'SYNTHETIC','nearest_confirmed_peak':{'price':1100,
            'confirmed_at':(AT-timedelta(minutes=20)).isoformat(),
            'available_at':(AT-timedelta(minutes=20)).isoformat(),'observation_source':'ACTUAL_RECEIVED'}}
    def prepare(self,depth=1000):
        f=frame(depth=depth)
        self.account.step(f,market_status={'pass':True,'checked_at':AT.isoformat(),'reason_code':'SYNTHETIC_FIXTURE'})
        s,detail=self.strategy.evaluate(f.tick,bars(),self.watch,self.account.e.watch_budget(),
            self.account.e.risk_equity()*.02,AT)
        self.assertIsNotNone(s,detail)
        sig=paper_engine_signal(s,self.cfg,self.auto,self.account.e.costs,
            source_evidence={'source_count':2,'timestamp':AT.isoformat()})
        return s,sig
    def test_direct_broker_submit_is_not_an_integration_test(self):
        with self.assertRaises(Rejected):self.account.broker.submit('BUY',SYM,1,1004)
        self.assertEqual(self.account.broker.submissions,[])
    def test_completed_breakout_retest_reaches_engine_buy_fill_and_real_exit_functions(self):
        original,sig=self.prepare()
        order=self.account.enter(sig)
        self.assertIsNotNone(order)
        self.assertEqual(order['status'],'FILLED')
        self.assertIn(SYM,self.account.e.s['positions'])
        self.assertTrue(original.extra['SHADOW_ONLY']);self.assertFalse(original.extra['actual_order_permission'])
        qty=self.account.e.s['positions'][SYM]['qty']
        self.assertLessEqual(qty*self.account.e.quantity_details(sig,self.account.e.risk_equity(),71822,3000)['risk_per_share'],71822*.02)
        self.account.step(frame(AT+timedelta(seconds=1),988))
        self.assertNotIn(SYM,self.account.e.s['positions'])
        self.assertEqual([o['side'] for o in self.account.broker.submissions],['BUY','SELL'])
        self.assertEqual(self.account.result()['completed_count'],1)
        self.assertIn('비상선',self.account.result()['completed_trades'][0]['reason'])
    def test_partial_fills_do_not_reuse_the_same_displayed_book(self):
        _,sig=self.prepare(depth=2);order=self.account.enter(sig)
        self.assertIsNotNone(order);self.assertEqual(order['filled'],2)
        self.account.broker.advance(self.account.frame)
        self.assertEqual(order['filled'],2)
        later=replace(frame(AT+timedelta(milliseconds=100),depth=2),source_id='new-book')
        self.account.step(later)
        self.assertEqual(order['filled'],4)
    def test_pending_order_prevents_duplicate_symbol_entry(self):
        _,sig=self.prepare(depth=2);self.account.enter(sig)
        self.account.enter(sig)
        self.assertEqual(len([o for o in self.account.broker.submissions if o['side']=='BUY']),1)
    def test_latency_does_not_fill_before_order_arrival(self):
        self.account.broker.latency=timedelta(milliseconds=500)
        _,sig=self.prepare();order=self.account.enter(sig)
        self.assertEqual(order['filled'],0)
        self.account.step(frame(AT+timedelta(milliseconds=499)))
        self.assertEqual(order['filled'],0)
        self.account.step(frame(AT+timedelta(milliseconds=500)))
        self.assertEqual(order['status'],'FILLED')
    def test_no_depth_is_not_assumed_to_be_a_thousand_shares(self):
        _,sig=self.prepare();self.account.frame=frame(depth=0)
        self.assertIsNone(self.account.enter(sig));self.assertEqual(self.account.broker.submissions,[])
    def test_manual_holdings_do_not_inflate_auto_risk_budget_or_get_sold(self):
        a=PaperAccount(AT,initial_cash=71822,cfg=self.cfg,auto=self.auto,manual={'005930':{'qty':100,'available':100,'avg':70000}})
        self.addCleanup(a.close)
        self.assertEqual(a.e.risk_equity(),71822)
        self.assertEqual(a.e.s['positions'],{})
        self.assertEqual(a.broker.holdings['005930']['qty'],100)
        self.assertAlmostEqual(a.e.daily_risk_budget(),2872.88)
    def test_cash_is_not_account_equity_or_the_approved_cap(self):
        self.assertEqual(self.account.e.risk_equity(),71822)
        self.assertAlmostEqual(self.account.e.daily_risk_budget(),2872.88)
        self.assertAlmostEqual(self.account.e.risk_equity()*.02,1436.44)
    def test_missing_source_evidence_cannot_manufacture_buy_permission(self):
        _,sig=self.prepare();sig.pop('source_evidence')
        self.assertIsNone(self.account.enter(sig));self.assertEqual(self.account.broker.submissions,[])
    def test_missing_source_count_is_unknown_instead_of_a_comparison_error(self):
        _,sig=self.prepare();sig['source_evidence']['source_count']=None
        self.assertIsNone(self.account.enter(sig));self.assertEqual(self.account.broker.submissions,[])
    def test_future_intraday_target_is_unknown(self):
        f=frame();w={**self.watch,'nearest_confirmed_peak':{**self.watch['nearest_confirmed_peak'],
            'available_at':(AT+timedelta(seconds=1)).isoformat()}}
        s,d=self.strategy.evaluate(f.tick,bars(),w,71822,1436.44,AT)
        self.assertIsNone(s);self.assertEqual(d['reason'],'KNOWN_INTRADAY_TARGET_UNAVAILABLE')
    def test_no_synthetic_end_of_day_fill_from_last_bar(self):
        _,sig=self.prepare();self.account.enter(sig)
        result=self.account.result()
        self.assertEqual(result['completed_count'],0);self.assertIsNone(result['net_pnl_closed'])
        self.assertIn(SYM,result['open_positions']);self.assertFalse(result['forced_close_from_last_bar'])
    def test_gap_loss_can_exceed_the_daily_limit_and_new_entries_still_block(self):
        _,sig=self.prepare();self.account.enter(sig)
        self.account.step(frame(AT+timedelta(seconds=1),700))
        self.assertGreater(self.account.e.s['daily_loss'],self.account.e.daily_risk_budget())
        self.assertIsNone(self.account.enter(sig))
    def test_one_adverse_tick_does_not_raise_the_persisted_buy_limit(self):
        self.account.broker.adverse_ticks=1
        _,sig=self.prepare();order=self.account.enter(sig)
        self.assertEqual(order['filled'],0);self.assertEqual(order['limit'],1004)
    def test_partial_sells_are_one_trade_and_post_exit_prices_do_not_change_mfe(self):
        _,sig=self.prepare();self.account.enter(sig)
        qty=self.account.e.s['positions'][SYM]['qty']
        self.account.step(frame(AT+timedelta(seconds=1),988,depth=2))
        self.assertEqual(self.account.e.s['positions'][SYM]['qty'],qty-2)
        self.assertEqual(self.account.result()['completed_count'],0)
        self.account.step(frame(AT+timedelta(seconds=2),988,depth=1000))
        result=self.account.result()
        self.assertEqual(result['completed_count'],1)
        self.assertEqual(result['completed_trades'][0]['qty'],qty)
        mfe=result['completed_trades'][0]['MFE']
        self.account.step(frame(AT+timedelta(seconds=3),1200))
        self.assertEqual(self.account.result()['completed_trades'][0]['MFE'],mfe)
    def test_cancel_of_unfilled_buy_uses_engine_state_machine(self):
        self.account.broker.latency=timedelta(seconds=2)
        _,sig=self.prepare();order=self.account.enter(sig)
        self.account.e.cancel(order)
        self.account.broker.advance(self.account.frame)
        self.assertEqual(order['status'],'CANCELED')
        self.assertEqual(self.account.e.active_orders(),[])
        self.assertEqual(self.account.e.s['positions'],{})
        self.assertEqual(self.account.e.s['cash'],71822)
    def test_hard_lock_cannot_be_entered_through_the_paper_adapter(self):
        _,sig=self.prepare();sig['symbol']='005930'
        self.account.frame=replace(self.account.frame,tick=replace(self.account.frame.tick,symbol='005930'))
        self.account.e.market_status['005930']={'pass':True}
        self.assertIsNone(self.account.enter(sig));self.assertEqual(self.account.broker.submissions,[])

if __name__=='__main__':unittest.main()
