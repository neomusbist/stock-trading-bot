"""Targeted tests for atomic_json file contention, retry recovery, and ledger preservation."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.util import atomic_json, read_json_retry


class AtomicJsonContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'

    def test_transient_winerror5_and_winerror32_recovers_within_retry_limit(self):
        """Simulate Windows ERROR_ACCESS_DENIED (5) and ERROR_SHARING_VIOLATION (32) intermittent locks."""
        original_replace = os.replace
        calls = []

        def failing_replace(src, dst):
            calls.append(len(calls))
            if len(calls) == 1:
                e = PermissionError(13, 'Access is denied', str(dst))
                e.winerror = 5
                raise e
            elif len(calls) in (2, 3):
                e = OSError(0, 'The process cannot access the file because it is being used by another process', str(dst))
                e.winerror = 32
                raise e
            return original_replace(src, dst)

        atomic_json(self.path, {'version': 1, 'positions': {}})
        with patch('core.util.os.replace', side_effect=failing_replace):
            atomic_json(self.path, {'version': 2, 'positions': {'092870': {'qty': 1}}})

        # Must recover on the 4th attempt
        self.assertEqual(len(calls), 4)
        saved = read_json_retry(self.path)
        self.assertEqual(saved['version'], 2)
        self.assertIn('092870', saved['positions'])
        # Temporary files must be completely cleaned up
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_unrecoverable_lock_cleans_up_tmp_and_preserves_original_file(self):
        """When file lock is permanent, atomic_json fails after bounded retries, leaving original file completely intact."""
        original_content = {'day': '2026-10-16', 'daily_loss': 1743.0, 'positions': {}}
        atomic_json(self.path, original_content)

        error = PermissionError(13, 'Access is denied', str(self.path))
        error.winerror = 5

        replace_attempts = []

        def always_fail(src, dst):
            replace_attempts.append(src)
            raise error

        with patch('core.util.os.replace', side_effect=always_fail), patch('core.util.time.sleep'):
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {'corrupted': True})

        # Exactly 8 attempts
        self.assertEqual(len(replace_attempts), 8)
        # Original content must be 100% intact and untouched
        restored = read_json_retry(self.path)
        self.assertEqual(restored, original_content)
        # No orphaned temp files
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_concurrent_reader_and_writer_stress(self):
        """Stress test concurrent reader and atomic writer on the same path."""
        atomic_json(self.path, {'count': 0, 'data': 'initial'})
        stop = threading.Event()
        errors = []

        def writer():
            try:
                for i in range(25):
                    atomic_json(self.path, {'count': i, 'data': 'x' * 5000})
                    time.sleep(0.005)
            except Exception as e:
                errors.append(('writer', e))
            finally:
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    val = read_json_retry(self.path)
                    self.assertIn('count', val)
                    self.assertIn('data', val)
                    time.sleep(0.003)
            except Exception as e:
                errors.append(('reader', e))

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        w.join(timeout=10)
        r.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
