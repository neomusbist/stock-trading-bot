"""Reproduce opening starvation and stale cash labels without a broker/API."""
from copy import deepcopy
from datetime import datetime
from queue import Queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from automation.runner import ready_account_results,record_observation_retention,evaluate_actual_entry,drain_feed
from core.settings import KST
from core.strategy import Tick

AT=datetime(2026,10,19,9,10,tzinfo=KST)


class AccountFairnessTests(unittest.TestCase):
    def test_continuously_replenished_account_queue_yields_to_a_real_quote(self):
        account=SimpleNamespace(q=Queue());account.q.put({'fills':['partial-1']})
        handled=[]
        for result in ready_account_results(account,True):
            handled.append(result);account.q.put({'fills':['partial-2']})
        self.assertEqual(handled,[{'fills':['partial-1']}])
        self.assertEqual(account.q.qsize(),1)
        feed=SimpleNamespace(q=Queue());feed.q.put((AT,{'trnm':'REAL','data':[]}))
        seen=[];drain_feed(feed,lambda *r:seen.append(r),clock=lambda:AT,timeout=0)
        self.assertEqual(len(seen),1)
        self.assertEqual(list(ready_account_results(account,True)),[{'fills':['partial-2']}])

    def test_fifo_fill_evidence_is_never_merged_or_discarded(self):
        account=SimpleNamespace(q=Queue())
        first={'fills':[{'id':1,'qty':1}]};second={'fills':[{'id':1,'qty':4}]}
        account.q.put(first);account.q.put(second)
        self.assertEqual(list(ready_account_results(account,True)),[first])
        self.assertEqual(list(ready_account_results(account,True)),[second])
        self.assertEqual(list(ready_account_results(account,True)),[])

    def test_closed_session_never_consumes_pending_account_evidence(self):
        account=SimpleNamespace(q=Queue());account.q.put({'fills':[1]})
        self.assertEqual(list(ready_account_results(account,False)),[])
        self.assertEqual(account.q.qsize(),1)

    def test_retention_unchanged_for_725_loops_is_logged_once(self):
        engine=Mock();prior=set();retained={'009190','009900'}
        for _ in range(725):
            prior=record_observation_retention(engine,retained,prior,retained,360,64)
        self.assertEqual(engine.event.call_count,1)
        retained.add('354320')
        self.assertNotEqual(prior,retained)
        prior=record_observation_retention(engine,retained,prior,retained,360,64)
        prior=record_observation_retention(engine,set(),prior,set(),360,64)
        record_observation_retention(engine,set(),prior,set(),360,64)
        self.assertEqual(engine.event.call_count,3)
        self.assertEqual(engine.event.call_args.kwargs['symbols'],[])


class CachedCashMarkerTests(unittest.TestCase):
    def build(self,fresh,reason='ACCOUNT_CAPITAL_UNAVAILABLE'):
        engine=SimpleNamespace(connected=True,capital_account_valid=Mock(return_value=fresh),
            decisions=Mock(),a=SimpleNamespace(min_source_count=2),names={},market_status={})
        detector=Mock();detector.signal.return_value=None;detector.evaluation_gates.return_value={}
        detector.last_eval={}
        tick=Tick('009900',AT,9750,1,1,9750,9740,True)
        watch={'status':'WATCH','source_count':2,'observation_only':True,'reason':reason}
        # No FAST, broker or account order methods exist. The detector is
        # the first observable stage after the cached-cash marker.
        return engine,detector,tick,watch

    def evaluate(self,engine,detector,tick,watch):
        return evaluate_actual_entry(engine,detector,tick,watch,SimpleNamespace(market_status={}),{},Mock())

    def test_stale_account_keeps_observation_only_even_with_a_setup(self):
        e,d,t,w=self.build(False);self.evaluate(e,d,t,w)
        d.signal.assert_not_called()
        self.assertEqual(e.decisions.emit.call_args.args[3],'ACCOUNT_CAPITAL_UNAVAILABLE')

    def test_recovered_account_can_reach_pattern_checks_without_changing_watch(self):
        e,d,t,w=self.build(True);before=deepcopy(w)
        e.a.fast_momentum_enabled=False
        self.evaluate(e,d,t,w)
        d.signal.assert_called_once();self.assertEqual(w,before)
        self.assertEqual(e.decisions.emit.call_args.args[3],'PATTERN_OR_GATE_NOT_READY')

    def test_other_observation_only_reasons_cannot_gain_buy_permission(self):
        e,d,t,w=self.build(True,reason='UNVERIFIED_STRUCTURE');self.evaluate(e,d,t,w)
        d.signal.assert_not_called()

    def test_recovered_account_does_not_bypass_real_zero_cash_sizing(self):
        from tests.test_r6_fast_live_pipeline import FastPipelineProductionLikeFlagsTests,SYM
        case=FastPipelineProductionLikeFlagsTests(methodName='runTest');case.setUp();self.addCleanup(case.doCleanups)
        case.e.available_cash=0;case.e.s['cash']=0
        case.watch[SYM].update(observation_only=True,reason='ACCOUNT_CAPITAL_UNAVAILABLE')
        case.series()
        self.assertEqual(len(case.orders('BUY')),0)
