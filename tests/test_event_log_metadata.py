"""Lossless synchronous event appends with no redundant metadata operations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.event_log import append_jsonl


class EventLogMetadataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root/'events.jsonl'

    def test_existing_parent_does_not_require_mkdir_for_every_event(self):
        original = Path.mkdir
        calls = []

        def mkdir(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, 'mkdir', mkdir):
            for n in range(3):
                append_jsonl(self.path, {'sequence': n})
        self.assertEqual(calls, [], 'a normal append need not recreate an existing parent')
        self.assertEqual(len(self.path.read_text(encoding='utf-8').splitlines()), 3)

    def test_rotation_checks_size_once_per_event_instead_of_exists_then_stat(self):
        append_jsonl(self.path, {'sequence': 0})
        original = Path.stat
        calls = []

        def stat(path, *args, **kwargs):
            if path == self.path:
                calls.append(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, 'stat', stat):
            for n in range(1, 4):
                append_jsonl(self.path, {'sequence': n})
        self.assertEqual(len(calls), 3, 'rotation must still inspect actual size on every append')

    def test_unicode_default_and_event_order_preserve_exact_existing_bytes(self):
        rows = [{'event': 'ENTRY_EVAL', 'name': '한글 종목', 'qty': 0},
                {'event': 'ORDER_ACCEPTED', 'side': 'BUY', 'qty': 1,
                 'time': datetime(2026, 10, 22, 9, 30)},
                {'event': 'FILL', 'side': 'BUY', 'qty': 1}]
        for row in rows:
            append_jsonl(self.path, row, default=str)
        expected = ''.join(json.dumps(row, ensure_ascii=False, allow_nan=False,
                                      default=str) + os.linesep for row in rows)
        self.assertEqual(self.path.read_bytes(), expected.encode('utf-8'))

    def test_missing_parent_is_created_and_first_event_is_not_lost(self):
        path = self.root/'not-yet-created'/'nested'/'events.jsonl'
        append_jsonl(path, {'event': 'ORDER_INTENT', 'sequence': 1})
        append_jsonl(path, {'event': 'ORDER_ACCEPTED', 'sequence': 2})
        self.assertEqual([json.loads(row)['sequence'] for row in
                          path.read_text(encoding='utf-8').splitlines()], [1, 2])

    def test_rotation_remains_lossless_and_observes_external_growth(self):
        append_jsonl(self.path, {'sequence': 1}, max_bytes=1000)
        external = {'event': 'EXTERNAL_PROCESS', 'sequence': 2}
        with self.path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(external)+'\n')
        append_jsonl(self.path, {'sequence': 3}, max_bytes=1)
        archived = list((self.root/'archive').glob('events.jsonl.*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual([json.loads(row)['sequence'] for row in
                          archived[0].read_text(encoding='utf-8').splitlines()], [1, 2])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['sequence'], 3)

    def test_parallel_writers_keep_complete_lines_and_each_writer_fifo(self):
        def write(writer):
            for sequence in range(20):
                append_jsonl(self.path, {'writer': writer, 'sequence': sequence})

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(4)))
        rows = [json.loads(row) for row in self.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 80)
        self.assertEqual(len({(row['writer'], row['sequence']) for row in rows}), 80)
        for writer in range(4):
            self.assertEqual([row['sequence'] for row in rows if row['writer'] == writer],
                             list(range(20)))

    def test_permission_errors_and_nonfinite_data_are_not_hidden(self):
        with patch.object(Path, 'open', side_effect=PermissionError('blocked')):
            with self.assertRaises(PermissionError):
                append_jsonl(self.path, {'event': 'ORDER_INTENT'})
        with self.assertRaises(ValueError):
            append_jsonl(self.path, {'price': float('nan')})
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
