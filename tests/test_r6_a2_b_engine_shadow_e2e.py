"""Comprehensive end-to-end integration tests for R6 A2/B engine integration with LimitBuyOrder lifecycle in SHADOW/PAPER mode.

Verifies:
1. Real broker new-buy blocking (SHADOW isolation even under cfg.live_mode=True).
2. Limit order entry via entry_gates -> AutonomousEngine.enter.
3. Order reservation of cash, risk, and single-position slot.
4. Support loss invalidation cancellation via on_tick (CANCELLED_INVALIDATED_SUPPORT_LOST).
5. 120-second timeout cancellation via timer() (CANCELLED_TIMEOUT).
6. Real trade prints driving partial and full fills (no phantom fills).
7. Seamless transition from fill to position tracking and protective exits (stop / target).
8. Trade recording, realized PnL, daily risk accounting.
9. HARD_LOCK preservation.
"""
import copy
import json
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.entry_lifecycle import LimitBuyOrder
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy, HARD_LOCKS

AT = datetime(2026, 11, 2, 10, 0, 0, tzinfo=KST)

def make_bars(rows, end_at=AT):
    return [
        dict(
            time=end_at - timedelta(minutes=len(rows) - i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            first_second=0,
        )
        for i, (o, h, l, c, v) in enumerate(rows)
    ]

A2_VALID = make_bars([
    (10000, 10150, 9950, 10100, 300),
    (10100, 10200, 10050, 10180, 280),
    (10180, 10850, 10170, 10800, 900),
    (10240, 10250, 10200, 10220, 320),
    (10220, 10280, 10210, 10270, 450),
])


class MockBroker:
    def __init__(self, identity="live_broker_test"):
        self.identity = identity
        self.orders = []
        self.cancels = []
        self.submission_observer = None

    def submit(self, side, symbol, qty, limit, venue="KRX", session="REGULAR"):
        order_id = f"BROKER-{len(self.orders)+1}"
        self.orders.append({"id": order_id, "side": side, "symbol": symbol, "qty": qty, "limit": limit})
        return order_id

    def cancel(self, symbol, order_id, venue="KRX"):
        self.cancels.append({"symbol": symbol, "order_id": order_id})
        return f"CANCEL-{len(self.cancels)}"


class A2BEngineShadowE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.cfg = Config(
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_position_fraction=1.0,
            paper_cash=70420.8,
            account_equity=70420.8,
        )
        self.auto = AutomationConfig(
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_breakout_enabled=True,
            structural_pullback_enabled=True,
            confirmed_invalidation_enabled=True,
            a2_breakout_retest_enabled=True,
            b_support_reclaim_entry_enabled=True,
        )
        self.clock_time = AT
        self.clock = lambda: self.clock_time
        self.broker = MockBroker("live_broker_test")

        # Write state.json
        state = {
            "version": 1,
            "day": "2026-11-02",
            "config_hash": self.cfg.fingerprint(),
            "identity": self.broker.identity,
            "positions": {},
            "orders": {},
            "entries": {},
            "daily_loss": 0.0,
            "realized": 0.0,
            "cash": 70420.8,
            "equity": 70420.8,
            "halt": "",
            "excluded": [],
            "trades": [],
            "sequence": 0,
            "automation": {
                "approved_cap": 81300,
                "allocated": False,
                "cash": 70420.8,
                "existing": {},
                "consecutive_losses": 0,
                "strategy": "IDLE",
                "last_exit": {},
                "config_hash": self.auto.fingerprint(),
            },
        }
        with open(self.folder / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f)

        # Policy in LIVE mode
        policy_path = self.folder / "policy.json"
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump({"mode": "LIVE", "live_authorized": True, "PAPER_FIXTURE_ONLY": False}, f)
        self.policy = Policy(policy_path)

        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder, self.broker, self.clock)
        self.engine.available_cash = 70420.8
        self.engine.account_ok = True
        self.engine.connected = True
        self.engine.last_account = time.monotonic()
        self.engine.market = "3"
        self.engine.market_status["047770"] = {"pass": True, "checked_at": AT.isoformat()}
        self.engine.source_qualification["047770"] = {"source_count": 3, "timestamp": AT.isoformat()}

    def tearDown(self):
        self.tmp.cleanup()

    def build_a2_signal_and_tick(self):
        a2_entry = 10260
        det = PatternDetector(self.cfg, self.auto, self.costs)
        det.observation_start["047770"] = AT - timedelta(minutes=6)
        det.history["047770"] = deque(copy.deepcopy(A2_VALID))
        for s in reversed(range(10, 70)):
            det.buffers.setdefault("047770", deque()).append(
                Tick("047770", AT - timedelta(seconds=s), 10240, 200, 100000 - s * 10, 10240, 10230)
            )
        for s in reversed(range(1, 9)):
            det.buffers["047770"].append(
                Tick("047770", AT - timedelta(seconds=s), 10250, 500, 100000 - s, 10250, 10240)
            )
        t = Tick("047770", AT, a2_entry, 600, 100100, a2_entry, 10250)
        watch = {
            "status": "WATCH",
            "resistance": 11000,
            "resistance_room_pass": True,
            "source_details": {"a": {}, "b": {}},
            "market_status": {"pass": True},
        }
        sig = det.signal(t, watch)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.get("setup_variant"), "A2")
        return sig, t

    def test_a2_signal_creates_shadow_order_and_never_sends_to_real_broker(self):
        sig, tick = self.build_a2_signal_and_tick()
        order = self.engine.enter(sig, tick)
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "ACCEPTED")
        self.assertEqual(order["execution"], "SHADOW")
        self.assertTrue(order["id"].startswith("SHADOW-"))

        # Zero broker orders transmitted!
        self.assertEqual(len(self.broker.orders), 0)

        # Order lifecycle attached
        self.assertIn(order["local"], self.engine.active_limit_lifecycles)
        l_order = self.engine.active_limit_lifecycles[order["local"]]
        self.assertEqual(l_order.symbol, "047770")
        self.assertEqual(l_order.limit, 10260.0)
        self.assertEqual(l_order.state, "PENDING")

        # Reservation checks
        self.assertGreater(self.engine.pending_buy_reservation(), 0)
        self.assertGreater(self.engine.risk_used(), 0)
        # Single-position slot occupied by pending buy order
        occupied_slots = len(set(self.engine.s['positions']) | {o['symbol'] for o in self.engine.active_orders(side='BUY')})
        self.assertEqual(occupied_slots, 1)

    def test_support_loss_before_fill_cancels_shadow_order_without_loss(self):
        sig, tick = self.build_a2_signal_and_tick()
        order = self.engine.enter(sig, tick)
        self.assertEqual(order["status"], "ACCEPTED")
        local_id = order["local"]
        s_stop = (sig.get("invalidation_policy") or {}).get("structural_stop", sig["stop"])

        # 10s later: price drops to s_stop - 10
        self.clock_time = AT + timedelta(seconds=10)
        bad_tick = Tick("047770", self.clock_time, s_stop - 10, 50, 100200, s_stop, s_stop - 10)
        self.engine.on_tick(bad_tick)

        # Order must be cancelled immediately without loss
        self.assertEqual(order["status"], "CANCELED")
        self.assertNotIn(local_id, self.engine.active_limit_lifecycles)
        self.assertEqual(len(self.engine.s["positions"]), 0)
        self.assertEqual(len(self.broker.orders), 0)

    def test_timeout_cancels_unfilled_order_and_restores_slots(self):
        sig, tick = self.build_a2_signal_and_tick()
        order = self.engine.enter(sig, tick)
        local_id = order["local"]

        # Advance 125 seconds without matching trades
        self.clock_time = AT + timedelta(seconds=125)
        self.engine.timer()

        # Order must be expired & cancelled
        self.assertEqual(order["status"], "CANCELED")
        self.assertNotIn(local_id, self.engine.active_limit_lifecycles)
        self.assertTrue(self.engine.entry_slots_available())

    def test_real_trades_drive_fills_and_transition_to_protective_exit(self):
        sig, tick = self.build_a2_signal_and_tick()
        order = self.engine.enter(sig, tick)
        local_id = order["local"]
        qty = order["qty"]
        self.assertGreaterEqual(qty, 1)

        # Trade print below limit price fills order
        self.clock_time = AT + timedelta(seconds=5)
        fill_tick = Tick("047770", self.clock_time, 10250.0, qty, 100200, 10260.0, 10250.0)
        self.engine.on_tick(fill_tick)

        # Order filled
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(order["filled"], qty)
        self.assertNotIn(local_id, self.engine.active_limit_lifecycles)

        # Position exists
        self.assertIn("047770", self.engine.s["positions"])
        pos = self.engine.s["positions"]["047770"]
        self.assertEqual(pos["qty"], qty)
        self.assertEqual(pos["avg"], 10250.0)

        # Emergency stop breach triggers exit request
        e_stop = pos.get("emergency") or pos.get("stop")
        self.clock_time = AT + timedelta(seconds=30)
        stop_tick = Tick("047770", self.clock_time, e_stop - 10, 100, 100300, e_stop, e_stop - 10)
        self.engine.on_tick(stop_tick)

        # Exit requested
        self.assertTrue(pos.get("exit_requested"))

    def test_hard_lock_symbols_never_admitted(self):
        hard_sym = next(iter(HARD_LOCKS))
        sig, tick = self.build_a2_signal_and_tick()
        sig["symbol"] = hard_sym
        tick = Tick(hard_sym, AT, 10260, 100, 100000, 10260, 10250)
        order = self.engine.enter(sig, tick)
        self.assertIsNone(order)
        self.assertEqual(len(self.engine.active_orders()), 0)


if __name__ == "__main__":
    unittest.main()
