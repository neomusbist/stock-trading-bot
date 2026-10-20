import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta

from research.evidence_collector import JsonTail,ResearchEvidenceCollector
from tests.test_causal_replay import AT,SYM


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'live';self.source.mkdir()
        self.output=self.root/'research'
    def collector(self,**kw):
        c=ResearchEvidenceCollector(self.output,self.source,clock=lambda:AT,start_writer=False,**kw)
        self.addCleanup(c.close);return c
    def rows(self,c):
        c.capture.flush()
        return [json.loads(line) for p in self.output.glob('trade-review-*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
    def test_output_cannot_overlap_live(self):
        with self.assertRaises(ValueError):ResearchEvidenceCollector(self.source/'runtime/research',self.source)
    def test_partial_json_tail_is_not_advanced_or_invented(self):
        p=self.source/'a.jsonl';p.write_bytes(b'{"x":1}\n{"x":')
        tail=JsonTail(p);self.assertEqual(tail.read(),[{'x':1}])
        self.assertEqual(tail.read(),[])
        with p.open('ab') as f:f.write(b'2}\n')
        self.assertEqual(tail.read(),[{'x':2}])
    def test_next_session_starts_at_eof_without_rescanning_history(self):
        p=self.source/'a.jsonl';p.write_text('{"old":true}\n',encoding='utf-8')
        tail=JsonTail(p,start_at_end=True);self.assertEqual(tail.read(),[])
        with p.open('a',encoding='utf-8') as f:f.write('{"new":true}\n')
        self.assertEqual(tail.read(),[{'new':True}])
    def test_capacity_selects_first_arrivals_and_records_exclusion(self):
        c=self.collector();e={'entry':1000,'stop':990,'target':1040}
        for sym in ('000001','000002','000003'):c.candidate(sym,'A',AT,e)
        self.assertEqual(len(c.active),2)
        rows=self.rows(c)
        selected=[r['symbol'] for r in rows if r['event']=='RESEARCH_EPISODE']
        self.assertEqual(selected,['000001','000002'])
        self.assertEqual(next(r for r in rows if r['event']=='RESEARCH_EPISODE_EXCLUDED')['symbol'],'000003')
        self.assertFalse(any(r.get('actual_order_permission') for r in rows))
    def test_repeat_is_not_an_independent_opportunity(self):
        c=self.collector();e={'entry':1000,'stop':990,'target':1040}
        for _ in range(10):c.candidate(SYM,'A',AT,e)
        self.assertEqual(c.stats['INDEPENDENT_EPISODES_SELECTED'],1)
        self.assertEqual(c.stats['REPEATED_EVENT'],9)
    def test_window_end_is_not_a_simulated_sell(self):
        c=self.collector(window_seconds=60);c.candidate(SYM,'A',AT,{'entry':1000})
        c.prune(AT+timedelta(seconds=61))
        rows=self.rows(c)
        self.assertFalse(c.active)
        self.assertEqual(next(r for r in rows if r['event']=='RESEARCH_EPISODE_END')['reason'],
            'OBSERVATION_WINDOW_END_NOT_A_VIRTUAL_FILL')
    def test_missing_source_processing_is_unknown(self):
        c=self.collector();c.frame({'event':'TRADE','time':AT.isoformat(),'received':AT.isoformat(),'symbol':SYM})
        self.assertEqual(c.stats['FRAME_AVAILABILITY_UNKNOWN'],1)
    def test_original_capture_losses_are_retained(self):
        c=self.collector();c.metadata({'event':'OPPORTUNITY_SAMPLE','asof':AT.isoformat(),'symbol':SYM,
            'watch':{},'trials':[],'capture_session':'source','capture_sequence':500,'capture_losses':7})
        row=next(r for r in self.rows(c) if r['event']=='OPPORTUNITY_SAMPLE')
        self.assertEqual(row['source_capture']['capture_losses'],7)
    def test_observer_never_writes_source(self):
        marker=self.source/'state.json';marker.write_text('UNCHANGED',encoding='utf-8')
        before={str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        c=self.collector();c.candidate(SYM,'A',AT,{'entry':1000});self.rows(c)
        after={str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
