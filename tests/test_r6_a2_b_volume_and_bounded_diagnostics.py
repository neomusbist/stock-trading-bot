"""Regression and unit tests for R6 entry structure improvements:
1. Setup A2 (confirmed high breakout -> first pullback/retest -> support hold -> re-advance entry)
2. Setup B pullback vs recovery volume phase separation (positive and negative tests)
3. Actual account (70,847 KRW cash / 81,300 KRW cap / 2% risk / 4% daily risk) symbol selection & 0-share reasons
4. Bounded-capacity core decision diagnostic recorder under <32 GiB free space and <5 GiB safety stop.
"""
import copy
import json
import tempfile
import unittest
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.settings import Config, KST
from core.strategy import Tick
from core.util import floor_tick
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy, HARD_LOCKS
from automation.radar import DynamicRadar
from automation.structural_breakout import breakout_retest_setup, structural_breakout
from automation.trade_review_capture import TradeReviewCapture, BoundedCoreDecisionLog
from tests import test_audit_followup as fixture
from tests.test_structural_pullback import AT


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


# A2 fixture:
# - Bars 0..1: prior base with confirmed ceiling at 10,200 and range support at 10,000
# - Bar 2: breakout impulse bar breaking 10,200 -> high=10,360, close=10,320, high volume (900)
# - Bar 3: first pullback/retest after breakout -> pulls back from 10,360 to low=10,200 (holds broken ceiling),
#          high=10,250, close=10,220 (above 10,200)
# - Bar 4: completed recovery confirmation bar -> higher low=10,210, closes at 10,270 (> retest high 10,250)
A2_VALID = make_bars([
    (10000, 10150, 9950, 10100, 300),
    (10100, 10200, 10050, 10180, 280),
    (10180, 10850, 10170, 10800, 900),
    (10240, 10250, 10200, 10220, 320),
    (10220, 10280, 10210, 10270, 450),
])


# B fixture where pullback phase has LOW volume (120, 100 <= 800 * 0.75 = 600),
# but the completed recovery confirmation bar has HIGH buying volume (1,400),
# so unseparated mean(pull) = (120 + 100 + 1400)/3 = 540 or with stronger recovery (1,800 -> mean=673 > 600)
# would falsely fail PULLBACK_VOLUME_FAIL unless pullback and recovery phases are separated.
B_HIGH_RECOVERY_VOL = make_bars([
    (1000, 1005, 1000, 1004, 100),
    (1004, 1105, 1004, 1100, 400),
    (1065, 1066, 1040, 1045, 100),
    (1045, 1046, 1040, 1044, 80),
    (1044, 1050, 1041, 1049, 600),
])


class SetupA2AndModifiedBTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_breakout_enabled=True,
            structural_pullback_enabled=True,
            confirmed_invalidation_enabled=True,
            a_breakout_retest_entry_enabled=True,
            a2_breakout_retest_enabled=True,
            b_support_reclaim_entry_enabled=True,
        )

    def test_a2_secures_better_entry_price_than_chase_and_passes_net_rr_and_sizing(self):
        setup = breakout_retest_setup(A2_VALID, AT, self.cfg, auto=self.auto)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["pattern"], "A")
        self.assertEqual(setup.get("setup_variant"), "A2")
        self.assertEqual(setup["entry_basis"], "CONFIRMED_BREAKOUT_RETEST")
        self.assertEqual(setup["old_ceiling"], 10200)
        self.assertEqual(setup["support"], 10200)
        self.assertEqual(setup["breakout"], 10250)

        # Existing A would require chasing above the impulse high (10,360 -> 10,370).
        # A2 enters after the retest & recovery at 10,260 (110 KRW lower than 10,370!).
        impulse_high = max(b["high"] for b in A2_VALID)
        a2_entry = 10260
        self.assertLess(a2_entry, impulse_high)

        ok, proof = structural_breakout(
            A2_VALID, AT, setup, self.cfg, a2_entry, 10250, a2_entry, auto=self.auto, decision_bars=A2_VALID
        )
        self.assertTrue(ok, proof)
        self.assertLess(proof["emergency_stop"], proof["structural_stop"])
        self.assertLess(proof["structural_stop"], a2_entry)

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
        self.assertEqual(sig["pattern"], "A")
        self.assertEqual(sig.get("setup_variant"), "A2")
        self.assertEqual(sig["entry_basis"], "CONFIRMED_BREAKOUT_RETEST")
        self.assertEqual(sig["limit"], a2_entry)
        self.assertGreaterEqual(sig["entry_economics"]["net_rr"], 1.5)

    def test_a2_rejects_failed_breakout_and_unconfirmed_falling_retest(self):
        # 1) Failed breakout: retest bar closes below broken ceiling (10,200 -> 10,180)
        failed_breakout = copy.deepcopy(A2_VALID)
        failed_breakout[3]["close"] = 10180
        failed_breakout[3]["low"] = 10150
        self.assertIsNone(breakout_retest_setup(failed_breakout, AT, self.cfg, auto=self.auto))

        # 2) Retest without recovery confirmation (last bar is still falling / making new low)
        still_falling = copy.deepcopy(A2_VALID[:-1])
        still_falling = make_bars([(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in still_falling])
        self.assertIsNone(breakout_retest_setup(still_falling, AT, self.cfg, auto=self.auto))

        # 3) Nearby overhead resistance caps target and rejects when net RR < 1.5
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
        t = Tick("047770", AT, 10260, 600, 100100, 10260, 10250)
        watch_capped = {
            "status": "WATCH",
            "resistance": 10380,  # Known nearby overhead resistance caps target to 10,380
            "resistance_room_pass": True,
            "source_details": {"a": {}, "b": {}},
            "market_status": {"pass": True},
        }
        self.assertIsNone(det.signal(t, watch_capped))

    def test_b_separates_pullback_and_recovery_volume_positive_and_negative(self):
        det = PatternDetector(self.cfg, self.auto, self.costs)

        # Positive case: Low pullback volume (100, 80) + High bullish recovery volume (600)
        setup, reason, ev = det.pullback_analysis(B_HIGH_RECOVERY_VOL)
        self.assertIsNotNone(setup, f"Expected valid setup, got reason={reason}, ev={ev}")
        self.assertIsNone(reason)
        self.assertEqual(ev["volume_basis"], "SEPARATED_PULLBACK_AND_RECOVERY")
        self.assertAlmostEqual(ev["pullback_phase_volume"], 90.0)
        self.assertAlmostEqual(ev["recovery_phase_volume"], 600.0)
        self.assertGreater(ev["all_post_peak_volume"], ev["impulse_volume"] * self.auto.pullback_volume_ratio)
        self.assertLessEqual(ev["pullback_phase_volume"], ev["impulse_volume"] * self.auto.pullback_volume_ratio)

        # Negative case 1: High volume during the pullback phase down to low -> MUST fail PULLBACK_VOLUME_FAIL
        high_pull_vol = copy.deepcopy(B_HIGH_RECOVERY_VOL)
        high_pull_vol[2]["volume"] = 350
        high_pull_vol[3]["volume"] = 320
        high_pull_vol[4]["volume"] = 80
        setup_fail1, reason_fail1, _ = det.pullback_analysis(high_pull_vol)
        self.assertIsNone(setup_fail1)
        self.assertEqual(reason_fail1, "PULLBACK_VOLUME_FAIL")

        # Negative case 2: High volume on a BEARISH post-low bar -> MUST fail PULLBACK_VOLUME_FAIL
        bearish_post_low = copy.deepcopy(B_HIGH_RECOVERY_VOL)
        bearish_post_low[4]["open"] = 1049
        bearish_post_low[4]["close"] = 1042  # Bearish candle on high volume
        setup_fail2, reason_fail2, ev_fail2 = det.pullback_analysis(bearish_post_low)
        self.assertIsNone(setup_fail2)
        self.assertEqual(reason_fail2, "PULLBACK_VOLUME_FAIL")
        self.assertEqual(ev_fail2["volume_basis"], "UNSEPARATED_POST_PEAK")


class ActualAccountFeasibilityAndBoundedDiagnosticsTests(unittest.TestCase):
    setUp = fixture.AuditFixture.setUp
    build = fixture.SetupReachabilityTests.build

    def test_actual_account_70847_cash_and_81300_cap_symbol_selection_and_zero_qty_reasons(self):
        self.clock[0] = AT
        self.e.s["day"] = AT.date().isoformat()
        self.e.c = replace(self.e.c, risk_fraction=0.02, max_daily_loss_fraction=0.04, max_positions=1, max_position_fraction=1.0)
        self.cfg = self.e.c
        self.e.a = replace(
            self.e.a,
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_pullback_enabled=True,
            structural_breakout_enabled=True,
            confirmed_invalidation_enabled=True,
        )
        self.e.available_cash = 70847.0
        self.e.s["cash"] = 70847.0
        self.e.s["automation"]["approved_cap"] = 81300
        self.e.s["automation"]["cash"] = 70847.0

        cap_ctx = self.e.capital_context()
        self.assertAlmostEqual(cap_ctx["actual_orderable_cash"], 70847.0)
        self.assertEqual(cap_ctx["approved_cap"], 81300)
        self.assertAlmostEqual(cap_ctx["allowed_risk_krw"], 70847.0 * 0.02, places=2)

        radar = DynamicRadar(self.cfg, self.e.a, self.policy)
        radar.capital_context = {**cap_ctx, "enforce_risk_affordability": True}

        # 1) Stock priced above 70,847 KRW -> ONE_SHARE_UNAFFORDABLE
        expensive = {"code": "000660", "name": "하이닉스", "reference_price": 75000}
        self.assertFalse(radar.affordable(expensive))
        self.assertEqual(expensive["affordability"]["reason_code"], "ONE_SHARE_UNAFFORDABLE")

        sig_unafford = {
            "symbol": "000660",
            "pattern": "A",
            "limit": 75000,
            "stop": 73500,
            "target": 79000,
            "invalidation_policy": {"enabled": True, "structural_stop": 74000, "emergency_stop": 73500},
        }
        q_unafford = self.e.quantity_details(sig_unafford, self.e.risk_equity(), 70847.0, 2833.88)
        self.assertEqual(q_unafford["quantity"], 0)
        self.assertEqual(q_unafford["reason_code"], "ONE_SHARE_UNAFFORDABLE")
        self.assertFalse(q_unafford["one_share_cash_affordable"])
        self.assertIn("ONE_SHARE_UNAFFORDABLE", q_unafford["zero_quantity_detail"])

        # 2) Stock cash-affordable (43,550 <= 70,847), but 1-share emergency risk > 1,416.94 KRW -> RISK_BUDGET_ZERO_QTY
        sig_risk_zero = {
            "symbol": "014830",
            "pattern": "A",
            "limit": 43550,
            "stop": 42150,
            "target": 46500,
            "invalidation_policy": {"enabled": True, "structural_stop": 42650, "emergency_stop": 42150},
        }
        q_risk_zero = self.e.quantity_details(sig_risk_zero, self.e.risk_equity(), 70847.0, 2833.88)
        self.assertEqual(q_risk_zero["quantity"], 0)
        self.assertEqual(q_risk_zero["capital_quantity"], 1)
        self.assertEqual(q_risk_zero["risk_quantity"], 0)
        self.assertEqual(q_risk_zero["reason_code"], "RISK_BUDGET_ZERO_QTY")
        self.assertTrue(q_risk_zero["one_share_cash_affordable"])
        self.assertFalse(q_risk_zero["one_share_risk_affordable"])
        self.assertIn("RISK_BUDGET_ZERO_QTY", q_risk_zero["zero_quantity_detail"])

        # 3) Stock both cash-affordable and 1-share risk <= 1,416.94 KRW (e.g. A2 at 10,260) -> QUANTITY_AVAILABLE
        sig_feasible = {
            "symbol": "047770",
            "pattern": "A",
            "setup_variant": "A2",
            "limit": 10260,
            "stop": 10070,
            "target": 10720,
            "invalidation_policy": {"enabled": True, "structural_stop": 10180, "emergency_stop": 10070},
        }
        q_feasible = self.e.quantity_details(sig_feasible, self.e.risk_equity(), 70847.0, 2833.88)
        self.assertGreaterEqual(q_feasible["quantity"], 1)
        self.assertEqual(q_feasible["reason_code"], "QUANTITY_AVAILABLE")
        self.assertTrue(q_feasible["one_share_cash_affordable"])
        self.assertTrue(q_feasible["one_share_risk_affordable"])
        self.assertIsNone(q_feasible["zero_quantity_detail"])

        # 4) HARD_LOCK symbols are unconditionally blocked
        for locked_sym in HARD_LOCKS:
            self.assertEqual(self.e.new_entry_reason(locked_sym), "HARD_LOCK")

    def test_a2_and_modified_b_reach_order_boundary_and_protection_exit_on_70847_account(self):
        self.clock[0] = AT
        self.e.s["day"] = AT.date().isoformat()
        self.e.c = replace(self.e.c, risk_fraction=0.02, max_daily_loss_fraction=0.04, max_positions=1, max_position_fraction=1.0)
        self.cfg = self.e.c
        self.e.a = replace(
            self.e.a,
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_pullback_enabled=True,
            structural_breakout_enabled=True,
            confirmed_invalidation_enabled=True,
            a_breakout_retest_entry_enabled=True,
            a2_breakout_retest_enabled=True,
            b_support_reclaim_entry_enabled=True,
        )
        self.e.available_cash = 70847.0
        self.e.s["cash"] = 70847.0
        self.e.s["automation"]["approved_cap"] = 81300
        self.e.s["automation"]["cash"] = 70847.0

        # Test Modified B (with high-volume recovery bar) reaching fake broker order on 70,847 KRW account
        d, t, w = self.build("B")
        d.cfg = self.cfg
        d.a = self.e.a
        d.history["999990"] = deque(copy.deepcopy(B_HIGH_RECOVERY_VOL))
        d.observation_start["999990"] = AT - timedelta(minutes=len(B_HIGH_RECOVERY_VOL))
        d.observation_session["999990"] = (AT.date(), "REGULAR")
        t_entry = replace(t, price=1051, ask=1051, bid=1050)
        self.assertTrue(d.ingest(t_entry, AT))
        sig_b = d.signal(t_entry, w)
        self.assertIsNotNone(sig_b)
        self.assertEqual(sig_b["pattern"], "B")
        order_b = self.e.enter(sig_b, t_entry)
        self.assertIsNotNone(order_b)
        self.assertEqual(order_b["status"], "ACCEPTED")
        self.assertGreaterEqual(order_b["qty"], 1)
        self.assertLessEqual(order_b["qty"] * order_b["limit"] * (1 + self.e.a.commission_buy), 70847.0)

        # Fill and verify protection exit on emergency stop breach
        self.e.cumulative_fill(order_b, order_b["qty"], order_b["qty"] * order_b["limit"])
        pos = self.e.s["positions"]["999990"]
        self.assertEqual(pos["qty"], order_b["qty"])
        self.assertEqual(pos["initial_stop"], sig_b["evaluation_gates"]["pullback_structure"]["values"]["emergency_stop"])

    def test_a2_reaches_order_boundary_and_protection_exit_on_70847_account(self):
        self.clock[0] = AT
        self.e.s["day"] = AT.date().isoformat()
        self.e.c = replace(self.e.c, risk_fraction=0.02, max_daily_loss_fraction=0.04, max_positions=1, max_position_fraction=1.0)
        self.cfg = self.e.c
        self.e.a = replace(
            self.e.a,
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_pullback_enabled=True,
            structural_breakout_enabled=True,
            confirmed_invalidation_enabled=True,
            a_breakout_retest_entry_enabled=True,
            a2_breakout_retest_enabled=True,
        )
        self.e.available_cash = 70847.0
        self.e.s["cash"] = 70847.0
        self.e.s["automation"]["approved_cap"] = 81300
        self.e.s["automation"]["cash"] = 70847.0

        d, t, w = self.build("A")
        d.cfg = self.cfg
        d.a = self.e.a
        d.history["999990"] = deque(copy.deepcopy(A2_VALID))
        d.price_context.merge_history("999990", A2_VALID, AT, "KRX")
        d.observation_start["999990"] = AT - timedelta(minutes=len(A2_VALID))
        d.observation_session["999990"] = (AT.date(), "REGULAR")
        d.buffers["999990"].append(replace(t, time=AT - timedelta(seconds=1), price=10250, ask=10250, bid=10240, cumulative=t.cumulative - 10))
        t_a2 = replace(t, time=AT, price=10260, ask=10260, bid=10250)
        self.assertTrue(d.ingest(t_a2, AT))
        w["resistance"] = 11500
        sig_a2 = d.signal(t_a2, w)
        self.assertIsNotNone(sig_a2)
        self.assertEqual(sig_a2["pattern"], "A")
        self.assertEqual(sig_a2.get("setup_variant"), "A2")
        self.assertEqual(sig_a2["entry_basis"], "CONFIRMED_BREAKOUT_RETEST")
        order_a2 = self.e.enter(sig_a2, t_a2)
        self.assertIsNotNone(order_a2)
        self.assertEqual(order_a2["status"], "ACCEPTED")
        self.assertGreaterEqual(order_a2["qty"], 1)
        self.assertLessEqual(order_a2["qty"] * order_a2["limit"] * (1 + self.e.a.commission_buy), 70847.0)
        self.e.cumulative_fill(order_a2, order_a2["qty"], order_a2["qty"] * order_a2["limit"])
        self.assertEqual(self.e.s["positions"]["999990"]["qty"], order_a2["qty"])

    def test_bounded_core_decision_diagnostics_records_under_32gib_and_stops_safely_below_5gib(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            # Simulate 29.4 GiB free disk space (< 32 GiB main limit, >= 5 GiB diagnostic safety limit)
            usage_29gib = SimpleNamespace(total=500 * 1024**3, used=470 * 1024**3, free=int(29.4 * 1024**3))
            with patch("automation.trade_review_capture.shutil.disk_usage", return_value=usage_29gib):
                cap = TradeReviewCapture(
                    folder,
                    start=False,
                    min_free_bytes=32 * 1024**3,
                    diagnostic_max_bytes=4 * 1024 * 1024,
                    diagnostic_min_free_bytes=5 * 1024**3,
                    clock=lambda: AT,
                )
                try:
                    # Main 32 GiB tape is blocked on flush
                    self.assertFalse(cap.flush())
                    self.assertTrue(cap.storage_blocked)
                    self.assertEqual(cap.storage_block_reason, "CAPTURE_FREE_SPACE_RESERVE")

                    # Core decision diagnostic still records compact decision row safely!
                    self.clock[0] = AT
                    sig = {
                        "symbol": "047770",
                        "pattern": "A",
                        "setup_variant": "A2",
                        "entry_basis": "CONFIRMED_BREAKOUT_RETEST",
                        "limit": 10260,
                        "stop": 10070,
                        "target": 10720,
                        "invalidation_policy": {"structural_stop": 10180, "emergency_stop": 10070},
                        "entry_economics": {"gross_rr": 2.42, "net_rr": 1.68},
                    }
                    gates = {
                        "risk_quantity": {
                            "result": "PASS",
                            "values": {
                                "capital_quantity": 6,
                                "risk_quantity": 5,
                                "quantity": 5,
                                "reason_code": "QUANTITY_AVAILABLE",
                                "structural_stop": 10180,
                                "emergency_stop": 10070,
                            },
                        },
                        "risk_reward": {
                            "result": "PASS",
                            "values": {"target": 10720, "gross_rr": 2.42, "net_rr": 1.68},
                        },
                    }
                    cap.decision_evidence(self.e, "047770", "BUY", "ENTRY_INTENT_ACCEPTED", gates, sig, {"quantity": 5})
                    cap.core_decisions.flush()

                    diag_path = folder / ("core-decisions-" + AT.strftime("%Y%m%d") + ".jsonl")
                    self.assertTrue(diag_path.exists())
                    rows = [json.loads(line) for line in diag_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    self.assertEqual(len(rows), 1)
                    r = rows[0]
                    self.assertEqual(r["event"], "CORE_DECISION_DIAGNOSTIC")
                    self.assertEqual(r["symbol"], "047770")
                    self.assertEqual(r["setup"], "A2")
                    self.assertEqual(r["entry_price"], 10260)
                    self.assertEqual(r["structural_stop"], 10180)
                    self.assertEqual(r["emergency_stop"], 10070)
                    self.assertEqual(r["target"], 10720)
                    self.assertAlmostEqual(r["gross_rr"], 2.42)
                    self.assertAlmostEqual(r["net_rr"], 1.68)
                    self.assertEqual(r["capital_quantity"], 6)
                    self.assertEqual(r["risk_quantity"], 5)
                    self.assertEqual(r["quantity"], 5)
                    self.assertEqual(r["primary_reason_code"], "ENTRY_INTENT_ACCEPTED")
                    self.assertIn("evaluation_time", r)
                    self.assertIn("candidate_rejection_reason", r)
                    self.assertFalse(r["observation_gap"])

                    # Now simulate free space dropping below 5.0 GiB (e.g. 4.2 GiB) -> safely pauses & increments omitted
                    usage_4gib = SimpleNamespace(total=500 * 1024**3, used=495 * 1024**3, free=int(4.2 * 1024**3))
                    with patch("automation.trade_review_capture.shutil.disk_usage", return_value=usage_4gib):
                        cap.decision_evidence(self.e, "014830", "NO_BUY", "RISK_BUDGET_ZERO_QTY", gates, sig, {"quantity": 0})
                        cap.core_decisions.flush()
                        st = cap.stats()["core_decision_diagnostics"]
                        self.assertTrue(st["blocked"])
                        self.assertEqual(st["block_reason"], "DIAGNOSTIC_FREE_SPACE_BELOW_SAFETY_LIMIT")
                        self.assertEqual(st["omitted_rows"], 1)
                        self.assertEqual(st["written_rows"], 1)
                finally:
                    cap.close()


if __name__ == "__main__":
    unittest.main()
