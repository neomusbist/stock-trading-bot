import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from core.settings import KST
from audit.r6_work_repair_20261014.replay import Execution,simulate,quote_valid,shift_ticks

T=datetime(2026,10,14,14,30,tzinfo=KST)

def ev(line,sec,price=10000,bid=9990,ask=10000,symbol='999990'):
    return dict(line=line,dt=T+timedelta(seconds=sec),symbol=symbol,name='synthetic',price=price,
                bid=bid,ask=ask,spread=.001,gain=.01,ratio=2.,turnover=1e7,decision='NO_BUY')

class ExecutionRepairTests(unittest.TestCase):
    def test_missing_quote_keeps_position_and_never_fabricates_exit(self):
        result=simulate([ev(1,0),ev(2,200,price=9900,bid=None,ask=None)],Execution())
        self.assertEqual(result['closed_trades'],0)
        self.assertIsNotNone(result['unresolved'])
        self.assertIsNone(result['unresolved']['mark_bid'])

    def test_data_end_is_not_market_close(self):
        result=simulate([ev(1,0),ev(2,5,price=10000)],Execution())
        self.assertEqual(result['unresolved']['data_ends_at'],(T+timedelta(seconds=5)).isoformat())
        self.assertEqual(result['unresolved']['basis'],'OPEN_AT_DATA_END_NOT_MARKET_CLOSE')

    def test_cost_components_reconcile_without_spread_or_delay_double_charge(self):
        p=Execution();r=simulate([ev(1,0),ev(2,200,price=9900,bid=9890,ask=9900)],p)
        tr=r['trades'][0]
        self.assertAlmostEqual(tr['net_pnl'],tr['gross_at_assumed_fills']-tr['buy_fee']-tr['sell_fee']-tr['sell_tax'])
        self.assertAlmostEqual(tr['net_pnl'],tr['gross_at_fill_quotes']-tr['entry_slippage_including_tick_rounding']
                               -tr['exit_slippage_including_tick_rounding']-tr['adverse_tick_drag']
                               -tr['buy_fee']-tr['sell_fee']-tr['sell_tax'])
        self.assertGreater(tr['sell_tax'],0)

    def test_full_daily_loss_budget_blocks_entry(self):
        r=simulate([ev(1,0)],Execution(prior_daily_loss=3252))
        self.assertIsNone(r['unresolved']);self.assertEqual(r['closed_trades'],0)
        self.assertEqual(r['declines']['RISK_OR_INTEGER_CASH'],1)

    def test_delayed_entry_expiry_and_chase_are_not_executed(self):
        for final in (ev(2,16),ev(2,1,price=10100,bid=10090,ask=10100)):
            r=simulate([ev(1,0),final],Execution(delay_snapshots=1))
            self.assertIsNone(r['unresolved']);self.assertEqual(r['closed_trades'],0)
            self.assertEqual(r['declines']['EXPIRED_OR_CHASED_ENTRY'],1)

    def test_later_exit_uses_later_bid_even_when_target_touched_before(self):
        rows=[ev(1,0),ev(2,1),ev(3,5,price=10500,bid=10490,ask=10500),
              ev(4,6,price=10010,bid=10000,ask=10010)]
        r=simulate(rows,Execution(delay_snapshots=1));tr=r['trades'][0]
        self.assertEqual(tr['exit_bid'],10000);self.assertEqual(tr['exit_line'],4)
        self.assertGreater(tr['target'],tr['sell_assumed'])

    def test_profit_does_not_replenish_loss_allowance(self):
        rows=[ev(1,0),ev(2,200,9900,9890,9900),ev(3,201,symbol='999980'),
              ev(4,220,price=10500,bid=10490,ask=10500,symbol='999980')]
        r=simulate(rows,Execution());loss=sum(max(0,-tr['net_pnl'])for tr in r['trades'])
        self.assertAlmostEqual(r['loss_used'],loss)
        self.assertGreater(r['loss_used'],max(0,-r['net_pnl']))

    def test_adverse_ticks_use_band_at_each_step(self):
        self.assertEqual(shift_ticks(2000,-1),1999)
        self.assertEqual(shift_ticks(1999,1),2000)
        self.assertFalse(quote_valid(ev(1,0,bid=float('nan'))))

    def test_single_slot_and_no_orders_are_evidence_not_profitability(self):
        r=simulate([ev(1,0),ev(2,1,symbol='999980')],Execution())
        self.assertEqual(r['unresolved']['code'],'999990')
        self.assertEqual(r['actual_FAST_backtest'],'UNKNOWN_NO_RAW_TICKS')
        self.assertEqual(r['profitability_verdict'],'UNKNOWN_SINGLE_DAY_SNAPSHOT_PROXY')

    def test_profit_cash_does_not_expand_approved_investment_cap(self):
        rows=[ev(1,0),ev(2,5,price=10500,bid=10490,ask=10500),
              ev(3,6,price=1000,bid=999,ask=1000,symbol='999980')]
        r=simulate(rows,Execution())
        self.assertGreater(r['trades'][0]['cash_after'],81300)
        opened=r['unresolved']
        self.assertEqual(opened['code'],'999980')
        self.assertLessEqual(opened['qty']*opened['buy_assumed']*(1+Execution().buy_fee),81300)

if __name__=='__main__':unittest.main()
