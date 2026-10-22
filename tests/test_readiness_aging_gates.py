"""Read-only readiness aging keeps displayed gates and blockers consistent."""
import copy
from datetime import datetime, timedelta
import unittest

from automation.runner import heartbeat_order_readiness
from core.settings import Config, KST


AT = datetime(2026, 10, 22, 9, 5, tzinfo=KST)


class ReadinessAgingGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(stale_seconds=5, reconcile_seconds=5)

    def heartbeat(self):
        tick = AT - timedelta(seconds=4)
        return {'time': AT.isoformat(), 'connected': True, 'readiness': {
            'state': 'OPEN_READY', 'order_ready': True, 'missing': [],
            'off_reasons': [], 'gates': {
                'fresh_regular_tick': True, 'account_fresh': True,
                'websocket_reg_ack': True, 'safe_state': True},
            'regular_tick_age': 4.0, 'regular_tick_timing_valid': True,
            'last_regular_tick_at': tick.isoformat(),
            'last_regular_tick_received_at': tick.isoformat(),
            'account_age_seconds': 0.0, 'event_loop_lag_seconds': 2.294,
            'feed_queue_depth': 63}}

    def test_aged_tick_updates_gate_missing_and_backlog_off_together(self):
        ready = heartbeat_order_readiness(self.heartbeat(), self.cfg, AT + timedelta(seconds=2))
        self.assertFalse(ready['gates']['fresh_regular_tick'])
        self.assertIn('FRESH_REGULAR_TICK_NOT_READY', ready['missing'])
        self.assertEqual(ready['tick_lag_source'], 'EVENT_LOOP_LAG')
        self.assertFalse(ready['order_ready'])

    def test_account_expiry_updates_gate_and_missing(self):
        hb = self.heartbeat()
        hb['readiness']['account_age_seconds'] = 14.0
        ready = heartbeat_order_readiness(hb, self.cfg, AT + timedelta(seconds=2))
        self.assertFalse(ready['gates']['account_fresh'])
        self.assertIn('ACCOUNT_FRESH_NOT_READY', ready['missing'])
        self.assertFalse(ready['order_ready'])

    def test_disconnected_snapshot_updates_registration_gate_and_missing(self):
        hb = self.heartbeat()
        hb['connected'] = False
        ready = heartbeat_order_readiness(hb, self.cfg, AT)
        self.assertFalse(ready['gates']['websocket_reg_ack'])
        self.assertIn('WEBSOCKET_REG_ACK_NOT_READY', ready['missing'])
        self.assertFalse(ready['order_ready'])

    def test_expired_heartbeat_revokes_freshness_gates_and_open_state(self):
        ready = heartbeat_order_readiness(self.heartbeat(), self.cfg, AT + timedelta(seconds=11))
        self.assertFalse(ready['gates']['fresh_regular_tick'])
        self.assertFalse(ready['gates']['account_fresh'])
        self.assertFalse(ready['gates']['websocket_reg_ack'])
        self.assertNotEqual(ready['state'], 'OPEN_READY')
        self.assertIn('HEARTBEAT_NOT_CURRENT', ready['off_reasons'])
        self.assertFalse(ready['order_ready'])

    def test_engine_off_is_not_revived_but_aged_gates_still_update(self):
        hb = self.heartbeat()
        hb['readiness'].update(order_ready=False, off_reasons=['ENGINE_DATA_NOT_READY'])
        ready = heartbeat_order_readiness(hb, self.cfg, AT + timedelta(seconds=2))
        self.assertFalse(ready['order_ready'])
        self.assertFalse(ready['gates']['fresh_regular_tick'])
        self.assertIn('FRESH_REGULAR_TICK_NOT_READY', ready['missing'])
        self.assertIn('ENGINE_DATA_NOT_READY', ready['off_reasons'])

    def test_prior_registration_failure_cannot_be_revived_by_connected_transport(self):
        hb = self.heartbeat()
        hb['readiness']['gates']['websocket_reg_ack'] = False
        hb['readiness'].update(order_ready=False, state='WAITING',
            missing=['WEBSOCKET_REG_ACK_NOT_READY'], off_reasons=['WEBSOCKET_REG_ACK_NOT_READY'])
        ready = heartbeat_order_readiness(hb, self.cfg, AT)
        self.assertFalse(ready['gates']['websocket_reg_ack'])
        self.assertFalse(ready['order_ready'])

    def test_future_heartbeat_does_not_display_fresh_gates(self):
        ready = heartbeat_order_readiness(self.heartbeat(), self.cfg, AT - timedelta(seconds=1))
        self.assertFalse(ready['gates']['fresh_regular_tick'])
        self.assertFalse(ready['gates']['account_fresh'])
        self.assertFalse(ready['order_ready'])
        self.assertIn('HEARTBEAT_NOT_CURRENT', ready['off_reasons'])

    def test_aging_never_mutates_saved_heartbeat(self):
        hb = self.heartbeat()
        saved = copy.deepcopy(hb)
        heartbeat_order_readiness(hb, self.cfg, AT + timedelta(seconds=2))
        self.assertEqual(hb, saved)


if __name__ == '__main__':
    unittest.main()
