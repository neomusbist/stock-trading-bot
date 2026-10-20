"""Real SHADOW metadata handoff; synthetic inputs, temporary ledger, FakeBroker."""
from copy import deepcopy
from datetime import datetime,timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.settings import KST
from automation.runner import merge_verified_shadow_candidates,latest_evaluation_watch,evaluate_actual_entry
from tests import test_subscription_refresh as fixture

AT=datetime(2026,10,21,9,4,tzinfo=KST)
SYM='999990'


def status(at=AT,passed=True,reason='NORMAL'):
    return {'pass':passed,'checked_at':at.isoformat(),'reason_code':reason}


def shadow(st):
    row={'code':SYM,'name':'SYNTHETIC_ONLY','status':'RAW','source_count':2,
         'affordability':{'entry_eligible':True},'reference_price':1000,
         'shadow_view':{'status':'ELIGIBLE','resistance':1200,'market_status':deepcopy(st)}}
    return SimpleNamespace(observer=SimpleNamespace(rows={SYM:row}),metadata={SYM:{'status':deepcopy(st)}})


class StatusMergeTests(unittest.TestCase):
    def raw(self):
        return [{'code':SYM,'status':'UNKNOWN','reason':'STATUS_SCAN_LIMIT','source_count':2,
                 'source_details':{'one':{},'two':{}},'affordability':{'entry_eligible':True}}]

    def test_existing_shadow_merge_gets_refreshed_status(self):
        worker=shadow(status(AT-timedelta(seconds=120)))
        first=merge_verified_shadow_candidates(self.raw(),worker,{})
        self.assertEqual(first[0]['status'],'SHADOW')
        fresh=status()
        worker.observer.rows[SYM]['shadow_view']['market_status']=fresh
        worker.metadata[SYM]['status']=fresh
        refreshed=merge_verified_shadow_candidates(first,worker,{})
        self.assertEqual(refreshed[0]['market_status'],fresh)
        self.assertNotEqual(first[0]['market_status'],fresh,'input snapshot stays immutable')

    def test_status_scan_limit_accepts_only_current_verified_worker_state(self):
        for seconds,expected in ((-180,'SHADOW'),(-181,'UNKNOWN'),(.001,'UNKNOWN')):
            with self.subTest(seconds=seconds):
                rows=merge_verified_shadow_candidates(self.raw(),shadow(status(AT+timedelta(seconds=seconds))),{},AT,180)
                self.assertEqual(rows[0]['status'],expected)

    def test_missing_naive_malformed_previous_day_status_cannot_fill_scan_limit(self):
        for checked in (None,'bad',AT.replace(tzinfo=None).isoformat(),(AT-timedelta(days=1)).isoformat()):
            with self.subTest(checked=checked):
                st={'pass':True}
                if checked is not None:st['checked_at']=checked
                rows=merge_verified_shadow_candidates(self.raw(),shadow(st),{},AT,180)
                self.assertEqual(rows[0]['status'],'UNKNOWN')

    def test_existing_shadow_expiry_and_unknown_block_then_fresh_metadata_recovers(self):
        worker=shadow(status())
        rows=merge_verified_shadow_candidates(self.raw(),worker,{},AT,180)
        expired=merge_verified_shadow_candidates(rows,worker,{},AT+timedelta(seconds=181),180)
        self.assertEqual(expired[0]['status'],'UNKNOWN')
        self.assertIsNone(expired[0]['market_status']['pass'])
        fresh=status(AT+timedelta(seconds=181))
        worker.metadata[SYM]['status']=fresh
        refreshed=merge_verified_shadow_candidates(expired,worker,{},AT+timedelta(seconds=181),180)
        self.assertEqual(refreshed[0]['status'],'SHADOW')
        self.assertEqual(refreshed[0]['market_status'],fresh)

    def test_merge_preserves_fresher_existing_market_state(self):
        rows=self.raw();rows[0].update(status='SHADOW',market_status=status())
        old=shadow(status(AT-timedelta(seconds=10)))
        merged=merge_verified_shadow_candidates(rows,old,{},AT,180)
        self.assertEqual(merged[0]['market_status'],rows[0]['market_status'])

    def test_unknown_shadow_recovers_after_actual_new_worker_check_outside_radar_scan(self):
        worker=shadow(status())
        admitted=merge_verified_shadow_candidates(self.raw(),worker,{},AT,180)
        worker.metadata[SYM]['status']={'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'}
        unknown_at=AT+timedelta(seconds=1)
        blocked=merge_verified_shadow_candidates(admitted,worker,{},unknown_at,180)
        self.assertEqual(blocked[0]['status'],'UNKNOWN')
        self.assertEqual(blocked[0]['market_status']['checked_at'],AT.isoformat())
        self.assertEqual(blocked[0]['market_status']['unknown_observed_at'],unknown_at.isoformat())
        checked=AT+timedelta(seconds=2)
        worker.metadata[SYM]['status']=status(checked)
        recovered=merge_verified_shadow_candidates(blocked,worker,{},checked,180)
        self.assertEqual(recovered[0]['status'],'SHADOW')
        self.assertEqual(recovered[0]['market_status'],status(checked))


class LatestStateTests(unittest.TestCase):
    def view(self,watch,radar,worker):
        return latest_evaluation_watch({'market_status':watch},SimpleNamespace(status_cache={SYM:radar}),
                                       SYM,worker,AT,180)['market_status']

    def test_newest_worker_unknown_and_special_state_override_older_normal(self):
        for passed,reason in ((False,'VI_ACTIVE'),(None,'MARKET_STATUS_UNKNOWN')):
            with self.subTest(passed=passed):
                current=status(AT,passed,reason)
                result=self.view(status(AT-timedelta(seconds=20)),status(AT-timedelta(seconds=10)),shadow(current))
                self.assertEqual(result,current)

    def test_newer_radar_special_state_cannot_be_rolled_back_by_worker(self):
        current=status(AT,False,'TRADING_HALTED')
        self.assertEqual(self.view(status(AT-timedelta(seconds=20)),current,
                                   shadow(status(AT-timedelta(seconds=10)))),current)

    def test_stale_future_and_malformed_worker_state_never_grant_permission(self):
        for checked in ((AT-timedelta(seconds=181)).isoformat(),(AT+timedelta(microseconds=1)).isoformat(),
                        AT.replace(tzinfo=None).isoformat(),'bad'):
            with self.subTest(checked=checked):
                result=self.view(status(AT-timedelta(seconds=190)),None,shadow({'pass':True,'checked_at':checked}))
                self.assertIsNot(result.get('pass'),True)

    def test_fresh_normal_comes_from_worker_when_radar_has_no_scan_result(self):
        worker=shadow(status());before=deepcopy(worker.metadata)
        self.assertEqual(self.view(status(AT-timedelta(seconds=181)),None,worker),status())
        self.assertEqual(worker.metadata,before)

    def test_malformed_radar_cannot_erase_newer_halt_before_older_worker_pass(self):
        newest=status(AT,False,'VI_ACTIVE')
        malformed={'pass':True,'checked_at':'bad'}
        result=self.view(newest,malformed,shadow(status(AT-timedelta(seconds=1))))
        self.assertIsNone(result['pass'])

    def test_untimed_unknown_cannot_be_resolved_by_older_normal_worker_state(self):
        for passed in (None,False):
            with self.subTest(passed=passed):
                unknown={'pass':passed,'reason_code':'MARKET_STATUS_UNKNOWN_OR_VI'}
                result=self.view(unknown,None,shadow(status(AT-timedelta(seconds=1))))
                self.assertIsNone(result['pass'])

    def test_equal_time_normal_cannot_overwrite_special_or_unknown_state(self):
        for passed in (False,None):
            with self.subTest(passed=passed):
                current=status(AT,passed,'SPECIAL_OR_UNKNOWN')
                self.assertIsNot(self.view(current,status(),shadow(status()))['pass'],True)

    def test_untimed_unknown_recovers_only_from_check_after_its_observation(self):
        old_worker=shadow(status(AT-timedelta(seconds=1)))
        first=latest_evaluation_watch({'market_status':{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'}},
                                     None,SYM,old_worker,AT,180)
        self.assertIsNone(first['market_status']['pass'])
        self.assertEqual(first['market_status']['unknown_observed_at'],AT.isoformat())
        self.assertNotIn('checked_at',first['market_status'],'unknown observation is not a verified status check')
        checked=AT+timedelta(seconds=1)
        second=latest_evaluation_watch(first,None,SYM,shadow(status(checked)),checked,180)
        self.assertEqual(second['market_status'],status(checked))

    def test_same_malformed_cache_does_not_permanently_block_new_worker_check(self):
        malformed={'pass':True,'checked_at':'bad'}
        radar=SimpleNamespace(status_cache={SYM:malformed})
        first=latest_evaluation_watch({'market_status':status(AT,False,'VI_ACTIVE')},radar,SYM,
                                     shadow(status(AT-timedelta(seconds=1))),AT,180)
        self.assertIsNone(first['market_status']['pass'])
        self.assertEqual(first['market_status']['checked_at'],AT.isoformat())
        checked=AT+timedelta(seconds=1)
        second=latest_evaluation_watch(first,radar,SYM,shadow(status(checked)),checked,180)
        self.assertEqual(second['market_status'],status(checked))


class ActualEntryStatusTests(unittest.TestCase):
    setUp=fixture.SubscriptionRefreshTests.setUp
    tick=fixture.SubscriptionRefreshTests.tick

    def test_actual_entry_uses_observation_worker_status_after_watch_promotion(self):
        fresh=status(self.clock)
        self.e.observation_worker=shadow(fresh)
        self.radar.status_cache={}
        self.watch['market_status']=status(self.clock-timedelta(seconds=181))
        before=deepcopy(self.watch)
        detector=Mock();detector.signal.return_value=None;detector.evaluation_gates.return_value={};detector.last_eval={}
        evaluate_actual_entry(self.e,detector,self.tick(),self.watch,self.radar,{},self.account)
        self.assertEqual(self.e.market_status[SYM],fresh)
        self.assertEqual(detector.signal.call_args.args[1]['market_status'],fresh)
        self.assertEqual(self.watch,before)
        self.assertEqual(self.broker.orders,[])

    def test_actual_engine_blocks_refreshed_worker_unknown_special_and_expired_state(self):
        detector=Mock();detector.signal.return_value=None;detector.evaluation_gates.return_value={};detector.last_eval={}
        self.radar.status_cache={}
        for st,reason in ((status(self.clock,False,'VI_ACTIVE'),'VI_ACTIVE'),
                          (status(self.clock,None,'MARKET_STATUS_UNKNOWN'),'MARKET_STATUS_UNKNOWN'),
                          (status(self.clock+timedelta(microseconds=1)),'MARKET_STATUS_STALE')):
            with self.subTest(reason=reason):
                self.e.observation_worker=shadow(st)
                self.watch['market_status']=status(self.clock-timedelta(seconds=181))
                evaluate_actual_entry(self.e,detector,self.tick(),self.watch,self.radar,{},self.account)
                self.assertEqual(self.e.new_entry_reason(SYM),reason)
                self.assertEqual(self.broker.orders,[])

    def test_actual_entry_recovers_unknown_on_following_verified_worker_cycle(self):
        detector=Mock();detector.signal.return_value=None;detector.evaluation_gates.return_value={};detector.last_eval={}
        self.radar.status_cache={}
        self.watch['market_status']={'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'}
        self.e.observation_worker=shadow(status(self.clock-timedelta(seconds=1)))
        before=deepcopy(self.watch)
        first_at=self.clock
        evaluate_actual_entry(self.e,detector,self.tick(),self.watch,self.radar,{},self.account)
        self.assertIsNone(self.e.market_status[SYM]['pass'])
        self.assertEqual(self.e.market_status[SYM]['unknown_observed_at'],first_at.isoformat())
        self.clock+=timedelta(seconds=1)
        self.e.observation_worker.metadata[SYM]['status']=status(self.clock)
        evaluate_actual_entry(self.e,detector,self.tick(),self.watch,self.radar,{},self.account)
        self.assertEqual(self.e.market_status[SYM],status(self.clock))
        self.assertEqual(self.watch,before)
        self.assertEqual(self.broker.orders,[])


if __name__=='__main__':unittest.main()
