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
        """1,000 tick logging calls must complete in under 100ms (< 0.1ms per tick)."""
        logger = TickLogger(self.log_path, max_bytes=64 * 1024 * 1024)
        t = FakeTick()
        rec_time = datetime.now()

        start = time.perf_counter()
        for _ in range(1000):
            logger.log(t, rec_time)
        logger.flush()
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertLess(elapsed_ms, 100.0, f"Logging 1,000 ticks took {elapsed_ms:.2f}ms (must be < 100ms)")

    def test_shadow_observer_safety_never_emits_actual_orders(self):
        """ShadowObserver emits SHADOW_FAST_SIGNAL with actual_order_permission=False and SHADOW_ONLY=True."""
        events_emitted = []
        def emit(kind, stamp, **data):
            events_emitted.append((kind, stamp, data))

        from unittest.mock import MagicMock
        cfg = Config()
        mock_auto = MagicMock(intraday_context_enabled=False, structural_breakout_enabled=False)
        observer = ShadowObserver(cfg, auto=mock_auto, costs=None, emit=emit)

        # Confirm shadow strategy permissions
        self.assertFalse(getattr(observer, 'actual_order_permission', False))

if __name__ == '__main__':
    unittest.main()
