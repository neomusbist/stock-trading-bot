"""First-fill entry lifecycle, failure suppression, and exact own INTENT E2E."""
import copy
import json
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from core.broker import Rejected, Rest
from core.engine import Engine
from core.util import atomic_json
from tests import test_v1 as v1
from tests.test_audit_followup import AuditFixture
from tests import test_discovery_resume as discovery

class EntryLifecycleTests(AuditFixture):
    daily_approval=discovery.DiscoveryResumeTests.daily_approval

    def test_two_internal_rejects_then_first_fill_and_partial_counts_once(self):
        self.daily_approval()
        send=self.broker.submit
        for sym in ('999980','999970'):
            self.e.market_status[sym]={'pass':True}
            with patch.object(self.broker,'submit',side_effect=Rejected('DAILY_AUTHORIZATION_LIVE_APPROVAL_CRITICAL_STATE_CHANGED')):
                order=self.e.enter({**self.signal(),'symbol':sym},replace(self.tick(),symbol=sym))
            self.assertEqual(order['status'],'REJECTED')
            self.assertEqual(sum(self.e.s['entries'].values()),0)
        self.assertEqual(self.e.s['entry_diagnostics']['internal_reject_count'],2)
        self.assertEqual(self.e.s['entry_diagnostics'].get('broker_submit_count',0),0)
        self.assertIsNone(self.e.enter({**self.signal(),'symbol':'999980'},replace(self.tick(),symbol='999980')))
        order=self.e.enter(self.signal(),self.tick())
        self.assertEqual(order['status'],'ACCEPTED');self.assertEqual(self.e.s['entries'],{})
        self.assertEqual(len(self.broker.orders),1)
        self.e.cumulative_fill(order,1,order['limit'])
        self.assertEqual(self.e.s['entries'],{'999990':1})
        total=min(order['qty'],2);self.e.cumulative_fill(order,total,total*order['limit'])
        self.e.cumulative_fill(order,total,total*order['limit'])
        self.assertEqual(self.e.s['entries'],{'999990':1})
        self.e.cancel(order)
        self.assertEqual(self.e.s['entry_diagnostics']['entry_count'],1)
        state=json.loads(self.e.path.read_text(encoding='utf-8'))
        self.assertEqual(state['entry_lifecycles'],{order['local']:'999990'})
        self.assertEqual(state['positions']['999990']['initial_stop'],self.signal()['stop'])

    def test_rejected_signal_cannot_retry_after_restart_but_other_symbols_not_counted(self):
        with patch.object(self.broker,'submit',side_effect=Rejected('internal gate')):
            order=self.e.enter(self.signal(),self.tick())
        self.assertEqual(sum(self.e.s['entries'].values()),0)
        from automation.engine import AutonomousEngine
        resumed=AutonomousEngine(self.cfg,self.auto,self.policy,self.e.folder,self.broker,lambda:self.clock[0])
        resumed.reconcile(self.snap);resumed.connected=True;resumed.market='3';resumed.market_status=self.e.market_status
        self.assertIsNone(resumed.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])

    def test_legacy_three_unfilled_rejects_migrate_without_losing_records(self):
        state=copy.deepcopy(self.e.s);state.pop('entry_count_basis');state.pop('entry_lifecycles')
        state['entries']={'003350':1,'038500':1,'012210':1}
        for i,sym in enumerate(state['entries']):
            state['orders'][str(i)]={'local':str(i),'symbol':sym,'side':'BUY','filled':0,'status':'REJECTED','failure_reason':'DAILY_AUTHORIZATION_LIVE_APPROVAL_CRITICAL_STATE_CHANGED'}
        atomic_json(self.e.path,state)
        from automation.engine import AutonomousEngine
        resumed=AutonomousEngine(self.cfg,self.auto,self.policy,self.e.folder,self.broker,lambda:self.clock[0])
        self.assertEqual(resumed.s['entries'],{})
        self.assertEqual(resumed.s['orders'],state['orders'])
        self.assertEqual(resumed.s['entry_counter_migration']['legacy_attempt_entries'],state['entries'])
        self.assertEqual(len(resumed.s['entry_failures']),3)

    def test_explicit_broker_reject_classified_separately_and_no_position(self):
        with patch.object(self.broker,'submit',side_effect=Rejected('kt10000 error',api='kt10000',response={'return_code':99,'return_msg':'synthetic'})):
            order=self.e.enter(self.signal(),self.tick())
        self.assertEqual(order['status'],'REJECTED');self.assertEqual(order['filled'],0)
        self.assertEqual(self.e.s['entries'],{});self.assertFalse(self.e.s['positions'])
        counters=self.e.s['entry_diagnostics']
        self.assertEqual(counters['broker_reject_count'],1);self.assertEqual(counters['broker_submit_count'],1)
        self.assertEqual(counters.get('internal_reject_count',0),0)

    def test_zero_fill_cancel_does_not_create_entry_and_first_fill_restart_dedup(self):
        order=self.e.enter(self.signal(),self.tick());self.e.cancel(order)
        self.assertEqual(self.e.s['entries'],{})
        self.e.reconcile(self.snap)
        self.assertEqual(order['status'],'CANCELED')
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        sym='999980';self.e.market_status[sym]={'pass':True}
        order=self.e.enter({**self.signal(),'symbol':sym},replace(self.tick(),symbol=sym));self.e.cumulative_fill(order,1,order['limit'])
        from automation.engine import AutonomousEngine
        resumed=AutonomousEngine(self.cfg,self.auto,self.policy,self.e.folder,self.broker,lambda:self.clock[0])
        durable=resumed.s['orders'][order['local']]
        resumed.cumulative_fill(durable,1,order['limit'])
        self.assertEqual(resumed.s['entries'],{'999980':1})
        self.assertEqual(resumed.s['entry_diagnostics']['entry_count'],1)

class PreIntentDiagnosticTests(AuditFixture):
    def test_risk_and_authorization_rejections_are_separate_from_filled_entries(self):
        self.policy.command('ARMED')
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.e.s['entry_diagnostics']['signal_count'],1)
        self.assertEqual(self.e.s['entry_diagnostics']['internal_reject_count'],1)
        self.assertEqual(self.e.s['entry_diagnostics']['entry_count'],0)
        self.assertEqual(self.e.s['entry_diagnostics']['broker_submit_count'],0)
        self.assertEqual(self.broker.orders,[])

class DailyEntryLimitTests(unittest.TestCase):
    def test_pending_slots_cannot_overbook_daily_fill_limit(self):
        fixture=v1.EngineTests();fixture.setUp()
        try:
            e,b=fixture.engine(True,max_positions=5,max_entries_per_day=3)
            for sym in ('111110','222220','333330'):self.assertIsNotNone(fixture.enter(e,sym))
            self.assertEqual(e.s['entries'],{});self.assertEqual(e.pending_entry_lifecycles(),3)
            self.assertIsNone(fixture.enter(e,'444440'))
        finally:fixture.tearDown()

    def test_three_distinct_filled_lifecycles_then_fourth_blocked(self):
        fixture=v1.EngineTests();fixture.setUp()
        try:
            e,b=fixture.engine(True,max_positions=1,max_entries_per_day=3)
            for sym in ('111110','222220','333330'):
                order=fixture.enter(e,sym);self.assertEqual(order['status'],'ACCEPTED')
                self.assertEqual(sum(e.s['entries'].values()),len(e.s['trades']))
                e.cumulative_fill(order,order['qty'],order['qty']*10000)
                e.request_exit(sym,'synthetic');e.drive_exits()
                sell=e.active_orders(sym,'SELL')[0];e.cumulative_fill(sell,sell['qty'],sell['qty']*10010)
            self.assertEqual(sum(e.s['entries'].values()),3)
            self.assertIsNone(fixture.enter(e,'444440'))
            self.assertEqual(len([o for o in b.orders if o[0]=='BUY']),3)
            self.assertEqual(e.s['entry_diagnostics']['entry_count'],3)
        finally:fixture.tearDown()

if __name__=='__main__':unittest.main()

