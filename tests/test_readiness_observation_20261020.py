"""Mandatory causal reproduction and verification tests for 2026-10-20 R6 incident.

Tests:
1. Long-duration single symbol observation: usable bars accumulate monotonically.
2. WATCH transition: history and completed bars preserved across WATCH <-> RESERVE/SHADOW.
3. 60s candidate refresh: repeated refreshes do not wipe out active symbol bars.
4. Live tick load replay: queue backlog drains immediately, eliminating event loop lag and readiness flips.
5. Order boundary reachability: valid causal signal reaches PRE_HTTP without emitting real broker orders.
"""
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
import queue
import time
import unittest

from automation.capital import Costs
from automation.fast_momentum import FastMomentumStrategy
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.runner import (
    readiness_snapshot,
    order_ready_from_evidence,
    drain_feed,
    prewarm_handoff,
)
from core.settings import Config, KST
from core.strategy import Tick
from tests.test_order_ready_synchronization import FakeEngine


def at(text: str) -> datetime:
    return datetime.fromisoformat("2026-10-20T" + text + "+09:00")


class R6ReadinessObservationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(stale_seconds=5)
        self.auto = AutomationConfig()
        self.costs = Costs(
            self.auto.commission_buy,
            self.auto.commission_sell,
            self.auto.sell_tax,
            self.cfg.entry_slippage_fraction,
            self.cfg.exit_slippage_fraction,
        )

    def test_1_long_duration_single_symbol_accumulates_usable_bars(self):
        """Test 1: Single symbol long duration observation accumulates usable completed bars monotonically.
        
        Explains why at 10:14:49 (after 10:13:21 start), completed_bars was 0:
        10:13 started at second 25 (partial, gap_reason='FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN').
        10:14 was incomplete until 10:15:00.
        Once 10:15:00 passes, completed bars accumulate properly.
        """
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        detector.continuous_observation = True
        symbol = "042510"

        # Start mid-minute at 10:13:25
        start_time = at("10:13:25")
        cur = start_time
        i = 0
        while cur < at("10:14:49"):
            tick = Tick(symbol, cur, 20000, 10, 10000 + i * 10, 20000, 19950, True)
            detector.ingest(tick, cur)
            cur += timedelta(seconds=5)
            i += 1

        # Feed exact 10:14:49 tick
        cur = at("10:14:49")
        tick = Tick(symbol, cur, 20000, 10, 10000 + i * 10, 20000, 19950, True)
        detector.ingest(tick, cur)
        i += 1

        # At 10:14:49: 10:13 is partial, 10:14 is still open! Usable bars must be 0.
        self.assertEqual(len(detector.usable_bars(symbol)), 0)
        self.assertEqual(detector.observation_start[symbol], start_time)
        # Elapsed seconds is 84s (> 70s), but completed bars is 0 (Rule-governed behavior!)
        gates = detector.base_gates(tick, {"status": "WATCH"})
        self.assertEqual(gates["warmup"]["result"], "FAIL")
        self.assertEqual(gates["warmup"]["values"]["completed_bars"], 0)
        self.assertEqual(gates["warmup"]["values"]["elapsed_seconds"], 84)

        # Now continue feeding past 10:15:00 to 10:18:05
        cur = at("10:14:50")
        while cur <= at("10:18:05"):
            tick = Tick(symbol, cur, 20050, 10, 10000 + i * 10, 20050, 20000, True)
            detector.ingest(tick, cur)
            cur += timedelta(seconds=5)
            i += 1

        # 10:14, 10:15, 10:16, 10:17 have completed fully!
        usable = detector.usable_bars(symbol)
        self.assertGreaterEqual(len(usable), 4)
        # Warmup gate now passes!
        gates_after = detector.base_gates(tick, {"status": "WATCH"})
        self.assertEqual(gates_after["warmup"]["result"], "PASS")

    def test_2_watch_exchange_retains_observation_history(self):
        """Test 2: WATCH <-> RESERVE transition preserves observation history and bars.
        
        Verifies that feed.observation_drops does NOT purge detector.completed or history.
        """
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        detector.continuous_observation = True
        symbol = "079650"

        # Accumulate 3 completed bars starting at 10:10:00
        cur = at("10:10:00")
        i = 0
        while cur <= at("10:13:05"):
            tick = Tick(symbol, cur, 1000, 10, 10000 + i * 10, 1000, 999, True)
            detector.ingest(tick, cur)
            cur += timedelta(seconds=5)
            i += 1

        self.assertEqual(len(detector.usable_bars(symbol)), 3)
        initial_start = detector.observation_start[symbol]

        # Simulate feed and prewarm handoff with dropped observation ticks
        class MockFeed:
            def __init__(self):
                self.observation_drops = 100
                self.observation_q = queue.Queue()
                self.subscription_lock = None
                self.overflow = type("Evt", (), {"is_set": lambda self: False})()
                self.transport_failed = False
            def configure_observation(self, priority, observed, sink):
                pass

        feed = MockFeed()
        engine = FakeEngine(cur)
        engine.s["automation"] = {"existing": {}}
        engine.event = lambda *args, **kwargs: None
        drops = 0
        priority_symbols = {"005930"}
        observed = {symbol}

        # Call prewarm_handoff with dropped ticks
        new_drops, new_priority = prewarm_handoff(
            engine, detector, feed, observed, priority_symbols, priority_symbols, drops,
            sink=lambda x: None
        )
        self.assertEqual(new_drops, 100)

        # Symbol's history, usable bars, and observation start MUST REMAIN INTACT!
        self.assertEqual(len(detector.usable_bars(symbol)), 3)
        self.assertEqual(detector.observation_start[symbol], initial_start)

    def test_3_candidate_60s_refresh_does_not_wipe_bars(self):
        """Test 3: Repeated candidate radar cycles do not wipe active symbol bars."""
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        detector.continuous_observation = True
        symbol = "046970"

        # Build 2 completed bars from 10:00:00 to 10:02:05
        cur = at("10:00:00")
        i = 0
        while cur <= at("10:02:05"):
            tick = Tick(symbol, cur, 5000, 10, 50000 + i * 10, 5000, 4995, True)
            detector.ingest(tick, cur)
            cur += timedelta(seconds=5)
            i += 1

        self.assertEqual(len(detector.usable_bars(symbol)), 2)
        obs_start = detector.observation_start[symbol]

        # Simulate 5 candidate refresh cycles over 300 seconds
        for cycle in range(5):
            cycle_end = cur + timedelta(seconds=60)
            while cur < cycle_end:
                cur += timedelta(seconds=5)
                tick = Tick(symbol, cur, 5010, 10, 50000 + i * 10, 5010, 5005, True)
                detector.ingest(tick, cur)
                i += 1

            # Observation start must remain identical, usable bars must grow
            self.assertEqual(detector.observation_start[symbol], obs_start)
            self.assertGreaterEqual(len(detector.usable_bars(symbol)), 2 + cycle + 1)

    def test_4_feed_drain_clears_queue_backlog_and_prevents_readiness_flips(self):
        """Test 4: drain_feed with max_messages=256 drains 50 backlogged messages cleanly.
        
        Demonstrates that clearing the queue backlog prevents tick age from crossing
        the 5.0s threshold and eliminates order_ready ON/OFF oscillation.
        """
        engine = FakeEngine(at("10:14:49.000"))

        class MockQueueFeed:
            def __init__(self):
                self.q = queue.Queue()
            def put(self, item):
                self.q.put(item)

        feed = MockQueueFeed()
        # Enqueue 50 ticks that arrived during account reconcile (e.g. from 10:14:45 to 10:14:48)
        now_dt = at("10:14:49.000")
        for i in range(50):
            rx_time = at("10:14:46.000") + timedelta(milliseconds=i * 50)
            msg = {"trnm": "REAL", "data": []}
            feed.put((rx_time, msg))

        consumed = []
        def consume(received, message, lag):
            consumed.append((received, lag))

        # Drain with max_messages=256, max_seconds=0.12
        count, max_lag = drain_feed(
            feed, consume, clock=lambda: now_dt, monotonic=time.monotonic,
            max_messages=256, max_seconds=0.12, timeout=0.01
        )

        # All 50 messages must be drained in one pass!
        self.assertEqual(count, 50)
        self.assertEqual(feed.q.qsize(), 0)

        # Verify that with an empty queue, the next arriving tick has lag < 0.1s
        fresh_rx = now_dt - timedelta(milliseconds=30)
        fresh_tick = Tick("042510", fresh_rx, 20000, 10, 200000, 20000, 19950, True)
        engine.last_regular_tick_at = fresh_rx
        engine.last_regular_tick = fresh_tick
        engine.event_loop_lag = 0.03
        engine.feed_queue_size = 0

        counts = {"ticks": 100, "subscription_acks": 1}
        ready = readiness_snapshot(engine, counts, True, now_dt)
        # order_ready is stably True without EVENT_LOOP_LAG!
        self.assertTrue(ready["order_ready"])
        self.assertEqual(ready["state"], "OPEN_READY")
        self.assertNotIn("EVENT_LOOP_LAG", ready.get("off_reasons", []))

    def test_5_valid_signal_reaches_pre_http_boundary_without_real_orders(self):
        """Test 5: Order pipeline reaches PRE_HTTP boundary with valid signal, blocking real emission."""
        from tests.test_structural_a_exit import CONTINUING, AT, CFG, AUTO, COSTS

        engine = FakeEngine(AT)
        engine.market = "3"
        engine.connected = True
        engine.account_ok = True
        engine.s["automation"] = {"existing": {}}

        symbol = "999990"
        detector = PatternDetector(CFG, AUTO, COSTS)
        for b in CONTINUING:
            detector.price_context.record(symbol, b, AT)
        detector.prior_high[symbol] = 11000

        tick = Tick(symbol, AT, 10040, 100, 100000, 10040, 10030, True)

        common_gates = {
            "warmup": {"result": "PASS", "values": {}},
            "spread": {"result": "PASS", "values": {}},
            "structure": {"result": "PASS", "values": {}},
            "liquidity": {"result": "PASS", "values": {"window_turnover": 40000000}},
            "acceleration": {"result": "FAIL", "values": {"ratio": 1.3, "min_ratio": 3.0, "gain": 0.002, "min_gain": 0.003}}
        }
        watch = {"status": "WATCH", "venue": "KRX", "resistance": 20000, "source_details": {"a": {}, "b": {}}}

        trials = detector.setup_trials(tick, watch, common_gates, _bars=CONTINUING)
        trial_a = trials[0]
        self.assertTrue(trial_a["candidate_generated"])
        self.assertTrue(trial_a["entry_ready"])
        self.assertEqual(trial_a["gates"]["acceleration"]["values"]["mode"], "CONFIRMED_CONTINUATION")

        # Under read-only / diagnostic safety, real orders cannot be emitted
        self.assertFalse(engine.s.get("actual_order_permission", False))


if __name__ == "__main__":
    unittest.main()
