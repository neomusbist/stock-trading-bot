import unittest
from research.fast_trade_summary import summarize


def leg(trade_id='T1', pnl=-10, completed=True, source='BROKER_QUERY'):
    return {'ownership':'BOT','pattern':'FAST','trade_id':trade_id,'symbol':'123456',
            'opened':'2026-11-05T09:00:00+09:00','completed':completed,
            'pnl_source':source,'pnl_estimate':pnl}


class FastTradeSummaryTests(unittest.TestCase):
    def test_partial_exits_are_one_position_and_all_costs_count(self):
        result=summarize([leg(pnl=-106.5,completed=False),leg(pnl=-379.5)])
        self.assertEqual(result['positions'],1)
        self.assertEqual(result['net_pnl'],-486)
        self.assertEqual(result['trades'][0]['exit_legs'],2)
        self.assertEqual(result['losses'],1)
    def test_missing_broker_cost_on_any_leg_excludes_entire_position(self):
        result=summarize([leg(pnl=-10,completed=False,source='ESTIMATED'),leg(pnl=15)])
        self.assertEqual(result['positions'],0);self.assertIsNone(result['net_pnl'])
        self.assertIn('BROKER_COST_SOURCE_INCOMPLETE',result['unknown_positions'][0]['reasons'])
    def test_missing_final_or_id_is_unknown_not_zero(self):
        result=summarize([leg(completed=False),leg(trade_id=None)])
        self.assertEqual(result['positions'],0);self.assertEqual(result['missing_trade_id_rows'],1)
        self.assertIn('FINAL_EXIT_COUNT_NOT_ONE',result['unknown_positions'][0]['reasons'])
    def test_duplicate_final_is_unknown(self):
        result=summarize([leg(),leg()])
        self.assertEqual(result['positions'],0)
        self.assertIn('FINAL_EXIT_COUNT_NOT_ONE',result['unknown_positions'][0]['reasons'])

if __name__=='__main__':unittest.main()
