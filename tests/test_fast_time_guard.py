"""The separate FAST path must share the proven bounded clock-skew guard."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.fast_momentum import FastMomentumStrategy,MicroTape
from tests.test_r6_fast_repair import ticks,watch

AT=datetime(2026,10,16,11,33,25,tzinfo=KST)


class FastTimeGuardTests(unittest.TestCase):
    def tick(self,at=AT):
        return Tick('079650',at,7950,10,10,7950,7940,True,ask_qty=100,bid_qty=100,
                    depth_available=True,book_time=at)

    def test_seosan_and_daeyang_clock_leads_reach_fast_metrics_and_quote(self):
        for lead in (.051,.293):
            received=AT-timedelta(seconds=lead);t=self.tick()
            tape=MicroTape();strategy=FastMomentumStrategy(Config(),AutomationConfig())
            with self.subTest(lead=lead):
                self.assertTrue(tape.ingest(t,received))
                m=tape.metrics(received)
                self.assertEqual(m['ticks_count'],1);self.assertTrue(m['data_fresh'])
                self.assertIsNone(strategy._quote(t,received))

    def test_real_stale_and_abnormally_future_quotes_remain_blocked(self):
        for delta in (-6,1.001):
            t=self.tick(AT+timedelta(seconds=delta));tape=MicroTape()
            with self.subTest(delta=delta):
                self.assertFalse(tape.ingest(t,AT))
                self.assertIsNotNone(FastMomentumStrategy(Config(),AutomationConfig())._quote(t,AT))

    def test_late_local_receipt_stays_stale_even_with_a_fresh_exchange_stamp(self):
        t=replace(self.tick(),received_at=AT-timedelta(seconds=6))
        self.assertFalse(MicroTape().ingest(t,AT))
        self.assertIsNotNone(FastMomentumStrategy()._quote(t,AT))

    def test_unreceived_prints_are_not_used_by_a_historical_cutoff(self):
        tape=MicroTape();t=self.tick();self.assertTrue(tape.ingest(t,AT))
        before=AT-timedelta(seconds=.1)
        self.assertEqual(tape.metrics(before),{})
        self.assertFalse(tape.structure(before)['pass'])

    def test_old_observed_prints_keep_their_original_sixty_second_window(self):
        tape=MicroTape()
        for i in range(61):
            t=replace(self.tick(AT+timedelta(seconds=i)),cumulative=10*(i+1))
            self.assertTrue(tape.ingest(t,t.time))
        m=tape.metrics(AT+timedelta(seconds=60))
        self.assertEqual(m['ticks_60'],61)
        self.assertEqual(m['ticks_10'],11)

    def test_same_causal_fast_signal_survives_bounded_exchange_lead(self):
        for lead in (0,.051,.293):
            strategy=FastMomentumStrategy(Config(),AutomationConfig())
            data=ticks(start=AT)
            for t in data:self.assertTrue(strategy.ingest_tick(t,t.time-timedelta(seconds=lead)))
            result=strategy.evaluate(data[-1],watch(at=AT),ingest=False,
                received=data[-1].time-timedelta(seconds=lead),capital_limit=81300,risk_budget=1626)
            with self.subTest(lead=lead):self.assertIsNotNone(result)

    def test_cross_date_and_old_book_remain_blocked(self):
        strategy=FastMomentumStrategy()
        t=replace(self.tick(),book_time=AT-timedelta(seconds=6))
        self.assertIsNotNone(strategy._quote(t,AT))
        self.assertFalse(MicroTape().ingest(self.tick(),AT+timedelta(days=1)))

    def test_completed_bar_is_unavailable_until_engine_processed_it(self):
        from automation.profit_review import at
        received=AT-timedelta(seconds=.293);processed=AT+timedelta(seconds=.2)
        row={'event':'COMPLETED_BAR','confirmed_at':received.isoformat(),'processed_at':processed.isoformat()}
        self.assertEqual(at(row),processed)
