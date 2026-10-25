"""The external OS sampler must not lock operating status replacements."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.measure_runtime_budget import heartbeat_summary


class SharedStatusReader(unittest.TestCase):
    def test_all_live_status_files_use_delete_sharing_reader(self):
        seen=[]
        @contextmanager
        def reader(path):
            seen.append(path.name)
            yield BytesIO(b'{}')
        with patch('core.util.shared_binary_reader',reader), patch.object(Path,'read_bytes',side_effect=AssertionError('PINNING_READER')):
            summary=heartbeat_summary(Path('synthetic-root'))
        self.assertEqual(set(seen), {'heartbeat.json','operator_runner.json','shadow_snapshot.json','candidate_summary.json'})
        self.assertIsNone(summary['order_ready'])

    def test_unreadable_operating_state_remains_unknown(self):
        with patch('core.util.shared_binary_reader',side_effect=PermissionError('synthetic')):
            summary=heartbeat_summary(Path('synthetic-root'))
        self.assertIsNone(summary['connected'])
        self.assertIsNone(summary['pid'])
        self.assertEqual(summary['counts'],{})

    @unittest.skipUnless(os.name=='nt','Windows OS sampler')
    def test_standalone_cli_outside_repo_can_import_shared_reader(self):
        script=Path(__file__).resolve().parents[1]/'tools/measure_runtime_budget.py'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'synthetic-state';root.mkdir()
            out=Path(folder)/'measurement'
            result=subprocess.run([sys.executable,'-B',str(script),'--root',str(root),'--out',str(out),
                '--seconds','1','--interval','1'],cwd=folder,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((out/'resource_summary.json').is_file())


if __name__=='__main__':unittest.main()
