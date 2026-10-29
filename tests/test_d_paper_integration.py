"""Synthetic D-only causal replay; no live order or market performance claim."""
import contextlib,io,unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from research.strict_benchmark import FullPathReplay
from tests import test_strict_benchmark_pipeline as f

class DPaperIntegration(unittest.TestCase):
    def setUp(self):
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__,None,None,None)
        cfg,auto=f.settings();self.r=FullPathReplay(f.AT-timedelta(minutes=5),'D',cfg,auto,70420,latency_ms=25)
        self.addCleanup(self.r.close)

    def watch(self,at,**changes):
        return {'code':f.SYMBOL,'status':'WATCH','source_count':2,
                'source_details':{'SYNTHETIC_1':{},'SYNTHETIC_2':{}},'resistance':38000,
                'market_status':{'pass':True,'checked_at':at.isoformat(),'reason_code':'SYNTHETIC_STATUS_NORMAL'},'next_resistance':45000,
                'next_resistance_known_at':(f.AT-timedelta(minutes=6)).isoformat(),**changes}

    def setup_input(self):
        original=self.r.step
        def historical(frame,**kwargs):return original(frame,evaluate=False)
        self.r.step=historical
        try:
            with patch.object(f,'metadata',side_effect=self.watch):
                cum=f.populate(self.r)
        finally:self.r.step=original
        return cum

    def test_detector_engine_order_fill_and_exit_in_sealed_paper(self):
        cum=self.setup_input();self.r.qualify(f.SYMBOL,self.watch(f.AT),f.AT)
        order=self.r.step(f.packet(f.AT,38100,cum+20000))
        self.assertIsNotNone(order,self.r.result()['reasons'])
        self.assertEqual(order['signal']['pattern'],'D')
        self.assertTrue(order['signal']['paper_only'])
        self.assertGreaterEqual(order['signal']['entry_economics']['net_rr'],1.5)
        for sec,price in ((1,38100),(2,37000),(3,37000)):
            self.r.step(f.packet(f.AT+timedelta(seconds=sec),price,cum+20000*(sec+1)))
        result=self.r.result()
        self.assertEqual([x['side'] for x in result['fills']],['BUY','SELL'])
        self.assertFalse(result['actual_order_permission'])

    def test_missing_or_future_overhead_never_becomes_a_target(self):
        cum=self.setup_input()
        for change in ({'next_resistance':None}, {'next_resistance_known_at':(f.AT+timedelta(seconds=1)).isoformat()}):
            with self.subTest(change=change):
                w=self.watch(f.AT,**change);self.r.qualify(f.SYMBOL,w,f.AT)
                self.assertIsNone(self.r.step(f.packet(f.AT,38100,cum+20000),quote_retry=True))
                self.assertFalse(self.r.detector.last_eval[f.SYMBOL]['trials'][0]['candidate_generated'])
        self.assertEqual(self.r.account.broker.submissions,[])
