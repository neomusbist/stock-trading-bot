"""Diagnostic disk stalls must not run on the order/position thread."""
import json
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from automation import runner
from core.realtime import Feed
from core.settings import Config, KST


class DiagnosticSnapshotWriterTests(unittest.TestCase):
    def setUp(self):
        self.cls = getattr(runner, 'DiagnosticSnapshotWriter', None)
        self.assertIsNotNone(self.cls, 'diagnostic snapshots still write on the event loop')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.heartbeat = self.root/'heartbeat.json'
        self.summary = self.root/'candidate_summary.json'

    def make_writer(self, write=None):
        writer = self.cls((self.heartbeat, self.summary), write=write, retry_seconds=.01)
        self.addCleanup(writer.close, 1.)
        return writer

    def test_slow_disk_does_not_block_submit_or_mutable_snapshot(self):
        entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
        writes = []
        def slow(path, value, **kwargs):
            entered.set()
            if not release.wait(4): raise OSError('test barrier timed out')
            writes.append((path, value, kwargs))
        writer = self.make_writer(slow)
        self.addCleanup(release.set)
        value = {'time': '2026-10-22T09:35:00+09:00', 'readiness': {'asof': 'unchanged', 'gates': {'fresh': True}}}
        writer.submit(self.heartbeat, value)
        self.assertTrue(entered.wait(2))
        value['readiness']['gates']['fresh'] = False
        thread = threading.Thread(target=lambda: (writer.submit(self.summary, {'time': 'captured'}), submitted.set()))
        thread.start(); self.addCleanup(thread.join, 2.)
        self.assertTrue(submitted.wait(1), 'submit waited on a slow filesystem callback')
        release.set(); self.assertTrue(writer.close(2))
        saved = next(v for p, v, _ in writes if p == self.heartbeat)
        self.assertEqual(saved['time'], value['time'])
        self.assertEqual(saved['readiness']['asof'], 'unchanged')
        self.assertTrue(saved['readiness']['gates']['fresh'])

    def test_only_latest_pending_snapshot_for_each_of_two_paths_is_retained(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        def slow(path, value, **kwargs):
            if not writes:
                entered.set()
                if not release.wait(4): raise OSError('test barrier timed out')
            writes.append((path, value))
        writer = self.make_writer(slow); self.addCleanup(release.set)
        writer.submit(self.heartbeat, {'generation': 0}); self.assertTrue(entered.wait(2))
        for generation in range(1, 101):
            writer.submit(self.heartbeat, {'generation': generation})
            writer.submit(self.summary, {'generation': generation})
        self.assertEqual(writer.snapshot()['pending'], 2)
        release.set(); self.assertTrue(writer.close(2))
        self.assertLessEqual(len(writes), 3)
        latest = {p: value for p, value in writes}
        self.assertEqual(latest[self.heartbeat]['generation'], 100)
        self.assertEqual(latest[self.summary]['generation'], 100)

    def test_only_exact_diagnostic_paths_are_accepted(self):
        with self.assertRaises(ValueError): self.cls((self.root/'state.json', self.heartbeat))
        writer = self.make_writer(lambda *args, **kwargs: None)
        with self.assertRaises(ValueError): writer.submit(self.root/'state.json', {'cash': 1})

    def test_successful_close_drains_then_permits_terminal_stop_without_late_overwrite(self):
        writer = self.make_writer()
        writer.submit(self.heartbeat, {'mode': 'LIVE', 'time': 'original'})
        writer.submit(self.summary, {'count': 3})
        self.assertTrue(writer.close(2))
        runner.atomic_json(self.heartbeat, {'mode': 'STOP', 'time': 'terminal'}, fsync=False)
        self.assertFalse(writer.thread.is_alive())
        self.assertEqual(json.loads(self.heartbeat.read_text())['mode'], 'STOP')
        self.assertEqual(json.loads(self.summary.read_text())['count'], 3)
        with self.assertRaises(RuntimeError): writer.submit(self.heartbeat, {'mode': 'LIVE'})

    def test_close_timeout_forbids_competing_terminal_write(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        def slow(path, value, **kwargs):
            entered.set()
            if not release.wait(4): raise OSError('test barrier timed out')
            writes.append(value)
        writer = self.make_writer(slow); self.addCleanup(release.set)
        writer.submit(self.heartbeat, {'mode': 'LIVE', 'time': 'old_capture'})
        self.assertTrue(entered.wait(2))
        closed = writer.close(.01)
        self.assertFalse(closed)
        if closed: writes.append({'mode': 'STOP'})
        self.assertEqual(writes, [], 'terminal write raced an active diagnostic writer')
        release.set(); self.assertTrue(writer.close(2))
        self.assertEqual(writes, [{'mode': 'LIVE', 'time': 'old_capture'}])

    def test_write_error_is_handed_to_main_even_if_retry_succeeds(self):
        failed, retried = threading.Event(), threading.Event()
        calls = []
        def fail_once(path, value, **kwargs):
            calls.append(value)
            if len(calls) == 1:
                failed.set()
                raise PermissionError(13, 'blocked diagnostic path', str(path))
            retried.set()
        writer = self.make_writer(fail_once)
        writer.submit(self.heartbeat, {'time': 'never_rewritten'})
        self.assertTrue(failed.wait(2)); self.assertTrue(retried.wait(2))
        with self.assertRaises(PermissionError) as raised: writer.check_error()
        self.assertEqual(raised.exception.filename, str(self.heartbeat))
        writer.check_error()
        self.assertTrue(writer.close(2))
        self.assertEqual(calls[0], calls[-1])

    def test_non_io_serialization_error_is_not_silently_lost(self):
        writer = self.make_writer(lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('nonfinite diagnostic')))
        writer.submit(self.summary, {'bad': True})
        deadline = time.monotonic() + 2
        while writer.snapshot()['errors'] == 0 and time.monotonic() < deadline: time.sleep(.005)
        self.assertGreater(writer.snapshot()['errors'], 0)
        with self.assertRaises(OSError): writer.check_error()
        self.assertTrue(writer.close(2))

    def test_diagnostic_writes_always_preserve_fsync_false_and_never_touch_ledger(self):
        calls = []
        writer = self.make_writer(lambda path, value, **kwargs: calls.append((path, value, kwargs)))
        writer.submit(self.heartbeat, {'time': 'one'})
        writer.submit(self.summary, {'time': 'two'})
        self.assertTrue(writer.close(2))
        self.assertEqual({p for p, _, _ in calls}, {self.heartbeat, self.summary})
        self.assertTrue(all(kwargs == {'fsync': False} for _, _, kwargs in calls))

    def test_blocked_snapshot_preserves_real_feed_order_book_and_trade_fifo_progress(self):
        at = datetime(2026, 10, 22, 9, 35, tzinfo=KST)
        frames = [{'trnm': 'REAL', 'data': [{'type': kind}]} for kind in ('00', '0D', '0B')]
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                feed = Feed(SimpleNamespace(cfg=Config()), [])
                for frame in frames: feed.q.put_nowait((at, frame))
                entered, release, consumed = threading.Event(), threading.Event(), threading.Event()
                seen, errors = [], []
                def disk(path, value, **kwargs):
                    entered.set()
                    if not release.wait(4): raise OSError('test barrier timed out')
                writer = self.make_writer(disk) if asynchronous else None
                self.addCleanup(release.set)
                def main_thread():
                    try:
                        if writer: writer.submit(self.heartbeat, {'time': at.isoformat()})
                        else: disk(self.heartbeat, {'time': at.isoformat()}, fsync=False)
                        runner.drain_feed(feed, lambda received, frame, lag: seen.append(frame['data'][0]['type']),
                                          timeout=0, max_seconds=1., clock=lambda: at)
                        consumed.set()
                    except Exception as error: errors.append(error)
                thread = threading.Thread(target=main_thread)
                thread.start()
                self.assertTrue(entered.wait(2))
                try:
                    if asynchronous:
                        self.assertTrue(consumed.wait(1), 'diagnostic disk stalled the actual Feed.q consumer')
                        self.assertEqual(seen, ['00', '0D', '0B'])
                    else:
                        self.assertFalse(consumed.wait(.03), 'old direct-write comparison was not blocked')
                        self.assertEqual(seen, [])
                finally:
                    release.set(); thread.join(2)
                self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                self.assertEqual(seen, ['00', '0D', '0B'])
                if writer: self.assertTrue(writer.close(2))


if __name__ == '__main__': unittest.main()
