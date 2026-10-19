"""A late consumer may retain history, but cannot backdate executable signals."""
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import patch
import unittest

from automation.entry_frequency_experiments import replay_variants
from automation.opening_frequency_research import OpeningPrint
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from core.settings import Config,KST
from core.strategy import Tick

AT=datetime(2026,10,19,9,1,tzinfo=KST)


class FrequencyTimingReplayTests(unittest.TestCase):
    def tick(self):return Tick('999990',AT,1000,10,10,1000,999,received_at=AT.isoformat())

    def test_timely_received_but_late_processed_print_is_history_only(self):
        sample=OpeningPrint(self.tick(),AT,{},'REAL_WS',AT+timedelta(seconds=6))
        with patch.object(PatternDetector,'signal',side_effect=AssertionError('NO_BACKDATED_EVALUATION')):
            result=replay_variants([sample],Config(),AutomationConfig(),AT+timedelta(seconds=6))
        self.assertTrue(all(v['accepted_prints']==1 and v['execution_stale_prints']==1 and v['detector_episodes']==0 for v in result['variants']))

    def test_unprocessed_at_cutoff_print_is_not_available_to_any_variant(self):
        sample=OpeningPrint(self.tick(),AT,{},'REAL_WS',AT+timedelta(seconds=2))
        result=replay_variants([sample],Config(),AutomationConfig(),AT+timedelta(seconds=1))
        self.assertTrue(all(v['accepted_prints']==0 for v in result['variants']))

    def test_old_process_counter_does_not_pretend_to_be_a_current_clock_trace(self):
        tick=replace(self.tick(),received_monotonic=1.)
        result=replay_variants([OpeningPrint(tick,AT,{},'REAL_WS',AT)],Config(),AutomationConfig(),AT)
        self.assertTrue(all(v['accepted_prints']==1 and v['execution_stale_prints']==0 for v in result['variants']))
        self.assertIsNone(result['variants'][0]['actual_trades'])
        self.assertFalse(result['orders_enabled'])
