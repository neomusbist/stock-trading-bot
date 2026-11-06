import copy,json,tempfile,threading,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from automation.external_close_commit import commit_staged_close

class CommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'live/state.json';self.path.parent.mkdir()
        self.before={'positions':{'999990':{'qty':1}},'halt':'H','automation':{'existing':{'005930':{'qty':2}}},'trades':[]}
        self.after=copy.deepcopy(self.before);self.after['positions']={}
        self.after['external_close_reviews']=[{'order_id':'1','execution_id':'2'}]
        self.after['trades']=[{'pnl_source':'BROKER_QUERY'}]
        self.proof={'status':'PROOF_ONLY_NOT_APPLIED','symbol':'999990','order_id':'1','execution_id':'2',
                    'quantity':1,'price':12345,'account_query_started_at':'2026-11-06T13:40:00+09:00'}
        self.path.write_text(json.dumps(self.before),encoding='utf8')
        self.service=type('Svc',(),{'_idle_thread':threading.get_ident()})()
    def run_commit(self):
        return commit_staged_close(service=self.service,state_path=self.path,backup_dir=self.root/'backup',
            original=self.before,staged=self.after,proof=self.proof,stamp=datetime.now(timezone.utc))
    def test_applies_once_with_backup_and_receipt(self):
        self.assertEqual(self.run_commit()['status'],'APPLIED')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')),self.after)
        self.assertEqual(json.loads((self.root/'backup/before.json').read_text(encoding='utf8')),self.before)
        with self.assertRaisesRegex(ValueError,'STATE_CHANGED'):self.run_commit()
    def test_log_failure_rolls_back(self):
        with patch('automation.external_close_commit.append_jsonl',side_effect=OSError('unwritable')):
            with self.assertRaises(OSError):self.run_commit()
        self.assertEqual(json.loads(self.path.read_text(encoding='utf8')),self.before)
    def test_rejects_missing_lock(self):
        self.service._idle_thread=None
        with self.assertRaisesRegex(ValueError,'IDLE_LOCK'):self.run_commit()
