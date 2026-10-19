"""Persist no-entry evidence within existing limits, without persisting all ticks."""
from datetime import datetime,timedelta
from pathlib import Path
import json,tempfile,unittest

from automation.trade_review_capture import TradeReviewCapture
from core.settings import KST
from core.strategy import Tick

AT=datetime(2026,10,19,9,1,tzinfo=KST)


class OpportunityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def capture(self,**kw):
        c=TradeReviewCapture(self.folder,start=False,clock=lambda:AT,**kw)
        self.addCleanup(c.close);c.set_scope(['079650'],{},AT);return c

    def tick(self,at=AT,symbol='079650'):
        return Tick(symbol,at,7950,100,1000,7950,7940,ask_qty=100,bid_qty=100,
                    depth_available=True,book_time=at,received_at=at.isoformat(),received_monotonic=1000.)

    def rows(self,c):
        c.flush()
        return [json.loads(line) for p in self.folder.glob('trade-review-*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]

    def sample(self,c,at=AT,symbol='079650'):
        c.evaluation(self.tick(at,symbol),{'status':'WATCH','source_count':2},
            {'gates':{'liquidity':{'result':'FAIL','values':{'window_turnover':123}}},'trials':[]},
            {'time':at.isoformat(),'status':'REJECT','reason':'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER'},at)

    def test_completed_watch_bar_survives_without_a_trade_candidate(self):
        c=self.capture();bar={'time':AT-timedelta(minutes=1),'open':7900,'high':7960,
            'low':7890,'close':7950,'volume':10000,'gap_reason':None}
        c.tick(self.tick(),AT)
        c.completed_bar('079650',bar,AT,AT+timedelta(seconds=1))
        c.completed_bar('079650',bar,AT,AT+timedelta(seconds=1))
        rows=self.rows(c)
        self.assertEqual(len([r for r in rows if r['event']=='COMPLETED_BAR']),1)
        self.assertFalse(any(r['event']=='TRADE' for r in rows))
        self.assertEqual(next(r for r in rows if r['event']=='COMPLETED_BAR')['processed_at'],(AT+timedelta(seconds=1)).isoformat())

    def test_opening_samples_are_bounded_and_expose_actual_fast_rejection(self):
        c=self.capture()
        for i in range(31):self.sample(c,AT+timedelta(seconds=i))
        rows=[r for r in self.rows(c) if r['event']=='OPPORTUNITY_SAMPLE']
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]['fast']['reason'],'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER')
        self.assertEqual(rows[0]['received_monotonic'],1000.)
        self.assertEqual(rows[0]['gates']['liquidity']['values']['window_turnover'],123)
        self.assertTrue(all(r['coverage']=='SAMPLED_NOT_ALL_OPPORTUNITIES' and not r['actual_order_permission'] for r in rows))

    def test_later_session_uses_one_sample_per_minute(self):
        c=self.capture();start=AT.replace(hour=10)
        for i in range(121):self.sample(c,start+timedelta(seconds=i))
        self.assertEqual(len([r for r in self.rows(c) if r['event']=='OPPORTUNITY_SAMPLE']),3)

    def test_unwatched_symbols_do_not_force_disk_capture(self):
        c=self.capture();self.sample(c,symbol='009190')
        self.assertFalse(any(r['event']=='OPPORTUNITY_SAMPLE' for r in self.rows(c)))

    def test_day_and_total_caps_still_apply_to_new_samples(self):
        c=self.capture(day_bytes=1600,total_bytes=1600)
        for i in range(10):self.sample(c,AT+timedelta(minutes=i));c.flush()
        self.assertTrue(c.stats()['storage_blocked'])
        self.assertLessEqual(sum(p.stat().st_size for p in self.folder.glob('trade-review-*.jsonl')),1600)
        self.assertGreater(c.stats()['capture_losses'],0)

    def test_watch_change_drops_only_sampling_state_not_existing_files(self):
        c=self.capture();self.sample(c);self.rows(c);paths={p:p.read_bytes() for p in self.folder.glob('trade-review-*.jsonl')}
        c.set_scope([],{},AT+timedelta(minutes=1))
        self.assertNotIn('079650',c.last_evaluation_at)
        self.assertTrue(all(p.read_bytes()==data for p,data in paths.items()))
