import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from research.fast_timing_observation import replay,strict_breakout_trigger


class FastTimingObservationTests(unittest.TestCase):
    def setUp(self):
        self.at=datetime.fromisoformat('2026-11-05T18:21:00+09:00')
    def row(self,sec,price=1000,volume=100,cumulative=None):
        at=self.at+timedelta(seconds=sec)
        return {'symbol':'443670','event':'TRADE','time':at.isoformat(),'received':at.isoformat(),
            'price':price,'volume':volume,'cumulative':cumulative or 1000+100*sec,
            'ask':price+1,'bid':price-1,'ask_qty':100,'bid_qty':100,'depth_available':True,
            'book_time':at.isoformat(),'book_received_at':at.isoformat(),'regular':True}
    def test_future_rows_do_not_change_prior_results_or_claim_orderability(self):
        early=[self.row(i) for i in range(1,5)]
        later=self.row(5,volume=10000,cumulative=20000)
        start=self.at;end=self.at+timedelta(seconds=10)
        a=replay(early,'443670',start,end)
        b=replay(early+[later],'443670',start,end)
        self.assertEqual(a['rows'],b['rows'][:len(a['rows'])])
        self.assertEqual(a['rows'][-1]['regime_reason'],'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER')
        self.assertEqual(b['rows'][-1]['turnover_10'],10400000)
        self.assertTrue(all(r['orderable']=='UNKNOWN' for r in b['rows']))
        self.assertEqual(b['feed_continuity'],'UNKNOWN')
    def test_duplicate_and_out_of_order_receive_are_counted(self):
        rows=[self.row(1),self.row(2),self.row(1),self.row(2)]
        result=replay(rows,'443670',self.at,self.at+timedelta(seconds=10))
        self.assertEqual(result['accepted_rows'],2)
        self.assertEqual(result['rejected_rows'],2)
    def test_missing_book_is_not_treated_as_executable(self):
        row=self.row(1);row['book_time']=None
        result=replay([row],'443670',self.at,self.at+timedelta(seconds=10))
        self.assertEqual(result['rows'][0]['quote_reason'],'BOOK_TIMESTAMP_UNKNOWN_OR_STALE')
        self.assertEqual(result['rows'][0]['orderable'],'UNKNOWN')
    def test_breakout_equal_is_not_a_pass_and_pullback_route_is_unknown(self):
        s={'pass':True,'breakout':5030}
        self.assertEqual(strict_breakout_trigger(SimpleNamespace(price=5030,ask=5030),s),'FAIL_TRADE_NOT_ABOVE_BREAKOUT')
        self.assertEqual(strict_breakout_trigger(SimpleNamespace(price=5040,ask=5040),s),'PASS_STRICT_BREAKOUT_ONLY')
        self.assertEqual(strict_breakout_trigger(SimpleNamespace(price=5020,ask=5020),s),'UNKNOWN_OTHER_GEOMETRY_ROUTE')

if __name__=='__main__':unittest.main()
