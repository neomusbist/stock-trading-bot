"""Synthetic multi-share cancellation and protection; no broker network."""
from dataclasses import replace
from copy import deepcopy
from unittest.mock import patch
import unittest

from tests.test_audit_followup import AuditFixture


class PartialExitMultiQuantityTests(AuditFixture):
    def setUp(self):
        super().setUp()
        self.no_network=patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK'))
        self.no_network.start();self.addCleanup(self.no_network.stop)
        # The original generic fixture risks 0.3% and only buys one share after
        # costs. Deliberately establish the real multi-share precondition before
        # asserting partial-fill behavior; no live account or risk is modified.
        self.cfg=replace(self.cfg,risk_fraction=.02,max_daily_loss_fraction=.04)
        self.e.c=self.cfg

    def test_partial_buy_must_cancel_and_confirm_before_selling_filled_shares(self):
        order=self.buy(partial=True)
        self.assertGreaterEqual(order['qty'],2)
        self.assertEqual(order['filled'],1)
        self.assertNotEqual(order['status'],'FILLED')
        self.e.on_tick(self.tick(975))
        self.assertEqual(order['status'],'CANCEL_SENT')
        self.assertEqual(self.sells(),[])
        for _ in range(3):self.e.drive_exits()
        self.assertEqual(self.sells(),[])
        self.e.reconcile({**self.snap,'holdings':deepcopy(self.e.actual)})
        self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.assertEqual(self.sells()[0][2],1)
        for _ in range(3):self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)

    def test_partial_sell_cancel_reconcile_before_confirmed_residual(self):
        order=self.buy()
        self.assertGreaterEqual(order['qty'],2)
        self.e.on_tick(self.tick(975))
        sell=self.e.active_orders('999990','SELL')[0]
        self.assertEqual(sell['qty'],order['qty'])
        self.e.cumulative_fill(sell,1,980)
        self.e.actual['999990']['qty']-=1
        self.e.actual['999990']['available']-=1
        self.e.cancel(sell)
        self.assertEqual(sell['status'],'CANCEL_SENT')
        for _ in range(3):self.e.drive_exits()
        self.assertEqual(len(self.sells()),1)
        self.e.reconcile({**self.snap,'holdings':deepcopy(self.e.actual)})
        self.e.drive_exits()
        self.assertEqual(len(self.sells()),2)
        self.assertEqual(self.sells()[-1][2],order['qty']-1)
        for _ in range(3):self.e.drive_exits()
        self.assertEqual(len(self.sells()),2)


if __name__=='__main__':unittest.main()
