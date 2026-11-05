"""Unit and integration tests for Classics Strategy Engine (투자 원전 9권 기반 전략 검증)."""

import unittest
from datetime import datetime, timedelta
from automation.classics_strategy import (
    ClassicsMicrostructureGate,
    ClassicsRiskSizer,
    ClassicsBreakoutStrategy,
    ClassicsRoleReversalStrategy,
    ClassicsBoxExpansionStrategy,
    ClassicsStrategyEngine,
    HARD_LOCK_SYMBOLS
)


def make_mock_bar(time_str: str, open_: float, high: float, low: float, close: float, volume: int):
    return {
        'time': datetime.fromisoformat(f"2026-11-05T{time_str}"),
        'open': float(open_),
        'high': float(high),
        'low': float(low),
        'close': float(close),
        'volume': int(volume)
    }


class TestClassicsMicrostructureGate(unittest.TestCase):
    def setUp(self):
        self.gate = ClassicsMicrostructureGate()

    def test_hard_lock_protection(self):
        for sym in HARD_LOCK_SYMBOLS:
            passed, reason, _ = self.gate.evaluate_quote(sym, 50000, 50100, 50100)
            self.assertFalse(passed)
            self.assertEqual(reason, 'HARD_LOCK_PROTECTED_SYMBOL')

    def test_quote_spread_limits(self):
        # 10,000 KRW stock, tick size 10 KRW
        # Spread = (10100 - 10000)/10000 = 1.0% > 0.4% -> Reject
        passed, reason, _ = self.gate.evaluate_quote('012340', 10000, 10100, 10000)
        self.assertFalse(passed)
        self.assertEqual(reason, 'SPREAD_EXCEEDS_MAXIMUM')

        # Tight spread = (10010 - 10000)/10000 = 0.1% -> Pass
        passed, reason, _ = self.gate.evaluate_quote('012340', 10000, 10010, 10010)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')

    def test_invalid_quotes_rejected(self):
        # Zero bid/ask or crossed book
        passed, reason, _ = self.gate.evaluate_quote('012340', 0, 10000, 10000)
        self.assertFalse(passed)
        self.assertEqual(reason, 'EXECUTABLE_QUOTE_INVALID')

        passed, reason, _ = self.gate.evaluate_quote('012340', 10050, 10000, 10000)
        self.assertFalse(passed)
        self.assertEqual(reason, 'EXECUTABLE_QUOTE_INVALID')

    def test_economics_net_rr_hurdle(self):
        # Entry 10,000, Stop 9,800 (Risk 200), Target 10,100 (Reward 100) -> Gross RR 0.5 -> Fail
        passed, reason, econ = self.gate.evaluate_economics(entry=10000, stop=9800, target=10100)
        self.assertFalse(passed)
        self.assertEqual(reason, 'NET_REWARD_RISK_LOW')
        self.assertLess(econ['net_rr'], 1.5)

        # Entry 10,000, Stop 9,800 (Risk ~200), Target 10,600 (Reward ~600) -> Net RR > 2.5 -> Pass
        passed, reason, econ = self.gate.evaluate_economics(entry=10000, stop=9800, target=10600)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertGreaterEqual(econ['net_rr'], 1.5)


class TestClassicsRiskSizer(unittest.TestCase):
    def setUp(self):
        self.sizer = ClassicsRiskSizer(risk_fraction=0.02, min_stop_distance_fraction=0.015)

    def test_stop_too_tight_noise_rejection(self):
        # Emulates Aribio Holdings incident where stop was only 1.2% away
        bars = [
            make_mock_bar("09:01:00", 3600, 3680, 3590, 3650, 10000),
            make_mock_bar("09:02:00", 3650, 3690, 3620, 3670, 12000),
            make_mock_bar("09:03:00", 3670, 3700, 3640, 3660, 11000)
        ]
        # Entry 3655, stop 3610 (distance 45 KRW = 1.23% < min 1.5% and < ATR noise)
        economics = {
            'risk_net_per_share': 55.0,
            'buy_outlay': 3660.0
        }
        passed, reason, data = self.sizer.evaluate_stop_and_size(
            entry=3655,
            structural_stop=3610,
            bars=bars,
            equity=10000000,
            capital_limit=1000000,
            economics=economics
        )
        self.assertFalse(passed)
        self.assertEqual(reason, 'STOP_TOO_TIGHT_NOISE_RISK')

    def test_elder_2_percent_sizing(self):
        bars = [
            make_mock_bar("09:01:00", 10000, 10050, 9950, 10000, 5000),
            make_mock_bar("09:02:00", 10000, 10060, 9960, 10020, 6000),
        ]
        # Entry 10,000, Stop 9,700 (distance 300 KRW = 3.0% >= 1.5%)
        # Equity 10,000,000 -> 2% risk budget = 200,000 KRW
        # Risk net per share = 330 KRW (with tax/fee)
        # Expected shares by risk = floor(200,000 / 330) = 606 shares
        # Capital limit = 5,000,000 KRW -> floor(5,000,000 / 10010) = 499 shares
        # Final quantity should be min(606, 499) = 499 shares
        economics = {
            'risk_net_per_share': 330.0,
            'buy_outlay': 10010.0
        }
        passed, reason, data = self.sizer.evaluate_stop_and_size(
            entry=10000,
            structural_stop=9700,
            bars=bars,
            equity=10000000,
            capital_limit=5000000,
            economics=economics
        )
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(data['quantity'], 499)
        self.assertEqual(data['shares_by_risk'], 606)
        self.assertEqual(data['shares_by_cap'], 499)

    def test_zero_quantity_rejected(self):
        bars = [
            make_mock_bar("09:01:00", 10000, 10050, 9950, 10000, 5000),
            make_mock_bar("09:02:00", 10000, 10060, 9960, 10020, 6000),
        ]
        economics = {
            'risk_net_per_share': 330.0,
            'buy_outlay': 10010.0
        }
        # Capital limit is 5,000 KRW, but 1 share costs 10,010 KRW -> shares_by_cap = 0
        passed, reason, data = self.sizer.evaluate_stop_and_size(
            entry=10000,
            structural_stop=9700,
            bars=bars,
            equity=10000000,
            capital_limit=5000,
            economics=economics
        )
        self.assertFalse(passed)
        self.assertEqual(reason, 'RISK_ZERO_QUANTITY')
        self.assertEqual(data['quantity'], 0)


class TestClassicsBreakoutStrategy(unittest.TestCase):
    def setUp(self):
        self.strat = ClassicsBreakoutStrategy(min_vol_ratio=1.5, max_chase_pct=0.025, lookback_bars=6)

    def test_breakout_volume_surge_and_chase(self):
        # 5 baseline bars with average volume 1,000
        bars = [
            make_mock_bar(f"09:0{i}:00", 10000, 10100, 9950, 10050, 1000) for i in range(1, 6)
        ]
        # Bar 6: Breakout above 10,100 pivot with volume 2,500 (2.5x > 1.5x)
        bars.append(make_mock_bar("09:06:00", 10050, 10150, 10020, 10150, 2500))

        # Price 10,120 (above pivot 10,100, chase 0.2%) -> Pass
        passed, reason, data = self.strat.evaluate(bars, price=10120, ask=10130)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(data['pivot'], 10100)
        self.assertGreater(data['vol_ratio'], 1.5)

        # Chasing too far: Price 10,400 (chase 2.97% > 2.5%) -> Fail
        passed, reason, _ = self.strat.evaluate(bars, price=10400, ask=10410)
        self.assertFalse(passed)
        self.assertEqual(reason, 'CHASING_EXTENDED_BREAKOUT')


class TestClassicsRoleReversalStrategy(unittest.TestCase):
    def setUp(self):
        self.strat = ClassicsRoleReversalStrategy(tolerance_pct=0.005, min_impulse_rise=0.008, max_pullback_vol_ratio=0.85)

    def test_role_reversal_retest_and_bounce(self):
        # 1. Base bars with resistance R at 10,000
        bars = [
            make_mock_bar("09:01:00", 9900, 9980, 9880, 9950, 2000),
            make_mock_bar("09:02:00", 9950, 10000, 9920, 9980, 2500),
            make_mock_bar("09:03:00", 9970, 9990, 9910, 9960, 2100),
            make_mock_bar("09:04:00", 9960, 10000, 9940, 9990, 2200),
        ]
        # 2. Breakout and impulse peak to 10,150 (impulse rise 1.5% > 0.8%) on volume 6,000
        bars.extend([
            make_mock_bar("09:05:00", 9990, 10100, 9980, 10080, 5000),
            make_mock_bar("09:06:00", 10080, 10150, 10060, 10140, 6000),
        ])
        # 3. Pullback to test broken resistance R (10,000). Pullback low 10,010 (within 0.5% tolerance) on low volume 1,500
        bars.extend([
            make_mock_bar("09:07:00", 10130, 10130, 10050, 10070, 1800),
            make_mock_bar("09:08:00", 10060, 10080, 10010, 10040, 1500),
        ])

        # Current price bouncing back to 10,060 (holding R 10,000)
        passed, reason, data = self.strat.evaluate(bars, price=10060, ask=10070)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(data['strategy'], 'CLASSICS_ROLE_REVERSAL')
        self.assertEqual(data['broken_resistance_r'], 10000)
        self.assertEqual(data['pullback_low'], 10010)
        self.assertGreater(data['target'], 10150)


class TestClassicsBoxExpansionStrategy(unittest.TestCase):
    def setUp(self):
        self.strat = ClassicsBoxExpansionStrategy(min_bars=5, atr_multiplier=2.0)

    def test_box_expansion_breakout(self):
        # 10 bars: first 5 bars have small range (ATR ~20 KRW)
        bars = [
            make_mock_bar(f"09:0{i}:00", 10000, 10020, 9990, 10010, 1000) for i in range(1, 6)
        ]
        # Next 5 bars form tight box between 10,000 and 10,050 (range 0.5%)
        bars.extend([
            make_mock_bar(f"09:{i:02d}:00", 10010, 10050, 10000, 10040, 1200) for i in range(6, 11)
        ])

        # Breakout price 10,060 (above box top 10,050)
        passed, reason, data = self.strat.evaluate(bars, price=10060, ask=10070)
        self.assertTrue(passed)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(data['strategy'], 'CLASSICS_BOX_EXPANSION')
        self.assertEqual(data['box_top'], 10050)
        self.assertEqual(data['box_low'], 10000)

    def test_box_range_too_wide_rejected(self):
        # 10 bars where last 5 bars are wildly volatile (range 4% > allowed range)
        bars = [
            make_mock_bar(f"09:0{i}:00", 10000, 10020, 9990, 10010, 1000) for i in range(1, 6)
        ]
        bars.extend([
            make_mock_bar(f"09:{i:02d}:00", 10000, 10450, 9950, 10300, 1500) for i in range(6, 11)
        ])

        passed, reason, _ = self.strat.evaluate(bars, price=10460, ask=10470)
        self.assertFalse(passed)
        self.assertEqual(reason, 'BOX_RANGE_TOO_WIDE')


class TestClassicsStrategyEngine(unittest.TestCase):
    def setUp(self):
        self.engine = ClassicsStrategyEngine(equity=10000000, capital_limit=1000000)

    def test_full_pipeline_signal_generation(self):
        # Construct valid role reversal setup
        bars = [
            make_mock_bar("09:01:00", 9900, 9980, 9880, 9950, 2000),
            make_mock_bar("09:02:00", 9950, 10000, 9920, 9980, 2500),
            make_mock_bar("09:03:00", 9970, 9990, 9910, 9960, 2100),
            make_mock_bar("09:04:00", 9960, 10000, 9940, 9990, 2200),
            make_mock_bar("09:05:00", 9990, 10120, 9980, 10100, 5000),
            make_mock_bar("09:06:00", 10100, 10180, 10080, 10160, 6000),
            make_mock_bar("09:07:00", 10150, 10150, 10060, 10080, 1800),
            make_mock_bar("09:08:00", 10070, 10090, 10010, 10050, 1500),
        ]

        signal, reason, data = self.engine.evaluate_candidate(
            symbol='000660',
            price=10070,
            bid=10060,
            ask=10070,
            bars=bars,
            watch={'name': 'SK하이닉스'}
        )

        self.assertIsNotNone(signal)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(signal['strategy'], 'CLASSICS_ROLE_REVERSAL')
        self.assertGreater(signal['quantity'], 0)
        self.assertGreaterEqual(signal['net_rr'], 1.5)
        self.assertLess(signal['stop'], signal['entry'])
        self.assertGreater(signal['target'], signal['entry'])


if __name__ == '__main__':
    unittest.main()
