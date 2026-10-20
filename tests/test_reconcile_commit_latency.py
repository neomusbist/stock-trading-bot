"""One durable final reconcile checkpoint; critical intent/fill saves stay synchronous."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.engine import AutonomousEngine
from automation.policy import AutomationConfig, Policy
from core.settings import Config, KST
from tests.test_automation import FakeBroker


class ReconcileCommitTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        auto=replace(AutomationConfig(),costs_confirmed=True)
        self.engine=AutonomousEngine(cfg,auto,Policy(self.root/'policy.json'),self.root/'book',
            FakeBroker(),lambda:datetime(2026,10,20,9,25,tzinfo=KST))
        self.engine.console.emit=lambda *a:None
        self.snapshot={'cash':71822.,'equity':71822.,'holdings':{},'orders':[]}

    def test_quiet_reconcile_publishes_only_one_complete_durable_state(self):
        import core.engine as core
        real=core.atomic_json;writes=[]
        def metered(path,value,fsync=True):
            writes.append((deepcopy(value),fsync));return real(path,value,fsync=fsync)
        with patch('core.engine.atomic_json',side_effect=metered):
            self.engine.reconcile(self.snapshot)
        self.assertEqual(len(writes),1)
        self.assertTrue(writes[0][1])
        self.assertEqual(json.loads(self.engine.path.read_text(encoding='utf-8')),self.engine.s)
        self.assertEqual(writes[0][0]['cash'],self.engine.s['automation']['cash'])

    def test_failed_final_fsync_preserves_old_ledger_and_blocks_account(self):
        old=self.engine.path.read_bytes()
        with patch('core.util.os.fsync',side_effect=OSError('synthetic disk fault')):
            with self.assertRaises(OSError):self.engine.reconcile(self.snapshot)
        self.assertEqual(self.engine.path.read_bytes(),old)
        self.assertFalse(self.engine.account_ok)

    def test_core_reconcile_default_still_saves(self):
        from core.engine import Engine
        cfg=Config();engine=Engine(cfg,self.root/'core',FakeBroker(),self.engine.clock)
        with patch.object(engine,'save',wraps=engine.save) as saved:
            self.assertTrue(engine.reconcile(self.snapshot))
        saved.assert_called_once_with()


if __name__=='__main__':unittest.main()
