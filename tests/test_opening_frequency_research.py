"""Point-in-time opening controls: real received prints, no history warmup."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from automation.opening_frequency_research import OpeningPrint, opening_prerequisites, replay_opening
from automation.policy import AutomationConfig
from core.settings import Config, KST
from core.strategy import Tick


OPEN = datetime(2026, 10, 14, 9, 0, tzinfo=KST)
WATCH = {'status': 'WATCH', 'resistance': 20000, 'source_count': 2,
         'market_status': {'pass': True}, 'source_details': {'one': {}, 'two': {}}}


def stream(seconds=301, start_second=0):
    result = []
    for second in range(start_second, seconds):
        at = OPEN + timedelta(seconds=second)
        price = 10000 + 10 * (second // 60) + (second % 60 // 20) * 10
        tick = Tick('999990', at, price, 100, 100 * (second + 1), price, price - 10)
        result.append(OpeningPrint(tick, at, copy.deepcopy(WATCH), 'SIMULATED'))
    return result


class OpeningFrequencyResearchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = replace(AutomationConfig(), structural_breakout_enabled=True,
                            confirmed_invalidation_enabled=True)

    def replay(self, samples, seconds=300, **kwargs):
        return replay_opening(samples, self.cfg, self.auto,
                              OPEN + timedelta(seconds=seconds), **kwargs)

    def test_original_minimums_are_not_forced_trade_intervals(self):
        rules = opening_prerequisites(self.cfg, self.auto)
        self.assertEqual(rules['warmup_seconds'], 70)
        self.assertEqual([rules[x]['earliest_from_open_seconds'] for x in ('A', 'A_continuation', 'B', 'C')],
                         [120, 180, 240, 300])
        self.assertIsNone(rules['roundtrip_duration'])

    def test_two_actual_complete_bars_needed_even_after_seventy_seconds(self):
        result = self.replay(stream(121), 120)
        rows = {r['exchange_at']: r for r in result['frames']}
        before = rows[(OPEN + timedelta(seconds=119)).isoformat()]
        after = rows[(OPEN + timedelta(seconds=120)).isoformat()]
        self.assertEqual(before['completed_bars'], 1)
        self.assertEqual(before['warmup']['result'], 'FAIL')
        self.assertEqual(after['completed_bars'], 2)
        self.assertEqual(after['warmup']['result'], 'PASS')

    def test_first_late_partial_minute_is_not_counted_as_complete_proof(self):
        result = self.replay(stream(181, start_second=20), 180)
        at_120 = next(r for r in result['frames'] if r['exchange_at'] == (OPEN + timedelta(seconds=120)).isoformat())
        self.assertEqual(at_120['completed_bars'], 1)
        self.assertEqual(at_120['warmup']['result'], 'FAIL')
        self.assertEqual(result['frames'][-1]['completed_bars'], 2)

    def test_future_ticks_cannot_change_a_prefix(self):
        base = self.replay(stream(121), 120)
        future = self.replay(stream(301), 120)
        self.assertEqual(base, future)

    def test_short_tape_cannot_be_reported_as_a_complete_five_minute_prefix(self):
        result = self.replay(stream(121), 120)
        five = next(p for p in result['prefixes'] if p['minutes_from_open'] == 5)
        self.assertFalse(five['prefix_complete'])
        self.assertEqual(five['cutoff'], (OPEN + timedelta(minutes=2)).isoformat())

    def test_future_exchange_timestamp_is_rejected(self):
        tick = Tick('999990', OPEN + timedelta(seconds=1), 10000, 100, 100, 10000, 9990)
        result = self.replay([OpeningPrint(tick, OPEN, WATCH, 'SIMULATED')], 60)
        self.assertEqual(result['rejections']['FUTURE_TRADE_TIME'], 1)
        self.assertEqual(result['frames'], [])

    def test_delayed_receipt_does_not_retroactively_enter_earlier_prefix(self):
        data = stream(121)
        final = data[-1]
        data[-1] = replace(final, received_at=final.received_at + timedelta(seconds=2))
        result = self.replay(data, 122)
        at_120 = self.replay(data, 120)
        self.assertEqual(at_120['frames'][-1]['completed_bars'], 1)
        self.assertEqual(result['frames'][-1]['completed_bars'], 2)
        self.assertEqual(result['frames'][-1]['receipt_lag_seconds'], 2)

    def test_stale_and_out_of_order_trade_prints_retain_detector_safety(self):
        records = stream(3)
        records.append(replace(records[0], received_at=OPEN + timedelta(seconds=3)))
        records.append(replace(records[-2], received_at=OPEN + timedelta(seconds=10)))
        result = self.replay(records, 10)
        self.assertEqual(result['rejections']['DETECTOR_REJECTED_TRADE'], 2)

    def test_missing_minute_does_not_generate_fabricated_candles(self):
        records = stream(61) + stream(181, start_second=180)
        result = self.replay(records, 180, continuous_coverage_verified=True)
        self.assertEqual(result['frames'][-1]['completed_bars'], 0)
        self.assertEqual(result['frames'][-1]['warmup']['result'], 'FAIL')

    def test_preopen_and_previous_day_prints_are_not_warmup(self):
        original = stream(1)[0]
        records = [replace(original, tick=replace(original.tick, time=OPEN-timedelta(seconds=1)),
                           received_at=OPEN-timedelta(seconds=1)),
                   replace(original, tick=replace(original.tick, time=OPEN-timedelta(days=1)),
                           received_at=OPEN-timedelta(days=1))]
        result = self.replay(records, 1)
        self.assertEqual(result['frames'], [])
        self.assertEqual(result['rejections']['NOT_OPENING_REGULAR_PRINT'], 2)

    def test_provenance_and_awareness_cannot_be_silently_inferred(self):
        with self.assertRaises(ValueError):
            self.replay([replace(stream(1)[0], provenance='HISTORICAL_MINUTE')], 1)
        with self.assertRaises(ValueError):
            self.replay([replace(stream(1)[0], received_at=OPEN.replace(tzinfo=None))], 1)
        with self.assertRaises(ValueError):
            self.replay([], 1, continuous_coverage_verified='yes')

    def test_no_signal_or_mock_path_is_presented_as_a_roundtrip_or_expectancy(self):
        result = self.replay(stream())
        self.assertEqual(result['data_kind'], 'SIMULATED')
        self.assertFalse(result['orders_enabled'])
        self.assertFalse(result['profitability_proven'])
        self.assertTrue(all(p['actual_completed_roundtrips'] is None and p['net_expectancy'] is None
                            for p in result['prefixes']))
        self.assertTrue(all(r['fill_feasibility'] == 'UNKNOWN' for r in result['frames']))

    def test_replay_does_not_mutate_input_watch_or_operating_parameters(self):
        data = stream()
        before = copy.deepcopy(data)
        self.replay(data)
        self.assertEqual(data, before)
        self.assertEqual(self.cfg.min_stop_fraction, .002)
        self.assertEqual(self.auto.box_min_bars, 5)


if __name__ == '__main__':
    unittest.main()
