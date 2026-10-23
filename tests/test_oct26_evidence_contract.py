"""Observation-only contracts; no strategy activation or financial endpoint."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
import os
from copy import deepcopy

from automation.trade_review_capture import TradeReviewCapture

AT=datetime(2026,10,26,9,tzinfo=timezone(timedelta(hours=9)))

class MetadataContinuity(unittest.TestCase):
    def test_order_event_scope_refresh_does_not_erase_qualification(self):
        with tempfile.TemporaryDirectory() as folder:
            c=TradeReviewCapture(folder,start=False,min_free_bytes=0,clock=lambda:AT)
            self.addCleanup(c.close)
            watch={'999990':{'status':'WATCH','source_count':2,'first_watch_at':AT.isoformat()}}
            c.set_scope(watch,{},AT)
            for _ in range(3):c.event_ref('NO_TRADE',{},AT,{})
            self.assertEqual(c.watch_metadata.get('999990',{}).get('source_count'),2)
            c.flush()
            rows=[json.loads(x) for p in Path(folder).glob('trade-review-*.jsonl') for x in p.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len([x for x in rows if x['event']=='WATCH_METADATA']),1)

    def test_role_only_removal_preserves_remaining_qualification(self):
        with tempfile.TemporaryDirectory() as folder:
            c=TradeReviewCapture(folder,start=False,min_free_bytes=0,clock=lambda:AT);self.addCleanup(c.close)
            c.set_scope({'999990':{'source_count':2},'999991':{'source_count':1}},{},AT)
            c.set_scope({'999990'},{},AT)
            self.assertEqual(c.watch_metadata.get('999990',{}).get('source_count'),2)
            self.assertNotIn('999991',c.watch_metadata)

    def test_capture_declares_sample_limits_without_claiming_first_onset(self):
        with tempfile.TemporaryDirectory() as folder:
            c=TradeReviewCapture(folder,start=False,min_free_bytes=0,clock=lambda:AT);self.addCleanup(c.close);c.flush()
            row=json.loads(next(Path(folder).glob('trade-review-*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
            self.assertEqual(row.get('detail_sampling_seconds'),{'opening_before_0930':15,'otherwise':60})
            self.assertFalse(row['samples_establish_exact_first_onset'])

class OrderReceiptLink(unittest.TestCase):
    def test_original_B_paper_intent_retains_the_received_trade_id(self):
        from tests.test_live_ready_20261021 import LiveReadyLifecycle
        h=LiveReadyLifecycle();h.setUp();self.addCleanup(h.doCleanups);h.seed('B')
        buys=[o for o in h.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1)
        receipt=buys[0]['signal'].get('receipt_evidence') or {}
        self.assertTrue(str(receipt.get('trade_receipt_id','')).startswith('0B:999990:'))
        self.assertTrue(h.broker.intent_checks[0]['durable_intent'])

@unittest.skipUnless(os.environ.get('R6_OCT23_FROZEN_LEDGER'),'Private Oct23 preserved ledger required')
class MondayPreservedLedger(unittest.TestCase):
    def test_oct23_ledger_and_approval_roll_to_monday_without_transport(self):
        from automation.engine import AutonomousEngine
        from automation.policy import Policy,load_automation
        from automation.readiness import authorization_valid
        from core.settings import load_config
        from tests.test_automation import FakeBroker
        import shutil
        frozen=Path(os.environ['R6_OCT23_FROZEN_LEDGER']).resolve()
        original=json.loads(frozen.read_bytes());root=frozen.parents[2]
        cfg=load_config(root/'config.json');auto=load_automation(root/'automation.json')
        with tempfile.TemporaryDirectory() as folder:
            dest=Path(folder);book=dest/'runtime/automation-production';book.mkdir(parents=True)
            shutil.copy2(frozen,book/'state.json')
            shutil.copy2(root/'runtime/operator_user_authorization.json',dest/'runtime/operator_user_authorization.json')
            policy=Policy(dest/'runtime/automation_policy.json');broker=FakeBroker();broker.identity=original['identity']
            e=AutonomousEngine(cfg,auto,policy,book,broker,lambda:AT.replace(hour=8,minute=50))
            self.assertEqual(e.s['day'],'2026-10-26');self.assertEqual(e.s['trades'],original['trades'])
            self.assertEqual(e.s['automation']['existing'],original['automation']['existing'])
            self.assertEqual(e.s['positions'],original['positions']);self.assertEqual(e.active_orders(),[])
            self.assertEqual(e.s['daily_loss'],0);self.assertEqual(e.s['entries'],{});self.assertFalse(e.account_ok)
            ok,reasons=authorization_valid(dest,cfg,auto,policy.state,AT.replace(hour=8,minute=50))
            self.assertFalse(ok);self.assertIn('DAILY_LIVE_APPROVAL_EXPIRED',reasons)
            self.assertEqual(broker.orders,[])

if __name__=='__main__':unittest.main()
