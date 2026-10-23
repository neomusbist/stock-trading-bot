"""Temporary files only: a fresh heartbeat alone is not a working session."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from research.evidence_collector import session_check
from core.settings import KST

AT = datetime(2026, 10, 26, 8, 50, tzinfo=KST)

class MondayPreflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put('config.json', {'max_watchlist': 12})
        self.put('automation.json', {'fast_momentum_enabled': False})
        self.put('runtime/automation_policy.json', {'mode': 'LIVE', 'live_authorized': True})
        self.put('runtime/operator_user_authorization.json', {'trading_day': '2026-10-26'})
        self.put('runtime/automation-production/heartbeat.json', {
            'time': AT.isoformat(), 'connected': True, 'pid': 999999,
            'readiness': {'state': 'PREOPEN', 'missing': []}})
        self.put('runtime/automation-production/trade-review/capture-status.json', {
            'at': AT.isoformat(), 'limits': {'day_bytes': 1024, 'total_bytes': 2048, 'min_free_bytes': 0},
            'capture_losses': 0, 'write_errors': 0})
        source = self.root/'automation/runner.py'; source.parent.mkdir(); source.write_bytes(b'# fixture\n')
        self.put('runtime/RELEASE_IDENTITY.json', {'release_id': 'TEST_OBSERVER', 'files': {
            'automation/runner.py': hashlib.sha256(source.read_bytes()).hexdigest()}})
    def put(self, relative, value):
        p=self.root/relative; p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(value),encoding='utf-8')
    def check(self): return session_check(self.root, AT, '2026-10-26')
    def test_changed_installed_source_is_not_ready(self):
        (self.root/'automation/runner.py').write_bytes(b'# changed\n')
        self.assertIn('RELEASE_HASH_MISMATCH', self.check()['problems'])
    def test_stop_is_a_named_start_prerequisite(self):
        self.put('runtime/automation_policy.json', {'mode':'STOP','live_authorized':True})
        self.assertIn('POLICY_NOT_LIVE', self.check()['live_start_prerequisites_missing'])
    def test_friday_receipt_is_not_monday_authorization(self):
        self.put('runtime/operator_user_authorization.json', {'trading_day':'2026-10-23'})
        self.assertIn('TARGET_DAY_APPROVAL_NOT_CONFIRMED', self.check()['live_start_prerequisites_missing'])
    def test_future_clock_is_not_fresh(self):
        h={'time':AT.replace(minute=51).isoformat(),'connected':True}
        self.put('runtime/automation-production/heartbeat.json',h)
        self.assertIn('NO_FRESH_TARGET_DAY_WORKER_HEARTBEAT',self.check()['problems'])
    def test_safety_options_and_hashes_are_reported_without_authority(self):
        d=self.check()
        self.assertEqual(d['release_integrity']['verified_files'],1)
        self.assertFalse(d['actual_order_permission'])
        self.assertEqual(d['gui_presence'],'NOT_INSPECTED_NOT_RECEIPT_PROOF')
    def test_check_does_not_modify_source(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.check()
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

if __name__=='__main__': unittest.main()
