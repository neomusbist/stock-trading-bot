"""
Unit tests for automated storage governance, log pruning, and disk limit defense.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.storage_governor import (
    prune_archive_dir,
    prune_shadow_ticks,
    truncate_console_log,
    run_storage_governance,
    get_free_disk_gb
)


class StorageGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prune_archive_dir_retention(self):
        """Archive pruning retains at most 2 files per prefix and enforces size ceiling."""
        archive = self.runtime / 'archive'
        archive.mkdir()

        # Create 5 rotated events.jsonl files with recent timestamps (1h to 5h ago)
        now_ts = time.time()
        for i in range(5):
            f = archive / f'events.jsonl.2026101{i}-100000.txt'
            f.write_text('X' * 1024, encoding='utf-8')
            # Stagger modification times within today
            os.utime(f, (now_ts - (i + 1) * 3600, now_ts - (i + 1) * 3600))

        # Create 4 rotated radar_history.jsonl files
        for i in range(4):
            f = archive / f'radar_history.jsonl.2026101{i}-100000.txt'
            f.write_text('Y' * 1024, encoding='utf-8')
            os.utime(f, (now_ts - (i + 1) * 3600, now_ts - (i + 1) * 3600))

        removed, freed = prune_archive_dir(archive, max_bytes=10 * 1024 * 1024)
        # Should retain 2 events and 2 radar files = 4 remaining; 5 removed
        remaining = list(archive.iterdir())
        self.assertEqual(len(remaining), 4)
        self.assertEqual(removed, 5)
        self.assertEqual(freed, 5 * 1024)

    def test_prune_shadow_ticks_exceeding_rotations(self):
        """Prunes shadow_ticks.jsonl.* files exceeding max_rotations."""
        (self.runtime / 'shadow_ticks.jsonl').write_text('active', encoding='utf-8')
        (self.runtime / 'shadow_ticks.jsonl.1').write_text('rot1', encoding='utf-8')
        (self.runtime / 'shadow_ticks.jsonl.2').write_text('rot2', encoding='utf-8')
        (self.runtime / 'shadow_ticks.jsonl.3').write_text('rot3', encoding='utf-8')
        (self.runtime / 'shadow_ticks.jsonl.4').write_text('rot4', encoding='utf-8')

        removed, freed = prune_shadow_ticks(self.runtime, max_rotations=2)
        self.assertEqual(removed, 2)  # .3 and .4 removed
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl').exists())
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl.1').exists())
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl.2').exists())
        self.assertFalse((self.runtime / 'shadow_ticks.jsonl.3').exists())
        self.assertFalse((self.runtime / 'shadow_ticks.jsonl.4').exists())

    def test_truncate_console_log(self):
        """Truncates operator_console.txt when it exceeds max_bytes while preserving tail."""
        log = self.runtime / 'operator_console.txt'
        # Write 200 KB
        log.write_text('Line\n' * 20000, encoding='utf-8')
        initial_sz = log.stat().st_size
        self.assertGreater(initial_sz, 100 * 1024)

        freed = truncate_console_log(log, max_bytes=50 * 1024, keep_bytes=10 * 1024)
        self.assertGreater(freed, 0)
        self.assertLess(log.stat().st_size, 20 * 1024)
        content = log.read_text(encoding='utf-8')
        self.assertIn('[StorageGovernor]', content)

    def test_run_storage_governance_end_to_end(self):
        """End-to-end governance execution cleans archives, ticks, console, and reports metrics."""
        arch = self.runtime / 'archive'
        arch.mkdir()
        for i in range(5):
            f = arch / f'events.jsonl.test{i}.txt'
            f.write_text('A' * 500, encoding='utf-8')
            os.utime(f, (1000 + i, 1000 + i))

        for i in range(1, 5):
            (self.runtime / f'shadow_ticks.jsonl.{i}').write_text('T' * 100, encoding='utf-8')

        comp = self.runtime / 'archive_compressed'
        comp.mkdir()
        (comp / 'archive_20261008.zip').write_bytes(b'dummy_zip')

        res = run_storage_governance(self.root)
        self.assertGreater(res['files_removed'], 0)
        self.assertGreater(res['bytes_freed'], 0)
        self.assertFalse((comp / 'archive_20261008.zip').exists())


if __name__ == '__main__':
    unittest.main()
