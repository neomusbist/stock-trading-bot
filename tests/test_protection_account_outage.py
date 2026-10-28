"""Synthetic real-engine protection scenarios. No network or actual orders."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import contextlib,io,json,time,unittest
from unittest.mock import patch
from core.broker import Rest,AmbiguousOrder,Rejected
from automation.runner import account_query_failed,error_record,AccountWorker
from tests import test_live_ready_20261021 as f


class ProtectionOutageTests(unittest.TestCase):
    def setUp(self):
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)
        self.no_network=patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK'));self.no_network.start();self.addCleanup(self.no_network.stop)
        self.h=f.LiveReadyLifecycle('runTest');self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.seed('B');self.e=self.h.e
        self.buy=next(o for o in self.e.s['orders'].values() if o['side']=='BUY')
        self.h.fill(self.buy,self.buy['qty'],self.buy['limit']);self.qty=self.buy['qty']
        self.original=deepcopy((self.e.s['cash'],self.e.actual))

    def clock_forward(self):
        self.h.clock+=timedelta(seconds=20)
        self.h.feed(self.h.clock,self.buy['limit'],execute=False)

    def stale(self,view=None):
        self.e.last_account=time.monotonic()-20
        result={**error_record(Rest.balance_rejection(1700,{})),'started':time.monotonic()}
        if view is not None:result['order_status_refresh']=view
        account_query_failed(self.e,result)

    def view(self,*,partial=None,exclude=()):
        fills=[];orders=[]
        for o in self.e.s['orders'].values():
            if o['id'] in exclude:continue
            total=o['filled'];amount=o['amount']
            if partial and partial[0]==o['local']:total=partial[1];amount=total*self.buy['limit']
            if total:fills.append(dict(ord_no=o['id'],stk_cd=o['symbol'],ord_stt='체결',
                cntr_qty=str(total),cntr_pric=str(amount/total),io_tp_nm='매수' if o['side']=='BUY' else '매도'))
            if o['status'] not in {'FILLED','CANCELED','REJECTED'} and total<o['qty']:
                orders.append(dict(ord_no=o['id'],stk_cd=o['symbol'],ord_qty=str(o['qty']),oso_qty=str(o['qty']-total)))
        return dict(orders=orders,fills=fills,complete=True,
            known_monotonic=time.monotonic(),
            query_started_at=(self.h.clock-timedelta(milliseconds=100)).isoformat(),known_at=self.h.clock.isoformat())

    def sells(self):return [o for o in self.e.s['orders'].values() if o['side']=='SELL']

    def test_balance_only_failure_can_protect_verified_fills_beyond_15_seconds(self):
        self.clock_forward();self.stale(self.view())
        self.e.request_exit(f.SYM,'SYNTHETIC_EMERGENCY');self.e.drive_exits({f.SYM:self.buy['limit']})
        sells=self.sells();self.assertEqual(len(sells),1)
        self.assertEqual((sells[0]['status'],sells[0]['qty']),('ACCEPTED',self.qty))
        self.assertIn('protection_authority',sells[0])
        self.assertFalse(self.e.capital_account_valid());self.assertFalse(self.e.account_ok)
        self.assertEqual((self.e.s['cash'],self.e.actual),self.original)
        self.assertTrue(self.e.new_entry_reason('999980'))
        for _ in range(3):self.e.drive_exits({f.SYM:self.buy['limit']})
        self.assertEqual(len(self.sells()),1)

    def test_pending_partial_sell_is_reserved_then_only_confirmed_residual_is_sold(self):
        part=max(1,self.qty//2);o=self.e.submit('SELL',f.SYM,part,self.buy['limit'])
        self.assertEqual(o['status'],'ACCEPTED');self.clock_forward()
        self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_PARTIAL');self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.assertEqual(self.e.s['protection_safety']['active_failures'][f.SYM]['unreserved'],self.qty-part)
        # Complete fresh fill query proves the first SELL filled; no inference
        # from a missing open-order row alone is permitted.
        self.h.clock+=timedelta(seconds=1)
        self.h.feed(self.h.clock,self.buy['limit'],execute=False)
        view=self.view(partial=(o['local'],part));self.stale(view);self.e.drive_exits()
        sells=self.sells();self.assertEqual(len(sells),2)
        self.assertEqual(sells[-1]['qty'],self.qty-part)
        self.assertEqual(sum(x['qty'] for x in sells),self.qty)

    def test_ambiguous_sell_is_never_retried(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_UNKNOWN')
        with patch.object(self.h.broker,'submit',side_effect=AmbiguousOrder('SYNTHETIC_TIMEOUT')):
            self.e.drive_exits()
        self.assertEqual(self.sells()[0]['status'],'UNKNOWN')
        for _ in range(3):self.stale(self.view());self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.assertTrue(self.e.s['protection_safety']['active_failures'])

    def test_two_failed_queries_persist_failure_and_do_not_zero_or_sell(self):
        self.clock_forward();self.stale();self.e.request_exit(f.SYM,'SYNTHETIC_DUAL_OUTAGE');self.e.drive_exits()
        state=json.loads(self.e.path.read_text(encoding='utf-8'))
        self.assertTrue(state['positions'][f.SYM]['exit_requested'])
        self.assertTrue(state['protection_safety']['active_failures'][f.SYM]['urgent'])
        self.assertEqual(self.sells(),[]);self.assertEqual((self.e.s['cash'],self.e.actual),self.original)

    def test_stale_partial_future_and_duplicate_status_rows_fail_closed(self):
        self.clock_forward()
        for kind in ('missing_complete','future','stale','duplicate','missing_fill','quantity','external'):
            with self.subTest(kind=kind):
                view=self.view()
                if kind=='missing_complete':view.pop('complete')
                if kind=='future':view['known_at']=(self.h.clock+timedelta(seconds=1)).isoformat()
                if kind=='stale':view['known_at']=(self.h.clock-timedelta(seconds=6)).isoformat()
                if kind=='duplicate':view['fills']*=2
                if kind=='missing_fill':view['fills']=[]
                if kind=='quantity':view['fills'][0]['cntr_qty']=str(self.qty+1)
                if kind=='external':view['orders']=[dict(ord_no='UNKNOWN-ID',stk_cd=f.SYM,ord_qty='1',oso_qty='1')]
                self.stale(view);self.e.request_exit(f.SYM,'SYNTHETIC_STATUS');self.e.drive_exits()
                self.assertEqual(self.sells(),[])

    def test_disconnect_and_restart_need_new_full_account_anchor(self):
        self.clock_forward();self.stale(self.view());self.e.disconnect();self.e.connected=True
        self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_DISCONNECT');self.e.drive_exits()
        self.assertEqual(self.sells(),[])
        self.assertIn('ANCHOR',self.e.s['protection_safety']['active_failures'][f.SYM]['reason'])
        snapshot=dict(cash=self.original[0],equity=self.original[0]+self.qty*self.buy['limit'],
            holdings=deepcopy(self.original[1]),orders=[],query_started_at=self.h.clock.isoformat())
        self.e.reconcile(snapshot);self.h.clock+=timedelta(seconds=1)
        self.h.feed(self.h.clock,self.buy['limit'],execute=False);self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)

    def test_stale_quote_and_HARD_LOCK_never_get_fallback_permission(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_QUOTE')
        self.e.s['positions'][f.SYM]['last_quote']={}
        self.e.drive_exits();self.assertEqual(self.sells(),[])
        self.e.s['excluded'].append(f.SYM);self.h.feed(self.h.clock+timedelta(seconds=1),self.buy['limit'],execute=False)
        self.e.drive_exits();self.assertEqual(self.sells(),[])

    def test_recorder_exception_does_not_block_verified_protection(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_RECORDER')
        class BrokenRecorder:
            def event_ref(self,*a,**kw):raise OSError('SYNTHETIC_DIAGNOSTIC_FAILURE')
        self.e.trade_review=BrokenRecorder();self.e.drive_exits()
        self.assertEqual(self.sells()[0]['status'],'ACCEPTED')
        self.assertTrue(json.loads(self.e.path.read_text(encoding='utf-8'))['orders'][self.sells()[0]['local']]['protection_authority'])

    def test_cancel_result_ambiguity_and_ledger_write_failure_never_send_an_extra_order(self):
        part=max(1,self.qty//2);o=self.e.submit('SELL',f.SYM,part,self.buy['limit'])
        o['status']='CANCEL_SENT';self.e.save();self.clock_forward()
        self.stale(self.view(exclude=(o['id'],)));self.e.request_exit(f.SYM,'SYNTHETIC_CANCEL_UNKNOWN');self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.assertEqual(o['status'],'CANCEL_SENT')
        self.assertTrue(self.e.s['protection_safety']['active_failures'])

    def test_financial_checkpoint_failure_prevents_transport(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_LEDGER')
        before=len(self.h.broker.orders)
        with patch.object(self.e,'save',side_effect=OSError('SYNTHETIC_CRITICAL_DISK_FAILURE')):
            with self.assertRaises(OSError):self.e.drive_exits()
        self.assertEqual(len(self.h.broker.orders),before)

    def test_policy_HARD_LOCK_and_unexplained_unsellable_anchor_block(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_LOCK')
        with patch('automation.engine.HARD_LOCKS',{f.SYM}):
            self.e.drive_exits();self.assertEqual(self.sells(),[])
        self.e._protection_anchor['holdings'][f.SYM]={'qty':self.qty,'available':0}
        self.e._protection_anchor['reserved'][f.SYM]=0
        self.e.drive_exits();self.assertEqual(self.sells(),[])

    def test_process_restart_does_not_restore_an_old_sell_certificate(self):
        from automation.engine import AutonomousEngine
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_RESTART')
        restored=AutonomousEngine(self.h.cfg,self.h.auto,self.h.policy,self.e.folder,self.h.broker,lambda:self.h.clock)
        restored.connected=True;restored.market='3'
        restored.accept_protection_orders(self.view())
        restored.drive_exits()
        self.assertFalse(any(o['side']=='SELL' for o in restored.s['orders'].values()))
        self.assertTrue(restored.s['positions'][f.SYM]['exit_requested'])
        self.assertEqual(restored.s['protection_safety']['active_failures'][f.SYM]['reason'],'FRESH_PROCESS_ACCOUNT_ANCHOR_REQUIRED')

    def test_durable_failure_is_visible_to_GUI_without_diagnostic_events(self):
        from tests.test_operator_interface import OperatorInterfaceTests
        self.clock_forward();self.stale();self.e.request_exit(f.SYM,'SYNTHETIC_UI');self.e.drive_exits()
        ui=OperatorInterfaceTests('runTest');ui.setUp();self.addCleanup(ui.doCleanups)
        state=json.loads(self.e.path.read_text(encoding='utf-8'))
        status=ui.s.public_status(source=(self.e.folder,state,{}),recent_events=[])
        self.assertTrue(status['current_error'].startswith('긴급: 보호청산 불가'))
        self.assertTrue(status['protection_failures'][f.SYM]['urgent'])
        from operator_ui_service import presentation
        self.assertTrue(presentation(status)[2].startswith('긴급 — 보호청산 불가'))

    def test_account_worker_fetches_complete_orders_and_fills_for_balance_only_network_error(self):
        calls=[]
        class Reader:
            def fills(self):calls.append('fills');return []
            def snapshot(self):calls.append('balance');raise TimeoutError('SYNTHETIC_BALANCE_TIMEOUT')
            def open_orders(self):calls.append('orders');worker.stop.set();return []
        worker=AccountWorker(Reader(),0);worker.min_query_interval=0;worker.position_symbols=frozenset({f.SYM})
        worker.run();result=worker.q.get_nowait()
        self.assertEqual(calls,['fills','balance','orders'])
        self.assertTrue(result['order_status_refresh']['complete']);self.assertNotIn('snapshot',result)

    def test_failed_order_status_does_not_certify_an_empty_account(self):
        calls=[]
        class Reader:
            def fills(self):calls.append('fills');return []
            def snapshot(self):calls.append('balance');raise Rest.balance_rejection(1700,{})
            def open_orders(self):calls.append('orders');worker.stop.set();raise TimeoutError('SYNTHETIC_ORDERS_TIMEOUT')
        worker=AccountWorker(Reader(),0);worker.min_query_interval=0;worker.need_fills=True
        worker.run();result=worker.q.get_nowait()
        self.assertNotIn('order_status_refresh',result);self.assertNotIn('snapshot',result)
        self.assertIn('order_status_error',result)

    def test_terminal_local_sell_cannot_hide_a_positive_broker_remainder(self):
        part=max(1,self.qty//2);o=self.e.submit('SELL',f.SYM,part,self.buy['limit'])
        o['status']='CANCELED';self.e.save();self.clock_forward();view=self.view()
        view['orders'].append(dict(ord_no=o['id'],stk_cd=f.SYM,ord_qty=str(part),oso_qty=str(part)))
        self.stale(view);self.e.request_exit(f.SYM,'SYNTHETIC_TERMINAL_CONFLICT');self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.assertEqual(self.e.s['protection_safety']['active_failures'][f.SYM]['reason'],'ORDER_STATUS_CONFLICT')

    def test_fresh_0D_without_new_trade_preserves_depth_and_protects(self):
        from automation.position_health import PositionDataHealth
        from types import SimpleNamespace
        self.clock_forward();self.stale(self.view());self.e.s['positions'][f.SYM]['last_quote']={}
        self.h.clock+=timedelta(seconds=1)
        health=PositionDataHealth(self.e,SimpleNamespace(),self.h.account,SimpleNamespace())
        hard=self.e.s['positions'][f.SYM]['signal']['invalidation_policy']['emergency_stop']
        book=dict(time=self.h.clock.isoformat(),received_at=self.h.clock.isoformat(),received_monotonic=time.monotonic(),
            best_bid=hard-10,best_ask=hard,bid_qty=10000,ask_qty=10000,depth_available=True)
        self.assertTrue(health.accept_book(f.SYM,book,self.h.clock,source='0D'))
        self.assertEqual(self.e.s['positions'][f.SYM]['last_quote']['bid_qty'],10000)
        self.e.drive_exits()  # next normal operating-loop protection pass, no invented 0B
        self.assertEqual(self.sells()[0]['status'],'ACCEPTED')

    def test_monotonic_expiry_and_final_HTTP_race_are_blocked(self):
        self.clock_forward();self.stale(self.view());self.e.request_exit(f.SYM,'SYNTHETIC_RACE')
        self.e._protection_order_view['known_monotonic']-=6
        self.e.drive_exits();self.assertEqual(self.sells(),[])
        self.stale(self.view());original=self.h.broker.submit;before=len(self.h.broker.orders)
        def race(side,sym,qty,limit,**kw):
            self.e.s['orders']['SYNTHETIC-RACE']=dict(local='SYNTHETIC-RACE',id='UNKNOWN-RACE',symbol=f.SYM,
                side='SELL',qty=1,filled=0,amount=0,limit=0,status='UNKNOWN',signal={},created=self.h.clock.isoformat())
            return original(side,sym,qty,limit,**kw)
        with patch.object(self.h.broker,'submit',side_effect=race):self.e.drive_exits()
        self.assertEqual(len(self.h.broker.orders),before)
        self.assertTrue(self.e.s['protection_safety']['active_failures'])

    def test_stale_account_fallback_is_not_a_general_sell_permission(self):
        self.clock_forward();self.stale(self.view());before=len(self.h.broker.orders)
        order=self.e.submit('SELL',f.SYM,self.qty,self.buy['limit'])
        self.assertEqual(order['status'],'REJECTED')
        self.assertEqual(len(self.h.broker.orders),before)

    def test_cross_symbol_broker_ID_alias_and_duplicate_local_ID_fail_closed(self):
        self.clock_forward()
        old=dict(local='SYNTHETIC-OLD',id='ID-ALIAS',symbol='999991',side='SELL',qty=1,filled=0,amount=0,
            limit=0,status='CANCELED',created=self.h.clock.isoformat(),signal={})
        self.e.s['orders'][old['local']]=old;view=self.view()
        view['orders'].append(dict(ord_no='ID-ALIAS',stk_cd=f.SYM,ord_qty='1',oso_qty='1'))
        self.stale(view);self.e.request_exit(f.SYM,'SYNTHETIC_ALIAS');self.e.drive_exits()
        self.assertFalse(any(o['symbol']==f.SYM for o in self.sells()))
        self.assertEqual(self.e.s['protection_safety']['active_failures'][f.SYM]['reason'],'OPEN_IDENTITY_MISMATCH')
        old['id']=self.buy['id'];self.stale(self.view());self.e.drive_exits()
        self.assertEqual(self.e.s['protection_safety']['active_failures'][f.SYM]['reason'],'LOCAL_BROKER_ID_COLLISION')


if __name__=='__main__':unittest.main()
