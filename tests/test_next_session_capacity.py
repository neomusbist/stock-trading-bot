"""Optional actual-ledger fixture stays in a temporary offline engine only."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from automation.engine import AutonomousEngine
from automation.policy import Policy, load_automation
from automation.readiness import authorization_valid
from core.settings import KST, load_config
from tests.test_live_ready_20261021 import FakeBroker


@unittest.skipUnless(os.environ.get('R6_CAPACITY_FIXTURE_ROOT'), 'private saved fixture required')
class NextSessionCapacityTests(unittest.TestCase):
    def setUp(self):
        source = Path(os.environ['R6_CAPACITY_FIXTURE_ROOT'])
        self.cfg = load_config(source / 'config.json')
        self.auto = load_automation(source / 'automation.json')
        self.original = json.loads((source / 'runtime/state.json').read_text(encoding='utf-8-sig'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.book = self.root / 'book'
        self.book.mkdir()
        shutil.copy2(source / 'runtime/state.json', self.book / 'state.json')
        self.next_at = datetime.fromisoformat(self.original['day']).replace(tzinfo=KST) + timedelta(days=1, hours=8, minutes=30)
        self.source = source

    def test_saved_ledger_rolls_one_day_and_preserves_cash_history_and_external_holdings(self):
        broker = FakeBroker()
        broker.identity = self.original['identity']
        policy = Policy(self.root / 'runtime/automation_policy.json')
        engine = AutonomousEngine(self.cfg, self.auto, policy, self.book, broker, lambda: self.next_at)
        engine.console.emit = lambda *a: None
        self.assertEqual(engine.s['day'], self.next_at.date().isoformat())
        self.assertEqual(engine.s['entries'], {})
        self.assertEqual(engine.s['daily_loss'], 0)
        self.assertEqual(engine.s['trades'], self.original['trades'])
        self.assertEqual(engine.s['automation']['existing'], self.original['automation']['existing'])
        self.assertEqual(engine.s['cash'], self.original['cash'])
        self.assertEqual(engine.s['positions'], {})
        self.assertEqual(engine.active_orders(), [])
        self.assertFalse(engine.account_ok)
        self.assertEqual(broker.orders, [])
        self.assertEqual((engine.c.risk_fraction, engine.c.max_daily_loss_fraction), (.02, .04))

    def test_previous_day_authorization_is_rejected_for_next_session(self):
        target = self.root / 'runtime'
        target.mkdir(exist_ok=True)
        shutil.copy2(self.source / 'runtime/operator_user_authorization.json', target / 'operator_user_authorization.json')
        policy = json.loads((self.source / 'runtime/automation_policy.json').read_text(encoding='utf-8-sig'))
        ok, reasons = authorization_valid(self.root, self.cfg, self.auto, policy, self.next_at)
        self.assertFalse(ok)
        self.assertIn('DAILY_LIVE_APPROVAL_EXPIRED', reasons)


if __name__ == '__main__':
    unittest.main()
