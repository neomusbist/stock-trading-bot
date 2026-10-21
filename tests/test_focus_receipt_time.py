import unittest
from datetime import datetime,timedelta
from types import SimpleNamespace
from core.settings import Config,KST
from core.strategy import Tick
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.runner import focus_readiness
from automation.focus_selection import select_focus


class FocusReceiptTimeTests(unittest.TestCase):
    def readiness(self, lead):
        now=datetime(2026,10,21,13,39,45,850000,tzinfo=KST)
        d=PatternDetector(Config(),AutomationConfig(),{})
        tick=Tick('138360',now+timedelta(seconds=lead),1642,100,1000,1642,1641,
                  received_at=now.isoformat())
        d.last[tick.symbol]=tick
        d.last_eval[tick.symbol]={'time':tick.time,'gates':{'market_status':{'result':'PASS'},'warmup':{'result':'FAIL'}},'trials':[]}
        d.fast_strategy=SimpleNamespace(active_signals={tick.symbol:SimpleNamespace(created_at=now,ttl_seconds=2)})
        return now,focus_readiness(d,{},now)

    def selection(self, now, readiness):
        current={'000001':dict(code='000001',status='WATCH',source_count=5,discovery_score=100)}
        candidate=dict(code='138360',status='ELIGIBLE',source_count=4,discovery_score=40)
        return select_focus(current,[candidate],readiness,max_watchlist=1,clock_iso=now.isoformat())

    def test_received_bounded_clock_lead_fast_signal_can_compete_for_watch(self):
        now,r=self.readiness(.15)
        self.assertEqual(r['138360']['tier'],0)
        self.assertIn('138360',self.selection(now,r).promoted)

    def test_unreceived_or_excessive_future_time_cannot_gain_priority(self):
        now,r=self.readiness(2)
        self.assertNotIn('138360',r)
        self.assertEqual(self.selection(now,r).promoted,[])

    def test_regular_past_tick_has_same_existing_priority(self):
        now,r=self.readiness(-.15)
        self.assertIn('138360',self.selection(now,r).promoted)

    def test_rejected_compression_is_not_misreported_as_watch_capacity(self):
        now,r=self.readiness(-.15)
        row=dict(code='138360',status='REJECT',source_count=4)
        s=select_focus({},[row],r,max_watchlist=12,clock_iso=now.isoformat())
        self.assertEqual(s.next_watch,[])
        self.assertEqual(s.blocked_reasons['138360'],'CANDIDATE_STATUS_REJECT')


if __name__=='__main__':unittest.main()
