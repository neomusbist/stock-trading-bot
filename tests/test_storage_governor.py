"""
Unit tests for automated storage governance, 3-tier data lifecycle protection,
log pruning, and disk limit defense.
"""
import json
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
    get_free_disk_gb,
    is_protected_file,
    is_deletable_diagnostic_archive,
    check_disk_safety_gate,
    PROTECTED_PATTERNS
)


class TieredStorageGovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tier_a_protected_files_never_deleted_under_any_condition(self):
        """Tier A protected files (state, events.jsonl, trade-review, bundles, fixtures) are never deleted."""
        archive = self.runtime / 'archive'
        archive.mkdir()

        # Place Tier A protected files with timestamps from 60 days ago and huge byte sizes
        old_time = time.time() - 60 * 86400
        protected_filenames = [
            'state.json.bak_20260901',
            'events.jsonl.20260915-090000.1234.uuid',
            'trade-review-20261019-0001.jsonl',
            'operator_account.json.bak',
            'policy.json.bak',
            'RELEASE_IDENTITY.json',
            'R6_INCIDENT_RESOLVED.bundle',
            'TRADE_PRIVATE.json',
            'BROKER_PRIVATE.json',
            'CAPTURE_PRIVATE.jsonl',
            'EVENT_WINDOW_PRIVATE.jsonl',
            'lunit_exit_20261019.json',
            'bexel_incident_evidence.json'
        ]

        for fname in protected_filenames:
            f = archive / fname
            f.write_text('CRITICAL ACCOUNTING LEDGER DATA', encoding='utf-8')
            os.utime(f, (old_time, old_time))
            self.assertTrue(is_protected_file(f), f"Expected {fname} to be recognized as protected")

        # Run prune with zero tolerance (0 age, 0 max bytes)
        removed, freed, logs = prune_archive_dir(archive, max_bytes=0, max_age_days=0)
        self.assertEqual(removed, 0)
        self.assertEqual(freed, 0)

        # Verify EVERY single file is still intact
        for fname in protected_filenames:
            self.assertTrue((archive / fname).exists(), f"Protected file {fname} was deleted!")

    def test_tier_c_deletable_diagnostic_logs_pruned(self):
        """Tier C allowable diagnostic logs (console logs, debug outputs) are pruned when expired or over quota."""
        archive = self.runtime / 'archive'
        archive.mkdir()

        now_ts = time.time()
        # Create 5 expired operator_console.txt rotations (5 days old)
        for i in range(5):
            f = archive / f'operator_console.txt.2026101{i}-100000.txt'
            f.write_text('X' * 1024, encoding='utf-8')
            os.utime(f, (now_ts - 5 * 86400, now_ts - 5 * 86400))
            self.assertTrue(is_deletable_diagnostic_archive(f))

        # Create 2 recent operator_console.txt rotations (1 hour old)
        for i in range(2):
            f = archive / f'operator_console.txt.recent_{i}.txt'
            f.write_text('Y' * 1024, encoding='utf-8')
            os.utime(f, (now_ts - 3600, now_ts - 3600))
            self.assertTrue(is_deletable_diagnostic_archive(f))

        removed, freed, logs = prune_archive_dir(archive, max_bytes=10 * 1024 * 1024, max_age_days=3)
        self.assertEqual(removed, 5)
        self.assertEqual(freed, 5 * 1024)
        self.assertEqual(len(list(archive.iterdir())), 2)

    def test_unlisted_unknown_files_rejected_from_deletion(self):
        """Files not matching allowlist prefixes are rejected from deletion, even if old."""
        archive = self.runtime / 'archive'
        archive.mkdir()

        old_ts = time.time() - 30 * 86400
        unlisted_file = archive / 'unknown_strategy_notes.csv'
        unlisted_file.write_text('date,signal,price\n2026-10-19,A,10000', encoding='utf-8')
        os.utime(unlisted_file, (old_ts, old_ts))

        self.assertFalse(is_deletable_diagnostic_archive(unlisted_file))
        removed, freed, _ = prune_archive_dir(archive, max_bytes=0, max_age_days=0)
        self.assertEqual(removed, 0)
        self.assertTrue(unlisted_file.exists())

    def test_shadow_ticks_bounded_retention(self):
        """Active shadow ticks and recent rotations are preserved; older rotations exceeding max are pruned."""
        (self.runtime / 'shadow_ticks.jsonl').write_text('active ticks', encoding='utf-8')
        now_ts = time.time()

        # Rotations 1 and 2 are recent (within 1 day)
        for i in [1, 2]:
            f = self.runtime / f'shadow_ticks.jsonl.{i}'
            f.write_text(f'recent rot {i}', encoding='utf-8')
            os.utime(f, (now_ts - 3600, now_ts - 3600))

        # Rotations 3, 4, 5 are old (5 days old)
        for i in [3, 4, 5]:
            f = self.runtime / f'shadow_ticks.jsonl.{i}'
            f.write_text(f'old rot {i}', encoding='utf-8')
            os.utime(f, (now_ts - 5 * 86400, now_ts - 5 * 86400))

        removed, freed, logs = prune_shadow_ticks(self.runtime, max_rotations=3, min_age_days=3)
        # Should keep active + .1 + .2 + .3; prune .4 and .5
        self.assertEqual(removed, 2)
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl').exists())
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl.1').exists())
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl.2').exists())
        self.assertTrue((self.runtime / 'shadow_ticks.jsonl.3').exists())
        self.assertFalse((self.runtime / 'shadow_ticks.jsonl.4').exists())
        self.assertFalse((self.runtime / 'shadow_ticks.jsonl.5').exists())

    def test_truncate_console_log(self):
        """Truncates operator_console.txt when it exceeds max_bytes while preserving tail."""
        log = self.runtime / 'operator_console.txt'
        log.write_text('Log line content\n' * 5000, encoding='utf-8')
        initial_sz = log.stat().st_size
        self.assertGreater(initial_sz, 50 * 1024)

        freed = truncate_console_log(log, max_bytes=20 * 1024, keep_bytes=5 * 1024)
        self.assertGreater(freed, 0)
        self.assertLess(log.stat().st_size, 10 * 1024)
        content = log.read_text(encoding='utf-8')
        self.assertIn('[StorageGovernor]', content)

    def test_disk_safety_gate_actions(self):
        """Tests that disk safety gate accurately flags emergency and blocks orders at critical levels."""
        with patch('automation.storage_governor.get_free_disk_gb', return_value=50.0):
            gate = check_disk_safety_gate(self.root)
            self.assertFalse(gate['disk_emergency'])
            self.assertFalse(gate['disk_critical'])
            self.assertFalse(gate['order_blocked'])
            self.assertIsNone(gate['order_block_reason'])

        with patch('automation.storage_governor.get_free_disk_gb', return_value=25.0):
            gate = check_disk_safety_gate(self.root)
            self.assertTrue(gate['disk_emergency'])
            self.assertFalse(gate['disk_critical'])
            self.assertFalse(gate['order_blocked'])

        with patch('automation.storage_governor.get_free_disk_gb', return_value=3.5):
            gate = check_disk_safety_gate(self.root)
            self.assertTrue(gate['disk_emergency'])
            self.assertTrue(gate['disk_critical'])
            self.assertTrue(gate['order_blocked'])
            self.assertIn('CRITICAL_LOW_DISK_SPACE', gate['order_block_reason'])

    def test_lunit_fixture_preservation_and_integrity(self):
        """Verifies that the Lunit test fixture is intact and recognized as protected."""
        fixture_path = Path(__file__).parent / 'fixtures/lunit_exit_20261019.json'
        self.assertTrue(fixture_path.exists(), "Lunit exit fixture must exist")
        self.assertTrue(is_protected_file(fixture_path))

        data = json.loads(fixture_path.read_text(encoding='utf-8'))
        self.assertIn('ticks', data)
        self.assertIn('position', data)
        self.assertEqual(data['position']['symbol'], '328130')
        self.assertGreater(len(data['ticks']), 10)

    def test_state_and_settings_restoration(self):
        """Verifies state.json and accounting ledgers survive full governance pass untouched."""
        prod_dir = self.runtime / 'automation-production'
        prod_dir.mkdir(parents=True)
        state_file = prod_dir / 'state.json'
        state_file.write_text(json.dumps({
            'daily_loss': 128.0,
            'positions': {},
            'orders': {},
            'market_phase': 'CLOSED'
        }), encoding='utf-8')

    def test_shadow_ticks_same_day_rotations_preserved_regardless_of_count(self):
        """Even if there are 8 or more rotations on the same day, files within min_age_days are 100% preserved."""
        (self.runtime / 'shadow_ticks.jsonl').write_text('active ticks', encoding='utf-8')
        now_ts = time.time()

        # Create 8 rotations all generated today (within the last few hours)
        for i in range(1, 9):
            f = self.runtime / f'shadow_ticks.jsonl.{i}'
            f.write_text(f'today busy rotation {i}', encoding='utf-8')
            os.utime(f, (now_ts - i * 600, now_ts - i * 600))  # 10 to 80 minutes ago

        # Prune with max_rotations=3, min_age_days=3
        removed, freed, logs = prune_shadow_ticks(self.runtime, max_rotations=3, min_age_days=3)
        self.assertEqual(removed, 0, "No same-day rotations should be deleted even if count >= 8")
        self.assertEqual(freed, 0)
        for i in range(1, 9):
            self.assertTrue((self.runtime / f'shadow_ticks.jsonl.{i}').exists())

    def test_disk_4gb_blocks_live_buy_orders(self):
        """Free disk space of 4GB triggers CRITICAL_LOW_DISK_SPACE and blocks live BUY orders."""
        from automation.engine import AutonomousEngine
        from automation.policy import AutomationConfig, Policy
        from core.settings import Config
        from tests.test_automation import FakeBroker

        with patch('automation.storage_governor.get_free_disk_gb', return_value=4.0):
            gate = check_disk_safety_gate(self.root)
            self.assertTrue(gate['order_blocked'])
            self.assertIn('CRITICAL_LOW_DISK_SPACE', gate['order_block_reason'])

            # Verify in AutonomousEngine
            from datetime import datetime
            from core.settings import KST
            cfg = Config(live_mode=True)
            auto = AutomationConfig(costs_confirmed=True, AUTO_TRADING_CAP_KRW=100000)
            policy = Policy(self.root / 'policy.json')
            broker = FakeBroker()
            clock = lambda: datetime(2026, 10, 19, 10, 0, 0, tzinfo=KST)
            engine = AutonomousEngine(cfg, auto, policy, self.runtime, broker, clock)

            # Buy order allowed must return False
            self.assertFalse(engine.live_order_allowed('BUY', '005930', 1, 10000, {}))
            # New entry reason must return CRITICAL_LOW_DISK_SPACE
            self.assertEqual(engine.new_entry_reason('005930'), 'CRITICAL_LOW_DISK_SPACE')
            # Entry market reason must return CRITICAL_LOW_DISK_SPACE
            self.assertEqual(engine.entry_market_reason({'symbol': '005930'}, 1), 'CRITICAL_LOW_DISK_SPACE')

    def test_disk_inquiry_error_blocks_live_buy_orders(self):
        """When disk space query fails, capacity is treated as UNKNOWN and blocks live BUY orders."""
        from datetime import datetime
        from core.settings import KST
        from automation.engine import AutonomousEngine
        from automation.policy import AutomationConfig, Policy
        from core.settings import Config
        from tests.test_automation import FakeBroker

        with patch('shutil.disk_usage', side_effect=OSError("Drive query failed")):
            free_gb = get_free_disk_gb(self.root)
            self.assertIsNone(free_gb)

            gate = check_disk_safety_gate(self.root)
            self.assertTrue(gate['order_blocked'])
            self.assertIn('DISK_CAPACITY_UNKNOWN', gate['order_block_reason'])

            cfg = Config(live_mode=True)
            auto = AutomationConfig(costs_confirmed=True, AUTO_TRADING_CAP_KRW=100000)
            policy = Policy(self.root / 'policy.json')
            broker = FakeBroker()
            clock = lambda: datetime(2026, 10, 19, 10, 0, 0, tzinfo=KST)
            engine = AutonomousEngine(cfg, auto, policy, self.runtime, broker, clock)

            self.assertFalse(engine.live_order_allowed('BUY', '005930', 1, 10000, {}))

    def test_disk_4gb_allows_existing_position_exit_orders(self):
        """Critical disk space (< 5GB) must NOT block existing position protection or exit orders (SELL)."""
        from datetime import datetime
        from core.settings import KST
        from automation.engine import AutonomousEngine
        from automation.policy import AutomationConfig, Policy
        from core.settings import Config
        from tests.test_automation import FakeBroker

        with patch('automation.storage_governor.get_free_disk_gb', return_value=4.0):
            cfg = Config(live_mode=True)
            auto = AutomationConfig(costs_confirmed=True, AUTO_TRADING_CAP_KRW=100000)
            policy = Policy(self.root / 'policy.json')
            broker = FakeBroker()
            clock = lambda: datetime(2026, 10, 19, 10, 0, 0, tzinfo=KST)
            engine = AutonomousEngine(cfg, auto, policy, self.runtime, broker, clock)

            # SELL order must NOT be blocked by disk safety gate!
            self.assertTrue(engine.live_order_allowed('SELL', '069540', 2, 5000, {}))

    def test_lunit_trade_reproducibility_with_remaining_data(self):
        """Verifies that the 2026-10-19 Lunit trade and core decisions are reproducible with remaining data."""
        fixture_path = Path(__file__).parent / 'fixtures/lunit_exit_20261019.json'
        fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
        self.assertEqual(fixture['position']['symbol'], '328130')
        self.assertEqual(fixture['position']['avg'], 12890)
        self.assertEqual(fixture['position']['qty'], 5)
        self.assertEqual(fixture['position']['initial_stop'], 12840)
        self.assertEqual(fixture['position']['emergency'], 12820)
        self.assertGreater(len(fixture['ticks']), 60)


    def test_compress_file_with_verification_roundtrip_integrity(self):
        """Verifies 5-stage fail-safe compression roundtrip and bit-for-bit extraction."""
        from automation.storage_governor import compress_file_with_verification
        import zipfile
        test_file = self.runtime / 'events.jsonl.1'
        original_data = b'{"time":"2026-11-01T09:00:00","event":"TEST_ROW"}\n' * 500
        test_file.write_bytes(original_data)

        success, freed, msg = compress_file_with_verification(test_file)
        self.assertTrue(success)
        self.assertGreater(freed, 0)
        self.assertFalse(test_file.exists(), "Original uncompressed file should be removed after verified compression")

        zip_file = self.runtime / 'events.jsonl.1.zip'
        self.assertTrue(zip_file.exists())

        # Verify content from zip matches original byte-for-byte
        with zipfile.ZipFile(zip_file, 'r') as zf:
            extracted = zf.read('events.jsonl.1')
            self.assertEqual(extracted, original_data)

    def test_compress_failure_preserves_source_file(self):
        """If compression or verification fails, original source file is strictly preserved."""
        from automation.storage_governor import compress_file_with_verification
        test_file = self.runtime / 'events.jsonl.2'
        original_data = b'CRITICAL UNTOUCHABLE DATA'
        test_file.write_bytes(original_data)

        # Patch zipfile.ZipFile.testzip to simulate corruption
        with patch('zipfile.ZipFile.testzip', return_value='corrupted_member'):
            success, freed, msg = compress_file_with_verification(test_file)
            self.assertFalse(success)
            self.assertEqual(freed, 0)
            self.assertTrue(test_file.exists(), "Source file MUST NOT be deleted when verification fails!")
            self.assertEqual(test_file.read_bytes(), original_data)
            # Ensure no orphan tmp zip is left behind
            tmp_zip = self.runtime / 'events.jsonl.2.zip.tmp'
            self.assertFalse(tmp_zip.exists())

    def test_compress_historical_archives_protects_active_and_recent(self):
        """Active files and files younger than min_age_days are NEVER compressed."""
        from automation.storage_governor import compress_historical_archives
        archive = self.runtime / 'archive'
        archive.mkdir(parents=True, exist_ok=True)
        trade_review = self.runtime / 'automation-production' / 'trade-review'
        trade_review.mkdir(parents=True, exist_ok=True)

        now_ts = time.time()
        # Active file in archive: events.jsonl
        active_events = archive / 'events.jsonl'
        active_events.write_text('active events', encoding='utf-8')
        os.utime(active_events, (now_ts - 5 * 86400, now_ts - 5 * 86400))

        # Recent rotated file (< 2 days old)
        recent_rotation = archive / 'events.jsonl.recent'
        recent_rotation.write_text('recent rotation', encoding='utf-8')
        os.utime(recent_rotation, (now_ts - 3600, now_ts - 3600))

        # Old rotated file (> 2 days old)
        old_rotation = archive / 'events.jsonl.old'
        old_rotation.write_text('old rotated ledger ' * 100, encoding='utf-8')
        os.utime(old_rotation, (now_ts - 4 * 86400, now_ts - 4 * 86400))

        # Old trade review file (> 2 days old)
        old_review = trade_review / 'trade-review-2026-10-15.jsonl'
        old_review.write_text('old trade review ' * 100, encoding='utf-8')
        os.utime(old_review, (now_ts - 10 * 86400, now_ts - 10 * 86400))

        n_comp, b_freed, logs = compress_historical_archives(self.runtime, min_age_days=2)
        self.assertEqual(n_comp, 2, "Only the 2 old rotated files should be compressed")
        self.assertTrue(active_events.exists(), "Active events.jsonl MUST NOT be compressed!")
        self.assertTrue(recent_rotation.exists(), "Recent rotation (< 2 days) MUST NOT be compressed!")
        self.assertFalse(old_rotation.exists(), "Old rotation should be compressed and removed")
        self.assertTrue((archive / 'events.jsonl.old.zip').exists())
        self.assertFalse(old_review.exists(), "Old trade review should be compressed and removed")
        self.assertTrue((trade_review / 'trade-review-2026-10-15.jsonl.zip').exists())


if __name__ == '__main__':
    unittest.main()
