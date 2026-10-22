"""Diagnostic gap logging cannot hold the live receiver's routing lock."""
from datetime import datetime, timedelta
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.runner import prewarm_handoff
from core.realtime import Feed
from core.settings import Config, KST


AT = datetime(2026, 10, 22, 9, 5, 0, tzinfo=KST)
OBS, WATCH, HELD, OTHER = '999991', '999992', '999993', '999994'


class ReceiverGapLockTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.detector = PatternDetector(self.cfg, AutomationConfig(), {})
        self.detector.continuous_observation = True
        self.feed = Feed(SimpleNamespace(cfg=self.cfg), [OBS, WATCH, HELD])
        self.feed.configure_observation({WATCH, HELD}, {OBS}, None)
        self.feed.observation_drops = 1
        self.feed.observation_gaps = {s: AT for s in (OBS, WATCH, HELD, OTHER)}
        self.forgotten = []
        self.events = []
        self.engine = SimpleNamespace(
            s={'positions': {HELD: {'qty': 1}}, 'automation': {'existing': {}}},
            active_orders=lambda: [], clock=lambda: AT,
            fast_strategy=SimpleNamespace(forget=lambda *args: self.forgotten.append(args)),
            event=lambda *args, **kwargs: self.events.append((args, kwargs)),
        )

    def handoff(self, drops=0):
        return prewarm_handoff(self.engine, self.detector, self.feed, {OBS},
                               {WATCH, HELD}, {WATCH, HELD}, drops)

    def frame(self):
        return {'trnm': 'REAL', 'data': [
            {'type': '0D', 'item': WATCH, 'values': {
                '21': AT.strftime('%H%M%S'), '41': '1001', '51': '1000',
                '61': '200', '71': '300', '121': '2000', '125': '3000'}},
            {'type': '0B', 'item': WATCH, 'values': {
                '20': AT.strftime('%H%M%S'), '10': '1001', '15': '10',
                '13': '1000', '27': '1001', '28': '1000', '290': '2'}},
        ]}

    def test_blocked_gap_log_does_not_block_real_receiver_book_and_trade(self):
        entered, release, received = threading.Event(), threading.Event(), threading.Event()
        errors, result = [], []

        def slow_event(*args, **kwargs):
            self.events.append((args, kwargs))
            entered.set()
            if not release.wait(5):
                raise AssertionError('test logging barrier not released')

        self.engine.event = slow_event

        def handoff():
            try:
                result.append(self.handoff())
            except Exception as error:
                errors.append(error)

        def receive():
            try:
                self.feed.emit(self.frame())
                received.set()
            except Exception as error:
                errors.append(error)

        main = threading.Thread(target=handoff, daemon=True)
        receiver = threading.Thread(target=receive, daemon=True)
        with patch('core.realtime.now', return_value=AT):
            main.start()
            try:
                self.assertTrue(entered.wait(2), 'gap log was not reached')
                # The gap must already prevent entries before potentially slow I/O.
                self.assertTrue(self.detector.coverage_gaps[OBS]['pending'])
                self.assertEqual(self.detector.usable_bars(OBS), [])
                self.assertEqual(self.forgotten, [(OBS, 'OBSERVATION_QUEUE_GAP')])
                receiver.start()
                self.assertTrue(received.wait(1),
                                'Feed.emit is waiting for diagnostic logging under subscription_lock')
                self.assertFalse(release.is_set())
                _, frame = self.feed.q.get_nowait()
                self.assertEqual([row['type'] for row in frame['data']], ['0D', '0B'])
                self.assertTrue(frame['data'][1]['_observation_tick'][1].depth_available)
                self.assertEqual(self.feed.orderbooks[WATCH]['ask_qty'], 200)
            finally:
                release.set()
                main.join(2)
                if receiver.ident is not None:
                    receiver.join(2)
        self.assertFalse(main.is_alive())
        self.assertFalse(receiver.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [(1, {WATCH, HELD})])
        self.assertEqual(set(self.detector.coverage_gaps), {OBS})

    def test_new_gap_during_logging_is_preserved_for_next_handoff(self):
        next_gap = AT + timedelta(seconds=1)

        def concurrent_gap(*args, **kwargs):
            # This models a later receiver overflow after the old gap snapshot.
            with self.feed.subscription_lock:
                self.feed.observation_drops += 1
                self.feed.observation_gaps[OBS] = next_gap

        self.engine.event = concurrent_gap
        drops, _ = self.handoff()
        self.assertEqual(drops, 1)
        self.assertEqual(self.feed.observation_drops, 2)
        self.assertEqual(self.feed.observation_gaps, {OBS: next_gap})
        self.engine.event = lambda *args, **kwargs: None
        drops, _ = self.handoff(drops)
        self.assertEqual(drops, 2)
        self.assertEqual(self.feed.observation_gaps, {})
        self.assertEqual(self.detector.coverage_gaps[OBS]['at'], next_gap)
        self.assertEqual(set(self.detector.coverage_gaps), {OBS})


if __name__ == '__main__':
    unittest.main()
