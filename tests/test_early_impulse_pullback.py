"""Unit tests for Early Impulse Pullback Strategy and Early Discovery."""
import unittest
from datetime import datetime, time
from automation.early_discovery import get_active_ranks, INTRADAY_VOLUME_SURGE_RANK
from automation.early_impulse_pullback import EarlyImpulsePullbackStrategy
from core.settings import KST


class EarlyImpulsePullbackTests(unittest.TestCase):
    def setUp(self):
        self.strat = EarlyImpulsePullbackStrategy()

    def test_early_discovery_swaps_ka10029_with_ka10030_during_regular_hours(self):
        regular_time = datetime(2026, 10, 21, 10, 15, 0, tzinfo=KST)
        ranks_regular = get_active_ranks(regular_time)
        apis_reg = [r[0] for r in ranks_regular]
        self.assertIn('ka10030', apis_reg)
        self.assertNotIn('ka10029', apis_reg)

        preopen_time = datetime(2026, 10, 21, 8, 45, 0, tzinfo=KST)
        ranks_preopen = get_active_ranks(preopen_time)
        apis_pre = [r[0] for r in ranks_preopen]
        self.assertIn('ka10029', apis_pre)
        self.assertNotIn('ka10030', apis_pre)

    def test_rebound_entry_new_power_plasma_causal_replay(self):
        """Replay exact 10:18~10:23 candles of 뉴파워프라즈마 (144960)."""
        # Baseline 5 bars before impulse
        bars = [
            {'time': '10:13:00', 'open': 13750, 'high': 13780, 'low': 13740, 'close': 13760, 'volume': 5000},
            {'time': '10:14:00', 'open': 13760, 'high': 13790, 'low': 13750, 'close': 13780, 'volume': 6000},
            {'time': '10:15:00', 'open': 13780, 'high': 13810, 'low': 13770, 'close': 13800, 'volume': 7000},
            {'time': '10:16:00', 'open': 13800, 'high': 13820, 'low': 13790, 'close': 13810, 'volume': 8000},
            {'time': '10:17:00', 'open': 13810, 'high': 13880, 'low': 13800, 'close': 13850, 'volume': 12000},
            # 10:18: Major Volume Impulse (95,536 shares, +1.8% gain)
            {'time': '10:18:00', 'open': 13850, 'high': 14100, 'low': 13850, 'close': 14080, 'volume': 95536},
            # Pullback bars
            {'time': '10:19:00', 'open': 14080, 'high': 14090, 'low': 14020, 'close': 14020, 'volume': 15000},
            {'time': '10:20:00', 'open': 14020, 'high': 14020, 'low': 13930, 'close': 13950, 'volume': 12000},
            {'time': '10:21:00', 'open': 13950, 'high': 13950, 'low': 13900, 'close': 13900, 'volume': 11000},
            {'time': '10:22:00', 'open': 13900, 'high': 13910, 'low': 13850, 'close': 13890, 'volume': 14000},
        ]

        # 1. At 10:22:00, price is at 13890, prev_bar high is 13950 -> Not yet rebounding
        signal, reason, ev = self.strat.evaluate('144960', '뉴파워프라즈마', bars, 13890, 13900)
        self.assertIsNone(signal)
        self.assertIn('REBOUND_NOT_CONFIRMED', reason)

        # 2. At 10:22:49, current price breaks above 13,910 (prev high) to 14,020 (below impulse high 14,100)
        signal, reason, ev = self.strat.evaluate('144960', '뉴파워프라즈마', bars, 14020, 14020, capital_limit=71822.0, risk_budget=1429.0)
        self.assertIsNotNone(signal, f"Expected signal, got: {reason}")
        self.assertEqual(reason, 'SIGNAL_GENERATED')
        self.assertEqual(signal.entry, 14020.0)
        self.assertEqual(signal.stop, 13840.0) # support 13850 - 10
        self.assertEqual(signal.quantity, 5)   # Sized accurately to 5 shares
        self.assertGreater(signal.net_reward_risk, 1.5)

        # 3. Verify comparison with actual LIVE FAST trade:
        # Original: entry 14,150, exit 14,170 -> Gross +100, Fees ~163 -> Net -61
        # Early Pullback: entry 14,020, exit 14,170 -> Gross +750, Fees ~162 -> Net +588!
        early_gain_per_share = 14170.0 - signal.entry # +150
        self.assertEqual(early_gain_per_share, 150.0)
        total_early_gross = early_gain_per_share * signal.quantity # +750
        est_fees = (signal.entry * 0.00215 + 14170.0 * 0.00215) * signal.quantity # ~60.6 + ~30.5 = ~91 (or ~162 with slippage)
        net_early_pnl = total_early_gross - 162.94
        self.assertGreater(net_early_pnl, 500.0)

    def test_breakdown_below_impulse_low_is_rejected(self):
        """Retracement that breaches below impulse low must be rejected."""
        bars = [
            {'time': '10:00:00', 'open': 10000, 'high': 10050, 'low': 9950, 'close': 10000, 'volume': 5000},
            {'time': '10:01:00', 'open': 10000, 'high': 10050, 'low': 9950, 'close': 10000, 'volume': 5000},
            {'time': '10:02:00', 'open': 10000, 'high': 10050, 'low': 9950, 'close': 10000, 'volume': 5000},
            {'time': '10:03:00', 'open': 10000, 'high': 10050, 'low': 9950, 'close': 10000, 'volume': 5000},
            {'time': '10:04:00', 'open': 10000, 'high': 10050, 'low': 9950, 'close': 10000, 'volume': 5000},
            # Impulse bar: low is 10,000
            {'time': '10:05:00', 'open': 10000, 'high': 10500, 'low': 10000, 'close': 10450, 'volume': 50000},
            {'time': '10:06:00', 'open': 10450, 'high': 10450, 'low': 10200, 'close': 10250, 'volume': 10000},
            # Breakdown below 10,000 to 9,900
            {'time': '10:07:00', 'open': 10250, 'high': 10250, 'low': 9900, 'close': 9950, 'volume': 15000},
        ]
        signal, reason, ev = self.strat.evaluate('000000', '가짜종목', bars, 10100, 10100)
        self.assertIsNone(signal)
        self.assertEqual(reason, 'BREAKDOWN_BELOW_IMPULSE_BASE')


if __name__ == '__main__':
    unittest.main()
