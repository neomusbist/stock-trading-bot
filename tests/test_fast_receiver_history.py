"""Receiver-time history survives consumer delay; stale orders never do."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

from automation.fast_momentum import FastMomentumStrategy, MicroTape
from core.settings import Config
from automation.policy import AutomationConfig
from tests.test_r6_fast_repair import BASE, ticks, watch


class FastReceiverHistoryTests(unittest.TestCase):
    def received_ticks(self):
        return [replace(t, received_at=(t.time+timedelta(seconds=.2)).isoformat(),
                        received_monotonic=100.2+i)
                for i,t in enumerate(ticks())]

    def test_catchup_retains_actual_received_rise_and_confirmed_pullback(self):
        data=self.received_ticks()
        strategy=FastMomentumStrategy(Config(),AutomationConfig())
        # Earlier prints were received on time but processed with the current
        # decision frame. This does not invent an earlier order opportunity.
        with patch('core.market_time.time.monotonic',return_value=110.2):
            for t in data:
                self.assertTrue(strategy.ingest_tick(t,t.received_at))
            current=data[-1];asof=current.time+timedelta(seconds=.2)
            signal=strategy.evaluate(current,watch(),ingest=False,received=asof)
        self.assertIsNotNone(signal,strategy.last_evaluation)
        evidence=signal.extra['geometry']['structural_evidence']
        self.assertEqual(evidence['origin'],984)
        self.assertEqual(evidence['support'],990)
        self.assertEqual(len(strategy.get_tape(current.symbol).ticks),len(data))
        self.assertFalse(signal.extra['actual_order_permission'])

    def test_old_current_frame_cannot_generate_signal_from_retained_history(self):
        data=self.received_ticks();strategy=FastMomentumStrategy()
        with patch('core.market_time.time.monotonic',return_value=120.):
            for t in data:self.assertTrue(strategy.ingest_tick(t,t.received_at))
            current=data[-1];asof=current.time+timedelta(seconds=10)
            self.assertFalse(strategy.get_tape(current.symbol).metrics(asof)['data_fresh'])
            self.assertIsNone(strategy.evaluate(current,watch(),ingest=False,received=asof))
            self.assertEqual(strategy._quote(current,asof),'CURRENT_TRADE_STALE_OR_FUTURE')

    def test_arrived_late_at_receiver_is_not_accepted_as_good_history(self):
        t=self.received_ticks()[0]
        t=replace(t,received_at=(t.time+timedelta(seconds=6)).isoformat())
        self.assertFalse(MicroTape().ingest(t,t.received_at))

    def test_abnormally_future_exchange_time_stays_rejected(self):
        t=self.received_ticks()[0]
        received=t.time-timedelta(seconds=1.001)
        t=replace(t,received_at=received.isoformat())
        self.assertFalse(MicroTape().ingest(t,received))

    def test_wall_clock_correction_cannot_revive_expired_receive_counter(self):
        t=self.received_ticks()[0];tape=MicroTape()
        self.assertTrue(tape.ingest(t,t.received_at))
        with patch('core.market_time.time.monotonic',return_value=106.2):
            # Wall time still looks current; six real seconds have elapsed.
            asof=BASE+timedelta(seconds=.3)
            self.assertFalse(tape.metrics(asof)['data_fresh'])
            self.assertEqual(FastMomentumStrategy()._quote(t,asof),'CURRENT_TRADE_STALE_OR_FUTURE')

    def test_unreceived_future_print_cannot_manufacture_support(self):
        tape=MicroTape();data=self.received_ticks()
        for t in data:self.assertTrue(tape.ingest(t,t.received_at))
        with patch('core.market_time.time.monotonic',return_value=101.2):
            earlier=BASE+timedelta(seconds=1.2)
            self.assertEqual(tape.metrics(earlier)['ticks_count'],2)
            self.assertFalse(tape.structure(earlier)['pass'])

    def test_invalid_receive_counter_remains_invalid(self):
        for value in (float('nan'),float('inf'),'bad'):
            with self.subTest(counter=value):
                t=replace(self.received_ticks()[0],received_monotonic=value)
                self.assertFalse(MicroTape().ingest(t,t.received_at))


if __name__=='__main__':unittest.main()
