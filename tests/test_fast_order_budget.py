"""FAST quote size must fit the same cash and remaining risk as the order."""
from dataclasses import replace
from datetime import timedelta
import time
import unittest
from unittest.mock import patch

from automation.runner import evaluate_actual_entry, evaluate_shadow_opportunity
from core.util import tick_size
from tests.test_live_ready_20261021 import LiveReadyLifecycle, SYM


class FastOrderBudgetTests(unittest.TestCase):
    def case(self, depth, position_fraction=1., remaining=None):
        h=LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.e.c=replace(h.e.c,max_position_fraction=position_fraction)
        if remaining is not None:h.e.s['daily_loss']=h.e.daily_risk_budget()-remaining
        original=h.feed
        def ingest_only(*args,**kwargs):
            kwargs['execute']=False
            return original(*args,**kwargs)
        with patch.object(h,'feed',side_effect=ingest_only):h.seed('FAST')
        h.latest=replace(h.latest,ask_qty=depth,bid_qty=depth,depth_available=True,
                         book_time=h.clock,book_received_at=h.clock.isoformat())
        h.book.update(ask_qty=depth,bid_qty=depth,best_ask=h.latest.ask,best_bid=h.latest.bid,
                      time=h.clock.isoformat(),received_at=h.clock.isoformat(),depth_available=True)
        h.e.last_account=time.monotonic()
        return h

    def enter_and_protect(self,h,maximum):
        order=evaluate_actual_entry(h.e,h.detector,h.latest,h.lookup[SYM],h.radar,h.prices,h.account)
        self.assertIsNotNone(order,h.e.fast_strategy.last_evaluation.get(SYM))
        self.assertEqual(order['status'],'ACCEPTED')
        self.assertGreater(order['qty'],0);self.assertLessEqual(order['qty'],maximum)
        self.assertLessEqual(order['qty']*order['limit']*(1+h.auto.commission_buy),h.e.watch_budget()+h.e.pending_buy_reservation())
        h.fill(order,order['qty'],order['limit'])
        position=h.e.s['positions'][SYM]
        hard=position['signal']['invalidation_policy']['emergency_stop']
        price=hard-tick_size(hard)
        h.feed(h.clock+timedelta(seconds=1),price)
        sell=next(o for o in h.e.s['orders'].values() if o['side']=='SELL')
        h.fill(sell,order['qty'],price)
        self.assertEqual(h.e.s['positions'],{})
        self.assertEqual([x['side'] for x in h.broker.intent_checks],['BUY','SELL'])
        self.assertEqual((h.e.c.risk_fraction,h.e.c.max_daily_loss_fraction),(.02,.04))

    def test_position_limited_entry_uses_executable_actual_size_before_depth_check(self):
        h=self.case(depth=14,position_fraction=.20)
        self.enter_and_protect(h,14)

    def test_daily_risk_limited_entry_uses_executable_actual_size_before_depth_check(self):
        h=self.case(depth=1,remaining=35)
        self.enter_and_protect(h,1)

    def test_shadow_precheck_receives_same_entry_cash_and_remaining_risk(self):
        h=self.case(depth=10000,position_fraction=.20,remaining=35)
        cash=h.e.watch_budget();risk=min(h.e.risk_equity()*h.e.c.risk_fraction,h.e.capital_context()['risk_remaining'])
        with patch.object(h.e.fast_strategy,'evaluate',wraps=h.e.fast_strategy.evaluate) as spy:
            evaluate_shadow_opportunity(h.e,h.detector,h.latest,h.lookup[SYM],h.radar,h.prices,h.account,probe_only=True)
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.kwargs['capital_limit'],cash)
        self.assertEqual(spy.call_args.kwargs['risk_budget'],risk)
        self.assertEqual(h.broker.orders,[])


if __name__=='__main__':unittest.main()
