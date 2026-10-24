"""Synthetic evidence contracts, never observations of a market trade."""
from datetime import datetime,timedelta
import unittest
from automation.capital import Costs,entry_economics,entry_quantity
from core.settings import Config,KST
from research.recovery_noise import prefix_audit,book_intervals,continuity_pairs,trades,path_audit,risk_stress

AT=datetime(2026,10,23,13,23,1,tzinfo=KST)

def trade(i,*,delay=0,price=100,cumulative=None):
    received=AT+timedelta(seconds=i);known=received+timedelta(seconds=delay)
    row={'event':'TRADE','symbol':'999990','capture_point':'STRATEGY_INGESTION','venue':'KRX',
        'exchange_at':received.isoformat(),'received_at':received.isoformat(),'processed_at':known.isoformat(),
        'volume':10,'cumulative':(i+100)*10 if cumulative is None else cumulative,'price':price,
        'regular':True,'bid':price-1,'ask':price+1,'depth_available':True,'depth_source':'0B_FIELDS',
        'book_time':received.isoformat(),'book_received_at':received.isoformat(),'bid_qty':100,'ask_qty':100}
    return known.isoformat(),'synthetic:'+str(i),row

def book(i,*,delay=0,valid=True):
    received=AT+timedelta(seconds=i);known=received+timedelta(seconds=delay)
    return known.isoformat(),'synthetic-book:'+str(i),{'event':'BOOK','symbol':'999990',
        'exchange_at':received.isoformat(),'received_at':received.isoformat(),'best_bid':99,'best_ask':101,
        'depth_available':valid,'bid_qty':100,'ask_qty':100}

class EvidenceContinuity(unittest.TestCase):
    def test_consumer_stale_history_is_not_a_receiver_loss_or_a_fresh_entry(self):
        records=[trade(i,delay=8 if i==-20 else 0) for i in range(-59,1)]
        d=prefix_audit(records,'999990',AT,98,Config())
        self.assertEqual(d['candidate_noise'],2)
        self.assertEqual(d['process_stale_history_rows'],1)
        self.assertEqual(d['max_receive_gap_seconds'],1)
        self.assertEqual(records[39][2]['processed_at'],(AT-timedelta(seconds=12)).isoformat())
        self.assertFalse(d['noise_model_validated']);self.assertFalse(d['actual_order_permission'])
    def test_no_trade_with_independent_books_requires_volume_conservation(self):
        records=[trade(0,cumulative=100),book(2),book(5),trade(8,cumulative=110)]
        rows=trades(records,'999990');d=continuity_pairs(rows,records,'999990',5)[0]
        self.assertEqual(d['classification'],'NO_UNACCOUNTED_TRADE_VOLUME_WITH_BOOK_UPDATES')
        self.assertEqual(d['BOOK_rows_between_receipts'],2)
        records[-1][2]['cumulative']=130
        d=continuity_pairs(trades(records,'999990'),records,'999990',5)[0]
        self.assertEqual(d['classification'],'UNKNOWN_UNACCOUNTED_CUMULATIVE_VOLUME')
    def test_a_consumer_gap_with_short_receive_gap_is_processing_delay(self):
        records=[trade(0,cumulative=100),trade(1,cumulative=110,delay=8)]
        d=continuity_pairs(trades(records,'999990'),records,'999990',5)[0]
        self.assertEqual(d['classification'],'PROCESSING_DELAY_NOT_RECEIVER_SILENCE')
    def test_no_unrecorded_volume_does_not_prove_a_complete_book_stream(self):
        d=prefix_audit([trade(i) for i in range(-59,1)],'999990',AT,98,Config())
        self.assertEqual(d['candidate_noise'],2)
        self.assertEqual(d['books']['covered_seconds'],0)
        self.assertEqual(d['unseen_book_widening'],'UNKNOWN_OUTSIDE_DOCUMENTED_BOOK_INTERVALS')
    def test_later_quote_cannot_backfill_earlier_knowledge(self):
        d=book_intervals([book(0,delay=4)],'999990',AT,AT+timedelta(seconds=10),5)
        self.assertEqual(d['valid_intervals'][0]['start'],AT+timedelta(seconds=4))
        self.assertEqual(d['valid_intervals'][0]['end'],AT+timedelta(seconds=5))
    def test_new_invalid_quote_expires_the_previous_one(self):
        d=book_intervals([book(0),book(2,valid=False)],'999990',AT,AT+timedelta(seconds=10),5)
        self.assertEqual(d['covered_seconds'],2)
    def test_observed_trade_BBO_change_invalidates_old_book_depth(self):
        d=book_intervals([book(0),trade(2,price=103)],'999990',AT,AT+timedelta(seconds=10),5)
        self.assertEqual(d['covered_seconds'],2)
    def test_future_books_and_prices_cannot_change_a_pretrigger_noise(self):
        rows=[trade(i) for i in range(-59,1)]
        before=prefix_audit(rows,'999990',AT,98,Config())
        rows.extend([trade(1,price=20),book(1)])
        after=prefix_audit(rows,'999990',AT,98,Config())
        self.assertEqual(before['candidate_noise'],after['candidate_noise'])
        self.assertEqual(after['books']['raw_BOOK_rows'],0)
    def test_explicit_subscription_gap_is_unknown_even_with_balanced_volume(self):
        rows=[trade(i) for i in range(-59,1)]
        rows.append(((AT-timedelta(seconds=10)).isoformat(),'scope',
            {'event':'SUBSCRIPTION_SCOPE','removed':['999990'],'added':[]}))
        d=prefix_audit(rows,'999990',AT,98,Config())
        self.assertIsNone(d['candidate_noise'])
        self.assertEqual(d['reason'],'UNKNOWN_EXPLICIT_RECORDING_OR_SUBSCRIPTION_GAP')
    def test_missing_volume_is_unknown_not_an_assertion_of_packet_loss(self):
        rows=[trade(i) for i in range(-59,1)];rows[4][2]['volume']=None
        d=prefix_audit(rows,'999990',AT,98,Config())
        self.assertIsNone(d['candidate_noise'])
        self.assertEqual(d['reason'],'UNKNOWN_UNACCOUNTED_TRADE_VOLUME')
    def test_receiver_only_tail_cannot_be_called_strategy_known_history(self):
        rows=[trade(i) for i in range(-59,0)];k,s,r=trade(0,price=80)
        rows.append((k,'receiver-tail',{**r,'capture_point':'RECEIVER','processed_at':None}))
        d=prefix_audit(rows,'999990',AT,98,Config())
        self.assertIsNone(d['candidate_noise'])
        self.assertEqual(d['reason'],'UNKNOWN_RECEIVER_ONLY_PREFIX_EVENTS_NOT_STRATEGY_KNOWN')

class StopStability(unittest.TestCase):
    def test_unaccounted_volume_does_not_certify_a_complete_structural_close(self):
        rows=[trade(20,price=102,cumulative=100),trade(60,price=97,cumulative=110),
              trade(90,price=97,cumulative=140),trade(120,price=100,cumulative=150)]
        d=path_audit(rows,'999990',AT,100,98,95,Config(),1,100,Costs(.00015,.00015,.002,.002,.003))
        self.assertIsNone(d['first_eligible_complete_close_failure'])
    def test_tighter_emergency_can_increase_gap_exposure_even_with_same_risk_budget(self):
        costs=Costs(.00015,.00015,.002,.002,.003);risk=70847*.02;results=[]
        for stop in (1970,2010):
            e=entry_economics(2045,2025,stop,2140,costs,1.5)
            size=entry_quantity(e,70847,risk);results.append(risk_stress(e,size,risk,15,costs))
        wide,narrow=results
        self.assertGreater(narrow['quantity'],wide['quantity'])
        self.assertGreater(narrow['extra_loss_over_plan'],wide['extra_loss_over_plan'])
        self.assertTrue(narrow['exceeds_trade_budget'])
        self.assertFalse(narrow['actual_fill'])
    def test_wick_and_later_valid_close_are_only_counterfactual_not_a_fill(self):
        rows=[trade(i,price=p,cumulative=100+j*10) for j,(i,p) in enumerate([(20,94),(60,101),(90,100),(120,102)])]
        d=path_audit(rows,'999990',AT,100,98,95,Config(),1,100,Costs(.00015,.00015,.002,.002,.003))
        self.assertTrue(d['touch_then_recovered_before_confirmed_failure'])
        self.assertIsNone(d['first_eligible_complete_close_failure'])
        self.assertEqual(d['first_eligible_close_above_structure_after_touch']['time'],(AT+timedelta(seconds=60)).replace(second=0))
        self.assertFalse(d['liquidation_sensitivity'][0]['actual_fill'])
        self.assertGreater(d['liquidation_sensitivity'][0]['scenario_loss_with_reserves'],d['liquidation_sensitivity'][0]['planned_loss_reserve'])
    def test_missing_future_does_not_mean_the_stop_was_stable(self):
        d=path_audit([],'999990',AT,100,98,95,Config(),1,100,Costs(.00015,.00015,.002,.002,.003))
        self.assertEqual(d['future_trade_rows'],0)
        self.assertIsNone(d['max_observed_entry_adverse_before_close_failure'])
        self.assertEqual(d['unseen_extremes'],'UNKNOWN_NOT_AN_EXCHANGE_CENSUS')

if __name__=='__main__':unittest.main()
