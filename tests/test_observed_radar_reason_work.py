"""Gate-only replay of a recorded rejection; this is not an order replay."""
import unittest
from datetime import date
from unittest.mock import patch
from automation.radar import observation_structure
from core.settings import Config


class ObservedRadarReasonTests(unittest.TestCase):
    def test_recorded_turnover_rejection_is_deferred_to_setup(self):
        # 2026-11-05 09:07:20 archived CANDIDATE_CYCLE_SUMMARY, 282720.
        # Original OHLCV and downstream WATCH/quote/account state are unknown.
        recorded = {'status': 'REJECT', 'reason': '거래대금 부족', 'resistance': 8480.0}
        self.assertNotEqual(recorded['reason'], '상단 저항 여유 부족')  # old branch stayed REJECT
        with patch('automation.radar.structure', return_value=dict(recorded)):
            result = observation_structure([], None, Config(), date(2026, 11, 5))
        self.assertEqual(result['status'], 'ELIGIBLE')
        self.assertEqual(result['setup_allowlist'], ['A', 'B', 'C'])

if __name__ == '__main__':
    unittest.main()
