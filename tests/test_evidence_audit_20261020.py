"""Offline audit cutoffs, partial fills and recording schema; no broker calls."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest

from automation.trade_review_capture import TradeReviewCapture,bar_knowledge_time,sampled_reward_ratio
from core.strategy import Tick
from research.r6_evidence_audit import captured_lines,grouped_trades,inventory,known_at


AT=datetime(2026,10,19,11,40,tzinfo=timezone(timedelta(hours=9)))


class EvidenceClockTests(unittest.TestCase):
    def test_bar_waits_for_actual_processing(self):
        bar={'time':(AT-timedelta(minutes=1)).isoformat()}
        out=bar_knowledge_time(bar,AT,AT+timedelta(seconds=7))
        self.assertEqual(out['known_at'],(AT+timedelta(seconds=7)).isoformat())

    def test_bar_cannot_become_known_before_its_close(self):
        out=bar_knowledge_time({'time':AT.isoformat()},AT,AT+timedelta(seconds=59))
        self.assertEqual(out['known_at'],(AT+timedelta(minutes=1)).isoformat())

    def test_missing_or_shifted_bar_clock_stays_unknown(self):
        for bar in ({},{'time':AT.replace(second=1).isoformat()},{'time':AT.replace(tzinfo=None).isoformat()}):
            self.assertIsNone(bar_knowledge_time(bar,AT,AT)['known_at'])

    def test_historical_confirmation_does_not_backdate_bar(self):
        row={'event':'COMPLETED_BAR','bar':{'time':(AT-timedelta(minutes=1)).isoformat()},
             'confirmed_at':AT.isoformat(),'processed_at':(AT+timedelta(seconds=12)).isoformat()}
        self.assertEqual(known_at(row),AT+timedelta(seconds=12))

    def test_tick_is_ordered_by_receipt_not_exchange_time(self):
        self.assertEqual(known_at({'event':'TRADE','received_at':AT.isoformat(),
                                  'exchange_at':(AT-timedelta(seconds=2)).isoformat()}),AT)

    def test_missing_processing_time_is_not_a_completed_bar(self):
        self.assertIsNone(known_at({'event':'COMPLETED_BAR','bar':{'time':AT.isoformat()},'confirmed_at':AT.isoformat()}))


class EvidenceRatioTests(unittest.TestCase):
    def test_actual_abc_producer_key_is_preserved(self):
        self.assertEqual(sampled_reward_ratio({'calculated_rr':1.7}),(1.7,'calculated_rr'))

    def test_fast_net_ratio_basis_is_distinct(self):
        self.assertEqual(sampled_reward_ratio({'net_rr':2.3}),(2.3,'net_rr'))

    def test_zero_is_a_known_ratio_not_unknown(self):
        self.assertEqual(sampled_reward_ratio({'calculated_rr':0,'rr':3}),(0,'calculated_rr'))

    def test_missing_ratio_not_fabricated(self):
        self.assertEqual(sampled_reward_ratio({}),(None,'UNKNOWN'))

    def test_sample_writes_calculated_ratio_without_changing_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=TradeReviewCapture(tmp,start=False,clock=lambda:AT)
            try:
                c.set_scope(['X'],{},AT)
                t=Tick('X',AT,100,1,1,101,100)
                evaluation={'trials':[{'setup':'B','candidate_generated':True,'entry_ready':False,
                    'gates':{'risk_reward':{'values':{'calculated_rr':1.2,'target':120}}}}]}
                c.evaluation(t,{},evaluation,{},AT);c.flush()
                rows=[json.loads(line) for p in Path(tmp).glob('trade-review-*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
                trial=next(r for r in rows if r['event']=='OPPORTUNITY_SAMPLE')['trials'][0]
                self.assertEqual((trial['rr'],trial['rr_basis']),(1.2,'calculated_rr'))
                self.assertFalse(evaluation['trials'][0]['entry_ready'])
                self.assertFalse(c.stats()['orders_enabled'])
            finally:c.close()


class InventoryTests(unittest.TestCase):
    def trade(self,qty=1,exit_price=110,**extra):
        return {'trade_id':'episode','symbol':'X','opened':AT.isoformat(),
            'closed':(AT+timedelta(seconds=20)).isoformat(),'qty':qty,'entry':100,'exit':exit_price,
            'net_pnl':(exit_price-100)*qty-1,'fee_actual':1,'pnl_source':'BROKER_QUERY',**extra}

    def test_partial_sells_are_one_trade_with_weighted_exit_and_summed_cost(self):
        row=grouped_trades({'trades':[self.trade(),self.trade(qty=3,exit_price=108)]},{'2026-10-19'})[0]
        self.assertEqual((row['qty'],row['sell_fill_rows'],row['exit'],row['actual_costs']),(4,2,108.5,2))
        self.assertEqual(row['broker_net_pnl'],32)

    def test_distinct_reentry_not_merged_with_previous_trade(self):
        rows=grouped_trades({'trades':[self.trade(),self.trade(trade_id='next')]},{'2026-10-19'})
        self.assertEqual(len(rows),2)

    def test_estimated_pnl_never_becomes_broker_confirmed(self):
        row=grouped_trades({'trades':[self.trade(pnl_source='ESTIMATE')]},{'2026-10-19'})[0]
        self.assertIsNone(row['broker_net_pnl'])

    def test_missing_market_extrema_never_become_zero(self):
        row=grouped_trades({'trades':[self.trade(MFE=None,MAE=None)]},{'2026-10-19'})[0]
        self.assertIsNone(row['MFE_seen_per_share'])
        self.assertIsNone(row['MAE_seen_per_share'])

    def test_freeze_read_extent_and_partial_row_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x';p.write_bytes(b'{"event":"A"}\n{"event":')
            size=p.stat().st_size
            with p.open('ab') as f:f.write(b'"B"}\n')
            self.assertEqual(list(captured_lines(p,size)),[({'event':'A'},14),(None,9)])

    def test_missing_tape_has_no_synthetic_data_or_opportunity_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=inventory({'trades':[self.trade()]},tmp,{'2026-10-19'})
            self.assertEqual(out['trades'][0]['coverage'],{})
            self.assertIsNone(out['independent_opportunity_count'])
            self.assertEqual(out['actual_orders'],0)

    def test_resource_bound_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):inventory({},tmp,{'2026-10-19'},tail_seconds=121)
            with self.assertRaises(ValueError):inventory({},tmp,{'2026-10-19'},max_bytes=65*1024*1024)

    def test_future_processed_bar_not_counted_in_holding_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            row={'event':'COMPLETED_BAR','symbol':'X','capture_session':'s','capture_losses':0,
                 'bar':{'time':(AT-timedelta(minutes=1)).isoformat()},'confirmed_at':AT.isoformat(),
                 'processed_at':(AT+timedelta(seconds=40)).isoformat()}
            (Path(tmp)/'trade-review-20261019-0001.jsonl').write_text(json.dumps(row)+'\n',encoding='utf-8')
            out=inventory({'trades':[self.trade()]},tmp,{'2026-10-19'})
            self.assertNotIn('holding',out['trades'][0]['coverage'])
            self.assertEqual(out['trades'][0]['coverage']['tail120']['records']['COMPLETED_BAR'],1)


if __name__=='__main__':unittest.main()
