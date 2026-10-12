"""Fresh metadata publication must never bypass the existing order freshness guard."""
import copy,unittest
from datetime import timedelta
from unittest.mock import Mock,patch
from automation.runner import latest_evaluation_watch,evaluate_actual_entry
import test_after_runtime_audit as radar_fixture
import test_subscription_refresh as entry_fixture

class RadarRefreshTests(unittest.TestCase):
    setUp=radar_fixture.RuntimeAuditRegressionTests.setUp
    item=radar_fixture.RuntimeAuditRegressionTests.item
    cycle=radar_fixture.RuntimeAuditRegressionTests.cycle
    def test_actual_watch_refreshes_one_cycle_before_existing_expiry(self):
        sym=radar_fixture.OLD[0];stamp=radar_fixture.STAMP
        self.worker.status_priority_symbols=frozenset({sym})
        self.worker.status_cache[sym]={'pass':True,'checked_at':(stamp-timedelta(seconds=121)).isoformat()}
        _,final=self.cycle([self.item(sym)],old=[sym])
        self.rest.stock_status.assert_called_once_with(sym)
        self.assertEqual(final['rows'][0]['market_status']['checked_at'],stamp.isoformat())
        self.assertEqual(self.worker.a.scan_seconds*3,180)
    def test_expired_status_cannot_be_used_when_refresh_budget_is_exhausted(self):
        sym=radar_fixture.OLD[0]
        self.worker.status_priority_symbols=frozenset({sym})
        self.worker.status_cache[sym]={'pass':True,'checked_at':(radar_fixture.STAMP-timedelta(seconds=181)).isoformat()}
        self.worker.status_fetched=self.worker.a.max_scan_charts
        with patch('automation.radar.now',return_value=radar_fixture.STAMP):view=self.worker.evaluate(self.item(sym))
        self.assertEqual(view,{'status':'UNKNOWN','reason':'STATUS_SCAN_LIMIT'})
        self.rest.stock_status.assert_not_called()

class PublicationPathTests(unittest.TestCase):
    setUp=entry_fixture.SubscriptionRefreshTests.setUp
    tick=entry_fixture.SubscriptionRefreshTests.tick
    def test_fast_entry_uses_fresh_cache_without_mutating_radar_row_or_network(self):
        self.watch['market_status']={'pass':True,'checked_at':(self.clock-timedelta(seconds=181)).isoformat()}
        before=copy.deepcopy(self.watch)
        fresh={'pass':True,'checked_at':self.clock.isoformat(),'reason_code':'NORMAL'}
        self.radar.status_cache={entry_fixture.SYM:fresh}
        detector=Mock();detector.signal.return_value=None;detector.evaluation_gates.return_value={};detector.last_eval={}
        evaluate_actual_entry(self.e,detector,self.tick(),self.watch,self.radar,{},self.account)
        self.assertEqual(self.e.market_status[entry_fixture.SYM],fresh)
        self.assertEqual(detector.signal.call_args.args[1]['market_status'],fresh)
        self.assertEqual(self.watch,before);self.assertEqual(self.broker.orders,[])
        self.assertEqual(self.radar.method_calls,[])
    def test_fresh_special_state_overrides_older_normal_and_blocks_buy(self):
        fresh={'pass':False,'checked_at':self.clock.isoformat(),'reason_code':'TRADING_HALTED'}
        watch={**self.watch,'market_status':{'pass':True,'checked_at':(self.clock-timedelta(seconds=10)).isoformat()}}
        self.radar.status_cache={entry_fixture.SYM:fresh}
        view=latest_evaluation_watch(watch,self.radar,entry_fixture.SYM)
        self.e.market_status[entry_fixture.SYM]=view['market_status']
        self.assertEqual(self.e.new_entry_reason(entry_fixture.SYM),'TRADING_HALTED')
        self.assertEqual(self.broker.orders,[])
    def test_stale_and_future_cached_status_do_not_extend_order_ttl(self):
        for seconds in (-181,2):
            fresh={'pass':True,'checked_at':(self.clock+timedelta(seconds=seconds)).isoformat()}
            self.radar.status_cache={entry_fixture.SYM:fresh}
            view=latest_evaluation_watch(self.watch,self.radar,entry_fixture.SYM)
            self.e.market_status[entry_fixture.SYM]=view['market_status']
            self.assertEqual(self.e.new_entry_reason(entry_fixture.SYM),'MARKET_STATUS_STALE')
        self.assertEqual(self.broker.orders,[])
    def test_older_cache_and_malformed_status_never_forge_fresh_permission(self):
        new={'pass':False,'checked_at':self.clock.isoformat(),'reason_code':'TRADING_HALTED'}
        watch={**self.watch,'market_status':new}
        self.radar.status_cache={entry_fixture.SYM:{'pass':True,'checked_at':(self.clock-timedelta(seconds=1)).isoformat()}}
        self.assertEqual(latest_evaluation_watch(watch,self.radar,entry_fixture.SYM)['market_status'],new)
        self.radar.status_cache={entry_fixture.SYM:{'pass':True,'checked_at':'bad'}}
        self.assertIsNone(latest_evaluation_watch(watch,self.radar,entry_fixture.SYM)['market_status']['pass'])

if __name__=='__main__':unittest.main()
