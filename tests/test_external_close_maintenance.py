import json,tempfile,unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from tests.test_external_close_proof import ExternalCloseProofTests,NOW
from automation.runner import complete_external_close_maintenance
from operator_service import OperatorService

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        fixture=ExternalCloseProofTests();fixture.setUp()
        self.state=fixture.state;self.state.update(halt='review',automation={'existing':{'005930':{'qty':2}}})
        self.state['unowned_order_events'][0]['observed_at']=(NOW-timedelta(seconds=3)).isoformat()
        self.snapshot=fixture.snapshot;self.snapshot['holdings']={'005930':{'qty':2}}
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
        self.path=self.folder/'request.json'
        self.request={'kind':'REVIEW_EXTERNAL_CLOSE','status':'REQUESTED','request_id':'test',
            'requested_at':(NOW-timedelta(seconds=1)).isoformat(),'symbol':'999990','order_id':'900001','execution_id':'700002'}
        self.path.write_text(json.dumps(self.request),encoding='utf8')
        self.engine=SimpleNamespace(s=self.state,save=Mock(),event=Mock(),folder=self.folder)
        self.account=SimpleNamespace(rest=SimpleNamespace(identity='test'),stop=Mock(),request=Mock(),thread=Mock())
        self.account.thread.is_alive.return_value=False
        self.policy=SimpleNamespace(mode='STOP')
        self.result={'error':None,'snapshot':self.snapshot,'fills':self.snapshot['fills'],'fills_complete':True}
    def attempt(self):return complete_external_close_maintenance(self.engine,self.policy,self.account,self.result,self.path,NOW)
    def test_broker_confirmed_external_close_records_stop_without_changing_position(self):
        self.assertTrue(self.attempt());self.engine.save.assert_called_once()
        self.assertEqual(self.state['positions']['999990']['qty'],1)
        self.assertEqual(self.state['halt'],'review')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8'))['status'],'COMPLETED')
        self.assertIn('MAINTENANCE_EXIT',(self.folder/'events.jsonl').read_text(encoding='utf8'))
    def test_missing_broker_fill_or_unresolved_order_never_stops(self):
        for bad in ('fills','open_order','mode','old_query'):
            with self.subTest(bad=bad):
                self.setUp()
                if bad=='fills':self.result['fills']=[]
                if bad=='open_order':self.snapshot['orders']=[{'stk_cd':'999990'}]
                if bad=='mode':self.policy.mode='LIVE'
                if bad=='old_query':self.snapshot['query_started_at']=(NOW-timedelta(seconds=2)).isoformat()
                self.assertFalse(self.attempt());self.account.stop.set.assert_not_called()
    def test_operator_request_to_worker_proof_without_broker_orders(self):
        runtime=self.folder/'runtime';(runtime/'automation-production').mkdir(parents=True)
        (runtime/'automation_policy.json').write_text(json.dumps({'mode':'STOP'}),encoding='utf8')
        (runtime/'automation-production/state.json').write_text(json.dumps(self.state),encoding='utf8')
        svc=object.__new__(OperatorService);svc.root=self.folder;svc.clock=lambda:NOW-timedelta(seconds=1)
        svc.running=lambda:True;svc.settings=lambda:(SimpleNamespace(broker_environment='production'),None,None)
        result=svc.request_external_close_maintenance('999990','900001','700002',confirmed=True)
        self.assertEqual(result['verdict'],'REQUESTED')
        request_path=runtime/'maintenance_request.json'
        self.assertTrue(complete_external_close_maintenance(self.engine,self.policy,self.account,self.result,request_path,NOW))
        self.assertEqual(json.loads(request_path.read_text(encoding='utf8'))['status'],'COMPLETED')
    def test_policy_changed_to_live_during_preflight_never_stops(self):
        self.policy.refresh=lambda:setattr(self.policy,'mode','LIVE')
        self.assertFalse(self.attempt())
        self.account.stop.set.assert_not_called()
