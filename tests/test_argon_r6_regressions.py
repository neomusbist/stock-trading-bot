"""Offline acceptance regressions for the seven failures found at baseline 5887ba4.

No production source is changed. All files written by the imported engine live
in TemporaryDirectory. The supplied repository must already exist.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.settings import Config, KST
from core.strategy import Tick
from automation.capital import Costs
from automation.observation_worker import ObservationWorker
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from research.causal_account import PaperAccount
from research.causal_strategy_adapters import StrategyAAdapter, StrategyBAdapter, StrategyCAdapter
from research import causal_replay

AT = datetime(2026, 10, 20, 9, 0, tzinfo=KST)
SYM = "009190"


class StrictAcceptance(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(Config(), live_mode=True, risk_fraction=.02,
                           max_daily_loss_fraction=.04, max_position_fraction=1.,
                           stale_seconds=5, entry_slippage_fraction=.002,
                           exit_slippage_fraction=.003)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300,
                            scan_seconds=60, costs_confirmed=True,
                            fast_momentum_enabled=True, confirmed_invalidation_enabled=True)
        self.costs = Costs(self.auto.commission_buy, self.auto.commission_sell,
                           self.auto.sell_tax, .002, .003)

    def row(self, status=None):
        row = {"code": SYM, "name": "Daeyang", "status": "SHADOW",
               "source_count": 2, "reference_price": 1024.,
               "affordability": {"entry_eligible": True}}
        if status is not None:
            row["market_status"] = status
        return row

    def bar(self, start):
        return {"time": start, "open": 1000., "high": 1001., "low": 999.,
                "close": 1000., "volume": 300, "first_second": 0,
                "gap_reason": None, "observation_source": "ACTUAL_RECEIVED",
                "available_at": (start + timedelta(seconds=62)).isoformat()}

    def test_status_keeps_the_original_check_time(self):
        old = AT.replace(hour=8, minute=58, second=27)
        now = AT.replace(second=34)
        with tempfile.TemporaryDirectory() as tmp:
            worker = ObservationWorker(self.cfg, self.auto, self.costs, Path(tmp))
            worker.clock = lambda: now
            worker.prepare([self.row({"pass": True, "checked_at": old.isoformat()})],
                           now, {"entry_budget": 71822.}, publish=False)
            self.assertEqual(worker.metadata[SYM]["checked"], old)

    def test_status_refreshes_by_true_age_within_query_budget(self):
        old = AT.replace(hour=8, minute=58, second=27)
        first, second = AT.replace(second=34), AT.replace(minute=2, second=59)
        with tempfile.TemporaryDirectory() as tmp:
            rest = Mock()
            rest.stock_status.return_value = {"pass": True, "checked_at": second.isoformat()}
            worker = ObservationWorker(self.cfg, self.auto, self.costs, Path(tmp), rest=rest)
            worker.clock = lambda: first
            worker.prepare([self.row({"pass": True, "checked_at": old.isoformat()})],
                           first, {"entry_budget": 71822.}, publish=False)
            worker.clock = lambda: second
            worker.prepare([self.row()], second, {"entry_budget": 71822.}, publish=False)
            self.assertEqual(worker.metadata[SYM]["status"]["checked_at"], second.isoformat())

    def test_rejected_duplicate_does_not_change_research_ohlcv(self):
        adapter = StrategyAAdapter(self.cfg, self.auto, self.costs)
        adapter.ingest_tick(Tick(SYM, AT, 1000., 100, 1000, 1000., 999.), AT)
        later = AT + timedelta(seconds=5)
        adapter.ingest_tick(Tick(SYM, later, 1090., 50, 1000, 1090., 1089.), later)
        self.assertEqual(adapter.detector.current[SYM]["high"], 1000.)
        self.assertEqual(adapter.detector.current[SYM]["tick_count"], 1)

    def test_bar_only_ingestion_records_completed_price_context(self):
        adapter = StrategyAAdapter(self.cfg, self.auto, self.costs)
        adapter.ingest_bar(AT + timedelta(seconds=62), SYM, self.bar(AT))
        row = adapter.detector.price_context.rows.get(SYM, {})
        self.assertEqual(len(row.get("bars", [])), 1)

    def test_tick_and_capture_bar_are_not_counted_twice(self):
        adapter = StrategyAAdapter(self.cfg, self.auto, self.costs)
        cum = 1000
        for minute in range(3):
            start = AT + timedelta(minutes=minute)
            for second in (0, 20, 40, 60):
                stamp = start + timedelta(seconds=second)
                cum += 100
                adapter.ingest_tick(Tick(SYM, stamp, 1000., 100, cum, 1000., 999.), stamp)
            adapter.ingest_bar(start + timedelta(seconds=62), SYM, self.bar(start))
        self.assertEqual(len(adapter.detector.history[SYM]), 3)
        self.assertEqual(len(adapter.detector.usable_bars(SYM)), 3)

    def test_run_replay_releases_logged_intraminute_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tick_path, capture_path = root / "ticks.jsonl", root / "capture.jsonl"
            stamps = [AT + timedelta(minutes=m, seconds=s)
                      for m in range(2) for s in (0, 15, 30, 45)]
            stamps += [AT + timedelta(minutes=2), AT + timedelta(minutes=2, seconds=50),
                       AT + timedelta(minutes=3)]
            rows = []
            for index, stamp in enumerate(stamps):
                rows.append({"event": "TRADE", "symbol": SYM, "time": stamp.isoformat(),
                             "received": stamp.isoformat(), "processed_at": stamp.isoformat(),
                             "price": 1000., "volume": 100, "cumulative": 1000 + 100 * index,
                             "bid": 999., "ask": 1000., "regular": True,
                             "depth_available": True, "depth_source": "0D",
                             "book_time": stamp.isoformat(), "book_received_at": stamp.isoformat(),
                             "ask_qty": 1000, "bid_qty": 1000})
            caps = [{"event": "OPPORTUNITY_SAMPLE", "symbol": SYM, "asof": AT.isoformat(),
                     "capture_session": "S1", "capture_sequence": 1, "capture_losses": 0,
                     "watch": self.row({"pass": True, "checked_at": AT.isoformat()})},
                    {"event": "OBSERVATION_RELEASED", "symbol": SYM,
                     "time": (AT + timedelta(minutes=2, seconds=5)).isoformat(),
                     "timestamp": (AT + timedelta(minutes=2, seconds=5)).isoformat()},
                    {"event": "OBSERVATION_REJOINED", "symbol": SYM,
                     "time": (AT + timedelta(minutes=2, seconds=45)).isoformat(),
                     "timestamp": (AT + timedelta(minutes=2, seconds=45)).isoformat(),
                     "subscription_gap_seconds": 40.}]
            for path, records in ((tick_path, rows), (capture_path, caps)):
                path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            instances = []
            original = causal_replay.HypothesisReplay
            def recording_constructor(*args, **kwargs):
                instance = original(*args, **kwargs)
                instances.append(instance)
                return instance
            with patch.object(causal_replay, "HypothesisReplay", side_effect=recording_constructor):
                causal_replay.run_replay([tick_path], [capture_path], "2026-10-20",
                                         self.cfg, self.auto, initial_cash=71822., with_accounts=False)
            detector = instances[0].strategies["STRATEGY_A"].detector
            self.assertEqual(len(detector.usable_bars(SYM)), 0)

    def test_engine_cannot_increase_explicit_strategy_quantity(self):
        account = PaperAccount(AT, initial_cash=71822., cfg=self.cfg, auto=self.auto)
        self.addCleanup(account.close)
        signal = {"symbol": SYM, "limit": 1024., "stop": 1012., "emergency": 1009.,
                  "planned_qty": 69, "pattern": "FAST"}
        details = account.e.quantity_details(signal, 71822., 71822., account.e.daily_risk_budget())
        self.assertLessEqual(details["quantity"], 69)

    def test_control_manual_gap_invalidates_continuity(self):
        detector = PatternDetector(self.cfg, self.auto, self.costs)
        detector.continuous_observation = True
        for minute in range(4):
            stamp = AT + timedelta(minutes=minute)
            detector.ingest(Tick(SYM, stamp, 1000., 100, 1000 + minute * 100, 1000., 999.), stamp)
        self.assertEqual(len(detector.usable_bars(SYM)), 3)
        detector.mark_coverage_gap(SYM, AT + timedelta(minutes=3, seconds=5))
        self.assertEqual(detector.usable_bars(SYM), [])

    def test_control_cost_estimate_charges_slippage_once(self):
        expected = 1024 * .00015 + 1009 * (.00015 + .002) + 1024 * .002 + 1009 * .003
        self.assertAlmostEqual(self.costs.estimate(1024., 1009.), expected)


class RepairBoundaryTests(unittest.TestCase):
    setUp = StrictAcceptance.setUp
    row = StrictAcceptance.row
    bar = StrictAcceptance.bar

    def test_abc_reject_nonregular_late_and_duplicate_ticks_without_mutation(self):
        for kind in (StrategyAAdapter, StrategyBAdapter, StrategyCAdapter):
            for case in ('nonregular', 'late', 'duplicate', 'out_of_order'):
                with self.subTest(adapter=kind.__name__, case=case):
                    adapter = kind(self.cfg, self.auto, self.costs)
                    first = Tick(SYM, AT, 1000., 100, 1000, 1000., 999.)
                    adapter.ingest_tick(first, AT)
                    before = deepcopy(adapter.detector.current[SYM])
                    stamp = AT + timedelta(seconds=1)
                    tick = Tick(SYM, stamp, 1090., 50, 1100, 1090., 1089.)
                    received = stamp
                    if case == 'nonregular':tick = replace(tick, regular=False)
                    if case == 'late':received += timedelta(seconds=6)
                    if case == 'duplicate':tick = replace(tick, cumulative=1000)
                    if case == 'out_of_order':tick = replace(tick, time=AT - timedelta(seconds=1))
                    adapter.ingest_tick(tick, received)
                    self.assertEqual(adapter.detector.current[SYM], before)
                    self.assertEqual(list(adapter.detector.buffers[SYM]), [first])

    def test_unavailable_bar_cannot_enter_history_or_context(self):
        adapter = StrategyAAdapter(self.cfg, self.auto, self.costs)
        adapter.ingest_bar(AT + timedelta(seconds=61), SYM, self.bar(AT))
        self.assertFalse(adapter.detector.history.get(SYM))
        self.assertNotIn(SYM, adapter.detector.price_context.rows)

    def test_capture_before_rollover_stays_single_and_input_is_not_mutated(self):
        adapter = StrategyAAdapter(self.cfg, self.auto, self.costs)
        adapter.ingest_tick(Tick(SYM, AT, 1000., 100, 1000, 1000., 999.), AT)
        bar = self.bar(AT)
        snapshot = deepcopy(bar)
        adapter.ingest_bar(AT + timedelta(seconds=62), SYM, bar)
        boundary = AT + timedelta(minutes=1)
        adapter.ingest_tick(Tick(SYM, boundary, 1000., 100, 1100, 1000., 999.), boundary)
        self.assertEqual(len(adapter.detector.history[SYM]), 1)
        self.assertEqual(bar, snapshot)

    def test_status_missing_naive_or_future_check_is_not_made_current(self):
        cases = [None, 'invalid', AT.replace(tzinfo=None).isoformat(),
                 (AT + timedelta(seconds=2)).isoformat()]
        for checked in cases:
            with self.subTest(checked=checked), tempfile.TemporaryDirectory() as tmp:
                worker = ObservationWorker(self.cfg, self.auto, self.costs, Path(tmp))
                worker.clock = lambda: AT
                worker.prepare([self.row({'pass': True, 'checked_at': checked})], AT,
                               {'entry_budget': 71822.}, publish=False)
                self.assertIsNone(worker.metadata[SYM]['checked'])
                self.assertIs(worker.metadata[SYM]['status']['pass'], False)

    def test_old_pass_cannot_overwrite_newer_risk_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = ObservationWorker(self.cfg, self.auto, self.costs, Path(tmp))
            now = AT + timedelta(minutes=2)
            checked = now - timedelta(seconds=10)
            worker.clock = lambda: now
            worker.metadata[SYM] = {'checked': now, 'status': {'pass': False,
                'checked_at': checked.isoformat(), 'reason_code': 'RISK_STATUS'}}
            worker.prepare([self.row({'pass': True, 'checked_at': AT.isoformat()})],
                           now, {'entry_budget': 71822.}, publish=False)
            self.assertEqual(worker.metadata[SYM]['checked'], checked)
            self.assertEqual(worker.metadata[SYM]['status']['reason_code'], 'RISK_STATUS')

    def test_status_refresh_respects_existing_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            rest = Mock()
            now = AT + timedelta(minutes=5)
            rest.stock_status.return_value = {'pass': False, 'checked_at': now.isoformat()}
            worker = ObservationWorker(self.cfg, self.auto, self.costs, Path(tmp), rest=rest)
            worker.clock = lambda: now
            rows = [{**self.row(), 'code': f'{index:06d}'} for index in range(20)]
            worker.prepare(rows, now, {'entry_budget': 71822.}, publish=False)
            self.assertEqual(rest.stock_status.call_count, worker.settings.metadata_budget)

    def test_invalid_quantity_is_blocked_and_missing_plan_keeps_existing_sizing(self):
        account = PaperAccount(AT, initial_cash=71822., cfg=self.cfg, auto=self.auto)
        self.addCleanup(account.close)
        signal = {'symbol': SYM, 'limit': 1024., 'stop': 1012., 'emergency': 1009., 'pattern': 'FAST'}
        def sizing(value):
            return account.e.quantity_details(value, 71822., 71822., account.e.daily_risk_budget())
        self.assertEqual(sizing(signal)['quantity'], 70)
        for value in (-1, '69', True, 69.5, float('nan'), float('inf')):
            with self.subTest(value=value):
                result = sizing({**signal, 'planned_qty': value})
                self.assertEqual(result['quantity'], 0)
                self.assertEqual(result['reason_code'], 'INVALID_PLANNED_QUANTITY')
        self.assertEqual(sizing({**signal, 'planned_qty': 0})['quantity'], 0)
        self.assertEqual(sizing({**signal, 'planned_qty': 1000})['quantity'], 70)

    def test_global_gap_resets_fast_tape_and_blocks_pre_gap_capture_bars(self):
        replay = causal_replay.HypothesisReplay(self.cfg, self.auto, initial_cash=71822.)
        self.addCleanup(replay.close)
        replay.watch[SYM] = {'watch': self.row()}
        fast = replay.strategies['STRATEGY_FAST'].fast_strategy
        fast.ingest_tick(Tick(SYM, AT, 1000., 100, 1000, 1000., 999.), AT)
        self.assertEqual(len(fast.get_tape(SYM).ticks), 1)
        gap = AT + timedelta(seconds=40)
        replay.coverage_gap(gap, '', {'reason': 'SHADOW_QUEUE_GAP'})
        self.assertEqual(len(fast.get_tape(SYM).ticks), 0)
        self.assertFalse(replay.bar(AT + timedelta(seconds=62), SYM, self.bar(AT)))
        self.assertFalse(replay.history.get(SYM))


if __name__ == "__main__":
    unittest.main()
