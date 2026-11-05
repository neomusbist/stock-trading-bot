"""
Unit and integration tests for Focus Persistence:
1. Retaining completed bars across rank drops and subscription detachment.
2. Zero data fabrication across unobserved gaps.
3. Memory governor capacity bound (max_persistent_symbols) and LRU eviction.
4. Decoupling real-time subscription slots from trading decision memory.
5. Shadow observer stability without premature SHADOW_DATA_RESET loop.
"""
import unittest
from datetime import datetime, timedelta
from collections import deque

from core.settings import Config, KST
from core.strategy import Tick
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, HARD_LOCKS
from automation.capital import Costs
from automation.shadow_observation import ShadowObserver


class FocusPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(stale_seconds=5)
        self.auto = AutomationConfig(
            focus_persistence_enabled=True,
            max_persistent_symbols=10,
            max_persistent_bars=60,
            structural_breakout_enabled=True,
            intraday_context_enabled=True,
        )
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.base_time = datetime(2026, 11, 5, 9, 0, 0, tzinfo=KST)

    def test_bars_preserved_across_rank_churn_and_subscription_gap(self):
        """Symbols whose rank drops and detach from subscription retain their completed bars upon rejoin."""
        det = PatternDetector(self.cfg, self.auto, self.costs)
        sym = "389260"
        cum = 10000

        # Build 5 consecutive completed bars (09:00 - 09:04)
        for m in range(5):
            for s in [2, 30]:
                t = self.base_time + timedelta(minutes=m, seconds=s)
                cum += 100
                det.ingest(Tick(sym, t, 14500 + m * 50, 100, cum, 14510 + m * 50, 14500 + m * 50, True), t)

        # Minute 5 completes when 09:05 arrives
        t_gap_start = self.base_time + timedelta(minutes=5, seconds=2)
        cum += 100
        det.ingest(Tick(sym, t_gap_start, 14750, 100, cum, 14760, 14750, True), t_gap_start)

        stored_before = len(det.history.get(sym, []))
        self.assertEqual(stored_before, 5, "Must have built 5 completed bars")

        # Simulate rank drop: symbol is detached at 09:05:10
        det.mark_coverage_gap(sym, self.base_time + timedelta(minutes=5, seconds=10), 'SUBSCRIPTION_GAP')

        # 3 minutes of detachment (09:05:10 - 09:08:15) where symbol received no ticks
        t_rejoin = self.base_time + timedelta(minutes=8, seconds=15)
        cum += 500  # Exchange continued trading, missed trades
        det.ingest(Tick(sym, t_rejoin, 14900, 100, cum, 14910, 14900, True), t_rejoin)

        # Under Focus Persistence, history was NOT wiped
        stored_after = len(det.history.get(sym, []))
        self.assertGreaterEqual(stored_after, 5, "Completed bars must be preserved across detachment")

        # In usable_bars: pre-gap completed bars are retained without being wiped
        usable = det.usable_bars(sym)
        self.assertGreaterEqual(len(usable), 1, "Usable bars must not be wiped out permanently")

    def test_no_data_fabrication_across_unobserved_interval(self):
        """Focus Persistence preserves genuine bars but NEVER fabricates unobserved bars."""
        det = PatternDetector(self.cfg, self.auto, self.costs)
        sym = "282720"
        cum = 5000

        # Bar 0 at 09:00, Bar 1 at 09:01
        for m in [0, 1]:
            t = self.base_time + timedelta(minutes=m, seconds=5)
            cum += 100
            det.ingest(Tick(sym, t, 8500, 100, cum, 8510, 8500, True), t)

        # Detachment at 09:02
        det.mark_coverage_gap(sym, self.base_time + timedelta(minutes=2, seconds=0), 'SUBSCRIPTION_GAP')

        # Rejoin at 09:06:05 (gap of 4 minutes)
        t_rejoin = self.base_time + timedelta(minutes=6, seconds=5)
        cum += 400
        det.ingest(Tick(sym, t_rejoin, 8700, 100, cum, 8710, 8700, True), t_rejoin)

        history_bars = det.history.get(sym, [])
        bar_times = [b['time'] for b in history_bars]

        # Verify: Only 09:00 is in completed history (09:01 completed at 09:02). No fake bars for 09:02, 09:03, 09:04, 09:05!
        for b in history_bars:
            m = b['time'].minute
            self.assertNotIn(m, [2, 3, 4, 5], f"Bar at minute {m} was unobserved and must NOT be fabricated!")

    def test_memory_governor_bounded_capacity_and_lru_eviction(self):
        """Memory governor strictly caps retained symbols at max_persistent_symbols and evicts LRU."""
        auto_small = AutomationConfig(
            focus_persistence_enabled=True,
            max_persistent_symbols=4,
            max_persistent_bars=30,
        )
        det = PatternDetector(self.cfg, auto_small, self.costs)

        # Add 4 symbols: S1, S2, S3, S4
        symbols = ["100001", "100002", "100003", "100004"]
        for i, sym in enumerate(symbols):
            cum = 100
            for m in [0, 1]:
                t = self.base_time + timedelta(minutes=m, seconds=i + 1)
                cum += 10
                det.ingest(Tick(sym, t, 1000 + i * 100, 10, cum, 1000, 990, True), t)

        self.assertEqual(len(det.history), 4)

        # Access S1, S2, S4 recently at 09:03, leaving S3 as the oldest accessed
        t_touch = self.base_time + timedelta(minutes=3, seconds=0)
        det.touch_symbol("100001", t_touch)
        det.touch_symbol("100002", t_touch + timedelta(seconds=1))
        det.touch_symbol("100004", t_touch + timedelta(seconds=2))

        # Now add 5th symbol: S5
        sym_5 = "100005"
        cum = 100
        for m in [3, 4]:
            t = self.base_time + timedelta(minutes=m, seconds=10)
            cum += 10
            det.ingest(Tick(sym_5, t, 5000, 10, cum, 5000, 4990, True), t)

        # Total symbols in history must remain <= 4
        self.assertLessEqual(len(det.history), 4, "History symbol count must not exceed capacity (4)")
        # S3 (the oldest accessed) must have been evicted
        self.assertNotIn("100003", det.history, "Least recently accessed symbol S3 must be evicted")
        # Newly added S5 must be present
        self.assertIn(sym_5, det.history)

    def test_protected_symbols_never_evicted_by_memory_governor(self):
        """HARD_LOCK and currently active positions are immune from memory eviction."""
        auto_small = AutomationConfig(
            focus_persistence_enabled=True,
            max_persistent_symbols=2,
            max_persistent_bars=30,
        )
        det = PatternDetector(self.cfg, auto_small, self.costs)

        # Symbol 1 is a HARD_LOCK symbol (005930)
        hard_lock_sym = "005930"
        self.assertIn(hard_lock_sym, HARD_LOCKS)

        cum = 100
        for m in [0, 1]:
            t = self.base_time + timedelta(minutes=m, seconds=1)
            cum += 10
            det.ingest(Tick(hard_lock_sym, t, 60000, 10, cum, 60000, 59900, True), t)

        # Symbol 2
        sym2 = "200002"
        cum = 100
        for m in [0, 1]:
            t = self.base_time + timedelta(minutes=m, seconds=2)
            cum += 10
            det.ingest(Tick(sym2, t, 10000, 10, cum, 10000, 9900, True), t)

        self.assertEqual(len(det.history), 2)

        # Even if 005930 has oldest access timestamp, it must NEVER be evicted
        det.touch_symbol(hard_lock_sym, self.base_time)
        det.touch_symbol(sym2, self.base_time + timedelta(minutes=10))

        # Add Symbol 3
        sym3 = "200003"
        cum = 100
        for m in [2, 3]:
            t = self.base_time + timedelta(minutes=m, seconds=3)
            cum += 10
            det.ingest(Tick(sym3, t, 20000, 10, cum, 20000, 19900, True), t)

        # 005930 must STILL be present!
        self.assertIn(hard_lock_sym, det.history, "HARD_LOCK symbol 005930 must never be evicted")

    def test_shadow_observer_no_false_reset_loop(self):
        """ShadowObserver does not enter a reset loop when a symbol drops out of top 32 for > 5s."""
        events = []
        emit = lambda kind, **data: events.append((kind, data))
        observer = ShadowObserver(self.cfg, self.auto, self.costs, emit, limit=32)

        sym = "389260"
        t0 = self.base_time

        row = {
            'code': sym,
            'name': '대명에너지',
            'status': 'RESERVE',
            'source_count': 3,
            'affordability': {'entry_eligible': True},
            'reference_price': 14800,
            'shadow_view': {'market_status': {'pass': True}},
        }

        # Step 1: Symbol is selected
        observer.update([row], t0)
        self.assertIn(sym, observer.rows)

        # Step 2: Symbol drops out of top 32 for 20 seconds (greater than 5s stale_seconds)
        t_drop = t0 + timedelta(seconds=20)
        observer.update([], t_drop)
        self.assertNotIn(sym, observer.rows)
        self.assertIn(sym, observer.detached)

        # Step 3: Check events - should NOT have emitted SHADOW_DATA_RESET (since 20s < 180s residence)
        reset_events = [e for e in events if e[0] == 'SHADOW_DATA_RESET' and e[1].get('symbol') == sym]
        self.assertEqual(len(reset_events), 0, "Must NOT emit premature SHADOW_DATA_RESET within residence grace")

        # Step 4: Symbol re-joins after 40 seconds
        t_rejoin = t0 + timedelta(seconds=40)
        observer.update([row], t_rejoin)
        self.assertIn(sym, observer.rows)
        self.assertNotIn(sym, observer.detached)

        # Still no false resets
        reset_events = [e for e in events if e[0] == 'SHADOW_DATA_RESET' and e[1].get('symbol') == sym]
        self.assertEqual(len(reset_events), 0, "Must NOT emit false SHADOW_DATA_RESET on normal ranking rejoin")


if __name__ == "__main__":
    unittest.main()
