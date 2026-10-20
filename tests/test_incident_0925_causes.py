"""Causal reproductions of the incident's clock, book and restart evidence."""
from dataclasses import replace
from datetime import datetime, timedelta
import unittest

from automation.capital import Costs
from automation.fast_momentum import FastMomentumStrategy
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.runner import readiness_snapshot
from core.settings import Config, KST
from core.strategy import Tick
from tests.test_order_ready_synchronization import FakeEngine


def at(text): return datetime.fromisoformat('2026-10-20T'+text+'+09:00')


class IncidentCauseTests(unittest.TestCase):
    def test_recorded_092509_flip_follows_tick_age_not_order_permission(self):
        counts = {'ticks': 100, 'subscription_acks': 1}
        for stamp, tick_at, lag, expected in [
            ('09:25:09.006128','09:25:04',4.591413,False),
            ('09:25:09.268307','09:25:05',4.021429,True),
            ('09:25:11.198862','09:25:06',4.323590,False),
            ('09:25:11.653000','09:25:07',4.381176,True)]:
            with self.subTest(stamp=stamp):
                engine=FakeEngine(at(stamp));engine.last_regular_tick_at=at(tick_at)
                engine.event_loop_lag=lag;engine.feed_queue_size=74
                ready=readiness_snapshot(engine,counts,True,at(stamp))
                self.assertEqual(ready['order_ready'],expected)
                if not expected:self.assertIn('EVENT_LOOP_LAG',ready['off_reasons'])
                self.assertNotIn('actual_order_permission',ready)

    def test_existing_book_timestamp_can_age_out_while_trade_is_fresh(self):
        cfg=Config(stale_seconds=5);auto=AutomationConfig()
        costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        fast=FastMomentumStrategy(cfg,auto,costs)
        tick=Tick('117730',at('09:24:57'),13170,10,1000,13170,13160,True,
                  depth_available=True,book_time=at('09:24:56'),ask_qty=100,bid_qty=100)
        self.assertIsNone(fast._quote(tick,at('09:24:57.139662')))
        self.assertEqual(fast._quote(tick,at('09:25:01.128432')),'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')
        self.assertEqual(tick.book_time,at('09:24:56'))
        self.assertEqual(fast._quote(replace(tick,book_time=None),at('09:24:57.139662')),'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')

    def test_restart_keeps_partial_first_minute_out_of_usable_signal_bars(self):
        cfg=Config();auto=AutomationConfig()
        detector=PatternDetector(cfg,auto,Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction))
        detector.continuous_observation=True
        # A persistent 08:58 discovery timestamp is metadata, not received
        # regular-session signal history in this fresh 09:23 process.
        start=at('09:23:06')
        for seconds in range(120):
            stamp=start+timedelta(seconds=seconds)
            tick=Tick('396300',stamp,10920,10,1000+seconds*10,10920,10910,True)
            self.assertTrue(detector.ingest(tick,stamp))
        self.assertEqual(detector.observation_start['396300'],start)
        self.assertEqual(len(detector.usable_bars('396300')),1)
        self.assertEqual(detector.history['396300'][0]['gap_reason'],'FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN')
        self.assertEqual(detector.base_gates(tick,{'status':'WATCH'})['warmup']['result'],'FAIL')


if __name__=='__main__':unittest.main()
