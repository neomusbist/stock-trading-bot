"""Recorder -> exact freeze -> disk index -> four detector variants, offline."""
from datetime import datetime,timedelta
from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from automation.trade_review_capture import TradeReviewCapture
from core.settings import KST
from core.strategy import Tick
from research.evidence_collector import freeze_session
from research.session_validation import verify_freeze,index_freeze,known_at,main,paper_from_preopen

AT=datetime(2026,10,26,9,tzinfo=KST)

class RecordedSessionValidation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.live=self.root/'live';self.live.mkdir()
        self.freeze=self.root/'frozen';self.output=self.root/'comparison'
        repo=Path(__file__).resolve().parents[1]
        for src,dst in [('config.example.json','config.json'),('automation.example.json','automation.json')]:
            value=json.loads((repo/src).read_bytes())
            if dst=='automation.json':value.update(fast_momentum_enabled=False,pullback_expansion_target_enabled=False)
            (self.live/dst).write_text(json.dumps(value),encoding='utf-8')
        folder=self.live/'runtime/automation-production/trade-review'
        self.recorder=TradeReviewCapture(folder,start=False,min_free_bytes=0,clock=lambda:AT)
        self.addCleanup(self.recorder.close)
        self.recorder.set_scope({'999990':{'status':'WATCH','source_count':2,'first_watch_at':AT.isoformat()}},{},AT)
    def tick(self,second):
        at=AT+timedelta(seconds=second)
        return Tick('999990',at,1000,100,100*(second+1),1001,1000,True,
            received_at=at.isoformat(),received_monotonic=1000+second,
            book_received_at=at.isoformat(),book_received_monotonic=1000+second,
            ask_qty=100,bid_qty=100,depth_available=True,book_time=at,depth_source='0B_FIELDS')
    def record(self):
        for i in range(0,181,5):
            t=self.tick(i);self.recorder.tick(t,t.time);self.recorder.tick(t,t.time,t.time)
        self.recorder.book('999990',{'time':(AT+timedelta(seconds=181)).isoformat(),
            'best_bid':1000,'best_ask':1001,'bid_qty':100,'ask_qty':100,
            'depth_available':True,'source':'0D'},AT+timedelta(seconds=181))
        self.recorder.flush()
        return freeze_session(self.live,self.freeze,'2026-10-26')
    def test_real_recorder_receipt_ids_survive_freeze_and_disk_index(self):
        self.record()
        with closing(sqlite3.connect(':memory:')) as db:
            _,counts=index_freeze(self.freeze,db,'2026-10-26','REGULAR')
            rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.event')='TRADE'")]
        self.assertEqual(counts['TRADE'],74)
        receiver=rows[0]['receipt_evidence'];ingest=rows[1]['receipt_evidence']
        self.assertEqual(receiver,ingest);self.assertTrue(receiver['trade_receipt_id'].startswith('0B:999990:'))
        self.assertNotEqual(receiver['trade_receipt_id'],receiver['book_receipt_id'])
    def test_four_variants_execute_without_order_authority_or_fake_cash(self):
        self.record()
        before={str(p):p.read_bytes() for p in self.live.rglob('*') if p.is_file()}
        self.assertEqual(main(['--freeze',str(self.freeze),'--output',str(self.output),'--evaluations','received']),0)
        result=json.loads((self.output/'SUMMARY.json').read_bytes())
        self.assertEqual(result['actual_orders'],0);self.assertEqual(result['network_attempts'],0)
        self.assertEqual(len(result['results']),4)
        for r in result['results']:
            self.assertEqual(r['counts']['accepted_ingestions'],37)
            self.assertIsNone(r['paper_account']);self.assertEqual(r['paper_basis'],'UNKNOWN_NO_PREOPEN_STATE')
            self.assertGreater(r['coverage_warnings']['BOOK_STRATEGY_AVAILABILITY_UNKNOWN'],0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.live.rglob('*') if p.is_file()})
    def test_tampered_frozen_file_is_rejected_before_output(self):
        self.record();(self.freeze/'config.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'FROZEN_INPUT_CHANGED'):
            main(['--freeze',str(self.freeze),'--output',str(self.output)])
        self.assertFalse(self.output.exists())
    def test_manifest_path_cannot_read_outside_frozen_root(self):
        self.record();p=self.freeze/'MANIFEST.json';m=json.loads(p.read_bytes())
        m['files'][0]['relative']='../outside';p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'UNSAFE_OR_DUPLICATE'):verify_freeze(self.freeze)
    def test_unfinished_last_record_is_reported_and_not_replayed(self):
        self.recorder.flush();p=next((self.live/'runtime/automation-production/trade-review').glob('*.jsonl'))
        with p.open('ab') as stream:stream.write(b'{"event":"TRADE"')
        freeze_session(self.live,self.freeze,'2026-10-26')
        with closing(sqlite3.connect(':memory:')) as db:
            _,counts=index_freeze(self.freeze,db,'2026-10-26','REGULAR')
        self.assertEqual(counts['INCOMPLETE_FILE_TAILS'],1);self.assertNotIn('TRADE',counts)
    def test_availability_is_latest_actual_clock_not_exchange_or_bar_label(self):
        self.assertEqual(known_at({'exchange_at':AT.isoformat(),'received_at':AT.isoformat(),
            'processed_at':(AT+timedelta(seconds=2)).isoformat()}),AT+timedelta(seconds=2))
        self.assertIsNone(known_at({'exchange_at':AT.isoformat()}))
        self.assertIsNone(known_at({'event':'TRADE','time':AT.isoformat()}))
        self.assertEqual(known_at({'event':'RAW_CANDIDATES','observed_at':AT.isoformat(),
            'time':(AT+timedelta(seconds=2)).isoformat()}),AT+timedelta(seconds=2))
    def test_watch_rank_rotation_is_distinct_from_actual_unsubscribe(self):
        t=self.tick(0);self.recorder.tick(t,t.time,t.time)
        self.recorder.set_scope({}, {}, AT+timedelta(seconds=1))
        self.recorder.set_scope({'999990':{'status':'WATCH','source_count':2}}, {}, AT+timedelta(seconds=2))
        self.recorder.subscription([],['999990'],AT+timedelta(seconds=3))
        self.record()
        main(['--freeze',str(self.freeze),'--output',str(self.output),'--evaluations','received'])
        rows=[json.loads(x) for x in (self.output/'baseline.jsonl').read_text(encoding='utf-8').splitlines()]
        role=[r for r in rows if r.get('cause')=='WATCH_METADATA']
        unsub=[r for r in rows if r.get('cause')=='SUBSCRIPTION_SCOPE']
        self.assertTrue(role);self.assertIsNone(role[-1]['coverage_gap'])
        self.assertEqual(unsub[-1]['coverage_gap']['reason'],'ACTUAL_UNSUBSCRIBE')
    def test_after_session_is_not_mixed_into_regular(self):
        t=replace(self.tick(0),time=AT.replace(hour=16),received_at=AT.replace(hour=16).isoformat())
        self.recorder.tick(t,t.time,t.time);self.record()
        with closing(sqlite3.connect(':memory:')) as db:
            _,counts=index_freeze(self.freeze,db,'2026-10-26','REGULAR')
        self.assertEqual(counts['TRADE'],74)
    def test_successful_subscription_ack_does_not_reset_continuity(self):
        self.recorder.stream('REG',AT+timedelta(seconds=1));self.record()
        main(['--freeze',str(self.freeze),'--output',str(self.output),'--evaluations','received'])
        d=json.loads((self.output/'baseline.json').read_bytes())
        self.assertNotIn('STREAM_REG',d['coverage_warnings'])
        self.assertEqual(d['counts']['stream_control_REG'],1)
    def test_preopen_paper_preserves_manual_holds_and_day_risk_counters(self):
        manifest=self.record()
        state=self.live/'runtime/automation-production/state.json'
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({'day':'2026-10-26','cash':70000,'positions':{},'orders':{},
            'daily_loss':120,'entries':{'999990':1},'consecutive_losses':1,
            'automation':{'existing':{'005930':{'qty':1,'available':1,'avg':60000}},
                'capital_snapshot':{'actual_orderable_cash':70000}}}))
        (self.live/'runtime/RELEASE_IDENTITY.json').write_text('{}')
        preopen=self.root/'preopen'
        freeze_session(self.live,preopen,'2026-10-26',preopen_at=AT.replace(hour=8,minute=50))
        from core.settings import load_config
        from automation.policy import load_automation
        account,basis=paper_from_preopen(preopen,manifest,load_config(self.live/'config.json'),load_automation(self.live/'automation.json'))
        self.assertIsNotNone(account,basis);self.addCleanup(account.close)
        self.assertEqual(account.broker.manual['005930']['qty'],1)
        self.assertEqual(account.e.s['daily_loss'],120);self.assertEqual(account.e.s['entries'],{'999990':1})
        self.assertEqual(account.broker.submissions,[])
    def test_reconstructed_bars_and_real_subscription_gap_are_separate_states(self):
        self.record()
        main(['--freeze',str(self.freeze),'--output',str(self.output),'--evaluations','received'])
        rows=[json.loads(x) for x in (self.output/'baseline.jsonl').read_text(encoding='utf-8').splitlines()]
        states=[x for x in rows if x.get('event')=='EVALUATION_STATE']
        self.assertEqual(states[0]['stored_bars'],0)
        self.assertEqual(states[-1]['stored_bars'],3)
        self.assertEqual(states[-1]['usable_bars'],3)
        self.assertEqual(states[0]['observation_start'],states[-1]['observation_start'])
    def test_raw_recognition_and_retry_processing_join_existing_journals(self):
        events=self.live/'runtime/automation-production/events.jsonl'
        events.write_text('\n'.join(json.dumps(x) for x in (
            {'event':'RAW_CANDIDATES','time':(AT+timedelta(seconds=2)).isoformat(),
             'observed_at':AT.isoformat(),'candidates':[{'code':'999990','first_raw_at':'2026-10-23T09:00:00+09:00'}]},
            {'event':'ENTRY_QUOTE_RETRY','time':(AT+timedelta(seconds=182)).isoformat(),
             'symbol':'999990','trade_received_at':(AT+timedelta(seconds=180)).isoformat(),
             'book_received_at':(AT+timedelta(seconds=181)).isoformat()},
        ))+'\n',encoding='utf-8')
        self.record();main(['--freeze',str(self.freeze),'--output',str(self.output),'--evaluations','received'])
        d=json.loads((self.output/'baseline.json').read_bytes())
        self.assertEqual(d['counts']['actual_quote_retry_processing_joined'],1)
        rows=[json.loads(x) for x in (self.output/'baseline.jsonl').read_text(encoding='utf-8').splitlines()]
        found=[x['raw_first_observed'] for x in rows if isinstance(x.get('raw_first_observed'),dict)]
        self.assertTrue(found)
        self.assertEqual(found[-1]['recognized_at'],(AT+timedelta(seconds=2)).isoformat())
    def test_record_budget_failure_has_no_complete_summary(self):
        self.record()
        with self.assertRaisesRegex(ValueError,'RECORD_BUDGET_EXCEEDED'):
            main(['--freeze',str(self.freeze),'--output',str(self.output),'--max-records','1'])
        self.assertFalse((self.output/'SUMMARY.json').exists())

if __name__=='__main__':unittest.main()
