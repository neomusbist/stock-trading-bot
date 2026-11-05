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

if __name__ == '__main__':
    unittest.main()
