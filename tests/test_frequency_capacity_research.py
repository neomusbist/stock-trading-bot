import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.frequency_research import CapacityReplay, break_even, completed_distribution


class CapacityResearchTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 10, 14, 10, tzinfo=KST)
        self.cfg = replace(Config(), risk_fraction=.02, max_daily_loss_fraction=.04,
                           max_position_fraction=1., max_positions=1)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300)

    def quote(self, price=10000, qty=100, stamp=None):
        return dict(time=(stamp or self.t).isoformat(), bid=price, ask=price,
                    ask_qty=qty, bid_qty=qty)

    def offer(self, symbol='001001', price=10000, score=1, identity=None, theme='UNKNOWN'):
        return dict(id=identity or symbol, symbol=symbol, known_at=self.t.isoformat(),
                    strategy_qualified=True, entry=price, stop=price*.99, target=price*1.03,
                    quote=self.quote(price), score=score, theme=theme)

    def replay(self, slots=1, fraction=1., **kwargs):
        return CapacityReplay(self.cfg, self.auto, cash=81300, max_positions=slots,
                              position_fraction=fraction, **kwargs)

    def test_pending_cash_and_risk_are_reserved_before_any_fill(self):
        r = self.replay(4, .25)
        for n in range(4):
            result = r.reserve(self.offer(f'00100{n}'), self.t)
            self.assertEqual(result['quantity'], 2)
        cash, risk = r.reservations()
        self.assertLessEqual(cash, r.cash)
        self.assertLessEqual(risk, r.equity()*.04)
        self.assertEqual(r.reserve(self.offer('001009'), self.t)['reason'], 'POSITION_SLOTS')

    def test_partial_fill_does_not_double_reserve_filled_shares(self):
        r = self.replay(2, .5)
        r.reserve(self.offer(), self.t)
        before = r.reservations()
        r.fill('001001', self.quote(), self.t, quantity=1)
        after = r.reservations()
        self.assertAlmostEqual(after[0], before[0]-10000*1.00015)
        self.assertAlmostEqual(after[1], before[1])
        r.cancel('001001')
        self.assertEqual(r.reservations()[0], 0)
        self.assertEqual(r.positions['001001']['qty'], 1)

    def test_unknown_depth_never_becomes_a_fill(self):
        r = self.replay()
        o = self.offer(); del o['quote']['ask_qty']
        self.assertEqual(r.reserve(o, self.t)['reason'], 'DEPTH_UNKNOWN')
        o = self.offer(); r.reserve(o, self.t)
        q = self.quote(); del q['ask_qty']
        self.assertEqual(r.fill('001001', q, self.t)['filled'], 0)
        self.assertEqual(len(r.positions), 0)

    def test_partial_entry_cannot_be_counted_closed_before_remainder_cancel(self):
        r = self.replay(); r.reserve(self.offer(), self.t)
        r.fill('001001', self.quote(), self.t, quantity=1)
        self.assertEqual(r.exit('001001', self.quote(10300), self.t)['reason'], 'ACTIVE_ENTRY_REMAINDER')
        self.assertEqual(r.summary()['completed_round_trips'], 0)
        r.cancel('001001')
        self.assertTrue(r.exit('001001', self.quote(10300), self.t)['closed'])

    def test_theme_stress_cap_bounds_correlated_open_risk(self):
        r = self.replay(4, .25, theme_risk_fraction=.005)
        for n in range(4):
            r.reserve(self.offer(f'00100{n}', theme='STEEL'), self.t)
        self.assertLessEqual(r.theme_risks()['STEEL'], r.equity()*.005)
        with self.assertRaises(ValueError):
            self.replay(theme_risk_fraction=.05)

    def test_quote_depth_model_still_does_not_prove_actual_fill(self):
        r = self.replay(); r.reserve(self.offer(), self.t)
        result = r.fill('001001', self.quote(), self.t)
        self.assertEqual(result['actual_fill'], 'UNKNOWN_QUEUE_AND_MARKET_IMPACT')

    def test_future_stale_or_above_limit_quotes_are_rejected(self):
        r = self.replay()
        o = self.offer(); o['known_at'] = (self.t+timedelta(seconds=1)).isoformat()
        self.assertEqual(r.reserve(o, self.t)['reason'], 'FUTURE_SIGNAL')
        for stamp in (self.t-timedelta(seconds=6), self.t+timedelta(seconds=1)):
            o = self.offer(); o['quote']['time'] = stamp.isoformat()
            self.assertEqual(r.reserve(o, self.t)['reason'], 'QUOTE_STALE_OR_FUTURE')
        o = self.offer(); o['quote']['ask'] = 10010
        self.assertEqual(r.reserve(o, self.t)['reason'], 'ASK_ABOVE_APPROVED_LIMIT')

    def test_duplicate_signal_is_not_another_trade(self):
        r = self.replay(2, .5); r.reserve(self.offer(), self.t)
        self.assertEqual(r.reserve(self.offer(), self.t)['reason'], 'DUPLICATE_LIFECYCLE')
        self.assertEqual(r.summary()['completed_round_trips'], 0)

    def test_simultaneous_ranking_ignores_future_result(self):
        r = self.replay()
        a, b = self.offer('001001', score=2), self.offer('001002', score=1)
        b['future_net'] = 999999
        result = r.reserve_simultaneous([b, a], self.t)
        self.assertEqual(result[0][0], '001001')
        self.assertTrue(result[0][1]['accepted'])
        self.assertEqual(result[1][1]['reason'], 'POSITION_SLOTS')

    def test_loss_budget_always_applies_even_with_free_slots(self):
        r = self.replay(4, .25, daily_loss=3252)
        self.assertEqual(r.reserve(self.offer(), self.t)['reason'], 'DAILY_OR_OPEN_RISK')

    def test_larger_structural_risk_reduces_integer_quantity(self):
        r = self.replay(); o = self.offer(); o['stop'] = 9500
        self.assertEqual(r.reserve(o, self.t)['quantity'], 2)
        self.assertLessEqual(r.reservations()[1], 1626)

    def test_theme_is_aggregated_without_claiming_diversification(self):
        r = self.replay(4, .25)
        for n in range(4):
            r.reserve(self.offer(f'00100{n}', theme='STEEL'), self.t)
        self.assertAlmostEqual(r.theme_risks()['STEEL'], r.reservations()[1])
        self.assertEqual(r.summary()['profitability'], 'NOT_ESTABLISHED')

    def test_all_positions_need_exit_depth_and_monitor_marks(self):
        r = self.replay(4, .25)
        for n in range(4):
            symbol = f'00100{n}'; r.reserve(self.offer(symbol), self.t)
            r.fill(symbol, self.quote(), self.t)
            r.mark(symbol, 10050, self.t, self.t+timedelta(seconds=1))
        self.assertEqual(len(r.monitor_age), 4)
        now = self.t+timedelta(minutes=5)
        self.assertFalse(r.exit('001000', self.quote(10300, qty=1, stamp=now), now)['closed'])
        self.assertEqual(len(r.positions), 4)
        for symbol in list(r.positions):
            self.assertTrue(r.exit(symbol, self.quote(10300, stamp=now), now)['closed'])
        self.assertEqual(r.summary()['completed_round_trips'], 4)
        self.assertEqual(r.reservations(), (0, 0))

    def test_round_trip_bins_include_zeros_and_not_partial_exits(self):
        rows = [dict(id='a', fully_closed=True, exit_at=(self.t+timedelta(minutes=7)).isoformat()),
                dict(id='b', fully_closed=False, exit_at=self.t.isoformat())]
        rows.append(dict(rows[0]))
        result = completed_distribution(rows, self.t, self.t+timedelta(minutes=20), 5)
        self.assertEqual(result['counts'], [0, 1, 0, 0])

    def test_break_even_separates_costs_spread_and_slippage(self):
        costs = Costs(.00015, .00015, .002, .002, .003)
        fees = break_even(6970, costs, include_slippage=False)
        stress = break_even(6970, costs)
        spread = break_even(6970, costs, spread=10)
        self.assertEqual(fees['price'], 6990)
        self.assertEqual(stress['price'], 7030)
        self.assertEqual(spread['price'], 7040)


if __name__ == '__main__':
    unittest.main()
