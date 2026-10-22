from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from automation.runner import process_message
from core.settings import Config,KST
from core.strategy import Tick
AT=datetime(2026,10,22,10,40,tzinfo=KST)

class RegularTickProgress(unittest.TestCase):
    def run_tick(self,prior,current):
        engine=SimpleNamespace(s={'positions':{}},c=Config(),a=SimpleNamespace(fast_momentum_enabled=False),
            clock=lambda:AT,market='3',connected=True,on_tick=Mock(),
            last_regular_tick=prior,last_regular_tick_at=prior.time if prior else None)
        detector=SimpleNamespace(ingest=Mock(return_value=True),history={},completed={})
        receipt=datetime.fromisoformat(current.received_at)
        frame={'trnm':'REAL','data':[{'type':'0B','item':current.symbol,'_observation_tick':(receipt,current)}]}
        count=process_message(engine,detector,frame,receipt,{current.symbol},{},None,{},None,False)
        self.assertEqual(count,1)
        return engine

    def tick(self,at,sym='999990',receipt=None):
        return Tick(sym,at,1000,10,1000,1001,1000,True,received_at=(receipt or at).isoformat())

    def test_old_history_on_other_symbol_cannot_revoke_current_market_evidence(self):
        newest=self.tick(AT)
        e=self.run_tick(newest,self.tick(AT-timedelta(seconds=8),'999991'))
        self.assertIs(e.last_regular_tick,newest)
        self.assertEqual(e.last_regular_tick_at,AT)
        e.on_tick.assert_not_called()

    def test_equal_exchange_second_cannot_replace_a_newer_receipt_with_older_receipt(self):
        newest=self.tick(AT)
        e=self.run_tick(newest,self.tick(AT,'999991',AT-timedelta(seconds=1)))
        self.assertIs(e.last_regular_tick,newest)

    def test_first_old_history_remains_stale_evidence_and_newer_print_advances(self):
        old=self.tick(AT-timedelta(seconds=8));e=self.run_tick(None,old)
        self.assertEqual(e.last_regular_tick_at,old.time)
        newest=self.tick(AT);e=self.run_tick(old,newest)
        self.assertIs(e.last_regular_tick,newest)
