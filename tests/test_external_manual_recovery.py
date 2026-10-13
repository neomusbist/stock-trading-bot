"""A manually executed SELL is reviewed, never replaced by another live SELL."""
import copy,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from core.settings import KST
from core.util import atomic_json,read_json_retry
from automation.recovery import recover_reviewed_halt

NOW=datetime(2026,10,13,19,35,tzinfo=KST)

class ExternalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.json'
        self.state={'identity':'synthetic','halt':'관리 종목 외부 미체결 주문','positions':{},
            'day':'2026-10-13','daily_loss':1000,'cash':50000,'entries':{'999990':1},
            'halt_info':{'occurred_at':(NOW-timedelta(minutes=10)).isoformat(),'active':True},
            'automation':{'existing':{'005930':{'qty':2}}},
            'orders':{'external':{'id':'TEST-SELL','symbol':'999990','side':'SELL','qty':3,'filled':3,'amount':30000,
                'status':'FILLED','signal':{'ownership':'USER_EXTERNAL'}}},
            'external_close_reviews':[{'source':'BROKER_CONFIRMED_USER_EXTERNAL_SELL','local':'external','symbol':'999990',
                'order_id':'TEST-SELL','quantity':3,'price':10000,'observed_at':(NOW-timedelta(seconds=5)).isoformat()}]}
        self.snapshot={'query_started_at':(NOW-timedelta(seconds=1)).isoformat(),'account_identity':'synthetic',
            'orders':[],'holdings':{'005930':{'qty':2}},'fills':[{'ord_no':'TEST-SELL','stk_cd':'999990',
                'io_tp_nm':'-매도','ord_stt':'체결','cntr_qty':'3','ord_qty':'3','oso_qty':'0','cntr_pric':'10000'}]}
        atomic_json(self.path,self.state)
    def test_fresh_matched_manual_full_fill_recovers_without_resetting_budget_or_ledger(self):
        result=recover_reviewed_halt(self.path,self.state,self.snapshot,NOW,'EXPLICIT_USER_MANUAL_CLOSE_REVIEW')
        self.assertEqual(result['halt'],'')
        for k in ('day','daily_loss','cash','entries','orders'):self.assertEqual(result[k],self.state[k])
    def test_flat_account_alone_cannot_clear_external_order_halt(self):
        state=copy.deepcopy(self.state);state.pop('external_close_reviews')
        with self.assertRaisesRegex(ValueError,'REVIEW_REQUIRED'):recover_reviewed_halt(self.path,state,self.snapshot,NOW,'review')
    def test_wrong_price_quantity_side_or_missing_fill_cannot_recover(self):
        for key,value in (('cntr_pric','10001'),('cntr_qty','2'),('io_tp_nm','+매수'),('ord_stt','접수')):
            snap=copy.deepcopy(self.snapshot);snap['fills'][0][key]=value
            with self.assertRaisesRegex(ValueError,'PROOF_INVALID'):recover_reviewed_halt(self.path,self.state,snap,NOW,'review')
        snap=copy.deepcopy(self.snapshot);snap['fills']=[]
        with self.assertRaisesRegex(ValueError,'PROOF_INVALID'):recover_reviewed_halt(self.path,self.state,snap,NOW,'review')
    def test_duplicated_fill_or_old_review_is_not_confirmation(self):
        snap=copy.deepcopy(self.snapshot);snap['fills']*=2
        with self.assertRaisesRegex(ValueError,'PROOF_INVALID'):recover_reviewed_halt(self.path,self.state,snap,NOW,'review')
        state=copy.deepcopy(self.state);state['external_close_reviews'][0]['observed_at']=(NOW-timedelta(minutes=6)).isoformat()
        with self.assertRaisesRegex(ValueError,'PROOF_INVALID'):recover_reviewed_halt(self.path,state,self.snapshot,NOW,'review')
    def test_pending_or_retained_bot_quantity_cannot_recover(self):
        for field,value in (('positions',{'999990':{'qty':1}}),('orders',{'foreign':{'status':'UNKNOWN'}})):
            state=copy.deepcopy(self.state);state[field]=value
            with self.assertRaisesRegex(ValueError,'NON_FLAT'):recover_reviewed_halt(self.path,state,self.snapshot,NOW,'review')
        snap=copy.deepcopy(self.snapshot);snap['orders']=[{'ord_no':'foreign'}]
        with self.assertRaisesRegex(ValueError,'PENDING'):recover_reviewed_halt(self.path,self.state,snap,NOW,'review')
    def test_ordinary_bot_fill_or_protected_account_mismatch_is_not_manual_clearance(self):
        state=copy.deepcopy(self.state);state['orders']['external']['signal']['ownership']='BOT'
        with self.assertRaisesRegex(ValueError,'PROOF_INVALID'):recover_reviewed_halt(self.path,state,self.snapshot,NOW,'review')
        snap=copy.deepcopy(self.snapshot);snap['holdings']['005930']['qty']=1
        with self.assertRaisesRegex(ValueError,'HOLDINGS_MISMATCH'):recover_reviewed_halt(self.path,self.state,snap,NOW,'review')
