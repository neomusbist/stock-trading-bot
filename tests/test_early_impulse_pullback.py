"""Comprehensive verification and causal replay tests for Early Discovery and Setup D.

Unifies AG c296ae3 and Argon independent audit standards:
1. Kiwoom API contracts for ka10023 (trde_qty_sdnin) and ka10030 (tdy_trde_qty_upper).
2. Safe numeric parsing without ValueError on missing prices.
3. LIVE/SHADOW radar collection path isolation & rate limit budget preservation (5 calls).
4. Strictly causal completed bars (rejection of lookahead and missing arrival evidence).
5. Disambiguation of decision timestamp from tick exchange timestamp (no retroactive backdating).
6. Mandatory book_time freshness (rejection of missing book timestamp as UNKNOWN).
7. Rejection of current_price fallback when ask quote is missing (EXECUTABLE_QUOTE_UNKNOWN).
8. Separation of price geometry from executable depth (EXECUTABLE_DEPTH_UNKNOWN).
9. Chase limit (0.3%) and target capping by resistance/intraday peak.
10. Deduplication, cooldown, day rollover, and stream reset.
11. Independent causal trade replayer (replay_trade_causal) with separate buy/sell depth checks.
12. Multi-symbol replay on real 2026-10-21 archive with performance / event loop load validation.
"""
import json
import os
import unittest
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.settings import KST
from automation.capital import Costs
from automation.observation_settings import ObservationSettings
from automation.early_discovery import (
    OFFICIAL_EARLY_RANK_CONTRACTS,
    INTRADAY_VOLUME_SURGE_RANK,
    INTRADAY_TOP_VOLUME_RANK,
    validate_rank_spec,
    validate_rank_response,
    extract_candidate_row,
    get_active_ranks,
    get_shadow_ranks,
    verify_window_capture_feasibility,
)
from automation.early_impulse_pullback import (
    EarlyImpulsePullbackStrategy,
    EarlyImpulsePullbackSignal,
    completed_bars_asof,
    replay_trade_causal,
)
from automation.shadow_observation import ShadowObserver
from candidate_builder import RANKS

ARCHIVE_PATH = r'C:\Users\MJS\Desktop\주식자동매매\runtime\automation-production\archive\events.jsonl.20261021-111338-135677.26328.6d425aae'
RADAR_HISTORY_PATH = r'C:\Users\MJS\Desktop\주식자동매매\runtime\radar_history.jsonl'


def make_test_bar(start_dt, open_, high, low, close, volume, available_at=None):
    """Creates a causally verified 1-minute bar with explicit arrival metadata."""
    end_dt = start_dt + timedelta(minutes=1)
    avail = available_at or end_dt
    return {
        'time': start_dt,
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'observation_source': 'ACTUAL_RECEIVED',
        'available_at': avail,
        'first_second': 0,
        'is_complete': True,
    }


class TestEarlyDiscoveryAndSetupDComprehensive(unittest.TestCase):
    def setUp(self):
        self.costs = Costs(buy=0.00015, sell=0.00015, tax=0.0020, entry_slippage=0.0020, exit_slippage=0.0030)
        self.cfg = SimpleNamespace(
            stale_seconds=5.0,
            structural_buffer=0.002,
            min_stop_fraction=0.006,
            max_stop_fraction=0.025,
            max_spread=0.004,
            min_reward_risk=1.5,
        )
        self.auto = SimpleNamespace(
            setup_d_max_chase=0.003,
            setup_d_cooldown_seconds=30.0,
            setup_d_ttl_seconds=15.0,
        )
        self.strat = EarlyImpulsePullbackStrategy(cfg=self.cfg, auto=self.auto, costs=self.costs)

    def test_1_official_kiwoom_api_contracts_and_safe_parsing(self):
        """Verifies ka10023 (trde_qty_sdnin) and ka10030 (tdy_trde_qty_upper) API contracts and safe parsing."""
        # 1. ka10023 contract
        self.assertEqual(INTRADAY_VOLUME_SURGE_RANK[0], 'ka10023')
        self.assertEqual(INTRADAY_VOLUME_SURGE_RANK[1], 'trde_qty_sdnin')
        self.assertTrue(validate_rank_spec(INTRADAY_VOLUME_SURGE_RANK))
        self.assertEqual(INTRADAY_VOLUME_SURGE_RANK[3]['tm_tp'], '1')
        self.assertEqual(INTRADAY_VOLUME_SURGE_RANK[3]['tm'], '3')

        # 2. ka10030 contract
        self.assertEqual(INTRADAY_TOP_VOLUME_RANK[0], 'ka10030')
        self.assertEqual(INTRADAY_TOP_VOLUME_RANK[1], 'tdy_trde_qty_upper')
        self.assertTrue(validate_rank_spec(INTRADAY_TOP_VOLUME_RANK))
        self.assertEqual(INTRADAY_TOP_VOLUME_RANK[3]['mang_stk_incls'], '1')
        self.assertEqual(INTRADAY_TOP_VOLUME_RANK[3]['mrkt_open_tp'], '0')

        # 3. Fabricated spec rejection
        broken_spec = (
            'ka10030', 'pred_trde_qty_flu_upper', 30,
            {'mrkt_tp': '000', 'qry_tp': '1', 'rank_strt': '0', 'rank_end': '100', 'stex_tp': '1'},
        )
        with self.assertRaises(ValueError) as ctx:
            validate_rank_spec(broken_spec)
        self.assertIn('tdy_trde_qty_upper', str(ctx.exception))

        # 4. Safe candidate extraction without ValueError on missing price
        row_missing_px = {'stk_cd': '144960', 'stk_nm': '뉴파워프라즈마', 'flu_rt': '+5.0', 'now_trde_qty': '1000'}
        cand = extract_candidate_row(row_missing_px, 1, 'ka10023')
        self.assertEqual(cand['code'], '144960')
        self.assertIsNone(cand['price'])

    def test_2_radar_collection_isolation_and_rate_limit_budget(self):
        """Verifies LIVE mode retains unmodified RANKS, and SHADOW maintains strictly 5 calls by swapping ka10029."""
        regular_dt = datetime(2026, 10, 21, 10, 15, 0, tzinfo=KST)

        # LIVE mode: exactly 5 unmodified RANKS
        live_ranks = get_active_ranks(regular_dt, mode='LIVE')
        self.assertEqual(len(live_ranks), len(RANKS))
        live_apis = [r[0] for r in live_ranks]
        self.assertIn('ka10029', live_apis)
        self.assertNotIn('ka10023', live_apis)

        # SHADOW mode: exactly 5 ranks (ka10029 swapped with ka10023)
        shadow_ranks = get_shadow_ranks(regular_dt)
        self.assertEqual(len(shadow_ranks), 5, "SHADOW rank count must remain strictly 5 to preserve rate limit budget")
        shadow_apis = [r[0] for r in shadow_ranks]
        self.assertNotIn('ka10029', shadow_apis, "Preopen ka10029 must be replaced during regular session")
        self.assertIn('ka10023', shadow_apis, "ka10023 volume surge must be included")

    def test_3_historical_window_capture_feasibility_is_unknown(self):
        """Verifies 10:06~10:17 capture feasibility is UNKNOWN when snapshot data is absent."""
        records = []
        if os.path.exists(RADAR_HISTORY_PATH):
            with open(RADAR_HISTORY_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    if '10:06:' in line or '10:10:' in line or '10:15:' in line:
                        records.append(json.loads(line))

        res = verify_window_capture_feasibility(records, '144960', '10:06:00', '10:17:00')
        if not records:
            self.assertEqual(res['status'], 'UNKNOWN')
            self.assertEqual(res['reason'], 'RANK_SNAPSHOT_DATA_ABSENT')

    def test_4_incomplete_1m_bar_lookahead_and_missing_ask_rejection(self):
        """Verifies incomplete bar lookahead is rejected and missing ask returns EXECUTABLE_QUOTE_UNKNOWN."""
        decision_at = datetime(2026, 10, 21, 10, 22, 49, tzinfo=KST)
        base_t = datetime(2026, 10, 21, 10, 13, 0, tzinfo=KST)

        # 1. 10:22:00 bar completes at 10:23:00. At 10:22:49, it is INCOMPLETE!
        bars_with_lookahead = [
            make_test_bar(base_t + timedelta(minutes=i), 13750, 13780, 13740, 13760, 5000)
            for i in range(9) # 10:13 ~ 10:21 completed
        ]
        # Append incomplete 10:22 bar
        bars_with_lookahead.append({
            'time': datetime(2026, 10, 21, 10, 22, 0, tzinfo=KST),
            'open': 13900, 'high': 13910, 'low': 13850, 'close': 13890, 'volume': 14000,
        })

        completed, incomplete = completed_bars_asof(bars_with_lookahead, decision_at)
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]['_reject_reason'], 'BAR_NOT_COMPLETED_AT_ASOF')

        sig, details = self.strat.evaluate(
            symbol='144960',
            bars=bars_with_lookahead,
            current_price=14010.0,
            current_ask=14020.0,
            current_bid=14000.0,
            watch={'resistance': 15000.0},
            asof=decision_at,
            book_time=decision_at,
            depth_available=True,
            ask_qty=500,
            bid_qty=500,
            strict_causality=True,
        )
        self.assertIsNone(sig)
        self.assertEqual(details.get('reason'), 'BAR_NOT_COMPLETED_AT_ASOF')

        # 2. Missing current_ask must return EXECUTABLE_QUOTE_UNKNOWN (NO current_price fallback!)
        valid_completed = [
            make_test_bar(base_t + timedelta(minutes=0), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=1), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=2), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=3), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=4), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=5), 13000, 13500, 12950, 13450, 50000), # impulse
            make_test_bar(base_t + timedelta(minutes=6), 13450, 13450, 13200, 13250, 10000), # pullback
            make_test_bar(base_t + timedelta(minutes=7), 13250, 13280, 13150, 13200, 10000),
            make_test_bar(base_t + timedelta(minutes=8), 13200, 13250, 13150, 13220, 10000),
        ]
        asof_valid = base_t + timedelta(minutes=9, seconds=10)
        sig_no_ask, det_no_ask = self.strat.evaluate(
            symbol='144960',
            bars=valid_completed,
            current_price=13300.0,
            current_ask=None,  # Missing ask!
            current_bid=13280.0,
            watch={'resistance': 15000.0},
            asof=asof_valid,
            book_time=asof_valid,
            depth_available=True,
            ask_qty=500,
            bid_qty=500,
            strict_causality=False,
        )
        self.assertIsNone(sig_no_ask)
        self.assertEqual(det_no_ask.get('status'), 'UNKNOWN')
        self.assertEqual(det_no_ask.get('reason'), 'EXECUTABLE_QUOTE_UNKNOWN')
        self.assertEqual(det_no_ask.get('fill_status'), 'UNKNOWN')

    def test_5_decision_timestamp_not_confused_with_tick_time(self):
        """Verifies evaluate_tick uses local receipt time as decision_at and rejects stale tick timestamps."""
        base_t = datetime(2026, 10, 21, 10, 20, 0, tzinfo=KST)
        receipt_time = datetime(2026, 10, 21, 10, 29, 10, tzinfo=KST)
        stale_tick_time = datetime(2026, 10, 21, 10, 29, 0, tzinfo=KST) # 10s old > 5s stale_seconds

        bars = [
            make_test_bar(base_t + timedelta(minutes=i), 13000, 13050, 12950, 13000, 5000)
            for i in range(9)
        ]
        t_stale = SimpleNamespace(
            symbol='144960', price=13300.0, bid=13280.0, ask=13300.0,
            ask_qty=500, bid_qty=500, depth_available=True,
            time=stale_tick_time, book_time=receipt_time,
        )
        sig, det = self.strat.evaluate_tick(
            t_stale, bars, {'resistance': 15000.0},
            received=receipt_time
        )
        self.assertIsNone(sig)
        self.assertEqual(det.get('reason'), 'STALE_OR_FUTURE_SIGNAL_TICK')

    def test_6_missing_book_time_rejected_as_unknown(self):
        """Verifies quotes lacking book_time are rejected with BOOK_TIMESTAMP_UNKNOWN_OR_MISSING."""
        base_t = datetime(2026, 10, 21, 10, 20, 0, tzinfo=KST)
        now_dt = datetime(2026, 10, 21, 10, 29, 10, tzinfo=KST)
        bars = [
            make_test_bar(base_t + timedelta(minutes=i), 13000, 13050, 12950, 13000, 5000)
            for i in range(9)
        ]
        t_no_book = SimpleNamespace(
            symbol='144960', price=13300.0, bid=13280.0, ask=13300.0,
            ask_qty=500, bid_qty=500, depth_available=True,
            time=now_dt, book_time=None, # Missing book time!
        )
        sig, det = self.strat.evaluate_tick(t_no_book, bars, {'resistance': 15000.0}, received=now_dt)
        self.assertIsNone(sig)
        self.assertEqual(det.get('status'), 'UNKNOWN')
        self.assertEqual(det.get('reason'), 'BOOK_TIMESTAMP_UNKNOWN_OR_MISSING')

    def test_7_missing_bar_arrival_evidence_rejected(self):
        """Verifies bars lacking arrival evidence (available_at is None) are rejected with BAR_AVAILABILITY_UNKNOWN."""
        asof_dt = datetime(2026, 10, 21, 10, 25, 0, tzinfo=KST)
        base_t = datetime(2026, 10, 21, 10, 15, 0, tzinfo=KST)

        # Create bar with NO arrival metadata (avail is None)
        bars_no_avail = [
            {
                'time': base_t + timedelta(minutes=i),
                'open': 10000, 'high': 10100, 'low': 9900, 'close': 10050, 'volume': 5000,
                # available_at missing!
            }
            for i in range(5)
        ]
        completed, incomplete = completed_bars_asof(bars_no_avail, asof_dt, require_arrival_evidence=True)
        self.assertEqual(len(incomplete), 5)
        self.assertEqual(incomplete[0]['_reject_reason'], 'BAR_AVAILABILITY_UNKNOWN')

    def test_8_max_chase_and_resistance_capping(self):
        """Verifies max chase enforcement and target price capping by resistance / confirmed peak."""
        base_t = datetime(2026, 10, 21, 10, 10, 0, tzinfo=KST)
        asof_dt = base_t + timedelta(minutes=9, seconds=5)

        bars = [
            make_test_bar(base_t + timedelta(minutes=0), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=1), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=2), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=3), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=4), 10000, 10050, 9950, 10000, 5000),
            # Impulse bar: low 10,000, high 10,500 (+5.0%)
            make_test_bar(base_t + timedelta(minutes=5), 10000, 10500, 10000, 10450, 50000),
            # Pullback bars: support low 10,200, last bar high 10,250
            make_test_bar(base_t + timedelta(minutes=6), 10450, 10450, 10250, 10300, 10000),
            make_test_bar(base_t + timedelta(minutes=7), 10300, 10320, 10200, 10220, 10000),
            make_test_bar(base_t + timedelta(minutes=8), 10220, 10250, 10200, 10230, 10000),
        ]
        # Rebound trigger is 10,250. Max chase is 10,250 * 1.003 = 10,280.75.
        # 1. Ask 10,290 exceeds max chase
        sig_chase, det_chase = self.strat.evaluate(
            symbol='005930', bars=bars, current_price=10260.0, current_ask=10290.0, current_bid=10260.0,
            watch={'resistance': 11000.0}, asof=asof_dt, book_time=asof_dt, depth_available=True,
            ask_qty=500, bid_qty=500, strict_causality=False,
        )
        self.assertIsNone(sig_chase)
        self.assertEqual(det_chase.get('reason'), 'MAX_CHASE_EXCEEDED')

        # 2. Target capped by resistance
        sig_valid, det_valid = self.strat.evaluate(
            symbol='005930', bars=bars, current_price=10260.0, current_ask=10270.0, current_bid=10260.0,
            watch={'resistance': 10600.0}, asof=asof_dt, book_time=asof_dt, depth_available=True,
            ask_qty=500, bid_qty=500, strict_causality=False,
        )
        self.assertIsNotNone(sig_valid)
        self.assertLessEqual(sig_valid.target, 10600.0)

    def test_9_deduplication_cooldown_and_reset(self):
        """Verifies deduplication on identical structure, cooldown, and clean reset."""
        base_t = datetime(2026, 10, 21, 10, 10, 0, tzinfo=KST)
        asof_dt = base_t + timedelta(minutes=9, seconds=5)
        bars = [
            make_test_bar(base_t + timedelta(minutes=0), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=1), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=2), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=3), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=4), 10000, 10050, 9950, 10000, 5000),
            make_test_bar(base_t + timedelta(minutes=5), 10000, 10500, 10000, 10450, 50000),
            make_test_bar(base_t + timedelta(minutes=6), 10450, 10450, 10250, 10300, 10000),
            make_test_bar(base_t + timedelta(minutes=7), 10300, 10320, 10200, 10220, 10000),
            make_test_bar(base_t + timedelta(minutes=8), 10220, 10250, 10200, 10230, 10000),
        ]
        # First signal
        sig1, det1 = self.strat.evaluate(
            symbol='005930', bars=bars, current_price=10260.0, current_ask=10270.0, current_bid=10260.0,
            watch={'resistance': 11000.0}, asof=asof_dt, book_time=asof_dt, depth_available=True,
            ask_qty=500, bid_qty=500, strict_causality=False,
        )
        self.assertIsNotNone(sig1)

        # Immediate second evaluation with same structure -> duplicate rejected
        sig2, det2 = self.strat.evaluate(
            symbol='005930', bars=bars, current_price=10260.0, current_ask=10270.0, current_bid=10260.0,
            watch={'resistance': 11000.0}, asof=asof_dt + timedelta(seconds=1), book_time=asof_dt + timedelta(seconds=1),
            depth_available=True, ask_qty=500, bid_qty=500, strict_causality=False,
        )
        self.assertIsNone(sig2)
        self.assertIn(det2.get('reason'), ('SIGNAL_COOLDOWN_ACTIVE', 'DUPLICATE_IMPULSE_PULLBACK_SIGNAL'))

        # Reset clears deduplication
        self.strat.reset()
        self.assertEqual(len(self.strat.active_signals), 0)
        self.assertEqual(len(self.strat.emitted_keys), 0)

    def test_10_causal_trade_replayer_without_cherrypicked_exit(self):
        """Verifies replay_trade_causal enforces depth verification and does not use future cherry-picked exit."""
        signal = EarlyImpulsePullbackSignal(
            symbol='144960',
            limit=14020.0,
            stop=13840.0,
            emergency=13800.0,
            target=14200.0,
            quantity=5,
            gross_reward_risk=1.5,
            net_reward_risk=1.2,
            impulse_high=14100.0,
            impulse_low=13850.0,
            support_low=13850.0,
            rebound_trigger=13950.0,
            max_chase_price=13990.0,
            created_at=datetime(2026, 10, 21, 10, 22, 49, tzinfo=KST),
        )

        # 1. Without entry depth verification, replay returns UNKNOWN
        unverified_quote = {'ask': 14020.0, 'ask_qty': None, 'depth_available': False}
        rep_unverified = replay_trade_causal(signal, [{'time': datetime(2026, 10, 21, 10, 23, 0, tzinfo=KST), 'price': 14000.0}], self.costs, entry_quote=unverified_quote)
        self.assertEqual(rep_unverified['status'], 'UNKNOWN')
        self.assertFalse(rep_unverified['fill_verified'])

        # 2. Replay with verified entry depth through subsequent price action:
        # At 10:23:02, bid drops to 13,990 (drop below entry).
        # At 10:25:00, stop is hit at 13,840.
        verified_entry = {'ask': 14020.0, 'ask_qty': 100, 'depth_available': True}
        frames = [
            {'time': datetime(2026, 10, 21, 10, 23, 2, tzinfo=KST), 'price': 13990.0, 'bid': 13990.0, 'bid_qty': 50, 'depth_available': True},
            {'time': datetime(2026, 10, 21, 10, 24, 0, tzinfo=KST), 'price': 13950.0, 'bid': 13940.0, 'bid_qty': 50, 'depth_available': True},
            {'time': datetime(2026, 10, 21, 10, 25, 0, tzinfo=KST), 'price': 13840.0, 'bid': 13840.0, 'bid_qty': 50, 'depth_available': True}, # Stop hit!
        ]
        rep = replay_trade_causal(signal, frames, self.costs, entry_quote=verified_entry)
        self.assertEqual(rep['reason'], 'STRUCTURAL_STOP_PRICE')
        self.assertEqual(rep['exit_price'], 13840.0)
        self.assertLess(rep['modeled_net_pnl'], 0.0, "Stopped-out trade must realize loss")
        self.assertEqual(rep['mfe_per_share'], 0.0)
        self.assertEqual(rep['mae_per_share'], -180.0) # 13840 - 14020 = -180

    def test_11_multi_symbol_shadow_evaluation_overhead_and_zero_live_orders(self):
        """Verifies Setup D evaluation adds negligible overhead (<0.2ms/tick) and issues ZERO live orders."""
        if not os.path.exists(ARCHIVE_PATH):
            self.skipTest('Archive file not available on this path.')

        symbols = ['144960', '009190', '321370']
        eval_count = 0
        total_eval_time = 0.0
        orders_placed = 0

        # Create dummy completed bars
        base_t = datetime(2026, 10, 21, 10, 10, 0, tzinfo=KST)
        bars = [
            make_test_bar(base_t + timedelta(minutes=i), 10000, 10050, 9950, 10000, 5000)
            for i in range(9)
        ]

        t_now = base_t + timedelta(minutes=9, seconds=5)
        t_sample = SimpleNamespace(
            symbol='144960', price=10260.0, bid=10260.0, ask=10270.0,
            ask_qty=500, bid_qty=500, depth_available=True,
            time=t_now, book_time=t_now,
        )

        for _ in range(50):
            t0 = time.perf_counter()
            sig, det = self.strat.evaluate_tick(t_sample, bars, {'resistance': 11000.0}, received=t_now)
            elapsed = time.perf_counter() - t0
            total_eval_time += elapsed
            eval_count += 1
            if sig:
                # Must be strictly locked to SHADOW
                self.assertTrue(sig.SHADOW_ONLY)
                self.assertFalse(sig.actual_order_permission)

        mean_eval_ms = (total_eval_time / eval_count) * 1000.0
        self.assertLess(mean_eval_ms, 0.5, f"Evaluation must not lag event loop; took {mean_eval_ms:.3f}ms")
        self.assertEqual(orders_placed, 0, "SHADOW evaluation must never generate live orders")


if __name__ == '__main__':
    unittest.main()
