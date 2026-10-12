"""Closed REG transport cannot terminate a worker or authorize a stale order."""
import unittest
from unittest.mock import Mock,patch
from datetime import timedelta
from websocket import WebSocketConnectionClosedException
import test_subscription_refresh as fixtures

class RefreshTransportTests(unittest.TestCase):
    setUp=fixtures.SubscriptionRefreshTests.setUp
    warm=fixtures.SubscriptionRefreshTests.warm
    tick=fixtures.SubscriptionRefreshTests.tick
    frame=fixtures.SubscriptionRefreshTests.frame
    send=fixtures.SubscriptionRefreshTests.send
    signal=fixtures.SubscriptionRefreshTests.signal
    refresh=fixtures.SubscriptionRefreshTests.refresh

    def test_closed_reg_blocks_same_cycle_then_fresh_reconnect_recovers(self):
        self.warm();self.feed.socket.send.side_effect=WebSocketConnectionClosedException('closed')
        self.refresh({fixtures.SYM,fixtures.OTHER})
        self.assertFalse(self.e.connected);self.assertFalse(self.e.account_ok)
        self.assertFalse(self.e.s['halt']);self.assertFalse(self.feed.logged_in)
        self.assertEqual(self.d.history,{});self.assertIn(fixtures.OTHER,self.feed.symbols)
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
        _,message=self.feed.q.get_nowait()
        self.assertEqual(message['stage'],'SUBSCRIPTION_SEND');self.send(message)
        self.send({'trnm':'REG','return_code':0});self.assertFalse(self.e.account_ok)
        self.e.reconcile(self.snapshot);self.assertTrue(self.e.account_ok)
        self.clock+=timedelta(seconds=1)
        self.assertEqual(self.send(self.frame(self.tick(sec=121))),1)
        self.assertEqual(self.broker.orders,[]);self.assertFalse(self.e.s['halt'])

    def test_normal_refresh_preserves_observation_and_permission(self):
        self.warm();bars=self.d.history[fixtures.SYM]
        for n in range(5):self.refresh({fixtures.SYM,fixtures.OTHER} if n%2 else {fixtures.SYM})
        self.assertIs(bars,self.d.history[fixtures.SYM]);self.assertTrue(self.e.connected)
        self.assertTrue(self.e.account_ok);self.assertEqual(self.broker.orders,[])

    def test_programming_and_storage_errors_are_not_transport_recovery(self):
        for error in (ValueError('bad packet'),PermissionError('storage failure')):
            self.feed.socket=Mock();self.feed.logged_in=True
            self.feed.socket.send.side_effect=error
            with self.assertRaises(type(error)):self.feed.update_symbols([fixtures.SYM,fixtures.OTHER] if self.feed.symbols==[fixtures.SYM] else [fixtures.SYM])

    def test_exchange_refresh_closed_transport_immediately_blocks(self):
        self.warm()
        self.feed.socket.send.side_effect=WebSocketConnectionClosedException('closed')
        with patch.object(self.feed,'subscription_items',return_value=[fixtures.SYM+'_AL']):
            self.refresh({fixtures.SYM})
        self.assertFalse(self.e.connected);self.assertFalse(self.e.account_ok)
        self.assertEqual(self.broker.orders,[]);self.assertFalse(self.e.s['halt'])

if __name__=='__main__':unittest.main()
