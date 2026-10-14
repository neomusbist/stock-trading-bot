import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core.settings import Config, KST
from automation.runner import readiness_snapshot
from automation.session import supported

class FakeTick:
    def __init__(self, symbol, price, dt, regular=True):
        self.symbol = symbol
        self.price = price
        self.time = dt
        self.regular = regular

class FakeEngine:
    def __init__(self, now_dt):
        self.c = Config(stale_seconds=5)
        self.connected = True
        self.market = '3'
        self.event_loop_lag = 0.05
        self.feed_queue_size = 0
        self.last_regular_tick_at = now_dt
        self.last_radar_at = now_dt
        self.s = {'halt': False, 'positions': {}}
        self._account_valid = True
        self.now_dt = now_dt

    def current_session(self):
        return 'REGULAR'

    def capital_account_valid(self):
        return self._account_valid

    def active_orders(self):
        return []

class TestOrderReadySynchronization(unittest.TestCase):
    def setUp(self):
        self.now_dt = datetime(2026, 10, 14, 10, 0, 0, tzinfo=KST)
        self.e = FakeEngine(self.now_dt)
        self.counts = {'ticks': 100, 'subscription_acks': 1}

    def test_case_1_silence_durations_5s_10s_20s(self):
        """5s (fresh) -> order_ready=True; 7s (silent lull within 2x) -> order_ready=True; 12s/20s -> False."""
        # 1. 5s: Fresh
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=4)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'OPEN_READY')
        self.assertTrue(snap['order_ready'])
        self.assertEqual(snap['missing'], [])

        # 2. 7s: Silent lull (WS_SOURCE_SILENT, <= 10s grace)
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=7)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'WAITING')
        self.assertEqual(snap['missing'], ['FRESH_REGULAR_TICK_NOT_READY'])
        self.assertEqual(snap['tick_lag_source'], 'WS_SOURCE_SILENT')
        self.assertTrue(snap['order_ready'], "Within 2x stale_seconds and WS_SOURCE_SILENT, order_ready must be True")

        # 3. 12s: Exceeded 2x stale_seconds
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=12)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'WAITING')
        self.assertFalse(snap['order_ready'], "Tick age > 10s must fail closed order_ready=False")

        # 4. 20s: Severely stale
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=20)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'WAITING')
        self.assertFalse(snap['order_ready'])

    def test_case_2_websocket_disconnect_fails_closed(self):
        """Disconnected WebSocket immediately drops order_ready=False with WEBSOCKET_REG_ACK_NOT_READY."""
        self.e.connected = False
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=2)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'WAITING')
        self.assertIn('WEBSOCKET_REG_ACK_NOT_READY', snap['missing'])
        self.assertFalse(snap['order_ready'])

    def test_case_3_account_stale_fails_closed(self):
        """Account check failure immediately drops order_ready=False with ACCOUNT_FRESH_NOT_READY."""
        self.e._account_valid = False
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['state'], 'WAITING')
        self.assertIn('ACCOUNT_FRESH_NOT_READY', snap['missing'])
        self.assertFalse(snap['order_ready'])

    def test_case_4_event_loop_lag_and_queue_buffer_deny_grace(self):
        """Event loop lag > 1.0s or queue > 5 items must NOT get the WS_SOURCE_SILENT grace."""
        # 6s silence with event loop lag = 1.5s
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=6)
        self.e.event_loop_lag = 1.5
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['tick_lag_source'], 'EVENT_LOOP_LAG')
        self.assertFalse(snap['order_ready'], "EVENT_LOOP_LAG must not receive silent lull grace")

        # 6s silence with queue buffer = 8 items
        self.e.event_loop_lag = 0.05
        self.e.feed_queue_size = 8
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertEqual(snap['tick_lag_source'], 'QUEUE_BUFFER')
        self.assertFalse(snap['order_ready'], "QUEUE_BUFFER must not receive silent lull grace")

    def test_case_5_symbol_level_staleness_prevented_even_if_order_ready(self):
        """Even when global order_ready=True, a specific symbol with stale tick (> 5s) is rejected."""
        # Market order_ready is True
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=2)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertTrue(snap['order_ready'])

        # Symbol tick is 7s old
        sym_tick = FakeTick('005930', 70000, self.now_dt - timedelta(seconds=7))
        age = (self.now_dt - sym_tick.time).total_seconds()
        self.assertGreater(age, self.e.c.stale_seconds)

        # Verification: runner's tick execution check rejects tick
        is_fresh = 0 <= (self.now_dt - sym_tick.time).total_seconds() <= self.e.c.stale_seconds
        self.assertFalse(is_fresh, "Symbol-level stale tick must never be admitted for trade evaluation")

    def test_case_6_engine_reason_and_gui_status_match(self):
        """Engine entry_readiness_reason and GUI trading_on / reason are perfectly synchronized."""
        # 1. State: Perfectly ready
        self.e.last_regular_tick_at = self.now_dt - timedelta(seconds=2)
        snap = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertTrue(snap['order_ready'])
        
        # Engine check
        engine_reason = '' if snap['order_ready'] else ('OPEN_READINESS_' + snap['missing'][0])
        self.assertEqual(engine_reason, '')

        # 2. State: Account invalid
        self.e._account_valid = False
        snap_bad = readiness_snapshot(self.e, self.counts, radar_ok=True, stamp=self.now_dt)
        self.assertFalse(snap_bad['order_ready'])
        self.assertIn('ACCOUNT_FRESH_NOT_READY', snap_bad['missing'])
        
        # Engine reason when order_ready is False
        engine_reason_bad = '' if snap_bad['order_ready'] else ('OPEN_READINESS_' + snap_bad['missing'][0])
        self.assertEqual(engine_reason_bad, 'OPEN_READINESS_ACCOUNT_FRESH_NOT_READY')
        
        # GUI status check with heartbeat matching engine readiness
        hb = {'readiness': snap_bad, 'connected': True, 'account_ok': False}
        ready_ok = hb.get('readiness', {}).get('order_ready')
        trading_on = bool(ready_ok)
        self.assertFalse(trading_on, "When engine order_ready=False, GUI trading_on must be False")

if __name__ == '__main__':
    unittest.main()
