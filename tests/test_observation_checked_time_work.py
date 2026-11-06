import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from automation.capital import Costs
from automation.observation_worker import ObservationWorker
from automation.policy import AutomationConfig
from core.settings import Config, KST


class StatusKnowledgeTimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.start = datetime(2026, 11, 5, 17, 42, 0, tzinfo=KST)
        self.rest = Mock()
        self.worker = ObservationWorker(Config(), AutomationConfig(),
            Costs(.00015, .00015, .002, .002, .003), Path(self.tmp.name), self.rest)
        self.worker.clock = lambda: self.start + timedelta(seconds=4)

    def row(self):
        return {'code': '389260', 'status': 'RAW', 'reference_price': 14000,
                'source_count': 1, 'affordability': {'entry_eligible': True}}

    def test_status_received_after_source_snapshot_is_not_false_unknown(self):
        checked = self.start + timedelta(seconds=3)
        self.rest.stock_status.return_value = {'pass': True, 'checked_at': checked.isoformat()}
        with patch.object(self.worker, 'daily_bars', return_value=[]), patch(
                'automation.observation_worker.fetch_bars', return_value=[]):
            self.worker.prepare([self.row()], self.start, {}, publish=False)
        self.assertEqual(self.worker.metadata['389260']['checked'], checked)
        self.assertTrue(self.worker.metadata['389260']['status']['pass'])

    def test_truly_future_status_is_not_admitted(self):
        checked = self.start + timedelta(seconds=30)
        self.rest.stock_status.return_value = {'pass': True, 'checked_at': checked.isoformat()}
        self.worker.prepare([self.row()], self.start, {}, publish=False)
        status = self.worker.metadata['389260']['status']
        self.assertFalse(status['pass'])
        self.assertEqual(status['reason_code'], 'MARKET_STATUS_CHECK_TIME_UNKNOWN')

if __name__ == '__main__':
    unittest.main()
