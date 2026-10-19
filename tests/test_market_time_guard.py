"""Receiver -> PRE_HTTP and readiness regressions. All orders use FakeBroker."""
import contextlib
import io
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from core.settings import Config,KST
from core.strategy import Tick
from core.realtime import Feed
from core.market_time import market_freshness,tick_freshness
from automation.engine import AutonomousEngine
from automation.policy import AutomationConfig,Policy,HARD_LOCKS
from automation.allocation import efficiency
from automation.exit_plan import build_plan,invalidation_lines
from automation.runner import (readiness_snapshot,heartbeat_order_readiness,
                               record_order_readiness_transition)
from tests.test_automation import FakeBroker
from tests.test_order_ready_synchronization import FakeEngine


def frame(symbol,stamp,price,cumulative=100):
    return {'trnm':'REAL','data':[{'type':'0B','item':symbol,'values':{
        '20':stamp.strftime('%H%M%S'),'10':str(price),'15':'100','13':str(cumulative),
        '27':str(price),'28':str(price-(10 if price>=2000 else 1)),
        '290':'2','9081':'KRX','61':'1000','71':'1000'}}]}


class FinalBoundaryTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        output=contextlib.redirect_stdout(io.StringIO());output.__enter__()
        self.addCleanup(output.__exit__,None,None,None)

    def setup_trade(self,symbol,stamp,lead,price):
        self.at=stamp-timedelta(seconds=lead)
        self.cfg=replace(Config(),live_mode=True,risk_fraction=.02,
                         max_daily_loss_fraction=.04,max_position_fraction=1.)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,
                          costs_confirmed=True,fast_momentum_enabled=True)
        policy=Policy(self.root/'policy.json');policy.command('LIVE',confirmation='LIVE')
        self.broker=FakeBroker()
        self.e=AutonomousEngine(self.cfg,self.auto,policy,self.root/'book',self.broker,lambda:self.at)
        self.e.reconcile({'cash':72000,'equity':72000,'orders':[],'holdings':{}})
        self.e.connected=True;self.e.market='3';self.e.last_account=1000.
        self.e.market_status[symbol]={'pass':True}
        self.feed=Feed(SimpleNamespace(cfg=self.cfg),[symbol])
        with patch('core.realtime.now',return_value=self.at),patch('time.monotonic',return_value=1000.):
            self.feed.emit(frame(symbol,stamp,price))
        self.tick,_=self.feed.entry_snapshot(symbol)
        self.e.entry_quote_provider=self.feed.entry_snapshot
        stop=price-(100 if price>=2000 else 10)
        self.signal={'symbol':symbol,'pattern':'FAST','time':self.at.isoformat(),
            'signal_detected_at':self.at.isoformat(),'ttl_seconds':15.,'max_chase_price':price*1.003,
            'limit':price,'stop':stop,'target':price*1.1,
            'efficiency':efficiency(price,stop,price*1.1,.8,10,self.e.costs)}
        return self.tick

    def assert_fake_order(self,symbol,stamp,lead,price,qty):
        self.setup_trade(symbol,stamp,lead,price)
        # PRE_INTENT sees the previous second. The real receiver then publishes
        # the next exchange second immediately before the broker's PRE_HTTP guard.
        tick=replace(self.tick,time=stamp-timedelta(seconds=1))
        self.feed.latest_ticks[symbol]=tick
        submit=self.broker.submit
        def receiver_updates_then_submit(*args,**kwargs):
            with patch('core.realtime.now',return_value=self.at):
                self.feed.emit(frame(symbol,stamp,price,cumulative=200))
            return submit(*args,**kwargs)
        self.broker.submit=receiver_updates_then_submit
        with patch('time.monotonic',return_value=1000.):
            order=self.e.enter(self.signal,tick)
        self.assertIsNotNone(order)
        self.assertEqual(order['status'],'ACCEPTED')
        self.assertEqual(order['qty'],qty)
        self.assertEqual(self.broker.orders,[('BUY',symbol,qty,price)])
        ev=self.e.latest_entry_execution_evidence
        self.assertEqual(ev['phase'],'PRE_HTTP')
        self.assertAlmostEqual(ev['tick_age_seconds'],-lead,places=6)
        self.assertEqual(ev['tick_timing']['receive_elapsed_seconds'],0)
        self.assertTrue(ev['tick_timing']['fresh'])

    def test_seosan_minus_0051_reaches_fake_broker_pre_http(self):
        self.assert_fake_order('079650',datetime(2026,10,16,11,33,26,tzinfo=KST),.051,7950,9)

    def test_daeyang_minus_0293_reaches_fake_broker_pre_http(self):
        self.assert_fake_order('009190',datetime(2026,10,16,11,34,49,tzinfo=KST),.293,1025,70)

    def test_genuinely_old_tick_is_still_rejected(self):
        self.setup_trade('079650',datetime(2026,10,16,11,33,26,tzinfo=KST),.051,7950)
        self.at+=timedelta(seconds=6)
        with patch('time.monotonic',return_value=1006.):
            self.assertEqual(self.e.entry_market_reason(self.signal,9,phase='PRE_HTTP'),'ENTRY_MARKET_DATA_STALE')
        self.assertEqual(self.broker.orders,[])

    def test_monotonic_receipt_expiry_blocks_even_when_wall_clock_understates_delay(self):
        self.setup_trade('009190',datetime(2026,10,16,11,34,49,tzinfo=KST),.293,1025)
        self.at+=timedelta(seconds=4.5)  # wall time understates the true six seconds
        with patch('time.monotonic',return_value=1006.):
            reason=self.e.entry_market_reason(self.signal,70,phase='PRE_HTTP')
        self.assertEqual(reason,'ENTRY_MARKET_DATA_STALE')
        self.assertLess(self.e.latest_entry_execution_evidence['tick_age_seconds'],5)
        self.assertEqual(self.e.latest_entry_execution_evidence['tick_timing']['receive_elapsed_seconds'],6)
        self.assertEqual(self.broker.orders,[])

    def test_exchange_lead_beyond_one_second_is_rejected(self):
        self.setup_trade('079650',datetime(2026,10,16,11,33,26,tzinfo=KST),1.001,7950)
        with patch('time.monotonic',return_value=1000.):
            self.assertEqual(self.e.entry_market_reason(self.signal,9,phase='PRE_HTTP'),'ENTRY_MARKET_DATA_STALE')
        self.assertEqual(self.broker.orders,[])

    def test_clock_catching_up_does_not_validate_a_future_dated_tick_at_original_receipt(self):
        self.setup_trade('079650',datetime(2026,10,16,11,33,26,tzinfo=KST),2.,7950)
        self.at+=timedelta(seconds=2)
        with patch('time.monotonic',return_value=1002.):
            self.assertEqual(self.e.entry_market_reason(self.signal,9,phase='PRE_HTTP'),'ENTRY_MARKET_DATA_STALE')

    def test_bounded_skew_does_not_remove_limit_or_spread_guards(self):
        self.setup_trade('079650',datetime(2026,10,16,11,33,26,tzinfo=KST),.051,7950)
        self.signal['limit']=7940
        with patch('time.monotonic',return_value=1000.):
            self.assertEqual(self.e.entry_market_reason(self.signal,9,phase='PRE_HTTP'),'ENTRY_QUOTE_ABOVE_LIMIT')
            self.signal['limit']=7950
            self.e.entry_quote_provider=lambda _: (replace(self.tick,bid=7800),{})
            self.assertEqual(self.e.entry_market_reason(self.signal,9,phase='PRE_HTTP'),'ENTRY_SPREAD_CHANGED')
        self.assertEqual(self.broker.orders,[])


class ReadinessTimingTests(unittest.TestCase):
    def test_0927_and_1036_receiving_ticks_do_not_toggle_off_for_small_negative_age(self):
        for hour,minute in ((9,27),(10,36)):
            start=datetime(2026,10,16,hour,minute,tzinfo=KST)
            engine=FakeEngine(start);engine.event=Mock()
            for i,age in enumerate((.2,-.051,-.293,.1)):
                exchange=start+timedelta(seconds=i)
                asof=exchange+timedelta(seconds=age)
                tick=Tick('079650',exchange,7950,100,100+i,7950,7940,
                          received_at=asof.isoformat(),received_monotonic=1000.+i)
                engine.last_regular_tick_at=exchange;engine.last_regular_tick=tick
                engine.last_radar_at=asof
                detector=SimpleNamespace(last={'079650':tick})
                with patch('time.monotonic',return_value=1000.+i):
                    ready=readiness_snapshot(engine,{'subscription_acks':1},True,asof,detector)
                self.assertTrue(ready['order_ready'],ready)
                self.assertAlmostEqual(ready['regular_tick_exchange_age'],age)
                self.assertGreaterEqual(ready['regular_tick_age'],0)
                hb={'time':asof.isoformat(),'connected':True,'readiness':ready}
                self.assertTrue(heartbeat_order_readiness(hb,engine.c,asof)['order_ready'])
                record_order_readiness_transition(engine,ready)
            self.assertEqual(engine.event.call_count,1)  # initial ON only

    def test_invalid_future_or_expired_ticks_remain_off(self):
        asof=datetime(2026,10,16,10,36,tzinfo=KST);engine=FakeEngine(asof)
        for age in (-1.001,12.):
            engine.last_regular_tick_at=asof-timedelta(seconds=age)
            ready=readiness_snapshot(engine,{'subscription_acks':1},True,asof)
            self.assertFalse(ready['order_ready'])

    def test_gui_cannot_revive_engine_off_or_extend_existing_heartbeat_deadline(self):
        asof=datetime(2026,10,16,10,36,tzinfo=KST);engine=FakeEngine(asof)
        ready=readiness_snapshot(engine,{'subscription_acks':1},True,asof)
        hb={'time':asof.isoformat(),'connected':True,'readiness':ready}
        # Preserve the existing global silent-source grace. Each actual order
        # still has the strict five-second receipt check tested above.
        self.assertTrue(heartbeat_order_readiness(hb,engine.c,asof+timedelta(seconds=6))['order_ready'])
        self.assertFalse(heartbeat_order_readiness(hb,engine.c,asof+timedelta(seconds=11))['order_ready'])
        ready['order_ready']=False
        self.assertFalse(heartbeat_order_readiness(hb,engine.c,asof)['order_ready'])


class ReceiptAndExistingStopTests(unittest.TestCase):
    def test_small_negative_book_age_keeps_matching_depth_and_actual_quantity_guard(self):
        exchange=datetime(2026,10,16,11,34,49,tzinfo=KST)
        received=exchange-timedelta(seconds=.293)
        feed=Feed(SimpleNamespace(cfg=Config()),['009190'])
        book={'type':'0D','item':'009190','values':{'21':'113449','41':'1025',
            '51':'1024','61':'100','71':'100','121':'100','125':'100'}}
        with patch('core.realtime.now',return_value=received),patch('time.monotonic',return_value=1000.):
            feed.emit({'trnm':'REAL','data':[book]})
            feed.emit(frame('009190',exchange,1025))
            tick,_=feed.entry_snapshot('009190')
            self.assertTrue(tick.depth_available)
            engine=object.__new__(AutonomousEngine)
            engine.c=Config();engine.a=AutomationConfig();engine.clock=lambda:received
            engine.current_session=lambda:'AFTER';engine.entry_quote_provider=feed.entry_snapshot
            signal={'symbol':'009190','pattern':'FAST','limit':1025,
                'signal_detected_at':received.isoformat(),'ttl_seconds':15.,'max_chase_price':1028}
            self.assertIsNone(engine.entry_market_reason(signal,70,phase='PRE_HTTP'))
            feed.orderbooks['009190']['ask_qty']=69
            self.assertEqual(engine.entry_market_reason(signal,70,phase='PRE_HTTP'),
                             'ENTRY_DEPTH_BELOW_ACTUAL_QUANTITY')

    def test_receiver_preserves_exchange_and_local_time_without_overwriting_either(self):
        exchange=datetime(2026,10,16,11,34,49,tzinfo=KST)
        received=exchange-timedelta(seconds=.293)
        feed=Feed(SimpleNamespace(cfg=Config()),['009190'])
        with patch('core.realtime.now',return_value=received),patch('time.monotonic',return_value=1000.):
            feed.emit(frame('009190',exchange,1025))
        tick,_=feed.entry_snapshot('009190')
        self.assertEqual(tick.time,exchange)
        self.assertEqual(tick.received_at,received.isoformat())
        self.assertEqual(tick.received_monotonic,1000.)

    def test_invalid_monotonic_nan_cross_day_and_naive_timestamps_are_rejected(self):
        at=datetime(2026,10,16,11,34,49,tzinfo=KST)
        for stamp,receipt,mono in ((at,at,1001.),(at,at,float('nan')),
                                  (at-timedelta(days=1),at,1000.),(at.replace(tzinfo=None),at,1000.)):
            self.assertFalse(market_freshness(stamp,at,5,received_at=receipt,
                received_monotonic=mono,monotonic_now=1000.)['fresh'])

    def test_arrival_delay_plus_monotonic_wait_cannot_be_hidden_by_clock_correction(self):
        exchange=datetime(2026,10,16,11,34,49,tzinfo=KST)
        received=exchange+timedelta(seconds=4)
        asof=received+timedelta(seconds=.5)
        evidence=market_freshness(exchange,asof,5,received_at=received,
            received_monotonic=1000.,monotonic_now=1002.)
        self.assertTrue(evidence['valid'])
        self.assertEqual(evidence['exchange_age_seconds'],4.5)
        self.assertEqual(evidence['receipt_based_age_seconds'],6)
        self.assertFalse(evidence['fresh'])

    def test_structural_and_emergency_lines_and_account_risk_remain_distinct(self):
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        auto=replace(AutomationConfig(),confirmed_invalidation_enabled=True)
        signal={'pattern':'FAST','limit':11020,'stop':10960,'target':11400,
            'invalidation_policy':{'structural_stop':10960,'emergency_stop':10940,'noise_band':20}}
        plan=build_plan(cfg,auto,signal,entry=11000,quantity=6,filled=True)
        position={'initial_stop':10960,'stop':10960,'signal':signal}
        self.assertEqual((plan['initial_invalidation'],plan['initial_emergency']),(10960,10940))
        lines=invalidation_lines(cfg,auto,position)
        self.assertEqual((lines['active_structure'],lines['emergency']),(10960,10940))
        self.assertEqual(cfg.risk_fraction,.02)
        self.assertEqual(cfg.max_daily_loss_fraction,.04)
        self.assertTrue({'005930','017900','052260'}<=set(HARD_LOCKS))


if __name__=='__main__':unittest.main()
