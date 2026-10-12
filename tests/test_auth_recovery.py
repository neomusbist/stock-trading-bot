"""No network: invalid-token recovery is for reads, never order retransmission."""
import contextlib,os,threading,unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import Mock,patch
from core.broker import Rest,Rejected
from core.settings import Config,now
from operator_service import Gateway,OperatorService

BAD={'return_code':3,'return_msg':'인증에 실패했습니다[8005:Token이 유효하지 않습니다]'}
GOOD={'return_code':0,'value':'read-only'}

class AuthenticationRecoveryTests(unittest.TestCase):
    def client(self,**kwargs):
        with patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE-AUTH','KIWOOM_APP_SECRET':'FAKE'}):
            client=Rest(Config(live_mode=True),**kwargs)
        client.token='OLD';client.expires=now()+timedelta(days=1)
        return client
    def test_explicit_invalid_token_query_refreshes_once(self):
        client=self.client();client._http=Mock(side_effect=[(BAD,{}),({'token':'NEW','expires_dt':'20500101000000'},{}),(GOOD,{})])
        self.assertEqual(client.query('kt00018',{}),GOOD)
        self.assertEqual([c.args[0] for c in client._http.call_args_list],['/api/dostk/acnt','/oauth2/token','/api/dostk/acnt'])
        self.assertEqual(client._http.call_args_list[-1].args[2]['authorization'],'Bearer NEW')
    def test_repeated_invalid_token_does_not_loop(self):
        client=self.client();client._http=Mock(side_effect=[(BAD,{}),({'token':'NEW','expires_dt':'20500101000000'},{}),(BAD,{})])
        with self.assertRaises(Rejected) as error:client.query('kt00018',{})
        self.assertEqual(error.exception.response,BAD);self.assertEqual(client._http.call_count,3)
    def test_code_three_alone_never_refreshes(self):
        client=self.client();client._http=Mock(return_value=({'return_code':3,'return_msg':'another error'},{}))
        with self.assertRaises(Rejected):client.query('kt00018',{})
        self.assertEqual(client._http.call_count,1);self.assertEqual(client.token,'OLD')
    def test_other_business_rejection_is_not_retried(self):
        client=self.client();client._http=Mock(return_value=({'return_code':20,'return_msg':'business rejection 8005:'},{}))
        with self.assertRaises(Rejected):client.query('kt00018',{})
        self.assertEqual(client._http.call_count,1)
    def test_buy_sell_cancel_are_never_retransmitted(self):
        for api in ('kt10000','kt10001','kt10003'):
            with self.subTest(api=api):
                client=self.client();client._http=Mock(return_value=(BAD,{}))
                with self.assertRaises(Rejected):client.call(api,'/api/dostk/ordr',{},mutation=True)
                self.assertEqual(client._http.call_count,1);self.assertEqual(client.token,'OLD')
    def test_diagnostic_never_refreshes_invalid_authentication(self):
        client=self.client();client.diagnostic_only=True;client._http=Mock(return_value=(BAD,{}))
        with self.assertRaises(Rejected):client.query('kt00018',{})
        self.assertEqual(client._http.call_count,1);self.assertEqual(client.token,'OLD')
    def test_concurrent_refresh_preserves_new_shared_token(self):
        client=self.client();paths=[]
        def http(path,body,headers):
            paths.append(path)
            if len(paths)==1:
                # Another client has already refreshed the shared session while
                # this request with OLD was in flight.
                client.token='OTHER-NEW';return BAD,{}
            self.assertEqual(headers['authorization'],'Bearer OTHER-NEW');return GOOD,{}
        client._http=http
        self.assertEqual(client.query('kt00018',{}),GOOD)
        self.assertNotIn('/oauth2/token',paths)
    def test_expired_diagnostic_auth_is_blocked_without_http(self):
        client=self.client();client.token='';client.diagnostic_only=True;client._http=Mock()
        with self.assertRaisesRegex(Rejected,'DIAGNOSTIC_DEFERRED_AUTH_REFRESH'):client.query('kt00018',{})
        client._http.assert_not_called()
    def test_running_gateway_rejects_account_and_recovery_before_auth(self):
        service=Mock();service.running.return_value=True;gateway=Gateway(service)
        with patch('main.load_credentials') as credentials:
            with self.assertRaisesRegex(ValueError,'STOPPED_RUNNER'):gateway.snapshot()
            with self.assertRaisesRegex(ValueError,'STOPPED_RUNNER'):gateway.recovery_snapshot()
        credentials.assert_not_called();service.idle.assert_not_called()
    def test_stopped_snapshot_holds_idle_lock_through_account_read(self):
        service=Mock();service.running.return_value=False;locked=[]
        @contextlib.contextmanager
        def idle():
            locked.append(True)
            try:yield
            finally:locked.pop()
        service.idle=idle;gateway=Gateway(service)
        def read():self.assertTrue(locked);return GOOD
        with patch.object(gateway,'_snapshot_stopped',side_effect=read):self.assertEqual(gateway.snapshot(),GOOD)
        self.assertEqual(locked,[])
    def test_official_recovery_reads_under_own_idle_lock_not_false_running(self):
        with tempfile.TemporaryDirectory() as folder:
            service=OperatorService(Path(folder));gateway=Gateway(service)
            rest=Mock();rest.identity='FAKE-IDENTITY';rest.snapshot.return_value={'orders':[],'holdings':{},'cash':0}
            with patch('operator_service.load_config',return_value=Config()),patch('main.load_credentials'),patch('core.broker.Rest',return_value=rest):
                with service.idle():
                    self.assertTrue(service.running())  # busy sees our own OS locks.
                    result=gateway.recovery_snapshot()
                    self.assertEqual(result['account_identity'],'FAKE-IDENTITY')
                    errors=[]
                    def other_thread():
                        try:gateway.recovery_snapshot()
                        except ValueError as e:errors.append(str(e))
                    thread=threading.Thread(target=other_thread);thread.start();thread.join()
                    self.assertEqual(errors,['ACCOUNT_PROBE_REQUIRES_STOPPED_RUNNER_IDLE_LOCK'])
                self.assertIsNone(service._idle_thread)
                with self.assertRaisesRegex(ValueError,'IDLE_LOCK'):gateway.recovery_snapshot()
            self.assertEqual(rest.snapshot.call_count,1)

if __name__=='__main__':unittest.main()
