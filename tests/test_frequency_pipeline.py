"""Offline R5 frequency-path regressions; no changes to trade qualification."""
import tempfile
import unittest
import contextlib
import io
import time
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from automation.capital import Costs
from automation.observation_worker import ObservationWorker
from automation.policy import AutomationConfig
from automation.policy import Policy
from automation.engine import AutonomousEngine
from automation.allocation import efficiency
from automation.runner import bind_candidate_observation
from automation.entry_timing import ObservationTimeline
from core.settings import Config,KST
from core.strategy import Tick
from tests.test_automation import FakeBroker

AT=datetime(2026,10,14,13,20,tzinfo=KST)

class HoldingObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rest=Mock()
        self.rest.stock_status.side_effect=AssertionError('held-position shadow must not query REST')
        self.worker=ObservationWorker(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003),
                                      Path(self.temp.name),rest=self.rest)
        self.worker.clock=lambda:AT
        self.engine=SimpleNamespace(s={'positions':{}},active_orders=Mock(return_value=[]))
        self.radar=SimpleNamespace()
        bind_candidate_observation(self.radar,self.worker,self.engine)
    def row(self,symbol='999990'):
        return {'code':symbol,'name':'synthetic RAW','source_count':2,'status':'RAW',
                'reference_price':10000,'affordability':{'entry_eligible':True},'discovery_score':20}
    def deliver(self,rows):
        self.radar.observation_sink(rows,AT.isoformat(),{'entry_budget':50000})
        packet=self.worker.updates.get_nowait()
        self.worker.prepare(*packet)
        return packet
    def test_live_position_keeps_cached_raw_admission_without_extra_api(self):
        self.engine.s['positions']={'999980':{'qty':1}}
        self.worker.metadata['999990']={'checked':AT,'status':{'pass':True}}
        packet=self.deliver([self.row()])
        self.assertTrue(self.radar.observation_allowed)
        self.assertFalse(packet[3])
        self.assertIn('999990',self.worker.runtime_view()['symbols'])
        self.assertEqual(self.worker.apis,0)
        self.rest.stock_status.assert_not_called()
        self.assertFalse(self.worker.snapshot['actual_order_permission'])
    def test_pending_buy_has_same_api_priority_but_does_not_drop_raw_packet(self):
        self.engine.active_orders.return_value=[{'side':'BUY','symbol':'999980'}]
        self.worker.metadata['999990']={'checked':AT,'status':{'pass':True}}
        packet=self.deliver([self.row()])
        self.assertFalse(packet[3])
        self.assertIn('999990',self.worker.runtime_view()['symbols'])
        self.rest.stock_status.assert_not_called()
    def test_missing_or_stale_status_is_never_admitted_from_raw_while_holding(self):
        self.engine.s['positions']={'999980':{'qty':1}}
        self.worker.metadata['999991']={'checked':AT-timedelta(seconds=self.worker.settings.metadata_ttl_seconds+1),
                                       'status':{'pass':True}}
        self.deliver([self.row(),self.row('999991')])
        self.assertEqual(self.worker.runtime_view()['symbols'],())
        self.rest.stock_status.assert_not_called()
    def test_flat_state_rest_permission_recovers_without_rebinding(self):
        self.engine.s['positions']={'999980':{'qty':1}}
        self.radar.observation_sink([self.row()],AT.isoformat(),{})
        self.assertFalse(self.worker.updates.get_nowait()[3])
        self.engine.s['positions'].clear()
        self.radar.observation_sink([self.row()],AT.isoformat(),{})
        self.assertTrue(self.worker.updates.get_nowait()[3])

class PartialBuyReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.broker=FakeBroker()
        self.policy=Policy(root/'policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,max_consecutive_losses=None)
        self.cfg=replace(Config(),live_mode=True,max_positions=2,max_position_fraction=1.,
                         max_entries_per_day=None,max_entries_per_symbol=None,risk_fraction=.02,max_daily_loss_fraction=.04)
        output=contextlib.redirect_stdout(io.StringIO());output.__enter__();self.addCleanup(output.__exit__,None,None,None)
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,root/'book',self.broker,lambda:AT)
        self.e.reconcile({'cash':81300,'equity':81300,'orders':[],'holdings':{}})
        self.e.connected=True;self.e.market='3'
        self.e.market_status={s:{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'} for s in ('999990','999980')}
    def signal(self,symbol):
        return {'symbol':symbol,'limit':10020,'stop':9800,'pattern':'B','time':AT.isoformat(),
                'efficiency':efficiency(10020,9800,10600,.8,30,self.e.costs)}
    def tick(self,symbol):return Tick(symbol,AT,10000,100,100,10010,9990)
    def first(self):
        o=self.e.enter(self.signal('999990'),self.tick('999990'))
        self.assertIsNotNone(o);self.assertGreater(o['qty'],1)
        return o
    def test_unfilled_buy_retains_every_share_and_actual_buy_fee(self):
        o=self.first()
        self.assertAlmostEqual(self.e.pending_buy_reservation(),o['qty']*o['limit']*(1+self.auto.commission_buy))
        self.assertEqual(o['filled'],0)
    def test_partial_buy_then_different_symbol_can_use_only_unreserved_cash(self):
        o=self.first();filled=o['qty']-1
        self.e.cumulative_fill(o,filled,filled*o['limit'])
        self.e.actual={'999990':{'qty':filled,'available':filled,'avg':o['limit']}}
        self.e.last_account=time.monotonic()
        cash_before=self.e.s['cash'];remaining=o['limit']*(1+self.auto.commission_buy)
        self.assertLess(cash_before,o['qty']*o['limit']*(1+self.cfg.roundtrip_cost_fraction))
        self.assertAlmostEqual(self.e.pending_buy_reservation(),remaining)
        # Replay the unchanged R5 reservation against the identical ledger.
        def old_reservation(buys=None):
            buys=self.e.active_orders(side='BUY') if buys is None else buys
            return sum(order['qty']*order['limit']*(1+self.cfg.roundtrip_cost_fraction) for order in buys)
        with patch.object(self.e,'pending_buy_reservation',side_effect=old_reservation):
            old_quantity,_=self.e.entry_gates(self.signal('999980'),self.tick('999980'))
            self.assertEqual(old_quantity,0)
            self.assertIsNone(self.e.enter(self.signal('999980'),self.tick('999980')))
        quantity,gates=self.e.entry_gates(self.signal('999980'),self.tick('999980'))
        self.assertGreater(quantity,0)
        self.assertAlmostEqual(gates['risk_quantity']['values']['reserved_cash'],remaining)
        second=self.e.enter(self.signal('999980'),self.tick('999980'))
        self.assertIsNotNone(second)
        self.assertEqual(second['qty'],quantity)
        self.assertLessEqual(self.e.pending_buy_reservation(),cash_before)
        self.e.cumulative_fill(second,second['qty'],second['qty']*second['limit'])
        self.e.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        self.assertGreaterEqual(self.e.s['cash'],0)
        self.assertFalse(self.e.s['halt'])
        self.assertEqual(len(self.e.s['positions']),2)
    def test_same_symbol_duplicate_is_still_rejected_during_partial_buy(self):
        o=self.first();self.e.cumulative_fill(o,1,o['limit'])
        submitted=len(self.broker.orders)
        self.assertIsNone(self.e.enter(self.signal('999990'),self.tick('999990')))
        self.assertEqual(len(self.broker.orders),submitted)
    def test_unlimited_entry_counts_do_not_disable_daily_risk_budget(self):
        self.e.s['entries']={'999990':99,'999980':99}
        self.assertTrue(self.e.entry_slots_available())
        self.e.s['daily_loss']=self.e.daily_risk_budget()
        quantity,gates=self.e.entry_gates(self.signal('999980'),self.tick('999980'))
        self.assertEqual(quantity,0)
        self.assertEqual(gates['repeat_slots']['result'],'PASS')
        self.assertEqual(gates['risk_quantity']['result'],'FAIL')
        self.assertIsNone(self.e.enter(self.signal('999980'),self.tick('999980')))
        self.assertEqual(self.broker.orders,[])
    def test_cooldown_applies_only_to_last_exited_symbol(self):
        self.e.s['automation']['last_exit']['999990']=(AT-timedelta(seconds=899)).isoformat()
        self.assertEqual(self.e.new_entry_reason('999990'),'COOLDOWN')
        self.assertEqual(self.e.new_entry_reason('999980'),'')
        self.e.s['automation']['last_exit']['999990']=(AT-timedelta(seconds=900)).isoformat()
        self.assertEqual(self.e.new_entry_reason('999990'),'')
    def test_partial_buy_one_share(self):
        o=self.first()
        initial_qty=o['qty']
        initial_cash=self.e.s['cash']
        self.e.cumulative_fill(o,1,o['limit'])
        self.assertEqual(o['filled'],1)
        self.assertEqual(self.e.s['positions']['999990']['qty'],1)
        expected_cash=initial_cash-(o['limit']+self.e.buy_fee(o['limit']))
        self.assertAlmostEqual(self.e.s['cash'],expected_cash)
        remaining_qty=initial_qty-1
        expected_reserved=remaining_qty*o['limit']*(1+self.auto.commission_buy)
        self.assertAlmostEqual(self.e.pending_buy_reservation(),expected_reserved)
    def test_multiple_partial_fills_incremental(self):
        o=self.first()
        total_qty=o['qty']
        self.assertGreaterEqual(total_qty,4)
        self.e.cumulative_fill(o,1,o['limit'])
        self.assertAlmostEqual(self.e.pending_buy_reservation(),(total_qty-1)*o['limit']*(1+self.auto.commission_buy))
        self.e.cumulative_fill(o,3,3*o['limit'])
        self.assertAlmostEqual(self.e.pending_buy_reservation(),(total_qty-3)*o['limit']*(1+self.auto.commission_buy))
        self.assertEqual(self.e.s['positions']['999990']['qty'],3)
        self.e.cumulative_fill(o,total_qty,total_qty*o['limit'])
        self.assertEqual(o['status'],'FILLED')
        self.assertEqual(self.e.pending_buy_reservation(),0)
        self.assertEqual(self.e.s['positions']['999990']['qty'],total_qty)
    def test_full_fill_clears_reservation_and_records_position(self):
        o=self.first()
        total_qty=o['qty']
        initial_cash=self.e.s['cash']
        self.e.cumulative_fill(o,total_qty,total_qty*o['limit'])
        self.assertEqual(o['status'],'FILLED')
        self.assertEqual(self.e.pending_buy_reservation(),0)
        self.assertEqual(self.e.s['positions']['999990']['qty'],total_qty)
        cost=total_qty*o['limit']+self.e.buy_fee(total_qty*o['limit'])
        self.assertAlmostEqual(self.e.s['cash'],initial_cash-cost)
    def test_cancel_remainder_releases_reservation(self):
        o=self.first()
        self.e.cumulative_fill(o,1,o['limit'])
        self.assertGreater(self.e.pending_buy_reservation(),0)
        o['status']='CANCELED'
        self.assertEqual(self.e.pending_buy_reservation(),0)
        self.assertEqual(self.e.s['positions']['999990']['qty'],1)
    def test_rejected_order_releases_all_reservation(self):
        from core.broker import Rejected
        sig=self.signal('999980')
        with patch.object(self.broker,'submit',side_effect=Rejected('Broker reject')):
            self.e.enter(sig,self.tick('999980'))
        order=list(self.e.s['orders'].values())[-1]
        self.assertEqual(order['status'],'REJECTED')
        self.assertEqual(self.e.pending_buy_reservation(),0)
    def test_duplicate_and_out_of_order_fill_notifications(self):
        o=self.first()
        self.e.cumulative_fill(o,2,2*o['limit'])
        cash_after_two=self.e.s['cash']
        self.e.cumulative_fill(o,2,2*o['limit'])
        self.assertEqual(self.e.s['cash'],cash_after_two)
        self.assertEqual(o['filled'],2)
        self.e.cumulative_fill(o,1,o['limit'])
        self.assertEqual(self.e.s['cash'],cash_after_two)
        self.assertEqual(o['filled'],2)
    def test_concurrent_buy_orders_on_multiple_symbols(self):
        config=replace(self.cfg,max_positions=3,max_position_fraction=0.33)
        self.e.c=config
        o1=self.e.enter(self.signal('999990'),self.tick('999990'))
        o2=self.e.enter(self.signal('999980'),self.tick('999980'))
        self.assertIsNotNone(o1); self.assertIsNotNone(o2)
        expected_total=(o1['qty']*o1['limit']+o2['qty']*o2['limit'])*(1+self.auto.commission_buy)
        self.assertAlmostEqual(self.e.pending_buy_reservation(),expected_total)
        self.e.cumulative_fill(o1,1,o1['limit'])
        expected_after=((o1['qty']-1)*o1['limit']+o2['qty']*o2['limit'])*(1+self.auto.commission_buy)
        self.assertAlmostEqual(self.e.pending_buy_reservation(),expected_after)
    def test_cash_and_reservation_separation(self):
        o=self.first()
        initial_cash=self.e.s['cash']
        self.assertEqual(self.e.s['cash'],initial_cash)
        self.assertAlmostEqual(self.e.pending_buy_reservation(),o['qty']*o['limit']*(1+self.auto.commission_buy))
        self.e.cumulative_fill(o,2,2*o['limit'])
        spent=2*o['limit']+self.e.buy_fee(2*o['limit'])
        self.assertAlmostEqual(self.e.s['cash'],initial_cash-spent)
        self.assertAlmostEqual(self.e.pending_buy_reservation(),(o['qty']-2)*o['limit']*(1+self.auto.commission_buy))
    def test_exposure_and_risk_budget_no_double_counting(self):
        o=self.first()
        self.e.cumulative_fill(o,2,2*o['limit'])
        buys=self.e.active_orders(side='BUY')
        c=self.cfg
        pos=self.e.s['positions']['999990']
        used_pos_risk=max(0,pos['avg']-pos['stop']+pos['avg']*(c.roundtrip_cost_fraction+c.exit_slippage_fraction))*pos['qty']
        used_order_risk=sum((ord['limit']-ord['signal']['stop']+ord['limit']*(c.roundtrip_cost_fraction+c.exit_slippage_fraction))*(ord['qty']-ord['filled']) for ord in buys)
        unit_risk=o['limit']-o['signal']['stop']+o['limit']*(c.roundtrip_cost_fraction+c.exit_slippage_fraction)
        self.assertAlmostEqual(used_pos_risk+used_order_risk,o['qty']*unit_risk)
    def test_prevention_of_negative_cash_and_cap_breach(self):
        o=self.first()
        self.e.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        self.assertGreaterEqual(self.e.s['cash'],0)
        huge_sig={'symbol':'999980','limit':90000,'stop':80000,'pattern':'B','time':AT.isoformat(),
                  'efficiency':efficiency(90000,80000,95000,.8,30,self.e.costs)}
        qty,gates=self.e.entry_gates(huge_sig,Tick('999980',AT,90000,10,10,90050,89950))
        self.assertEqual(qty,0)
        self.assertIsNone(self.e.enter(huge_sig,Tick('999980',AT,90000,10,10,90050,89950)))
        self.assertGreaterEqual(self.e.s['cash'],0)
    def test_four_position_observation_and_exit_driver_visits_every_position(self):
        # Capacity fixture only: identical synthetic eligibility is not a
        # profitable four-symbol strategy or a real brokerage latency result.
        symbols=('999990','999980','999970','999960')
        clock=[AT];self.broker=FakeBroker()
        config=replace(self.cfg,max_positions=4,max_position_fraction=.25)
        self.e=AutonomousEngine(config,self.auto,self.policy,Path(self.temp.name)/'four',self.broker,lambda:clock[0])
        self.e.reconcile({'cash':81300,'equity':81300,'orders':[],'holdings':{}})
        self.e.connected=True;self.e.market='3'
        self.e.market_status={s:{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'} for s in symbols}
        for symbol in symbols:
            order=self.e.enter(self.signal(symbol),self.tick(symbol))
            self.assertIsNotNone(order);self.assertEqual(order['qty'],2)
            self.e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
            self.e.actual[symbol]={'qty':2,'available':2,'avg':10020}
            self.e.last_account=time.monotonic()
        self.assertEqual(len(self.e.s['positions']),4)
        self.assertGreaterEqual(self.e.s['cash'],0)
        clock[0]=AT+timedelta(seconds=1)
        started=time.perf_counter()
        for symbol in symbols:
            self.e.on_tick(Tick(symbol,clock[0],10020,100,100,10030,10010))
            self.assertEqual(self.e.s['positions'][symbol]['last_tick'],clock[0].isoformat())
        for symbol in symbols:self.e.request_exit(symbol,'OFFLINE_CAPACITY_DRIVER_TEST')
        self.e.drive_exits()
        elapsed=time.perf_counter()-started
        sells=[o for o in self.e.active_orders(side='SELL')]
        self.assertEqual({o['symbol'] for o in sells},set(symbols))
        self.assertTrue(all(o['qty']==2 and o['status']=='ACCEPTED' for o in sells))
        self.assertFalse(self.e.s['halt'])
        self.__class__.driver_benchmark={'symbols':4,'mock_sell_orders':4,
            'processing_seconds':elapsed,'actual_broker_latency':None,'fill_guarantee':False,
            'net_profitability':None}

class PairedTimingTests(unittest.TestCase):
    def test_delayed_ack_preserves_the_original_received_processed_pair(self):
        timeline=ObservationTimeline()
        received=AT+timedelta(milliseconds=575);processed=AT+timedelta(milliseconds=900)
        timeline.tick('999990',AT,[],10000,received=received,processed=processed)
        # Main-thread ACK arrives after a SHADOW tick, but its receiver time is
        # earlier than that tick's processing time. It is not future evidence.
        timeline.subscription_ack(['999990'],AT+timedelta(milliseconds=600))
        timeline.tick('999990',AT+timedelta(seconds=158),[],10000)
        row=timeline.opening_snapshot(AT+timedelta(seconds=159))['symbols']['999990']
        self.assertEqual(row['first_tick_received_at'],received.isoformat())
        self.assertEqual(row['first_tick_processed_at'],processed.isoformat())
        self.assertAlmostEqual(row['latency_seconds']['receiver_to_processing'],.325)
    def test_earlier_snapshot_does_not_expose_future_stages_or_latest_price(self):
        timeline=ObservationTimeline()
        timeline.tick('999990',AT+timedelta(seconds=2),[],10200)
        row=timeline.opening_snapshot(AT)['symbols']['999990']
        self.assertNotIn('first_tick_received_at',row)
        self.assertNotIn('first_tick_processed_at',row)
        self.assertNotIn('last_trade_price',row)
        self.assertIsNone(row['latency_seconds']['receiver_to_processing'])
        self.assertFalse(timeline.opening_snapshot(AT)['actual_order_permission'])
    def test_future_persisted_time_cannot_advance_validation_watermark(self):
        timeline=ObservationTimeline();timeline.day=AT.date()
        timeline.rows['999990']={'first_tick_processed_at':(AT+timedelta(hours=1)).isoformat()}
        self.assertNotIn('first_tick_processed_at',timeline.row('999990',AT))
        timeline.tick('999990',AT,[])
        self.assertEqual(timeline.rows['999990']['first_tick_processed_at'],AT.isoformat())

if __name__=='__main__':unittest.main()
