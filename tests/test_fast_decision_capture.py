"""Promotion-before-recorder-scope regression; no account, network or orders."""
import contextlib
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.engine import AutonomousEngine
from automation.fast_momentum import FastMomentumStrategy
from automation.policy import AutomationConfig, Policy
from automation.trade_review_capture import TradeReviewCapture
from core.settings import Config
from tests.test_automation import FakeBroker
from tests.test_r6_fast_repair import BASE, ticks


class DecisionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.strategy=FastMomentumStrategy(Config(),AutomationConfig())
        self.data=ticks()
        for t in self.data:self.assertTrue(self.strategy.ingest_tick(t,t.time))
        self.symbol=self.data[-1].symbol;self.asof=self.data[-1].time
        self.capture=TradeReviewCapture(self.root/'capture',start=False,clock=lambda:self.asof)
        self.addCleanup(self.capture.close)

    def rows(self):
        self.capture.flush()
        return [json.loads(line) for path in sorted((self.root/'capture').glob('trade-review-*.jsonl'))
                for line in path.read_text('utf-8').splitlines()]

    def test_handoff_candidate_preserves_history_even_before_watch_scope_heartbeat(self):
        # Receiver did not record SHADOW frames in its WATCH-only ring. FAST's
        # accepted tape survives handoff, which is the production missing path.
        for t in self.data:self.capture.tick(t,t.time)
        e=AutonomousEngine(replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04),AutomationConfig(),Policy(self.root/'policy.json'),
                           self.root/'engine',FakeBroker(),clock=lambda:self.asof)
        e.fast_strategy=self.strategy;e.trade_review=self.capture
        with contextlib.redirect_stdout(io.StringIO()):
            e.event('ENTRY_CANDIDATE',symbol=self.symbol,pattern='FAST',
                    signal_detected_at=self.asof.isoformat(),limit=1001,stop=989,qty=4)
        rows=self.rows()
        self.assertEqual(next(r for r in rows if r['event']=='PREENTRY_BUFFER')['rows'],0)
        header=next(r for r in rows if r['event']=='FAST_DECISION_TAPE')
        parts=[r for r in rows if r['event']=='FAST_DECISION_TAPE_PART']
        saved=[v for part in parts for v in part['rows']]
        self.assertEqual(len(saved),len(self.data))
        self.assertEqual(header['retained_rows'],len(saved))
        self.assertTrue(all(p['snapshot_id']==header['snapshot_id'] for p in parts))
        self.assertEqual(saved[0]['exchange_at'],self.data[0].time.isoformat())
        self.assertFalse(e.s['orders']);self.assertFalse(e.s['positions'])

    def test_receipt_cutoff_excludes_later_arrivals_in_same_exchange_second(self):
        t=self.data[-1];later=self.asof+timedelta(milliseconds=200)
        extra=replace(t,price=t.price+1,cumulative=t.cumulative+1,received_at=later.isoformat())
        self.assertTrue(self.strategy.ingest_tick(extra,later))
        proof=self.strategy.snapshot_decision_tape(self.symbol,self.asof)
        self.assertEqual(len(proof['rows']),len(self.data))
        self.assertTrue(all(r['received_at']<=self.asof.isoformat() for r in proof['rows']))

    def test_bounded_exchange_clock_lead_is_preserved_as_actual_received_data(self):
        at=self.asof+timedelta(milliseconds=200)
        t=replace(self.data[-1],time=self.asof+timedelta(seconds=1),
                  cumulative=self.data[-1].cumulative+1,received_at=at.isoformat())
        self.assertTrue(self.strategy.ingest_tick(t,at))
        proof=self.strategy.snapshot_decision_tape(self.symbol,at)
        self.assertEqual(proof['rows'][-1]['exchange_at'],t.time.isoformat())
        self.assertEqual(proof['rows'][-1]['received_at'],at.isoformat())

    def test_row_limit_and_missing_history_remain_explicit(self):
        proof=self.strategy.snapshot_decision_tape(self.symbol,self.asof,max_rows=3)
        self.assertEqual(len(proof['rows']),3);self.assertTrue(proof['truncated'])
        self.assertEqual(proof['eligible_rows'],len(self.data))
        self.assertIsNone(self.strategy.snapshot_decision_tape('MISSING',self.asof))
        self.assertIsNone(self.strategy.snapshot_decision_tape(self.symbol,'invalid'))
        self.assertIsNone(self.strategy.snapshot_decision_tape(self.symbol,self.asof,max_rows=257))

    def test_snapshot_is_detached_and_does_not_change_signal_or_history(self):
        original=list(self.strategy.get_tape(self.symbol).ticks)
        proof=self.strategy.snapshot_decision_tape(self.symbol,self.asof)
        proof['rows'][0]['price']=1
        self.assertEqual(list(self.strategy.get_tape(self.symbol).ticks),original)
        self.assertFalse(self.strategy.active_signals)

    def test_limited_queue_cannot_claim_continuous_coverage_or_disable_caps(self):
        self.capture.queue_rows=2
        proof=self.strategy.snapshot_decision_tape(self.symbol,self.asof)
        self.capture.decision_tape(self.symbol,self.asof,proof)
        self.assertLessEqual(self.capture.stats()['queue_rows'],2)
        self.assertGreater(self.capture.stats()['capture_losses'],0)
        header=next(r for r in self.rows() if r['event']=='FAST_DECISION_TAPE')
        self.assertIn('NOT_CONTINUOUS',header['coverage'])
        self.assertFalse(header['orders_enabled'])

    def test_capture_failure_cannot_modify_order_or_risk_state(self):
        e=AutonomousEngine(replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04),AutomationConfig(),Policy(self.root/'policy.json'),
                           self.root/'engine',FakeBroker(),clock=lambda:self.asof)
        e.fast_strategy=self.strategy;e.trade_review=self.capture
        before=json.dumps(e.s,sort_keys=True)
        with patch.object(self.capture,'decision_tape',side_effect=OSError('disk unavailable')):
            with contextlib.redirect_stdout(io.StringIO()):
                e.event('ENTRY_CANDIDATE',symbol=self.symbol,pattern='FAST',
                        signal_detected_at=self.asof.isoformat(),limit=1001,stop=989,qty=4)
        self.assertEqual(json.dumps(e.s,sort_keys=True),before)
        self.assertEqual(e.trade_review_errors,1)
        self.assertEqual((e.c.risk_fraction,e.c.max_daily_loss_fraction),(.02,.04))


if __name__=='__main__':unittest.main()
