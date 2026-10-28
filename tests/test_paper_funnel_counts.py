import unittest
from copy import deepcopy
from research.paper_funnel import PaperFunnel


class FunnelTests(unittest.TestCase):
    def test_repeated_evaluations_are_one_structure_and_gate_missing_is_unknown(self):
        f=PaperFunnel();f.feed(dict(event='WATCH_METADATA',symbol='999990',active=True,watch={'status':'WATCH'}))
        row=dict(event='ENTRY_DECISION_EVIDENCE',capture_session='SYNTHETIC-CAPTURE',symbol='999990',strategies=[dict(setup='A',episode_id='STRUCTURE-1',formed=True,status='READY',
            gate_results={'breakout_structure':'PASS'},net_rr=1.6,trigger={'result':'PASS'})],account={'min_reward_risk':1.5})
        for _ in range(100):f.feed(row)
        out=f.result(dict(completed_count=0,net_pnl_closed=None,fills=[],orders_submitted=0),{'REPORTED_CAPTURE_LOSS':1000})
        self.assertEqual(out['observed_watch_unique_symbols'],1)
        self.assertEqual(out['structure_fingerprints'],{'A':1});self.assertEqual(out['economic_and_trigger_pass'],{'A':1})
        self.assertEqual(out['native_reported_preintent_pass_NOT_revalidated'],{})
        self.assertIsNone(out['source_packet_loss_count']);self.assertEqual(out['record_loss_increase'],1000)
        self.assertIsNone(out['estimated_net_pnl_closed'])

    def test_no_native_decision_is_not_zero_opportunities_and_paper_fills_are_not_real_trades(self):
        f=PaperFunnel();out=f.result(dict(completed_count=1,net_pnl_closed=-100,
            fills=[{'side':'BUY'},{'side':'SELL'}],orders_submitted=2),{'SYNTHETIC_PAPER_BUY_INTENTS':1})
        self.assertIsNone(out['structure_fingerprints']);self.assertEqual(out['paper_buy_intents'],1)
        self.assertEqual(out['paper_buy_fills'],1);self.assertFalse(out['actual_broker_trades'])
        self.assertFalse(out['profitability_proven'])

    def test_count_budget_exhaustion_is_visible(self):
        f=PaperFunnel(limit=1)
        for symbol in ('999990','999991'):f.feed(dict(event='WATCH_METADATA',symbol=symbol,watch={'status':'WATCH'}))
        self.assertEqual(f.unknown['COUNT_BUDGET_EXCEEDED'],1)

    def test_reported_pass_is_not_validated_authority_and_capture_restarts_are_distinct(self):
        f=PaperFunnel()
        row=dict(event='ENTRY_DECISION_EVIDENCE',capture_session='ONE',symbol='999990',producer_execution='LIVE',
            input_evidence_basis='ACTUAL_RECEIVED_LIVE_PIPELINE',selected_setup='A',
            account=dict(quantity=1,account_fresh=False),authority=dict(live_authorized=False,
                observed_final_gates={k:'PASS' for k in ('policy','session','connection_account','tick','repeat_slots')}),
            final_required_gate_results={'order':'PASS'},strategies=[dict(setup='A',episode_id='SAME-ID',formed=True,gate_results={})])
        f.feed(row);f.feed({**row,'capture_session':'TWO'})
        out=f.result(dict(completed_count=0,fills=[],orders_submitted=0),{})
        self.assertEqual(out['structure_fingerprints'],{'A':2})
        self.assertEqual(out['native_reported_preintent_pass_NOT_revalidated'],{'A':2})
        self.assertEqual(out['observed_context_PAPER_accepted_buys'],0)
        self.assertIn('UNKNOWN',out['actual_historical_orderability'])
        self.assertIn('UNKNOWN',out['watch_completeness'])
        f.feed(dict(event='ENTRY_DECISION_EVIDENCE',strategies='INVALID'))
        self.assertEqual(f.unknown['MALFORMED_STRATEGY_LIST'],1)


if __name__=='__main__':unittest.main()
