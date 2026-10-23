"""Filtered replay sequence gaps must not be mistaken for recorder losses."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3,tempfile,unittest
from research.evidence_collector import freeze_session,audit_freeze
from research.session_validation import index_freeze

class WholeSelectedSequence(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source'
        folder=self.source/'runtime/automation-production/trade-review';folder.mkdir(parents=True)
        self.path=folder/'trade-review-20261026-0001.jsonl';self.freeze=self.root/'frozen'
    def write(self,sequences):
        rows=[{'event':'CAPTURE_STARTED' if seq==1 else 'POSITION_LINES','capture_session':'fixture',
            'capture_sequence':seq,'capture_losses':0,'at':'2026-10-26T09:00:00+09:00'} for seq in sequences]
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf-8')
        freeze_session(self.source,self.freeze,'2026-10-26')
    def test_unsupported_replay_events_still_prove_contiguous_recorder_sequence(self):
        self.write([1,2,3]);result=audit_freeze(self.freeze,self.root/'audit.json')
        self.assertEqual(result['sequenced_rows'],3);self.assertEqual(result['counts'].get('UNPROVEN_SEQUENCE_RANGES',0),0)
    def test_missing_prefix_and_middle_ranges_are_located(self):
        self.write([2,5]);result=audit_freeze(self.freeze,self.root/'audit.json')
        self.assertEqual([(r['first_missing'],r['last_missing']) for r in result['sequence_ranges']],[(1,1),(3,4)])
        self.assertTrue(all(r['next_source'] for r in result['sequence_ranges']))
    def test_overwrite_is_refused(self):
        self.write([1]);out=self.root/'audit.json';audit_freeze(self.freeze,out)
        with self.assertRaisesRegex(ValueError,'NEW_EXTERNAL'):audit_freeze(self.freeze,out)

if __name__=='__main__':unittest.main()
