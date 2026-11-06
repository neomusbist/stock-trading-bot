import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tools.reconcile_external_close import run

class RecoveryToolGuardTests(unittest.TestCase):
    def test_refuses_engine_from_different_root_before_broker_access(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('tools.reconcile_external_close.Rest') as rest:
                with self.assertRaisesRegex(ValueError,'INSTALLED_CODE_ROOT_MISMATCH'):
                    run(Path(d)/'other',Path(d)/'backup','999990','TESTORDER','TESTFILL',True)
                rest.assert_not_called()
    def test_refuses_backup_inside_target(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'BACKUP_MUST_BE_EXTERNAL'):
                run(d,Path(d)/'backup','999990','TESTORDER','TESTFILL',True)
