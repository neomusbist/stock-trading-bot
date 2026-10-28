"""No network: per-TR dispatch, process sharing and account/protection isolation."""
import contextlib,io,json,multiprocessing,os,tempfile,threading,time,unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from core.balance_rate import BalanceRateBudget,BalanceBudgetError
from core.broker import Rest,Rejected,AmbiguousOrder
from core.settings import Config,now


class Response(io.BytesIO):
    def __init__(self,data,headers=None):
        super().__init__(json.dumps(data).encode());self.headers=headers or {}


class Clock:
    def __init__(self):self.at=0.
    def mono(self):return self.at
    def wall(self):return 1700000000.+self.at
    def sleep(self,seconds):self.at+=max(0,seconds)


def client(root,opener,clock=None,shared=None,mock=False):
    with patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE-BUDGET-ONLY','KIWOOM_APP_SECRET':'FAKE-NOT-SECRET'}):
        r=Rest(replace(Config(),live_mode=True,broker_environment='mock' if mock else 'production'),opener=opener,auth_session=shared)
    r.token='SYNTHETIC-TOKEN';r.expires=now()+timedelta(days=1)
    args=dict(root=root,mock=mock)
    if clock:args.update(monotonic=clock.mono,wall=clock.wall,sleep=clock.sleep)
    r._balance_budget=BalanceRateBudget(r.identity,**args)
    return r


def process_reader(root,output):
    sent=[]
    def opener(req,**kw):sent.append(time.perf_counter());return Response({'return_code':0})
    r=client(root,opener);r._balance_budget.max_wait=12.
    try:
        for _ in range(2):r.query('kt00018',{})
        output.put((sent,None))
    except Exception as e:output.put((sent,type(e).__name__))


class KT00018RateBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def fake_time(self,clock):
        stack=contextlib.ExitStack()
        for name,value in [('monotonic',clock.mono),('perf_counter',clock.mono),('time',clock.wall),('sleep',clock.sleep)]:
            stack.enter_context(patch('core.broker.time.'+name,value))
        return stack

    def assert_budget(self,stamps,gap=.55):
        stamps=sorted(stamps);self.assertGreater(len(stamps),1)
        self.assertTrue(all(b-a>=gap-.001 for a,b in zip(stamps,stamps[1:])),stamps)
        self.assertLessEqual(max(sum(t<=s<t+1 for s in stamps) for t in stamps),2)

    def test_all_clients_pages_and_recreated_sessions_share_one_budget(self):
        clock=Clock();sent=[]
        def opened(req,**kw):sent.append(clock.mono());return Response({'return_code':0,'rows':[{}]})
        with self.fake_time(clock):
            a=client(self.root,opened,clock);b=client(self.root,opened,clock,shared=a.auth_session)
            for r in (a,b,client(self.root,opened,clock),a,b,client(self.root,opened,clock)):
                r.query('kt00018',{})
        self.assert_budget(sent)

    def test_concurrent_threads_do_not_dispatch_reserved_slots_in_a_burst(self):
        sent=[];errors=[];barrier=threading.Barrier(4)
        def opened(req,**kw):sent.append(time.perf_counter());return Response({'return_code':0})
        def work():
            try:
                r=client(self.root,opened);r._balance_budget.max_wait=12.;barrier.wait();r.query('kt00018',{})
            except Exception as e:errors.append(type(e).__name__)
        threads=[threading.Thread(target=work) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join(15);self.assertFalse(t.is_alive())
        self.assertEqual(errors,[]);self.assert_budget(sent)

    def test_independent_windows_processes_share_rate_state(self):
        ctx=multiprocessing.get_context('spawn');output=ctx.Queue()
        workers=[ctx.Process(target=process_reader,args=(str(self.root),output)) for _ in range(3)]
        for p in workers:p.start()
        rows=[output.get(timeout=25) for _ in workers]
        for p in workers:p.join(10);self.assertEqual(p.exitcode,0)
        self.assertTrue(all(error is None for _,error in rows),rows)
        self.assert_budget([s for stamps,_ in rows for s in stamps])

    def test_business_rate_limit_has_bounded_retries_and_shared_backoff(self):
        clock=Clock();sent=[]
        def opened(req,**kw):
            sent.append(clock.mono());return Response({'return_code':1700,'return_msg':'limited'} if len(sent)<3 else {'return_code':0})
        with self.fake_time(clock):self.assertEqual(client(self.root,opened,clock).query('kt00018',{})['return_code'],0)
        self.assertEqual(len(sent),3);self.assertGreaterEqual(sent[1]-sent[0],1);self.assertGreaterEqual(sent[2]-sent[1],2)
        self.assert_budget(sent)

    def test_http_429_retries_but_other_business_errors_do_not(self):
        clock=Clock();sent=[]
        def opened(req,**kw):
            sent.append(clock.mono())
            if len(sent)<3:raise HTTPError(req.full_url,429,'limited',{'Retry-After':'1'},None)
            return Response({'return_code':0})
        with self.fake_time(clock):self.assertEqual(client(self.root,opened,clock).query('kt00018',{})['return_code'],0)
        self.assert_budget(sent);self.assertEqual(len(sent),3)
        calls=[]
        def denied(req,**kw):calls.append(1);return Response({'return_code':20,'return_msg':'business rejection'})
        with self.fake_time(clock),self.assertRaises(Rejected) as error:client(self.root/'other',denied,clock).query('kt00018',{})
        self.assertEqual(len(calls),1);self.assertFalse(getattr(error.exception,'balance_rate_limited',False))

    def test_exhausted_limit_and_long_retry_after_never_become_empty_balance(self):
        for header,expected in (({},3),({'Retry-After':'60'},1)):
            with self.subTest(header=header):
                clock=Clock();sent=[]
                def opened(req,**kw):sent.append(clock.mono());return Response({'return_code':1700,'return_msg':'original'},header)
                with self.fake_time(clock):
                    r=client(self.root/str(expected),opened,clock)
                    with self.assertRaises(Rejected) as error:r.snapshot()
                self.assertEqual(len(sent),expected);self.assertEqual(error.exception.api,'kt00018')
                self.assertTrue(error.exception.balance_rate_limited)
                self.assertEqual(error.exception.response['return_msg'],'original')
                self.assertNotIn('holdings',error.exception.response);self.assertNotIn('cash',error.exception.response)

    def test_orders_and_other_TRs_do_not_take_the_balance_lock_or_retry_orders(self):
        clock=Clock();apis=[]
        def opened(req,**kw):apis.append(req.get_header('Api-id'));return Response({'return_code':0})
        with self.fake_time(clock):
            r=client(self.root,opened,clock);r.balance_budget().backoff(60)
            r.query('kt00001',{});r.query('ka10075',{})
            self.assertLess(clock.mono(),1)
            r.opener=lambda req,**kw:(_ for _ in ()).throw(HTTPError(req.full_url,429,'limited',{},None))
            with self.assertRaises(AmbiguousOrder):r.call('kt10000','/api/dostk/ordr',{},mutation=True)
        self.assertEqual(apis,['kt00001','ka10075'])

    def test_authentication_retry_and_reconnect_cannot_reset_the_TR_budget(self):
        clock=Clock();sent=[];auth=[]
        def opened(req,**kw):
            if req.get_header('Api-id') is None:
                auth.append(1);return Response({'token':'NEW','expires_dt':'20500101000000'})
            sent.append(clock.mono())
            return Response({'return_code':3,'return_msg':'bad [8005:Token invalid]'} if len(sent)==1 else {'return_code':0})
        with self.fake_time(clock):
            r=client(self.root,opened,clock);r.query('kt00018',{});client(self.root,opened,clock).query('kt00018',{})
        self.assertEqual(len(auth),1);self.assert_budget(sent)

    def test_diagnostics_defer_without_stealing_a_slot(self):
        clock=Clock();sent=[]
        with self.fake_time(clock):
            r=client(self.root,lambda *a,**k:sent.append(1),clock);r.balance_budget().backoff(1)
            r.diagnostic_only=True
            with self.assertRaises(Rejected):r.query('kt00018',{})
        self.assertEqual(sent,[])

    def test_abandoned_request_and_clock_reset_wait_instead_of_bursting(self):
        for mode in ('inflight','clock_reset'):
            with self.subTest(mode=mode):
                clock=Clock();b=BalanceRateBudget(mode,root=self.root,monotonic=clock.mono,wall=clock.wall,sleep=clock.sleep)
                state=b.state(inflight=mode=='inflight')
                if mode=='clock_reset':state['mono']=500
                b.write(state)
                with b.slot():self.assertGreaterEqual(clock.mono(),1.05)

    def test_invalid_state_fails_closed_before_HTTP(self):
        r=client(self.root,lambda *a,**k:self.fail('HTTP must not run'))
        r.balance_budget().path.parent.mkdir(parents=True,exist_ok=True);r.balance_budget().path.write_text('partial',encoding='utf-8')
        with self.assertRaises(Rejected) as error:r.query('kt00018',{})
        self.assertTrue(error.exception.balance_rate_limited)

    def test_snapshot_pagination_is_budgeted_at_each_HTTP_attempt(self):
        clock=Clock();sent=[]
        def opened(req,**kw):
            api=req.get_header('Api-id')
            if api=='kt00018':
                sent.append(clock.mono());more=len(sent)<3
                return Response({'return_code':0,'acnt_evlt_remn_indv_tot':[],'prsm_dpst_aset_amt':'70000'},
                    {'cont-yn':'Y' if more else 'N','next-key':str(len(sent))})
            return Response({'return_code':0,**({'100stk_ord_alow_amt':'70000'} if api=='kt00001' else {'oso':[]})})
        with self.fake_time(clock):snapshot=client(self.root,opened,clock).snapshot()
        self.assertEqual(snapshot['cash'],70000);self.assertEqual(snapshot['holdings'],{})
        self.assertEqual(len(sent),3);self.assert_budget(sent)

    def test_account_worker_keeps_independent_order_status_reads_on_balance_throttle(self):
        from types import SimpleNamespace
        from automation.runner import AccountWorker
        error=Rest.balance_rejection(1700,{'return_msg':'limited'});calls=[]
        class Reader:
            def fills(self):calls.append('fills');return [{'ord_no':'FAKE'}]
            def snapshot(self):calls.append('balance');raise error
            def open_orders(self):calls.append('orders');worker.stop.set();return [{'ord_no':'FAKE','oso_qty':'1'}]
        worker=AccountWorker(Reader(),0);worker.min_query_interval=0;worker.need_fills=True;worker.run()
        result=worker.q.get_nowait();self.assertEqual(calls,['fills','balance','orders'])
        self.assertTrue(result['balance_rate_limited']);self.assertNotIn('snapshot',result)
        self.assertEqual(result['order_status_refresh']['fills'],[{'ord_no':'FAKE'}])

    def test_transient_balance_error_blocks_BUY_but_preserves_original_protection_deadline(self):
        from automation.runner import account_query_failed,error_record
        from tests import test_live_ready_20261021 as f
        with contextlib.redirect_stdout(io.StringIO()):
            h=f.LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
            h.seed('B');order=next(o for o in h.e.s['orders'].values() if o['side']=='BUY');h.fill(order,order['qty'],order['limit'])
            before_cash=h.e.s['cash'];before_actual=deepcopy(h.e.actual);before_at=h.e.last_account
            account_query_failed(h.e,{**error_record(Rest.balance_rejection(1700,{})),'started':time.monotonic()})
            self.assertTrue(h.e.account_ok);self.assertFalse(h.e.capital_account_valid())
            self.assertEqual(h.e.new_entry_reason('999980'),'KT00018_BALANCE_REFRESH_UNCERTAIN')
            self.assertEqual((h.e.s['cash'],h.e.actual,h.e.last_account),(before_cash,before_actual,before_at))
            p=h.e.s['positions'][f.SYM];hard=(p['signal'].get('invalidation_policy') or {}).get('emergency_stop') or p['stop']
            h.feed(h.clock+timedelta(seconds=1),hard-1)
            self.assertTrue(any(o['side']=='SELL' for o in h.e.s['orders'].values()))
            h.e.last_account=time.monotonic()-16
            account_query_failed(h.e,{**error_record(Rest.balance_rejection(1700,{})),'started':time.monotonic()})
            self.assertFalse(h.e.account_ok)  # no extension of the 15-second safety limit

    def test_full_successful_reconciliation_clears_uncertainty_only_after_success(self):
        from tests import test_automation as f
        h=f.AutomationTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        with contextlib.redirect_stdout(io.StringIO()):
            e=h.make_engine();e.balance_refresh_uncertain=True
            e.reconcile({'cash':81300,'equity':81300,'holdings':{},'orders':[]})
        self.assertTrue(e.account_ok);self.assertFalse(e.balance_refresh_uncertain)


if __name__=='__main__':unittest.main()
