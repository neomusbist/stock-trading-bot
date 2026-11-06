import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import Mock
from core.broker import Rejected
from core.engine import Engine
from automation.runner import update_market

NOW=datetime(2026,11,6,9,4,tzinfo=timezone(timedelta(hours=9)))

class CircuitBreakerExitTests(unittest.TestCase):
    def setUp(self):
        self.e=object.__new__(Engine);self.time=NOW
        self.e.clock=lambda:self.time
        self.e.c=SimpleNamespace(live_mode=True,max_exit_attempts=3,reconcile_seconds=5,account_equity=100000)
        self.e.s={'day':'2026-11-06','positions':{'999990':{'qty':1,'exit_requested':True,'exit_attempts':1,'qty_blocked':False}},
                  'orders':{},'halt':'','excluded':[],'automation':{},'cash':10000,'equity':10000}
        self.e.save=Mock();self.e.event=Mock();self.e.submit=Mock();self.e.can_exit=Mock(return_value=True)
        self.e.exit_account_ready=Mock(return_value=True);self.e.market='3';self.e.account_ok=True
        self.order={'local':'a','symbol':'999990','side':'SELL','status':'INTENT','id':''}
        self.e.s['orders']['a']=self.order
    def cb(self):
        self.e.reject_order(self.order,Rejected('rejected',api='kt10001',response={'return_msg':'[2000](509193:CB 발동중입니다. 취소주문만 가능합니다.)'}),True)
    def snapshot(self,seconds=1,qty=1,available=1,orders=None):
        return {'query_started_at':(NOW+timedelta(seconds=seconds)).isoformat(),'holdings':{'999990':{'qty':qty,'available':available}},
                'orders':orders or [],'cash':10000,'equity':10000}
    def test_confirmed_cb_waits_for_both_new_account_and_exchange_event(self):
        self.cb();p=self.e.s['positions']['999990'];self.assertEqual(p['exit_attempts'],0)
        self.assertFalse(self.e.account_ok);self.e.drive_exits();self.e.submit.assert_not_called()
        self.time=NOW+timedelta(seconds=2)
        self.e.reconcile(self.snapshot(),[]);self.e.drive_exits();self.e.submit.assert_not_called()
        update_market(self.e,'3',self.time);self.e.drive_exits();self.e.submit.assert_called_once()
    def test_stale_snapshot_and_unavailable_shares_do_not_resume(self):
        for snapshot in (self.snapshot(0),self.snapshot(1,available=0)):
            with self.subTest(snapshot=snapshot):
                self.setUp();self.cb();self.time=NOW+timedelta(seconds=2)
                self.e.reconcile(snapshot,[]);update_market(self.e,'3',self.time);self.e.drive_exits()
                self.e.submit.assert_not_called()
    def test_unknown_order_blocks_resume(self):
        self.cb();self.e.s['orders']['u']={'local':'u','symbol':'999990','side':'SELL','status':'UNKNOWN','id':'x'}
        self.time=NOW+timedelta(seconds=2);update_market(self.e,'3',self.time)
        self.e.drive_exits();self.e.submit.assert_not_called()
    def test_old_exchange_event_or_nonopen_status_cannot_resume(self):
        self.cb();self.time=NOW+timedelta(seconds=2);self.e.reconcile(self.snapshot(),[])
        update_market(self.e,'3',NOW);self.e.drive_exits();self.e.submit.assert_not_called()
        update_market(self.e,'2',self.time);self.e.drive_exits();self.e.submit.assert_not_called()
    def test_mismatched_broker_position_cannot_resume(self):
        self.cb();self.time=NOW+timedelta(seconds=2);self.e.reconcile(self.snapshot(qty=2),[])
        update_market(self.e,'3',self.time);self.e.drive_exits();self.e.submit.assert_not_called()
    def test_other_rejections_do_not_receive_exception(self):
        err=Rejected('other',api='kt10001',response={'return_msg':'거부'})
        self.e.reject_order(self.order,err,True)
        self.assertNotIn('circuit_breaker_wait',self.e.s['positions']['999990'])
        self.assertEqual(self.e.s['positions']['999990']['exit_attempts'],1)
