"""Offline continuous trade and reconciliation races; no real broker."""
import copy,json,time,unittest
from datetime import timedelta
from unittest.mock import patch
from tests import test_reconciliation_race as fixture
from automation.recovery import recover_reviewed_halt

class ContinuousTradeTests(unittest.TestCase):
    setUp=fixture.ReconciliationRaceTests.setUp
    create_position=fixture.ReconciliationRaceTests.create_position
    def snapshot(self,holdings=None,started=None):
        return {'holdings':holdings or {},'orders':[],'cash':20000,'equity':20000,
                'query_started_at':(started or self.clock[0]).isoformat()}
    def sell(self,sym='069540',qty=2):
        self.clock[0]+=timedelta(seconds=1)
        self.engine.reconcile(self.snapshot({sym:{'qty':qty,'available':qty,'avg':4960}}))
        self.engine.request_exit(sym,'구조/이익보호선 이탈')
        self.engine.drive_exits({sym:4900})
        return self.engine.active_orders(sym,'SELL')[0]
    def test_balance_reaches_zero_before_sell_fill_does_not_halt_or_duplicate(self):
        self.create_position();o=self.sell();self.clock[0]+=timedelta(seconds=2)
        before=len(self.broker.orders)
        self.assertFalse(self.engine.reconcile(self.snapshot()))
        self.assertEqual(self.engine.s['halt'],'');self.assertFalse(self.engine.account_ok)
        self.assertTrue(self.engine.s['positions']['069540']['qty_blocked'])
        self.engine.drive_exits({'069540':4900});self.assertEqual(len(self.broker.orders),before)
        self.engine.cumulative_fill(o,2,9800)
        self.clock[0]+=timedelta(seconds=1);self.engine.reconcile(self.snapshot())
        self.assertTrue(self.engine.account_ok);self.assertFalse(self.engine.s['positions']);self.assertEqual(self.engine.s['halt'],'')
    def test_sell_partial_fill_old_snapshot_is_not_a_quantity_mismatch(self):
        self.create_position();o=self.sell();started=self.clock[0]
        self.clock[0]+=timedelta(seconds=1);self.engine.cumulative_fill(o,1,4900)
        self.assertFalse(self.engine.reconcile(self.snapshot({'069540':{'qty':2}},started)))
        self.assertEqual(self.engine.s['halt'],'');self.assertFalse(self.engine.account_ok)
        self.clock[0]+=timedelta(seconds=1)
        self.engine.reconcile(self.snapshot({'069540':{'qty':1,'available':1,'avg':4960}}))
        self.assertEqual(self.engine.s['positions']['069540']['qty'],1)
        self.assertEqual(self.engine.s['halt'],'')
    def test_unconfirmed_sell_beyond_timeout_still_halts(self):
        self.create_position();self.sell();self.clock[0]+=timedelta(seconds=11)
        self.engine.reconcile(self.snapshot())
        self.assertTrue(self.engine.s['halt'])
    def test_pending_sell_cannot_explain_increased_balance(self):
        self.create_position();self.sell();self.clock[0]+=timedelta(seconds=1)
        self.engine.reconcile(self.snapshot({'069540':{'qty':3}}))
        self.assertTrue(self.engine.s['halt'])
    def test_rest_partial_sell_fill_with_older_balance_defers_before_mismatch(self):
        self.create_position();o=self.sell();started=self.clock[0]
        self.clock[0]+=timedelta(seconds=1)
        fills=[{'ord_no':o['id'],'ord_stt':'체결','cntr_qty':1,'cntr_pric':4900}]
        self.assertFalse(self.engine.reconcile(self.snapshot({'069540':{'qty':2}},started),fills))
        self.assertEqual(self.engine.s['positions']['069540']['qty'],1)
        self.assertEqual(self.engine.s['halt'],'')
    def test_five_roundtrips_same_and_different_symbols_return_to_waiting(self):
        # Actual Engine submit/fill/reconcile boundary, not just state assignments.
        self.engine.c=__import__('dataclasses').replace(self.engine.c,risk_fraction=.02,max_daily_loss_fraction=.04,max_entries_per_day=None,max_entries_per_symbol=None)
        self.engine.a=__import__('dataclasses').replace(self.engine.a,max_consecutive_losses=None)
        self.engine.roll_day()
        for i,sym in enumerate(('069540','042510','069540','038500','002990')):
            # Independent re-entry must still respect the existing cooldown;
            # removing a cooldown is not part of this safety repair.
            self.clock[0]+=timedelta(seconds=self.engine.a.cooldown_seconds+1 if sym in self.engine.s['entries'] else 3)
            self.engine.market_status[sym]={'pass':True}
            self.create_position(sym)
            o=self.sell(sym)
            price=4980 if i%2==0 else 4940
            self.engine.cumulative_fill(o,2,2*price)
            self.clock[0]+=timedelta(seconds=1);self.engine.reconcile(self.snapshot())
            self.assertEqual(self.engine.s['halt'],'');self.assertFalse(self.engine.s['positions'])
            self.assertFalse(self.engine.active_orders());self.assertTrue(self.engine.account_ok)
        self.assertEqual(len(self.engine.s['trades']),5)
        self.assertEqual(sum(self.engine.s['entries'].values()),5)
        self.assertEqual(len(self.broker.orders),10)
    def test_explicit_recovery_preserves_loss_cash_history_and_needs_identity(self):
        self.engine.halt('관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.clock[0]+=timedelta(seconds=2);original=copy.deepcopy(self.engine.s)
        snap={**self.snapshot(),'account_identity':original['identity']}
        wrong={**snap,'account_identity':'different'}
        with self.assertRaisesRegex(ValueError,'IDENTITY'):recover_reviewed_halt(self.engine.path,original,wrong,self.clock[0],'user')
        updated=recover_reviewed_halt(self.engine.path,original,snap,self.clock[0],'user')
        for key in ('day','daily_loss','realized','cash','automation','trades','orders','entries'):
            self.assertEqual(updated[key],original[key])
        self.assertEqual(updated['halt'],'');self.assertFalse(updated['halt_info']['active'])
    def test_daily_risk_halt_and_broker_unfilled_cannot_be_recovered(self):
        self.engine.halt('일일 손실 한도');self.clock[0]+=timedelta(seconds=2)
        snap={**self.snapshot(),'account_identity':self.engine.s['identity']}
        with self.assertRaises(ValueError):recover_reviewed_halt(self.engine.path,self.engine.s,snap,self.clock[0],'user')
        self.engine.halt('관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.clock[0]+=timedelta(seconds=2);snap={**self.snapshot(),'account_identity':self.engine.s['identity'],'orders':[{'ord_no':'unknown'}]}
        with self.assertRaisesRegex(ValueError,'PENDING'):recover_reviewed_halt(self.engine.path,self.engine.s,snap,self.clock[0],'user')

class OperatorRecoveryTests(unittest.TestCase):
    from tests import test_operator_interface as ui
    setUp=ui.OperatorInterfaceTests.setUp
    def prepare(self):
        from pathlib import Path
        from core.util import atomic_json
        self.policy.command('STOP')
        self.path=self.root/'runtime/automation-production/state.json'
        self.state={'identity':'test-id','positions':{},'orders':{},'halt':'관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요',
            'halt_info':{'reason':'관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요','active':True,'occurred_at':(self.clock[0]-timedelta(seconds=5)).isoformat()},
            'automation':{'existing':{}},'day':'2026-10-01','daily_loss':100,'cash':10000,'realized':-100,'entries':{'069540':4},'trades':[]}
        atomic_json(self.path,self.state)
        self.gateway.recovery_snapshot=lambda:{'account_identity':'test-id','query_started_at':self.clock[0].isoformat(),'holdings':{},'orders':[],'cash':10000}
    def test_stopped_official_service_recovery_preserves_settings_and_counters(self):
        self.prepare();settings=self.s.settings()
        self.assertEqual(self.s.recover_halt(True)['verdict'],'RECOVERED')
        recovered=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(recovered['entries'],self.state['entries']);self.assertEqual(recovered['daily_loss'],100)
        self.assertEqual(self.s.settings(),settings);self.assertEqual(self.gateway.orders,[])
    def test_running_engine_or_missing_explicit_confirmation_cannot_be_recovered(self):
        self.prepare()
        with self.assertRaises(ValueError):self.s.recover_halt(False)
        with patch.object(self.s,'running',return_value=True):
            with self.assertRaisesRegex(ValueError,'STOP_RUNNER'):self.s.recover_halt(True)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),self.state)

if __name__=='__main__':unittest.main()
