"""Received Lunit tape: a half quote-step print must not halve the swing guard.

The fixture ends at the original first exit request. No later market data,
credentials, account/order identifiers or live runtime are used by these tests.
"""
import json
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from automation.fast_momentum import FastMomentumStrategy
from core.settings import KST
from core.strategy import Tick


class LunitExitQuoteUnitTests(unittest.TestCase):
    def incident(self,normalize_half_step=False):
        fixture=json.loads((Path(__file__).parent/'fixtures/lunit_exit_20261019.json').read_text(encoding='utf-8'))
        strategy=FastMomentumStrategy();position=fixture['position']
        for row in fixture['ticks']:
            r=deepcopy(row)
            if normalize_half_step:r['price']=r['price']//10*10
            t=Tick(symbol=r['symbol'],time=datetime.fromisoformat(r['exchange_at']),
                price=r['price'],volume=r['volume'],cumulative=r['cumulative'],
                bid=r['bid'],ask=r['ask'],bid_qty=r['bid_qty'],ask_qty=r['ask_qty'],
                regular=r['regular'],depth_available=r['depth_available'],depth_source=r['depth_source'],
                book_time=datetime.fromisoformat(r['book_time']),venue=r['venue'],
                received_at=r['received_at'],received_monotonic=r['received_monotonic'])
            self.assertTrue(strategy.ingest_tick(t,datetime.fromisoformat(r['received_at'])))
        decision=datetime.fromisoformat(fixture['decision_at'])
        elapsed=(decision-datetime.fromisoformat(r['received_at'])).total_seconds()
        with patch('core.market_time.time.monotonic',return_value=r['received_monotonic']+elapsed):
            result=strategy.evaluate_exit(position,t,decision)
        return result,strategy,position,t

    def test_actual_lunit_received_tape_does_not_confirm_one_quote_step_failure(self):
        result,_,position,_=self.incident()
        self.assertEqual(result[:2],(False,'HOLD'))
        self.assertEqual(result[2]['swing_tick_size'],10)
        self.assertEqual(position['initial_stop'],12840)
        self.assertEqual(position['emergency'],12820)
        self.assertNotIn('protection_stop',position)

    def test_half_step_print_cannot_turn_otherwise_identical_noise_into_failure(self):
        original=self.incident()[0]
        normalized=self.incident(normalize_half_step=True)[0]
        self.assertEqual(original[:2],normalized[:2])
        self.assertFalse(original[0])

    def feed(self,prices,*,entry=12890,stop=12840,emergency=12820,seconds=None):
        strategy=FastMomentumStrategy()
        start=datetime(2026,10,19,11,40,0,tzinfo=KST)
        position={'symbol':'328130','avg':entry,'qty':5,'initial_stop':stop,
                  'stop':stop,'emergency':emergency,'opened':start.isoformat()}
        seconds=seconds or [0,15,30,46,55,60]
        for index,(second,price) in enumerate(zip(seconds,prices)):
            stamp=start+timedelta(seconds=second)
            t=Tick(symbol='328130',time=stamp,price=price,volume=10,cumulative=100+index*10,
                bid=price,ask=price+10,bid_qty=500,ask_qty=500,depth_available=True,book_time=stamp)
            self.assertTrue(strategy.ingest_tick(t,stamp))
        return strategy,position,t

    def test_genuine_two_quote_step_failed_rebound_still_exits(self):
        strategy,position,t=self.feed([12890,12940,12900,12920,12890,12880])
        result=strategy.evaluate_exit(position,t,t.time)
        self.assertEqual(result[:2],(True,'MOMENTUM_FAILURE_EXIT'))
        self.assertEqual(result[2]['swing_tick_size'],10)
        self.assertEqual(result[2]['floor'],12900)
        self.assertEqual(result[2]['failed_rebound_high'],12920)

    def test_five_won_quote_unit_still_accepts_genuine_two_step_failure(self):
        strategy,position,t=self.feed([3305,3335,3315,3325,3310,3305],
            entry=3305,stop=3290,emergency=3285)
        result=strategy.evaluate_exit(position,t,t.time)
        self.assertEqual(result[:2],(True,'MOMENTUM_FAILURE_EXIT'))
        self.assertEqual(result[2]['swing_tick_size'],5)

    def test_structural_stop_two_independent_prints_preserved(self):
        strategy,position,t=self.feed([12890,12840,12840],seconds=[0,5,6])
        self.assertEqual(strategy.evaluate_exit(position,t,t.time)[:2],(True,'INVALIDATION_STOP_EXIT'))

    def test_emergency_does_not_wait_for_swing_or_stabilization(self):
        strategy,position,t=self.feed([12890,12820],seconds=[0,1])
        self.assertEqual(strategy.evaluate_exit(position,t,t.time)[:2],(True,'EMERGENCY_PLUNGE_EXIT'))

    def test_account_risk_limit_remains_immediate(self):
        strategy,position,t=self.feed([12890,12860],seconds=[0,1])
        position['risk_budget']=1
        self.assertEqual(strategy.evaluate_exit(position,t,t.time)[:2],(True,'ACCOUNT_RISK_LIMIT_EXIT'))

    def test_stale_trade_cannot_authorize_an_exit(self):
        strategy,position,t=self.feed([12890,12820],seconds=[0,1])
        self.assertEqual(strategy.evaluate_exit(position,t,t.time+timedelta(seconds=6))[:2],
                         (False,'CURRENT_TRADE_STALE_OR_FUTURE'))

    def test_incomplete_depth_cannot_authorize_an_exit(self):
        strategy,position,t=self.feed([12890,12820],seconds=[0,1])
        from dataclasses import replace
        self.assertEqual(strategy.evaluate_exit(position,replace(t,depth_available=False),t.time)[:2],
                         (False,'EXECUTABLE_DEPTH_UNKNOWN'))


if __name__=='__main__':unittest.main()
