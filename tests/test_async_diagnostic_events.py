"""Only nonsignal diagnostics may leave the critical synchronous event path."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import event_log
from core.engine import Engine
from core.settings import KST
from core.settings import Config
from core.realtime import Feed
from automation.runner import drain_feed


AT = datetime(2026, 10, 22, 9, 40, tzinfo=KST)


class AsyncDiagnosticEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'events.jsonl'

    def writer(self, **kwargs):
        cls = getattr(event_log, 'AsyncDiagnosticEventWriter', None)
        self.assertIsNotNone(cls, 'explicit bounded diagnostic writer is required')
        writer = cls(self.path, **kwargs)
        return writer

    def rows(self):
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]

    def diagnostic(self, **kwargs):
        return dict(time=AT.isoformat(), event='ENTRY_EVAL', decision='NO_BUY',
                    signal_detected=False, symbol='999990', **kwargs)

    def test_queued_diagnostic_does_not_wait_for_slow_append_and_freezes_original_values(self):
        original = getattr(event_log, '_append_line', None)
        entered, release = threading.Event(), threading.Event()

        def slow(path, line, max_bytes):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test release barrier not opened')
            return original(path, line, max_bytes)

        with patch.object(event_log, '_append_line', slow, create=True):
            writer = self.writer()
            row = self.diagnostic(values={'count': 1})
            try:
                self.assertEqual(writer.append(row), 'ASYNC')
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                row['values']['count'] = 999
            finally:
                release.set()
                writer.close()
        saved = self.rows()[0]
        self.assertEqual(saved['values']['count'], 1)
        self.assertEqual(saved['time'], AT.isoformat())
        self.assertTrue(saved['diagnostic_async'])
        self.assertEqual(saved['event_ingress_sequence'], 1)

    def test_full_queue_falls_back_synchronously_without_losing_rows_or_waiting_all_pending(self):
        original = getattr(event_log, '_append_line', None)
        entered, release = threading.Event(), threading.Event()

        def slow_background(path, line, max_bytes):
            if threading.current_thread().name.startswith('diagnostic-event-writer'):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test release barrier not opened')
            return original(path, line, max_bytes)

        with patch.object(event_log, '_append_line', slow_background, create=True):
            writer = self.writer(capacity=1)
            try:
                self.assertEqual(writer.append(self.diagnostic(number=1)), 'ASYNC')
                self.assertTrue(entered.wait(1))
                self.assertEqual(writer.append(self.diagnostic(number=2)), 'ASYNC')
                self.assertEqual(writer.append(self.diagnostic(number=3)), 'SYNC_FALLBACK')
                self.assertEqual(writer.append(dict(time=AT.isoformat(), event='ORDER_ACCEPTED',
                    side='BUY', number=4)), 'SYNC_CRITICAL')
                self.assertFalse(release.is_set())
                self.assertEqual([row['number'] for row in self.rows()], [3, 4])
                self.assertLessEqual(writer.stats()['queue_depth'], 1)
            finally:
                release.set()
                writer.close()
        rows = self.rows()
        self.assertEqual(sorted(row['number'] for row in rows), [1, 2, 3, 4])
        self.assertEqual([row['number'] for row in sorted(rows, key=lambda r:r['event_ingress_sequence'])],
                         [1, 2, 3, 4])
        self.assertFalse(next(row for row in rows if row['number'] == 3)['diagnostic_async'])

    def test_signals_orders_fills_halts_and_unknown_events_are_always_synchronous(self):
        writer = self.writer()
        rows = [dict(event=kind, time=AT.isoformat()) for kind in
                ('ORDER_INTENT', 'ORDER_ACCEPTED', 'FILL', 'HALT', 'ENTRY_HALTED',
                 'ENTRY_CANDIDATE', 'CANCEL', 'UNKNOWN_EVENT')]
        rows += [dict(self.diagnostic(), decision='BUY'),
                 dict(self.diagnostic(), decision='WAIT'),
                 dict(self.diagnostic(), side='BUY'),
                 dict(self.diagnostic(), signal_detected=True),
                 dict(self.diagnostic(), signal_at=AT.isoformat()),
                 dict(self.diagnostic(), setup='FAST'),
                 dict(time=AT.isoformat(), event='NO_TRADE', signal={'pattern':'FAST'})]
        try:
            for row in rows:
                self.assertEqual(writer.append(row), 'SYNC_CRITICAL')
                self.assertEqual(self.rows()[-1]['event'], row['event'])
                self.assertFalse(self.rows()[-1]['diagnostic_async'])
            self.assertEqual(writer.stats()['queued'], 0)
        finally:
            writer.close()

    def test_readiness_diagnostics_drain_on_normal_close_and_writes_after_close_fail(self):
        writer = self.writer()
        for number, kind in enumerate(('READINESS_CHANGED', 'ORDER_READINESS_CHANGED', 'NO_TRADE')):
            self.assertEqual(writer.append(dict(time=AT.isoformat(), event=kind, number=number)), 'ASYNC')
        writer.close()
        self.assertFalse(writer.thread.is_alive())
        self.assertEqual([row['number'] for row in self.rows()], [0, 1, 2])
        with self.assertRaises(OSError):
            writer.append(self.diagnostic())

    def test_background_io_error_is_propagated_to_main_as_original_oserror(self):
        writer = self.writer()
        error = PermissionError(13, 'blocked diagnostic append', str(self.path))
        with patch.object(event_log, '_append_line', side_effect=error, create=True):
            writer.append(self.diagnostic())
            writer.thread.join(1)
        with self.assertRaises(PermissionError) as raised:
            writer.check_error()
        self.assertEqual(raised.exception.filename, str(self.path))
        self.assertEqual(writer.stats()['failed_line_retained'], True)
        # The main OSError handler can still persist the halt synchronously.
        self.assertEqual(writer.append(dict(time=AT.isoformat(),event='ENTRY_HALTED',
                                            reason='diagnostic IO failed')), 'SYNC_CRITICAL')
        self.assertEqual(self.rows()[-1]['event'], 'ENTRY_HALTED')
        with self.assertRaises(PermissionError):
            writer.close()
        # Even a closed/failed diagnostic writer cannot suppress broker outcomes.
        for kind in ('ORDER_ACCEPTED', 'FILL'):
            self.assertEqual(writer.append(dict(time=AT.isoformat(),event=kind,
                                                side='BUY',local='accepted')), 'SYNC_CRITICAL')
        self.assertEqual([row['event'] for row in self.rows()],
                         ['ENTRY_HALTED', 'ORDER_ACCEPTED', 'FILL'])

    def test_large_diagnostic_is_synchronous_to_preserve_byte_bound(self):
        writer = self.writer(max_line_bytes=128)
        try:
            self.assertEqual(writer.append(self.diagnostic(payload='x'*256)), 'SYNC_FALLBACK')
            self.assertEqual(writer.stats()['queued'], 0)
            self.assertEqual(self.rows()[0]['payload'], 'x'*256)
        finally:
            writer.close()

    def test_writer_failure_is_reported_once_without_starving_later_protection(self):
        from automation.runner import check_diagnostic_event_writer
        writer=self.writer()
        engine=SimpleNamespace(diagnostic_event_writer=writer)
        with patch.object(event_log,'_append_line',side_effect=PermissionError('diagnostic only')):
            writer.append(self.diagnostic())
            writer.thread.join(1)
        with self.assertRaises(PermissionError):check_diagnostic_event_writer(engine,writer)
        self.assertIsNone(engine.diagnostic_event_writer)
        # Following loop can consume fills and protect positions after HALT.
        check_diagnostic_event_writer(engine,writer)
        with self.assertRaises(PermissionError):writer.close()

    def test_plain_engine_stays_synchronous_and_only_explicit_handler_routes_events(self):
        engine = Engine.__new__(Engine)
        engine.folder = self.path.parent
        engine.clock = lambda: AT
        engine.console = Mock()
        with patch('core.engine.append_jsonl') as append:
            engine.event('ORDER_INTENT', local='first')
            append.assert_called_once()
        writer = self.writer()
        engine.diagnostic_event_writer = writer
        try:
            engine.event('ENTRY_EVAL', decision='NO_BUY', signal_detected=False)
            engine.event('FILL', side='SELL', local='second')
        finally:
            writer.close()
        rows = self.rows()
        self.assertFalse(next(row for row in rows if row['event']=='FILL')['diagnostic_async'])
        self.assertTrue(next(row for row in rows if row['event']=='ENTRY_EVAL')['diagnostic_async'])

    def test_received_book_trade_and_control_fifo_drain_while_diagnostic_writer_is_blocked(self):
        original = getattr(event_log, '_append_line', None)
        entered, release = threading.Event(), threading.Event()

        def slow(path, line, max_bytes):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test release barrier not opened')
            return original(path, line, max_bytes)

        feed = Feed(SimpleNamespace(cfg=Config()), ['999990'])
        feed.configure_observation({'999990'}, set(), None)
        with patch.object(event_log, '_append_line', slow, create=True), \
                patch('core.realtime.now', return_value=AT):
            writer = self.writer()
            try:
                writer.append(self.diagnostic())
                self.assertTrue(entered.wait(1))
                feed.emit({'trnm':'REAL','data':[{'type':'0D','item':'999990','values':
                    {'21':'094000','41':'1001','51':'1000','61':'200','71':'300'}}]})
                feed.emit({'trnm':'REAL','data':[{'type':'0B','item':'999990','values':
                    {'20':'094000','10':'1001','15':'10','13':'1000','27':'1001','28':'1000','290':'2'}}]})
                feed.emit({'trnm':'REG','return_code':0})
                consumed = []

                def consume(received, message, lag):
                    consumed.append(message['data'][0]['type'] if message['trnm']=='REAL' else 'REG')
                    writer.append(dict(time=received.isoformat(),event='ORDER_READINESS_CHANGED'))

                count, _ = drain_feed(feed,consume,clock=lambda:AT,max_seconds=1,timeout=0)
                self.assertEqual(count, 3)
                self.assertEqual(consumed, ['0D','0B','REG'])
                self.assertTrue(feed.latest_ticks['999990'].depth_available)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                writer.close()


if __name__ == '__main__':
    unittest.main()
