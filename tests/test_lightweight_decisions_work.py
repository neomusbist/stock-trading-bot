import json
import tempfile
import threading
from contextlib import nullcontext
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.lightweight_decisions import AsyncDailyDecisionLog, SampledResearchLog
from automation.observation_worker import SampledTickLogger
from automation.trade_review_capture import TradeReviewCapture
from operator_service import OperatorService

AT = datetime(2026, 11, 5, 17, tzinfo=timezone(timedelta(hours=9)))


class LightweightDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.time = AT

    def make(self, **kw):
        return AsyncDailyDecisionLog(self.root, clock=lambda: self.time, min_free_bytes=0, **kw)

    def test_producer_does_not_touch_files_or_disk(self):
        d = self.make()
        with patch('pathlib.Path.open', side_effect=AssertionError('disk on producer')), patch(
                'automation.lightweight_decisions.shutil.disk_usage', side_effect=AssertionError('disk on producer')):
            self.assertTrue(d.record({'decision': 'BUY', 'symbol': '389260'}, '389260'))
        self.assertFalse(list(self.root.iterdir()))
        d.flush()
        rows = (self.root/'core-decisions-20261105.jsonl').read_text(encoding='utf8').splitlines()
        self.assertEqual(json.loads(rows[0])['symbol'], '389260')

    def test_queue_budget_is_bounded_and_reports_omissions(self):
        d = self.make(max_queue_rows=1)
        self.assertTrue(d.record({'decision': 'BUY'}, '111111'))
        self.assertFalse(d.record({'decision': 'BUY'}, '222222'))
        self.assertEqual(d.stats()['queue_rows'], 1)
        self.assertEqual(d.stats()['omitted_rows'], 1)
        self.assertEqual(d.stats()['block_reason'], 'DIAGNOSTIC_QUEUE_LIMIT')

    def test_slow_disk_writer_does_not_hold_producer_lock(self):
        d = self.make()
        d.record({'decision': 'BUY', 'n': 1})
        entered, release = threading.Event(), threading.Event()
        original = Path.open
        def slow_open(path, *args, **kwargs):
            if path.name.startswith('core-decisions-') and args and args[0] == 'ab':
                entered.set()
                if not release.wait(3):
                    raise OSError('test writer timed out')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', slow_open):
            writer = threading.Thread(target=d.flush)
            writer.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(d.record({'decision': 'BUY', 'n': 2}))
                self.assertEqual(d.stats()['queue_rows'], 1)
            finally:
                release.set()
                writer.join(3)
            self.assertFalse(writer.is_alive())
        d.flush()
        self.assertEqual(d.stats()['written_rows'], 2)

    def test_daily_cap_persists_across_restart_and_rotates_without_deletion(self):
        d = self.make(max_file_bytes=40)
        d.record({'decision': 'BUY', 'n': 1});d.flush()
        first = self.root/'core-decisions-20261105.jsonl'
        before = first.read_bytes()
        restarted = self.make(max_file_bytes=40)
        restarted.record({'decision': 'BUY', 'n': 2});restarted.flush()
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(restarted.stats()['block_reason'], 'DIAGNOSTIC_DAILY_BYTE_CAP')
        self.time += timedelta(days=1)
        restarted.record({'decision': 'BUY', 'n': 3});restarted.flush()
        self.assertTrue((self.root/'core-decisions-20261106.jsonl').exists())
        self.assertEqual(first.read_bytes(), before)

    def test_total_cap_preserves_earlier_files_and_reports_saturation(self):
        d = self.make(max_file_bytes=1000, max_total_bytes=40)
        d.record({'decision': 'BUY', 'n': 1});d.flush()
        first = self.root/'core-decisions-20261105.jsonl'
        before = first.read_bytes()
        self.time += timedelta(days=1)
        d.record({'decision': 'BUY', 'n': 2});d.flush()
        self.assertEqual(d.stats()['block_reason'], 'DIAGNOSTIC_TOTAL_BYTE_CAP')
        self.assertEqual(first.read_bytes(), before)
        self.assertFalse((self.root/'core-decisions-20261106.jsonl').exists())

    def test_compact_diagnostic_continues_when_bulk_reserve_blocks(self):
        cap = TradeReviewCapture(self.root, clock=lambda: self.time, start=False,
                                 diagnostic_async=True, min_free_bytes=32*1024**3,
                                 diagnostic_min_free_bytes=5*1024**3)
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=29*1024**3)):
            self.assertFalse(cap.flush())
            self.assertTrue(cap.core_decisions.record({'decision': 'BUY', 'symbol': '111111'}, '111111'))
            cap.core_decisions.flush()
            self.assertTrue((self.root/'core-decisions-20261105.jsonl').exists())
            self.assertEqual(cap.core_decisions.stats()['written_rows'], 1)
            cap.close()

    def test_default_watch_is_bounded_memory_not_unlimited_raw_tape(self):
        cap = TradeReviewCapture(self.root, clock=lambda: self.time, start=False, min_free_bytes=0)
        try:
            self.assertFalse(cap.continuous_watch)
            self.assertEqual(cap.day_limit, 100*1024**2)
            self.assertEqual(cap.stats()['watch_capture_mode'], 'MEMORY_RING_ONLY')
            self.assertIn('ring_evictions', cap.stats())
        finally:
            cap.close()

    def test_low_disk_drops_only_diagnostic_and_counts_it(self):
        d = AsyncDailyDecisionLog(self.root, clock=lambda: self.time, min_free_bytes=5*1024**3)
        d.record({'decision': 'BUY'})
        with patch('shutil.disk_usage', return_value=SimpleNamespace(free=4*1024**3)):
            d.flush()
        self.assertEqual(d.stats()['omitted_rows'], 1)
        self.assertEqual(d.stats()['block_reason'], 'DIAGNOSTIC_FREE_SPACE_BELOW_SAFETY_LIMIT')
        self.assertFalse(list(self.root.iterdir()))

    def test_research_sampling_records_trade_and_quote_separately_with_explicit_gaps(self):
        d = SampledResearchLog(self.root, clock=lambda: self.time, min_free_bytes=0, sample_seconds=5)
        self.assertTrue(d.record({'event': 'TRADE', 'price': 100}, '111111'))
        self.assertTrue(d.record({'event': 'BOOK', 'bid': 99}, '111111'))
        self.assertFalse(d.record({'event': 'TRADE', 'price': 101}, '111111'))
        self.time += timedelta(seconds=5)
        self.assertTrue(d.record({'event': 'TRADE', 'price': 102}, '111111'))
        d.flush()
        rows = [json.loads(x) for x in (self.root/'research-sampled-20261105.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual([row['event'] for row in rows], ['TRADE', 'BOOK', 'TRADE'])
        self.assertEqual(d.stats()['sampled_out_rows'], 1)
        self.assertTrue(all(row['coverage'] == 'SAMPLED_NOT_CONTINUOUS' for row in rows))

    def test_start_never_auto_deletes_existing_raw_tapes(self):
        runtime = self.root/'runtime';runtime.mkdir()
        original = runtime/'shadow_ticks.jsonl.7';original.write_bytes(b'original evidence')
        service = OperatorService(root=self.root, clock=lambda: self.time)
        child = SimpleNamespace(poll=lambda: None)
        cfg = SimpleNamespace(after_enabled=True)
        with patch.object(service, 'running', return_value=False), patch.object(service, 'idle', return_value=nullcontext()), \
             patch.object(service, 'settings', return_value=(cfg, None, {})), \
             patch.object(service, 'process_info', return_value={'token': 'fixture-token', 'managed': True}), \
             patch('operator_service.control_supported', return_value=True), \
             patch('operator_service.legacy_test_release_enabled', return_value=False), \
             patch('operator_service.uuid.uuid4', return_value=SimpleNamespace(hex='fixture-token')), \
             patch('operator_service.resolve_python', return_value='test-only'), \
             patch('operator_service.subprocess.Popen', return_value=child), \
             patch('automation.storage_governor.run_storage_governance', side_effect=AssertionError('must not delete')):
            self.assertIs(service.launch_continuous(), child)
        self.assertEqual(original.read_bytes(), b'original evidence')

    def test_shadow_logger_preserves_old_file_and_labels_sampled_capture(self):
        old = self.root/'shadow_ticks.jsonl'
        old.write_text('existing-data\n', encoding='utf8')
        logger = SampledTickLogger(old, clock=lambda: self.time, min_free_bytes=0, sample_seconds=5)
        tick = SimpleNamespace(symbol='389260', time=self.time, price=100, volume=3, cumulative=10,
                               bid=99, ask=100, depth_available=False)
        logger.log(tick, self.time, processed=self.time, sequence=42)
        logger.log(tick, self.time, processed=self.time, sequence=43)
        logger.flush()
        self.assertEqual(old.read_text(encoding='utf8'), 'existing-data\n')
        rows = (self.root/'research-sampled-20261105.jsonl').read_text(encoding='utf8').splitlines()
        self.assertEqual(json.loads(rows[0])['transport_sequence'], 42)
        self.assertEqual(logger.stats()['coverage'], 'SAMPLED_NOT_CONTINUOUS')
        self.assertEqual(logger.stats()['rows_sampled_out'], 1)

if __name__ == '__main__':
    unittest.main()
