"""Large ledgers must not turn JSON fragments into blocking stream writes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.util import atomic_json


class LedgerWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.old = {'halt': 'kept', 'orders': {'pending': 'UNKNOWN'}}
        atomic_json(self.path, self.old)

    def test_fragment_write_latency_does_not_starve_control_budget(self):
        # A controlled per-write delay represents TextIO/encoding work, without
        # wall-clock sleeps or a flaky speed threshold. The production-shaped
        # nested ledger is intentionally larger than a trivial config file.
        ledger = {'trades': [{'symbol': '999990', 'signal': {'gates': {
            'risk': {'result': 'PASS', 'values': {'budget': 1427.91}},
            'quote': {'result': 'FAIL', 'values': {'reason': '시세 지연'}}}}}
            for _ in range(500)], 'orders': {}, 'positions': {}}
        writes = []
        real_fdopen = os.fdopen
        class Metered:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def __getattr__(self, name): return getattr(self.stream, name)
            def write(self, text): writes.append(len(text)); return self.stream.write(text)
        with patch('core.util.os.fdopen', side_effect=lambda *a, **k: Metered(real_fdopen(*a, **k))):
            atomic_json(self.path, ledger)
        simulated_write_work = len(writes) * .001
        self.assertLess(simulated_write_work, .05, 'JSON fragment writes exhaust the feed-drain budget')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')), ledger)

    def test_durable_complete_temp_precedes_atomic_publish(self):
        ledger = {'orders': {'o': {'status': 'INTENT'}}, '한글': '보호 유지'}
        fsync = os.fsync
        replace = os.replace
        synced = []
        def sync(fd):
            synced.append(fd)
            return fsync(fd)
        def publish(src, dst):
            self.assertTrue(synced)
            self.assertEqual(json.loads(Path(src).read_text(encoding='utf8')), ledger)
            self.assertEqual(json.loads(self.path.read_text(encoding='utf8')), self.old)
            return replace(src, dst)
        with patch('core.util.os.fsync', side_effect=sync), patch('core.util.os.replace', side_effect=publish):
            atomic_json(self.path, ledger)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')), ledger)

    def test_nonfinite_cannot_replace_previous_risk_state(self):
        with self.assertRaises(ValueError): atomic_json(self.path, {'risk': float('nan')})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')), self.old)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_fsync_failure_does_not_publish_or_erase_old_state(self):
        with patch('core.util.os.fsync', side_effect=OSError('disk fault')):
            with self.assertRaises(OSError): atomic_json(self.path, {'orders': {}})
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')), self.old)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_json_bytes_and_optional_fsync_contract_remain_identical(self):
        ledger = {'한국어': [1, 2.5, False, None, {'risk': 0.02}], 'daily': 0.04}
        with patch('core.util.os.fsync') as sync:
            atomic_json(self.path, ledger, fsync=False)
        sync.assert_not_called()
        self.assertEqual(self.path.read_text(encoding='utf8'), json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__': unittest.main()
