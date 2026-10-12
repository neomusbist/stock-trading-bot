"""Explicit recovery of a confirmed emergency exit; no broker or auto reset."""
import copy,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from core.settings import KST
from core.util import atomic_json
from automation.recovery import recover_reviewed_halt,DATA_EXIT_HALT

class DataExitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.json'
        self.now=datetime(2026,10,12,19,23,tzinfo=KST)
        self.state={'identity':'synthetic','positions':{},'orders':{'sell':{'side':'SELL','symbol':'198440',
            'status':'FILLED','qty':8,'filled':8}},'halt':DATA_EXIT_HALT,
            'halt_info':{'reason':DATA_EXIT_HALT,'active':True,'occurred_at':(self.now-timedelta(seconds=10)).isoformat()},
            'trades':[{'symbol':'198440','qty':8,'reason':'시세 지연/연결 단절','ownership':'BOT',
                       'closed':(self.now-timedelta(seconds=8)).isoformat()}],
            'automation':{'existing':{'005930':{'qty':2}},'cash':77723,'config_hash':'unchanged'},
            'day':'2026-10-12','daily_loss':3694.5081,'realized':-4233.6271,'cash':77723,'entries':{'198440':1}}
        self.snapshot={'account_identity':'synthetic','query_started_at':(self.now-timedelta(seconds=1)).isoformat(),
                       'holdings':{'005930':{'qty':2}},'orders':[]}
        atomic_json(self.path,self.state)
    def recover(self,snapshot=None):return recover_reviewed_halt(self.path,self.state,snapshot or self.snapshot,self.now,'USER_APPROVED_DEPLOYMENT')
    def test_confirmed_stale_exit_explicit_recovery_preserves_loss_orders_and_protection(self):
        before=copy.deepcopy(self.state);result=self.recover()
        self.assertEqual(result['halt'],'');self.assertFalse(result['halt_info']['active'])
        for key in ('day','cash','daily_loss','realized','trades','orders','entries','automation'):
            self.assertEqual(result[key],before[key])
    def test_missing_or_partial_exit_confirmation_cannot_clear_data_halt(self):
        for bad in ('missing_trade','wrong_reason','old_trade','partial_sell','unknown_sell'):
            state=copy.deepcopy(self.state)
            if bad=='missing_trade':state['trades']=[]
            elif bad=='wrong_reason':state['trades'][0]['reason']='구조/이익보호선 이탈'
            elif bad=='old_trade':state['trades'][0]['closed']=(self.now-timedelta(minutes=1)).isoformat()
            elif bad=='partial_sell':state['orders']['sell']['filled']=7
            else:state['orders']['sell']['status']='UNKNOWN'
            with self.assertRaises(ValueError):recover_reviewed_halt(self.path,state,self.snapshot,self.now,'user')
    def test_fresh_flat_broker_and_matching_identity_still_required(self):
        for change in ({'account_identity':'wrong'},{'orders':[{'id':'pending'}]},
                       {'holdings':{'005930':{'qty':3}}},
                       {'query_started_at':(self.now-timedelta(seconds=30)).isoformat()}):
            with self.assertRaises(ValueError):self.recover({**self.snapshot,**change})
    def test_daily_risk_or_unknown_reason_is_not_recoverable(self):
        for reason in ('일일 손실 한도','unreviewed reason'):
            state={**self.state,'halt':reason}
            with self.assertRaisesRegex(ValueError,'HALT_REASON'):recover_reviewed_halt(self.path,state,self.snapshot,self.now,'user')

if __name__=='__main__':unittest.main()
