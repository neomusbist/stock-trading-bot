import copy
import unittest
from datetime import datetime,timedelta,timezone
from automation.external_close_proof import validate_external_close,validate_maintenance_shutdown

NOW=datetime(2026,11,6,13,0,tzinfo=timezone(timedelta(hours=9)))

class ExternalCloseProofTests(unittest.TestCase):
    def setUp(self):
        self.state={'identity':'test','positions':{'999990':{'qty':1,'ownership':'BOT'}},'orders':{},
          'unowned_order_events':[{'symbol':'999990','order_id':'900001','observed_at':NOW.isoformat(),
            'fields':{'909':'700002','913':'체결','905':'-매도','900':'1','902':'0','911':'1','910':'12345'}}]}
        self.snapshot={'account_identity':'test','query_started_at':NOW.isoformat(),'holdings':{},'orders':[],
          'holdings_complete':True,'orders_complete':True,'fills_complete':True,
          'fills':[{'ord_no':'900001','stk_cd':'999990','io_tp_nm':'-매도','ord_stt':'체결',
            'ord_qty':'1','cntr_qty':'1','oso_qty':'0','cntr_pric':'12345','tdy_trde_cmsn':'5','tdy_trde_tax':'70'}]}
    def verify(self):return validate_external_close(self.state,self.snapshot,NOW,symbol='999990',order_id='900001',execution_id='700002')
    def test_proof_does_not_change_state_or_claim_recovery(self):
        original=copy.deepcopy(self.state);result=self.verify()
        self.assertEqual(result['status'],'PROOF_ONLY_NOT_APPLIED');self.assertEqual(result['quantity'],1)
        self.assertEqual(self.state,original)
    def test_rejects_unknown_duplicate_and_mismatched_evidence(self):
        for kind in ('stale','missing_fill','duplicate_fill','wrong_price','already_recorded','open_order','still_held','wrong_execution','incomplete_pages','other_fill'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind=='stale':self.snapshot['query_started_at']=(NOW-timedelta(seconds=16)).isoformat()
                if kind=='missing_fill':self.snapshot['fills']=[]
                if kind=='duplicate_fill':self.snapshot['fills']*=2
                if kind=='wrong_price':self.snapshot['fills'][0]['cntr_pric']='37400'
                if kind=='already_recorded':self.state['external_close_reviews']=[{'order_id':'900001'}]
                if kind=='open_order':self.snapshot['orders']=[{'stk_cd':'999990'}]
                if kind=='still_held':self.snapshot['holdings']={'999990':{'qty':1}}
                if kind=='wrong_execution':self.state['unowned_order_events'][0]['fields']['909']='other'
                if kind=='incomplete_pages':self.snapshot['holdings_complete']=False
                if kind=='other_fill':self.snapshot['fills'].append({'ord_no':'OTHER','stk_cd':'999990'})
                with self.assertRaises(ValueError):self.verify()
    def test_missing_charges_do_not_become_zero(self):
        del self.snapshot['fills'][0]['tdy_trde_tax']
        with self.assertRaises(ValueError):self.verify()
    def test_prior_definitive_zero_fill_rejection_is_not_a_second_fill(self):
        self.snapshot['fills'].append({'ord_no':'REJECTED','stk_cd':'999990','ord_stt':'거부','cntr_qty':'0'})
        self.assertEqual(self.verify()['status'],'PROOF_ONLY_NOT_APPLIED')
    def test_maintenance_is_only_preflight_and_requires_matching_account(self):
        self.state.update(halt='review',automation={'existing':{'005930':{'qty':2}}})
        self.snapshot['holdings']={'005930':{'qty':2}}
        def check():return validate_maintenance_shutdown(self.state,self.snapshot,NOW,symbol='999990',
            order_id='900001',execution_id='700002',policy_mode='STOP')
        result=check()
        self.assertEqual(result['status'],'PREFLIGHT_ONLY_NOT_STOPPED')
        self.assertEqual(result['inflight_requests'],'UNKNOWN')
        self.snapshot['holdings']['005930']['qty']=1
        with self.assertRaisesRegex(ValueError,'OTHER_HOLDINGS_MISMATCH'):check()

if __name__=='__main__':unittest.main()
