"""Synthetic lifecycle with the exact optional diagnostic writer enabled."""
import json
import threading
from unittest.mock import patch

from core import event_log
from tests.test_live_ready_20261021 import LiveReadyLifecycle as _Lifecycle


class AsyncDiagnosticLifecycle(_Lifecycle):
    def setUp(self):
        super().setUp()
        self.writer=event_log.AsyncDiagnosticEventWriter(self.e.folder/'events.jsonl')
        self.e.diagnostic_event_writer=self.writer
        self.expected_writer_failure=False
        self.addCleanup(self.close_writer)

    def close_writer(self):
        self.e.diagnostic_event_writer=None
        if self.expected_writer_failure:
            try:self.writer.close()
            except PermissionError:pass
        else:self.writer.close()

    def test_diagnostic_failure_during_fake_transport_preserves_accepted_order(self):
        original_submit=self.broker.submit
        original_append=event_log._append_line

        def fail_background(path,line,max_bytes):
            if threading.current_thread() is self.writer.thread:
                raise PermissionError('synthetic diagnostic-only failure')
            return original_append(path,line,max_bytes)

        def submit(*args,**kwargs):
            with patch.object(event_log,'_append_line',fail_background):
                self.writer.append({'time':self.clock.isoformat(),'event':'NO_TRADE'})
                self.writer.thread.join(2)
                with self.assertRaises(PermissionError):self.writer.check_error()
                self.expected_writer_failure=True
            return original_submit(*args,**kwargs)

        with patch.object(self.broker,'submit',submit):
            self.seed('B')
        orders=list(self.e.s['orders'].values())
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['status'],'ACCEPTED',orders[0])
        saved=json.loads(self.e.path.read_text(encoding='utf-8'))
        self.assertEqual(next(iter(saved['orders'].values()))['status'],'ACCEPTED')
        events=[json.loads(line) for line in (self.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        accepted=[row for row in events if row.get('event')=='ORDER_ACCEPTED']
        self.assertEqual(len(accepted),1)
        self.assertFalse(accepted[0]['diagnostic_async'])
