"""R6 readiness integration: real service paths and final guarded mock orders."""
import copy
import contextlib
import io
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch

from automation.runner import (readiness_snapshot,heartbeat_order_readiness,
    record_order_readiness_transition)
from automation.engine import AutonomousEngine
from automation.policy import AutomationConfig,Policy
from automation.allocation import efficiency
from core.settings import Config,KST
from core.strategy import Tick
from operator_service import OperatorService
from operator_ui_service import UserOperations
from tests.test_order_ready_synchronization import FakeEngine
from tests.test_automation import FakeBroker

AT=datetime(2026,10,14,10,0,tzinfo=KST)

class ReadinessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.clock=AT
        self.cfg=replace(Config(),live_mode=True)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True)
        self.policy={'mode':'LIVE','live_authorized':True,'releases':{},'revision':0}
        self.e=FakeEngine(AT);self.e.event=Mock()
        self.counts={'ticks':100,'subscription_acks':1}
        self.state={'positions':{},'orders':{},'trades':[],'halt':'',
            'automation':{'capital_snapshot':{'timestamp':AT.isoformat(),'actual_orderable_cash':81300}}}
    def heartbeat(self,seconds=0):
        self.e.last_regular_tick_at=AT-timedelta(seconds=seconds)
        return {'time':AT.isoformat(),'mode':'LIVE','connected':self.e.connected,'account_ok':self.e._account_valid,
            'market':'3','session':'SUPPORTED_SESSION','strategy':'SCAN','counts':self.counts,
            'readiness':readiness_snapshot(self.e,self.counts,True,AT)}
    def service(self,cls,hb):
        service=cls(self.root,clock=lambda:self.clock,gateway=Mock())
        service.settings=Mock(return_value=(self.cfg,self.auto,self.policy))
        service.latest=Mock(return_value=(self.root/'runtime/automation-production',self.state,hb))
        service.states=Mock(return_value=[(self.root/'runtime/automation-production',self.state)])
        service.process_info=Mock(return_value={'running':True,'managed':False,'state':'RUNNING'})
        service.hard_locks_ok=Mock(return_value=True)
        return service
    def states(self,hb):
        console=self.service(OperatorService,hb).public_status(recent_events=[])
        with patch('automation.readiness.authorization_valid',return_value=(True,[])):
            gui=self.service(UserOperations,hb).snapshot()
        return console,gui
    def test_actual_gui_and_service_match_engine_for_5_7_12_20_second_silence(self):
        for seconds,expected in ((5,True),(7,True),(12,False),(20,False)):
            with self.subTest(seconds=seconds):
                hb=self.heartbeat(seconds);console,gui=self.states(hb)
                self.assertEqual(hb['readiness']['order_ready'],expected)
                self.assertEqual(console['trading_on'],expected)
                self.assertEqual(gui['trading_on'],expected)
                if not expected:self.assertIn('FRESH_REGULAR_TICK_NOT_READY',gui['order_off_reasons'])
    def test_display_ages_the_saved_tick_without_extending_grace(self):
        hb=self.heartbeat(7)
        self.clock=AT+timedelta(seconds=5)
        console,gui=self.states(hb)
        self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
        self.assertEqual(gui['readiness']['regular_tick_age'],12)
        self.assertTrue(hb['readiness']['order_ready'])  # Saved evidence remains read-only.
    def test_missing_readiness_is_unknown_not_an_implicit_permission(self):
        hb=self.heartbeat();hb.pop('readiness')
        console,gui=self.states(hb)
        self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
        self.assertIn('READINESS_UNKNOWN',gui['order_off_reasons'])
    def test_legacy_r5_readiness_without_order_ready_uses_same_evidence_rule(self):
        hb=self.heartbeat(7);hb['readiness'].pop('order_ready')
        console,gui=self.states(hb)
        self.assertTrue(console['trading_on']);self.assertTrue(gui['trading_on'])
        self.clock=AT+timedelta(seconds=5)
        console,gui=self.states(hb)
        self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
    def test_websocket_and_account_failures_are_shared_off_causes(self):
        for cause in ('socket','account'):
            self.e=FakeEngine(AT)
            if cause=='socket':self.e.connected=False
            else:self.e._account_valid=False
            hb=self.heartbeat(2);console,gui=self.states(hb)
            self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
            self.assertIn('WEBSOCKET_REG_ACK_NOT_READY' if cause=='socket' else 'ACCOUNT_FRESH_NOT_READY',gui['order_off_reasons'])
    def test_cpu_backlog_evidence_cannot_receive_quiet_market_grace(self):
        for field,value,label in (('event_loop_lag',1.5,'EVENT_LOOP_LAG'),('feed_queue_size',8,'QUEUE_BUFFER')):
            self.e=FakeEngine(AT);setattr(self.e,field,value)
            hb=self.heartbeat(7);console,gui=self.states(hb)
            self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
            self.assertEqual(gui['readiness']['tick_lag_source'],label)
    def test_receiver_transport_failure_blocks_before_main_queue_consumes_error(self):
        self.e.readiness_feed=Mock(transport_failed=True)
        hb=self.heartbeat(2)
        self.assertTrue(self.e.connected)  # The queued ERROR has not reached the state writer.
        self.assertFalse(hb['readiness']['order_ready'])
        self.assertTrue(hb['readiness']['transport_failure_seen'])
        console,gui=self.states(hb)
        self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
    def test_account_age_cannot_be_hidden_by_a_recent_heartbeat(self):
        hb=self.heartbeat(2);hb['readiness']['account_age_seconds']=14
        self.clock=AT+timedelta(seconds=2)
        console,gui=self.states(hb)
        self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
        self.assertIn('ACCOUNT_FRESH_NOT_READY',gui['order_off_reasons'])
    def test_engine_off_snapshot_cannot_be_revived_by_gui(self):
        hb=self.heartbeat(2);hb['readiness']['order_ready']=False
        ready=heartbeat_order_readiness(hb,self.cfg,AT)
        self.assertFalse(ready['order_ready'])
    def test_future_or_expired_heartbeat_never_displays_on(self):
        hb=self.heartbeat(2)
        for elapsed in (-1,11):
            self.clock=AT+timedelta(seconds=elapsed)
            console,gui=self.states(hb)
            self.assertFalse(console['trading_on']);self.assertFalse(gui['trading_on'])
            self.assertIn('HEARTBEAT_NOT_CURRENT',gui['order_off_reasons'])
    def test_same_waiting_state_logs_a_real_on_to_off_readiness_transition(self):
        ready=self.heartbeat(7)['readiness']
        self.assertTrue(record_order_readiness_transition(self.e,ready))
        self.assertFalse(record_order_readiness_transition(self.e,ready))
        failed=self.heartbeat(12)['readiness']
        self.assertEqual(ready['state'],failed['state'])
        self.assertTrue(record_order_readiness_transition(self.e,failed))
        kind=self.e.event.call_args.args[0];values=self.e.event.call_args.kwargs
        self.assertEqual(kind,'ORDER_READINESS_CHANGED')
        self.assertFalse(values['order_ready'])
        self.assertIn('FRESH_REGULAR_TICK_NOT_READY',values['reason_codes'])
        self.assertFalse(values['actual_order_permission'])

class FinalOrderFreshnessTests(unittest.TestCase):
    def test_final_mock_broker_guard_rejects_a_symbol_tick_aging_after_signal(self):
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            root=Path(directory);policy=Policy(root/'policy.json');policy.command('LIVE',confirmation='LIVE')
            ticks=[Tick('999990',AT,10000,100,100,10010,9990)]
            class AgingBroker(FakeBroker):
                def submit(self,*args,**kwargs):
                    ticks[0]=replace(ticks[0],time=AT-timedelta(seconds=7))
                    return super().submit(*args,**kwargs)
            broker=AgingBroker()
            config=replace(Config(),live_mode=True,max_position_fraction=1.,risk_fraction=.02,max_daily_loss_fraction=.04)
            auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True)
            engine=AutonomousEngine(config,auto,policy,root/'book',broker,lambda:AT)
            engine.reconcile({'cash':81300,'equity':81300,'holdings':{},'orders':[]})
            engine.connected=True;engine.market='3';engine.last_radar_at=AT
            engine.last_regular_tick_at=AT-timedelta(seconds=7)
            engine.event_loop_lag=0.;engine.feed_queue_size=0
            engine.market_status['999990']={'pass':True}
            engine.entry_quote_provider=lambda symbol:(ticks[0],{})
            engine.readiness_reason=lambda:'' if readiness_snapshot(engine,{'subscription_acks':1},True,AT)['order_ready'] else 'DATA_NOT_READY'
            self.assertTrue(readiness_snapshot(engine,{'subscription_acks':1},True,AT)['order_ready'])
            signal={'symbol':'999990','limit':10020,'stop':9800,'pattern':'B','time':AT.isoformat(),
                    'target':10600,  # same explicit target as the pre-existing efficiency calculation
                    'efficiency':efficiency(10020,9800,10600,.8,30,engine.costs)}
            order=engine.enter(signal,ticks[0])
            self.assertIsNotNone(order);self.assertEqual(order['status'],'REJECTED')
            self.assertEqual(broker.orders,[])
            self.assertEqual(engine.latest_entry_execution_evidence['phase'],'PRE_HTTP')
            self.assertEqual(engine.latest_entry_execution_evidence['tick_age_seconds'],7)

if __name__=='__main__':unittest.main()
