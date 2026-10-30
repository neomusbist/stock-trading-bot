"""Synthetic C2 proof with production C safety flags; never sends real orders."""
import contextlib,io,unittest
import os
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from datetime import timedelta
from research.c2_validation import C2Replay
from research.c2_proof import verify_c2_proof
from tests.test_c2_box_retest import AT,populate,settings
from tests.test_strict_benchmark_pipeline import packet

class C2LiveFlags(unittest.TestCase):
    def setUp(self):
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)
        root=os.environ.get('R6_LIVE_CONFIG_ROOT')
        if root:
            from core.settings import load_config
            from automation.policy import load_automation
            self.c=load_config(Path(root)/'config.json');self.a=load_automation(Path(root)/'automation.json')
        else:
            c,a=settings();self.c=c;self.a=replace(a,fresh_box_basis_enabled=True,confirmed_box_direction=True)
    def replay(self):
        r=C2Replay(AT,'C2',self.c,self.a,70847);self.addCleanup(r.close);return r
    def test_proof_with_live_c_flags_reaches_sealed_paper_buy(self):
        r=self.replay()
        with patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK')):
            order,frame=populate(r)
            self.assertIsNotNone(order)
            self.assertTrue(verify_c2_proof(order['signal'],frame.tick,r.cfg,r.auto))
            for sec,price in ((1,10050),(2,10400),(3,9890),(4,9890)):
                r.step(packet(frame.available_at+timedelta(seconds=sec),price,frame.tick.cumulative+sec*5000,5000,symbol=frame.tick.symbol,depth=10000))
        self.assertIsNotNone(order)
        self.assertEqual([x['side'] for x in r.result()['fills']],['BUY','SELL'])
        self.assertFalse(r.result()['actual_order_permission'])
    def test_tampering_or_real_broker_never_passes(self):
        r=self.replay();order,frame=populate(r);self.assertIsNotNone(order)
        sig=deepcopy(order['signal'])
        for field in ('break_price','known_at'):
            bad=deepcopy(sig)
            bad['c2_structure_proof'][field]=0 if field=='break_price' else '2099-01-01T00:00:00+09:00'
            self.assertFalse(verify_c2_proof(bad,frame.tick,r.cfg,r.auto))
            self.assertEqual(r.account.e.entry_market_reason(bad,order['qty'],frame.tick),'C2_STRUCTURE_PROOF_INVALID')
        for mutation in ('target','stop','retest','overhead','receipt','gap'):
            bad=deepcopy(sig)
            if mutation=='target':bad['target']+=1000
            elif mutation=='stop':bad['stop']+=10
            elif mutation=='retest':bad['c2_structure_proof']['post_break_bars'][-1]['low']-=500
            elif mutation=='overhead':bad['c2_structure_proof']['target_evidence']['source_bars'][1]['high']+=10
            elif mutation=='receipt':bad['c2_structure_proof']['break_receipt']['trade_receipt_id']=None
            else:bad['c2_structure_proof']['post_break_bars'][-1]['gap_reason']='DATA_LOSS_GAP'
            self.assertFalse(verify_c2_proof(bad,frame.tick,r.cfg,r.auto),mutation)
        r.account.e.paper_mode=False
        self.assertEqual(r.account.e.entry_market_reason(sig,order['qty'],frame.tick),'C2_SEALED_PAPER_REQUIRED')
