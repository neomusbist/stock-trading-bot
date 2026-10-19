"""A retained terminal zero-fill audit record is not a permanent symbol ban."""
from copy import deepcopy
from datetime import datetime
import unittest

from core.engine import Engine
from core.settings import KST
from tests.test_audit_followup import AuditFixture

DAY='2026-10-19'
OLD='2026-10-16'
AT=datetime(2026,10,19,9,25,17,tzinfo=KST)


class PriorDayEntryFailureTests(unittest.TestCase):
    def setUp(self):
        self.engine=Engine.__new__(Engine);self.engine.clock=lambda:AT
        self.failure={'local':OLD+'-81','reason':'ZERO_FILL_CANCELED_NO_AUTO_RETRY','day':OLD}
        self.order={'local':OLD+'-81','symbol':'079650','side':'BUY','status':'CANCELED',
                    'filled':0,'qty':9,'created':OLD+'T11:33:27.408384+09:00'}
        self.engine.s={'day':DAY,'entry_failures':{'079650':self.failure},
                       'orders':{self.order['local']:self.order},'positions':{}}

    def test_actual_seosan_terminal_zero_fill_latch_expires_without_ledger_mutation(self):
        before=deepcopy(self.engine.s)
        self.assertIsNone(self.engine.entry_failure('079650'))
        self.assertEqual(self.engine.s,before)

    def test_previous_day_explicit_reject_is_also_resolved(self):
        self.order['status']='REJECTED'
        self.assertIsNone(self.engine.entry_failure('079650'))

    def test_same_day_zero_fill_latch_still_blocks(self):
        self.failure['day']=DAY;self.order['created']=DAY+'T09:00:00+09:00'
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_unknown_or_unfinished_previous_order_still_blocks(self):
        for status in ('UNKNOWN','INTENT','ACCEPTED','CANCEL_SENT','PARTIAL','FILLED'):
            with self.subTest(status=status):
                self.order['status']=status
                self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_partial_fill_and_existing_position_never_expire_by_this_rule(self):
        self.order['filled']=1
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)
        self.order['filled']=0;self.engine.s['positions']['079650']={'qty':1}
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_another_current_order_keeps_duplicate_prevention(self):
        self.engine.s['orders']['new']={**self.order,'local':'new','status':'UNKNOWN'}
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_unknown_future_and_invalid_day_evidence_stays_blocked(self):
        for value in (None,'bad','2026-10-20'):
            with self.subTest(day=value):
                self.failure['day']=value
                self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_missing_mismatched_or_nonzero_order_evidence_stays_blocked(self):
        for changes in ({'symbol':'009190'},{'side':'SELL'},{'created':DAY+'T09:00:00+09:00'},
                        {'filled':None},{'filled':'0'},{'filled':False}):
            with self.subTest(changes=changes):
                original=dict(self.order);self.order.update(changes)
                self.assertEqual(self.engine.entry_failure('079650'),self.failure)
                self.order.clear();self.order.update(original)
        self.engine.s['orders'].clear()
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)

    def test_wall_day_and_ledger_day_mismatch_remains_blocked(self):
        self.engine.s['day']=OLD
        self.assertEqual(self.engine.entry_failure('079650'),self.failure)


class PriorDayEntryGateTests(AuditFixture):
    def test_only_resolved_past_day_block_changes_in_repeat_gate_no_order_sent(self):
        self.clock[0]=AT;self.e.s['day']=DAY
        order={'local':OLD+'-81','symbol':'999990','side':'BUY','status':'CANCELED',
               'filled':0,'qty':9,'created':OLD+'T11:33:27+09:00'}
        self.e.s['orders'][order['local']]=order
        failure={'local':order['local'],'reason':'ZERO_FILL_CANCELED_NO_AUTO_RETRY','day':OLD}
        self.e.s['entry_failures']['999990']=failure
        before=deepcopy(self.e.s)
        _,gates=self.e.entry_gates(self.signal(),self.tick())
        self.assertEqual(gates['repeat_slots']['result'],'PASS')
        self.assertEqual(self.e.s,before)
        self.assertEqual(self.broker.orders,[])
        failure['day']=DAY
        self.assertEqual(self.e.entry_gates(self.signal(),self.tick())[1]['repeat_slots']['result'],'FAIL')


if __name__=='__main__':unittest.main()
