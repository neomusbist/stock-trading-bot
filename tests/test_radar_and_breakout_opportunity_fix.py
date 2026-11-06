"""
Unit test verifying Radar observation structure deferral for emerging intraday movers.
Ensures that:
1. Real-time ranking candidates with combined resistance/turnover hurdles are admitted to WATCH for A/C breakout observation.
2. Genuine structural breakdown hurdles (e.g. falling below 5-day low) remain strictly REJECT.
3. Minimum net RR 1.5, account risk budget, and HARD_LOCK remain inviolate.
"""
import unittest
from datetime import date
from core.settings import Config
from automation.radar import observation_structure
from tests.test_v1 import bars

class RadarOpportunityFixTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.day = date(2026, 9, 30)

    def test_single_resistance_room_defers_to_setup_ac(self):
        data = bars()
        data[-30]['high'] = 10100
        res = observation_structure(data, 10000, self.cfg, self.day)
        self.assertEqual(res['status'], 'ELIGIBLE')
        self.assertEqual(res['setup_allowlist'], ['A', 'C'])
        self.assertEqual(res['compression_deferred'], 'RESISTANCE_ROOM_TO_SETUP')

    def test_combined_resistance_and_historical_turnover_defers_to_setup_ac(self):
        # A stock with both near resistance and low 20-day historical turnover (emerging intraday mover)
        data = bars()
        data[-30]['high'] = 10100
        # Reduce volume on all bars so turnover is < 1,000,000,000 KRW
        for b in data:
            b['volume'] = 100
        res = observation_structure(data, 10000, self.cfg, self.day)
        self.assertEqual(res['status'], 'ELIGIBLE')
        self.assertEqual(res['setup_allowlist'], ['A', 'C'])
        self.assertEqual(res['compression_deferred'], 'RESISTANCE_ROOM_TO_SETUP')

    def test_historical_turnover_only_defers_to_intraday_setup_liquidity(self):
        # Price is healthy above support, low 20-day historical volume, no resistance within room
        data = bars()
        for b in data:
            b['volume'] = 100
        # 10200 is above 5d support (9800) and no close resistance
        res = observation_structure(data, 10200, self.cfg, self.day)
        self.assertEqual(res['status'], 'ELIGIBLE')
        self.assertEqual(res['setup_allowlist'], ['A', 'B', 'C'])

    def test_structural_breakdown_remains_strictly_rejected(self):
        # Price 9800 is at or below recent 5-day support (breakdown)
        data = bars()
        data[-1]['volume'] = 0
        res = observation_structure(data, 9800, self.cfg, self.day)
        self.assertEqual(res['status'], 'REJECT')
        self.assertIsNone(res.get('setup_allowlist'))

    def test_heavy_overhead_remains_strictly_rejected(self):
        data = bars()
        for b in data:
            b['high'] = 10500
            b['low'] = 10050
            b['volume'] = 10_000_000
        res = observation_structure(data, 10000, self.cfg, self.day)
        self.assertEqual(res['status'], 'REJECT')
        self.assertIsNone(res.get('setup_allowlist'))


class FastBreakoutSupportFixTests(unittest.TestCase):
    def setUp(self):
        from core.settings import KST
        from automation.policy import AutomationConfig
        from automation.fast_momentum import FastMomentumStrategy
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(fast_momentum_enabled=True, min_stop_distance_fraction=0.012)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_double_bottom_equal_low_micro_tape_structure_passes(self):
        from datetime import datetime, timedelta
        from core.settings import KST
        from core.strategy import Tick
        from automation.fast_momentum import MicroTape
        tape = MicroTape()
        base = datetime(2026, 11, 5, 9, 30, 0, tzinfo=KST)
        prices = [1000, 1010, 1000, 1015]
        for i, p in enumerate(prices):
            t = Tick('000001', base + timedelta(seconds=i), float(p), 100, 1000 + i*100, float(p+5), float(p), True)
            tape.ingest(t, t.time)
        res = tape.structure(base + timedelta(seconds=10), exclude_decision=False)
        self.assertTrue(res['pass'])
        self.assertEqual(res['support'], 1000.0)
        self.assertEqual(res['breakout'], 1010.0)

    def test_early_breakout_with_compression_base_accepted(self):
        from datetime import datetime, timedelta
        from core.settings import KST
        from core.strategy import Tick
        base_time = datetime(2026, 11, 5, 9, 30, 0, tzinfo=KST)
        # 1000 -> 1005 -> 1000 -> 1005 -> 1010 (genuine base, 2 touches of 1000, breakout to 1010)
        prices = [1000, 1005, 1000, 1005]
        for i, p in enumerate(prices):
            t = Tick('008830', base_time + timedelta(seconds=i), float(p), 500, 10000 + i*500, float(p+5), float(p), True,
                     depth_available=True, book_time=base_time + timedelta(seconds=i), depth_source='0D',
                     ask_qty=1000, bid_qty=1000)
            self.strategy.ingest_tick(t, t.time)
        last_t = Tick('008830', base_time + timedelta(seconds=10), 1010.0, 500, 20000, 1010.0, 1005.0, True,
                      depth_available=True, book_time=base_time + timedelta(seconds=10), depth_source='0D',
                      ask_qty=1000, bid_qty=1000)
        m = self.strategy.get_tape('008830').metrics(last_t.time)
        watch = {
            'code': '008830', 'name': '대동기어', 'status': 'WATCH',
            'observed_at': base_time.isoformat(),
            'recent_high': 1070.0, 'daily_high_20d': 1070.0,
            'resistance_asof': base_time.isoformat()
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time,
            entry_mode='EARLY_BREAKOUT',
            regime_details={'entry_mode': 'EARLY_BREAKOUT', 'support': 1000.0, 'breakout': 1005.0}
        )
        self.assertTrue(geo['pass'], f"Expected PASS but got: {geo}")
        self.assertEqual(geo['structural_evidence']['basis'], 'EARLY_COMPRESSION_BASE')
        self.assertGreaterEqual(geo['net_rr'], 1.5)

    def test_early_breakout_monotonic_surge_remains_strictly_rejected(self):
        from datetime import datetime, timedelta
        from core.settings import KST
        from core.strategy import Tick
        base_time = datetime(2026, 11, 5, 9, 30, 0, tzinfo=KST)
        # Strictly monotonic rise: 984 -> 986 -> 988 -> 990 -> 992 -> 994 -> 996 -> 998 -> 1000 -> 1001
        prices = [984, 986, 988, 990, 992, 994, 996, 998, 1000]
        for i, p in enumerate(prices):
            t = Tick('008830', base_time + timedelta(seconds=i), float(p), 500, 10000 + i*500, float(p+2), float(p), True,
                     depth_available=True, book_time=base_time + timedelta(seconds=i), depth_source='0D',
                     ask_qty=1000, bid_qty=1000)
            self.strategy.ingest_tick(t, t.time)
        last_t = Tick('008830', base_time + timedelta(seconds=10), 1001.0, 500, 20000, 1002.0, 1000.0, True,
                      depth_available=True, book_time=base_time + timedelta(seconds=10), depth_source='0D',
                      ask_qty=1000, bid_qty=1000)
        m = self.strategy.get_tape('008830').metrics(last_t.time)
        watch = {
            'code': '008830', 'name': '대동기어', 'status': 'WATCH',
            'observed_at': base_time.isoformat(),
            'recent_high': 1070.0, 'daily_high_20d': 1070.0,
            'resistance_asof': base_time.isoformat()
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time,
            entry_mode='EARLY_BREAKOUT',
            regime_details={'entry_mode': 'EARLY_BREAKOUT', 'support': 984.0, 'breakout': 1000.0}
        )
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'CONFIRMED_MICRO_SUPPORT_UNKNOWN')

if __name__ == '__main__':
    unittest.main()
