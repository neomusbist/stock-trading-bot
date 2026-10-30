"""Incident-specific operator recovery; all files and accounts are synthetic."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from automation import recovery
from core.settings import KST
from core.util import atomic_json


class ReviewedBufferRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'runtime/automation-production/state.json'
        self.path.parent.mkdir(parents=True)
        self.incident=datetime(2026,10,30,13,16,30,291646,tzinfo=KST)
        self.now=self.incident+timedelta(minutes=10)
        self.state={'halt':recovery.BUFFER_HALT,'halt_info':{'reason':recovery.BUFFER_HALT,
            'occurred_at':self.incident.isoformat(),'active':True,'classification':'MANUAL_REVIEW'},
            'positions':{},'orders':{},'identity':'synthetic','automation':{'existing':{'005930':{'qty':2}}},
            'day':'2026-10-30','daily_loss':100,'cash':70000,'entries':{'x':1}}
        self.snapshot={'account_identity':'synthetic','query_started_at':(self.now-timedelta(seconds=1)).isoformat(),
                       'orders':[],'holdings':{'005930':{'qty':2}}}
        atomic_json(self.path,self.state)
        record={'event':'RUNTIME_ERROR','error':'KeyError','exception_message':"'buffer'",
                'traceback':"File automation\\engine.py in entry_market_reason\nKeyError: 'buffer'"}
        raw=(json.dumps(record)+'\n').encode();archive=self.path.parent/'archive';archive.mkdir()
        (archive/'event.jsonl').write_bytes(raw)
        engine=self.root/'automation/engine.py';engine.parent.mkdir();engine.write_bytes(b'synthetic fixed engine')
        sha=hashlib.sha256(engine.read_bytes()).hexdigest()
        identity={'release_id':'TEST_RELEASE','candidate_only':False,'live_deployed':True,
                  'files':{'automation/engine.py':sha}}
        atomic_json(self.root/'runtime/RELEASE_IDENTITY.json',identity)
        values={'BUFFER_INCIDENT_AT':self.incident.isoformat(),'BUFFER_EVENT_FILE':'event.jsonl',
                'BUFFER_EVENT_OFFSET':0,'BUFFER_EVENT_LENGTH':len(raw),'BUFFER_EVENT_SHA256':hashlib.sha256(raw).hexdigest(),
                'BUFFER_FIXED_RELEASE':'TEST_RELEASE','BUFFER_FIXED_ENGINE_SHA256':sha}
        self.addCleanup(patch.stopall)
        for name,value in values.items():patch.object(recovery,name,value).start()
        # Unit fault injection only. A separate release-integrity test runs the real checker.
        patch('automation.release_integrity.check_runtime_integrity',return_value={'valid':True,'manifest_verified':True}).start()

    def run_recovery(self,state=None,snapshot=None,confirmation=recovery.BUFFER_RECOVERY_CONFIRMATION):
        return recovery.recover_reviewed_halt(self.path,state or self.state,snapshot or self.snapshot,
                                               self.now,'EXPLICIT_USER_REVIEW',buffer_confirmation=confirmation)

    def test_verified_explicit_recovery_keeps_financial_state_and_audits(self):
        result=self.run_recovery()
        self.assertEqual(result['halt'],'')
        for key in ('day','cash','daily_loss','entries','orders','automation'):
            self.assertEqual(result[key],self.state[key])
        self.assertEqual(result['halt_info']['incident_review']['event_sha256'],recovery.BUFFER_EVENT_SHA256)
        self.assertIn('MANUAL_HALT_RECOVERED',(self.path.parent/'events.jsonl').read_text(encoding='utf8'))

    def test_unconfirmed_unknown_or_other_keyerror_stays_blocked(self):
        for confirmation in (None,'','RECOVER',True):
            with self.subTest(confirmation=confirmation),self.assertRaises(ValueError):
                self.run_recovery(confirmation=confirmation)
        bad=copy.deepcopy(self.state);bad['halt_info']['occurred_at']=(self.incident-timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'INCIDENT_MISMATCH'):self.run_recovery(state=bad)

    def test_tampered_evidence_or_uninstalled_fix_stays_blocked(self):
        (self.path.parent/'archive/event.jsonl').write_bytes(b'altered')
        with self.assertRaisesRegex(ValueError,'EVIDENCE_MISMATCH'):self.run_recovery()

    def test_wrong_or_tampered_release_and_unverified_integrity_stay_blocked(self):
        identity_path=self.root/'runtime/RELEASE_IDENTITY.json'
        identity=json.loads(identity_path.read_text(encoding='utf8'))
        for change in ({'release_id':'OTHER'}, {'candidate_only':True}, {'live_deployed':False}):
            atomic_json(identity_path,{**identity,**change})
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'RELEASE_MISMATCH'):self.run_recovery()
        atomic_json(identity_path,identity)
        (self.root/'automation/engine.py').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'RELEASE_MISMATCH'):self.run_recovery()
        with patch('automation.release_integrity.check_runtime_integrity',return_value={'valid':False}):
            with self.assertRaisesRegex(ValueError,'INTEGRITY_FAILED'):self.run_recovery()

    def test_position_and_unresolved_order_block_recovery(self):
        for change in ({'positions':{'999990':{'qty':1}}},
                       {'orders':{'x':{'status':'UNKNOWN'}}},
                       {'orders':{'x':{'status':'PARTIAL'}}}):
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'NON_FLAT'):
                self.run_recovery(state={**self.state,**change})

    def test_audit_write_failure_restores_original_halt(self):
        with patch('automation.recovery.append_jsonl',side_effect=OSError('synthetic audit failure')):
            with self.assertRaises(OSError):self.run_recovery()
        saved=json.loads(self.path.read_text(encoding='utf8'))
        self.assertEqual(saved,self.state)

    def test_wrong_broker_identity_pending_holdings_and_stale_rejected(self):
        for change in ({'account_identity':'other'},{'orders':[{'id':'x'}]},
                       {'holdings':{'005930':{'qty':3}}},
                       {'query_started_at':(self.now-timedelta(seconds=16)).isoformat()}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.run_recovery(snapshot={**self.snapshot,**change})


if __name__=='__main__':unittest.main()
