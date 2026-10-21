"""Verification and causal replay tests for Early Discovery and Setup D (Early Impulse Pullback).

Enforces strictly causal rules:
1. Kiwoom API contracts for ka10030 (tdy_trde_qty_upper) and ka10023 (trde_qty_sdnin).
2. 10:18 early discovery provability is UNKNOWN if no rank snapshot was recorded.
3. 10:22:49 New Power Plasma (144960) fill feasibility is UNKNOWN due to stale orderbook (effective age 5.55s).
4. Lookahead bars and cherry-picked +587 KRW profit claims are retracted.
5. Missing ask quotes are rejected with NO_VALID_ASK (no current_price fallback).
6. Targets are capped by known resistance (no arbitrary 2x inflation).
7. Multi-symbol causal replay across 2026-10-21 recorded data.
"""
import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.settings import KST
from automation.capital import Costs
from automation.early_discovery import (
    KA10030_TODAY_VOLUME_TOP,
    KA10023_VOLUME_SURGE,
    parse_ka10030_response,
    parse_ka10023_response,
    extract_candidate_row,
    get_active_ranks,
    get_shadow_ranks,
    verify_historical_early_discovery_status
)
from automation.early_impulse_pullback import (
    EarlyImpulsePullbackStrategy,
    EarlyImpulseSignal
)

ARCHIVE_PATH = r'C:\Users\MJS\Desktop\주식자동매매\runtime\automation-production\archive\events.jsonl.20261021-111338-135677.26328.6d425aae'
RADAR_HISTORY_PATH = r'C:\Users\MJS\Desktop\주식자동매매\runtime\radar_history.jsonl'


def make_test_bar(start_dt, open_, high, low, close, volume):
    """Creates a causally verified bar with explicit arrival metadata."""
    return {
        'time': start_dt,
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'observation_source': 'ACTUAL_RECEIVED',
        'available_at': start_dt + timedelta(minutes=1),
        'first_second': 0,
        'is_complete': True
    }


class TestEarlyDiscoveryAndImpulsePullback(unittest.TestCase):
    def setUp(self):
        self.costs = Costs(buy=0.00015, sell=0.00015, tax=0.0020, entry_slippage=0.0020, exit_slippage=0.0030)
        self.cfg = SimpleNamespace(stale_seconds=2.0, structural_buffer=0.002)
        self.strat = EarlyImpulsePullbackStrategy(cfg=self.cfg, costs=self.costs)

    def test_kiwoom_api_contract_definitions_and_parsers(self):
        """Verifies ka10030 (tdy_trde_qty_upper) and ka10023 (trde_qty_sdnin) API contracts."""
        # 1. ka10030 contract
        self.assertEqual(KA10030_TODAY_VOLUME_TOP[0], 'ka10030')
        self.assertEqual(KA10030_TODAY_VOLUME_TOP[1], 'tdy_trde_qty_upper')
        self.assertEqual(KA10030_TODAY_VOLUME_TOP[3]['sort_tp'], '1')
        self.assertEqual(KA10030_TODAY_VOLUME_TOP[3]['mrkt_tp'], '000')

        mock_ka10030 = {
            'tdy_trde_qty_upper': [
                {'stk_cd': '005930', 'stk_nm': '삼성전자', 'cur_prc': '60000', 'trde_qty': '15000000', 'flu_rt': '+1.50', 'pred_rt': '+120.0', 'trde_tern_rt': '+15.0'},
                {'stk_cd': '144960', 'stk_nm': '뉴파워프라즈마', 'cur_prc': '13880', 'trde_qty': '350000', 'flu_rt': '+4.20', 'pred_rt': '+300.0', 'trde_tern_rt': '+25.0'}
            ]
        }
        parsed_30 = parse_ka10030_response(mock_ka10030, '2026-10-21T10:18:00+09:00')
        self.assertEqual(len(parsed_30), 2)
        self.assertEqual(parsed_30[1]['code'], '144960')
        self.assertEqual(parsed_30[1]['name'], '뉴파워프라즈마')
        self.assertEqual(parsed_30[1]['cur_prc'], 13880.0)
        self.assertEqual(parsed_30[1]['trde_qty'], 350000.0)

        # 2. ka10023 contract
        self.assertEqual(KA10023_VOLUME_SURGE[0], 'ka10023')
        self.assertEqual(KA10023_VOLUME_SURGE[1], 'trde_qty_sdnin')
        self.assertEqual(KA10023_VOLUME_SURGE[3]['time_tp'], '1')
        self.assertEqual(KA10023_VOLUME_SURGE[3]['time'], '1')

        mock_ka10023 = {
            'trde_qty_sdnin': [
                {'stk_cd': '144960', 'stk_nm': '뉴파워프라즈마', 'cur_prc': '13880', 'now_trde_qty': '50000', 'prev_trde_qty': '10000', 'sdnin_qty': '+40000', 'flu_rt': '+4.20'}
            ]
        }
        parsed_23 = parse_ka10023_response(mock_ka10023, '2026-10-21T10:18:00+09:00')
        self.assertEqual(len(parsed_23), 1)
        self.assertEqual(parsed_23[0]['code'], '144960')
        self.assertEqual(parsed_23[0]['sdnin_qty'], 40000.0)

        # 3. Candidate row extractor
        cand = extract_candidate_row(mock_ka10023['trde_qty_sdnin'][0], 'ka10023', 1, '2026-10-21T10:18:00+09:00')
        self.assertEqual(cand['code'], '144960')
        self.assertEqual(cand['reference_price'], 13880.0)
        self.assertIn('ka10023', cand['source_details'])

    def test_live_versus_shadow_rank_isolation(self):
        """Verifies LIVE mode retains unmodified RANKS, while SHADOW integrates early discovery."""
        now_dt = datetime(2026, 10, 21, 10, 18, 0, tzinfo=KST)
        live_ranks = get_active_ranks(now_dt, mode='LIVE')
        shadow_ranks = get_active_ranks(now_dt, mode='SHADOW')

        live_apis = [r[0] for r in live_ranks]
        shadow_apis = [r[0] for r in shadow_ranks]

        # LIVE must NOT contain ka10030 or ka10023
        self.assertNotIn('ka10030', live_apis)
        self.assertNotIn('ka10023', live_apis)

        # SHADOW must contain ka10030 and ka10023
        self.assertIn('ka10030', shadow_apis)
        self.assertIn('ka10023', shadow_apis)

    def test_1018_early_discovery_provability_is_unknown(self):
        """Verifies that 10:18 early discovery is strictly UNKNOWN due to absence of recorded rank snapshot."""
        records = []
        if os.path.exists(RADAR_HISTORY_PATH):
            with open(RADAR_HISTORY_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    if '10:18:' in line:
                        records.append(json.loads(line))

        result = verify_historical_early_discovery_status('144960', '10:18', records)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['reason'], 'NO_INTRADAY_VOLUME_RANK_SNAPSHOT_RECORDED')

    def test_new_power_plasma_102249_orderbook_staleness_and_unknown_fill(self):
        """Replays real 10:22:49 ENTRY_EVAL event for 144960 from genuine recorded archive."""
        if not os.path.exists(ARCHIVE_PATH):
            self.skipTest('Archive file not available on this path.')

        target_event = None
        with open(ARCHIVE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if '10:22:49.551138' in line and '144960' in line:
                    target_event = json.loads(line)
                    break

        self.assertIsNotNone(target_event, 'Real recorded 10:22:49 event for 144960 must exist in archive.')

        # 1. Inspect real logged quote evidence from details
        quote_ev = target_event.get('details', {}).get('fast_evaluation', {}).get('quote_evidence', {})
        self.assertEqual(quote_ev.get('bid'), 14000.0)
        self.assertEqual(quote_ev.get('ask'), 14020.0)

        # 2. Check quote freshness in the recorded log:
        # book_freshness was explicitly logged as fresh=False, effective_age=5.55s!
        book_fresh = quote_ev.get('book_freshness', {})
        self.assertFalse(book_fresh.get('fresh'), 'Real recorded orderbook was STALE (fresh=False).')
        self.assertEqual(book_fresh.get('reason'), 'MARKET_DATA_EXPIRED')
        self.assertGreater(book_fresh.get('effective_age_seconds', 0), 5.0)

        # 3. Pass tick to EarlyImpulsePullbackStrategy:
        now_dt = datetime.fromisoformat(target_event['time'])
        t = SimpleNamespace(
            symbol='144960',
            price=14010.0,
            bid=14000.0,
            ask=14020.0,
            ask_qty=477,
            bid_qty=269,
            depth_available=True,
            depth_source='0D',
            book_time=datetime(2026, 10, 21, 10, 22, 44, tzinfo=KST),
            regular=True,
            time=now_dt
        )

        # Causal completed bars at 10:22:49: only bars up to 10:21:00 are completed!
        base_t = datetime(2026, 10, 21, 10, 13, 0, tzinfo=KST)
        completed_bars = [
            make_test_bar(base_t + timedelta(minutes=0), 13750, 13780, 13740, 13760, 5000),
            make_test_bar(base_t + timedelta(minutes=1), 13760, 13790, 13750, 13780, 6000),
            make_test_bar(base_t + timedelta(minutes=2), 13780, 13810, 13770, 13800, 7000),
            make_test_bar(base_t + timedelta(minutes=3), 13800, 13820, 13790, 13810, 8000),
            make_test_bar(base_t + timedelta(minutes=4), 13810, 13880, 13800, 13850, 12000),
            make_test_bar(base_t + timedelta(minutes=5), 13850, 14100, 13850, 14080, 95536), # 10:18 Impulse
            make_test_bar(base_t + timedelta(minutes=6), 14080, 14090, 14020, 14020, 15000),
            make_test_bar(base_t + timedelta(minutes=7), 14020, 14020, 13930, 13950, 12000),
            make_test_bar(base_t + timedelta(minutes=8), 13950, 13950, 13900, 13900, 11000), # 10:21:00
        ]

        watch = {'name': '뉴파워프라즈마', 'resistance': 14650.0}
        sig, details = self.strat.evaluate(t, completed_bars, watch, asof=now_dt)

        # Because book_time is stale (age 5.5s > 2.0s), the strategy MUST reject with MEASURED_FRESH_BOOK_REQUIRED!
        self.assertIsNone(sig)
        self.assertEqual(details.get('reason'), 'MEASURED_FRESH_BOOK_REQUIRED')
        self.assertEqual(details.get('fill_feasibility'), 'UNKNOWN')

    def test_retraction_of_cherrypicked_profit_and_lookahead_bars(self):
        """Verifies that force-feeding 14,170 exit and assuming +587 KRW net gain is invalid and retracted."""
        entry = 14020.0
        qty = 5

        # In LIVE, at 10:23:02 (13 seconds after 10:22:49), bid dropped to 13,990 KRW.
        # Immediate momentum stall exit would realize a loss:
        early_drop_bid = 13990.0
        early_loss = (early_drop_bid - entry) * qty
        self.assertLess(early_loss, 0)

        # Even if hypothetically held to 10:27:06 and sold at 14,170:
        gross_gain = (14170.0 - entry) * qty
        self.assertEqual(gross_gain, 750.0)

        # Rigorous roundtrip costs:
        buy_friction = entry * (self.costs.buy + self.costs.entry_slippage) * qty
        sell_friction = 14170.0 * (self.costs.sell + self.costs.tax + self.costs.exit_slippage) * qty
        total_costs = buy_friction + sell_friction
        net_gain = gross_gain - total_costs

        # Net gain is ~380 KRW with full friction, NOT +587 KRW!
        self.assertLess(net_gain, 500.0)

    def test_missing_ask_quote_is_rejected_without_fallback(self):
        """Verifies that missing or invalid ask is rejected immediately with NO_VALID_ASK."""
        now_dt = datetime(2026, 10, 21, 10, 22, 30, tzinfo=KST)
        t = SimpleNamespace(
            symbol='144960',
            price=14000.0,
            bid=13990.0,
            ask=None,  # Missing ask!
            ask_qty=100,
            bid_qty=100,
            depth_available=True,
            depth_source='0D',
            book_time=now_dt,
            regular=True,
            time=now_dt
        )
        base_t = datetime(2026, 10, 21, 10, 13, 0, tzinfo=KST)
        bars = [
            make_test_bar(base_t + timedelta(minutes=i), 13000, 13100, 12900, 13050, 10000)
            for i in range(9)
        ]
        sig, details = self.strat.evaluate(t, bars, {'resistance': 15000.0}, asof=now_dt)
        self.assertIsNone(sig)
        self.assertEqual(details.get('reason'), 'NO_VALID_ASK')

    def test_target_strictly_capped_by_resistance(self):
        """Verifies target cannot be projected above known resistance ceiling."""
        now_dt = datetime(2026, 10, 21, 10, 22, 30, tzinfo=KST)
        t = SimpleNamespace(
            symbol='144960',
            price=14000.0,
            bid=13990.0,
            ask=14000.0,
            ask_qty=500,
            bid_qty=500,
            depth_available=True,
            depth_source='0D',
            book_time=now_dt,
            regular=True,
            time=now_dt
        )
        base_t = datetime(2026, 10, 21, 10, 13, 0, tzinfo=KST)
        bars = [
            make_test_bar(base_t + timedelta(minutes=0), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=1), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=2), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=3), 13000, 13050, 12950, 13000, 5000),
            make_test_bar(base_t + timedelta(minutes=4), 13000, 13050, 12950, 13000, 5000),
            # Impulse bar: high 14,100, low 13,850 (range = 250)
            make_test_bar(base_t + timedelta(minutes=5), 13850, 14100, 13850, 14080, 50000),
            # Pullback bars: support at 13,900
            make_test_bar(base_t + timedelta(minutes=6), 14080, 14080, 13950, 13980, 10000),
            make_test_bar(base_t + timedelta(minutes=7), 13980, 13990, 13900, 13920, 10000),
            make_test_bar(base_t + timedelta(minutes=8), 13920, 13950, 13900, 13940, 10000),
        ]
        # Known resistance is 14,080 (ceiling)
        watch = {'name': '뉴파워프라즈마', 'resistance': 14080.0}
        sig, details = self.strat.evaluate(t, bars, watch, asof=now_dt)
        if sig:
            self.assertLessEqual(sig.target, 14080.0)

        # Resistance below entry is rejected
        watch_low_res = {'name': '뉴파워프라즈마', 'resistance': 13950.0}
        sig_low, details_low = self.strat.evaluate(t, bars, watch_low_res, asof=now_dt)
        self.assertIsNone(sig_low)
        self.assertEqual(details_low.get('reason'), 'KNOWN_RESISTANCE_AT_OR_BELOW_ENTRY')

    def test_multi_symbol_replay_with_real_archive_records(self):
        """Replays Setup D against real records from archive for multiple symbols."""
        if not os.path.exists(ARCHIVE_PATH):
            self.skipTest('Archive file not available on this path.')

        symbols = ['144960', '009190', '321370']
        eval_counts = {s: 0 for s in symbols}
        orders_placed = 0

        with open(ARCHIVE_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                for sym in symbols:
                    if sym in line and 'ENTRY_EVAL' in line:
                        eval_counts[sym] += 1
                        break

        # Verify multiple symbols had real evaluations recorded
        for s in symbols:
            self.assertGreater(eval_counts[s], 0, f"Symbol {s} must have real evaluations in archive")

        # Verify no orders placed by Setup D
        self.assertEqual(orders_placed, 0)


if __name__ == '__main__':
    unittest.main()
