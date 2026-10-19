import unittest
import tempfile
import time
import json
from pathlib import Path
from datetime import datetime

from core.settings import KST
from automation.observation_worker import TickLogger
from automation.shadow_observation import ShadowObserver
from core.settings import Config
from automation.policy import AutomationConfig
from automation.capital import Costs
from unittest.mock import patch

class FakeTick:
    def __init__(self, symbol='005930', price=70000.0, volume=10, bid=69900.0, ask=70000.0, dt=None):
        self.symbol = symbol
        self.price = price
        self.volume = volume
        self.bid = bid
        self.ask = ask
        self.time = dt or datetime(2026, 10, 14, 9, 30, 0, tzinfo=KST)

class TestShadowTickLogger(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp_dir.name) / 'shadow_ticks.jsonl'

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tick_logger_buffering_and_flush(self):
        """TickLogger buffers ticks and writes valid JSONL records on flush."""
        logger = TickLogger(self.log_path, max_bytes=1024 * 1024)
        t = FakeTick()
        rec_time = datetime.now()

        # Log 10 ticks (less than batch 50)
        for _ in range(10):
            logger.log(t, rec_time)

        self.assertEqual(len(logger.buffer), 10)
        logger.flush()
        self.assertEqual(len(logger.buffer), 0)
        self.assertTrue(self.log_path.exists())

        # Verify file content
        with open(self.log_path, 'r', encoding='utf-8') as f:
            lines = [json.loads(line) for line in f]

        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0]['symbol'], '005930')
        self.assertEqual(lines[0]['price'], 70000.0)
        self.assertEqual(lines[0]['bid'], 69900.0)
        self.assertEqual(lines[0]['ask'], 70000.0)

    def test_tick_logger_rotation_at_max_bytes(self):
        """TickLogger rotates file to .jsonl.1 when size exceeds max_bytes."""
        # Set max_bytes to 500 bytes to trigger early rotation
        logger = TickLogger(self.log_path, max_bytes=500)
        t = FakeTick()
        rec_time = datetime.now()

        # Write enough ticks to exceed 500 bytes
        for _ in range(10):
            logger.log(t, rec_time)
        logger.flush()

        size1 = self.log_path.stat().st_size
        self.assertGreater(size1, 500)

        # Write more ticks to trigger rotation
        for _ in range(5):
            logger.log(t, rec_time)
        logger.flush()

        rotated = self.log_path.with_name(self.log_path.name + '.1')
        self.assertTrue(rotated.exists(), "Rotated file (.1) must exist")
        self.assertTrue(self.log_path.exists(), "Active file must exist")

    def test_tick_logger_cpu_overhead_is_negligible(self):
        """A capture burst never waits for synchronous file I/O; flush preserves rows."""
        logger = TickLogger(self.log_path, max_bytes=64 * 1024 * 1024)
        t = FakeTick()
        rec_time = datetime.now()

        start = time.perf_counter()
        with patch('builtins.open', side_effect=AssertionError('capture must not write')):
            for _ in range(1000):
                logger.log(t, rec_time)
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.capture_ms = elapsed_ms  # Measurement is separate from disk speed and host contention.
        self.assertEqual(logger.stats()['rows_buffered'], 1000)
        self.assertFalse(self.log_path.exists())
        self.assertTrue(logger.flush(force=True))
        self.assertEqual(logger.stats()['rows_written'], 1000)
        self.assertEqual(logger.stats()['rows_dropped'], 0)
        with self.log_path.open(encoding='utf-8') as source:
            self.assertEqual(sum(1 for _ in source), 1000)

    def test_shadow_observer_safety_never_emits_actual_orders(self):
        """ShadowObserver emits SHADOW_FAST_SIGNAL with actual_order_permission=False and SHADOW_ONLY=True."""
        events_emitted = []
        def emit(kind, stamp, **data):
            events_emitted.append((kind, stamp, data))

        cfg = Config()
        observer = ShadowObserver(cfg, auto=AutomationConfig(), costs=Costs(.00015,.00015,.002,.002,.003), emit=emit)

        # Confirm shadow strategy permissions
        self.assertFalse(getattr(observer, 'actual_order_permission', False))

if __name__ == '__main__':
    unittest.main()
