"""NHN(181710) 2026-11-06 FAST_PROFIT_LOCK_EXIT audit tests.

Fixture ticks are SYNTHETIC. They reproduce the code path and the values recorded in
events.jsonl (micro protection 63,300 / structural 61,600 / sell limit 0), not the market.

Instrumentation only: none of these tests may change an order decision.
"""
import json
import unittest

from tests.test_nhn_fast_exit_fixture import NhnFastExitBase, SYM


class NhnCodePathCharacterization(NhnFastExitBase):
    def test_recorded_nhn_signature_is_reproduced_by_the_real_engine_path(self):
        pos = self.run_nhn_like_path()
        self.assertTrue(pos['exit_requested'])
        self.assertEqual(pos['exit_reason'], 'FAST_PROFIT_LOCK_EXIT')
        fe = pos['fast_exit_evidence']
        self.assertEqual(fe['reason'], 'PROFIT_LOCK_EXIT')
        self.assertEqual(fe['protection_authority'], 'FAST_TWO_INDEPENDENT_TRADE_BREACHES')
        self.assertEqual(fe['micro_protection_stop'], 63300)
        self.assertEqual(pos['stop'], 61600)          # structural line never moved
        self.assertEqual(fe['evidence']['stop'], 63300)
        self.assertLessEqual(fe['evidence']['bid'], 63300)
        sells = [o for o in self.broker.orders if o['side'] == 'SELL']
        self.assertEqual(len(sells), 1)
        self.assertEqual((sells[0]['qty'], sells[0]['limit']), (1, 0))   # market-style exit order

    def test_micro_line_geometry_is_one_tick_below_a_one_tick_swing_low(self):
        """Characterizes design D: the line sits 1 quote unit under a 1-tick tick-swing low."""
        pos = self.run_nhn_like_path()
        high = pos['high']
        self.assertEqual(high, 63500.0)
        self.assertEqual(high - pos['protection_stop'], 200.0)   # == entry-time noise_band (200)


class NhnMicroStopAuditTrail(NhnFastExitBase):
    """These FAIL on e3dc0dd: the FAST micro stop was invisible to every audit surface."""

    def test_micro_stop_changes_are_recorded_with_evidence(self):
        pos = self.run_nhn_like_path()
        hist = pos.get('micro_stop_history')
        self.assertTrue(hist, 'micro stop was raised but nothing recorded')
        self.assertEqual(hist[-1]['stop'], 63300)
        for key in ('time', 'prior_stop', 'price', 'bid', 'ask', 'high', 'authority'):
            self.assertIn(key, hist[-1])
        self.assertIs(hist[-1]['order_effect'], False)
        self.assertLessEqual(len(hist), 32)
        self.assertEqual([h['stop'] for h in hist], sorted(h['stop'] for h in hist))

    def test_exit_state_does_not_claim_unprotected_while_micro_stop_is_armed(self):
        pos = self.run_nhn_like_path()
        es = pos['exit_state']
        fmp = es.get('fast_micro_protection')
        self.assertIsNotNone(fmp)
        self.assertEqual(fmp['stop'], 63300)
        self.assertEqual(fmp['structural_stop'], 61600)
        self.assertEqual(fmp['authority'], 'FAST_TWO_INDEPENDENT_TRADE_BREACHES')
        self.assertEqual(es['presentation']['profit']['micro_protect_price'], 63300)

    def test_completed_trade_keeps_micro_history_and_bounded_decision_tape(self):
        pos = self.run_nhn_like_path()
        sell = next(o for o in self.engine.s['orders'].values() if o['side'] == 'SELL')
        self.engine.cumulative_fill(sell, 1, 63100.0)
        trade = self.engine.s['trades'][-1]
        self.assertEqual(trade['reason'], 'FAST_PROFIT_LOCK_EXIT')
        self.assertTrue(trade.get('micro_stop_history'))
        self.assertEqual(trade['micro_stop_history'][-1]['stop'], 63300)
        tape = trade['exit_trigger_evidence'].get('fast_decision_tape')
        self.assertIsNotNone(tape, 'no tick evidence preserved for the FAST exit decision')
        self.assertTrue(0 < len(tape['rows']) <= 256)
        self.assertEqual(tape['source'], 'ACCEPTED_FAST_MICRO_TAPE_AT_SIGNAL_RECEIPT_CUTOFF')
        self.assertLessEqual(tape['rows'][-1]['price'], 63300)
        json.dumps(trade, default=str)  # persisted ledger must stay serializable

    def test_tape_is_not_duplicated_into_every_exit_state_event(self):
        pos = self.run_nhn_like_path()
        self.assertNotIn('decision_tape', pos['fast_exit_evidence'])
        self.assertNotIn('decision_tape', json.dumps(pos['exit_state'], default=str))


class NhnInstrumentationDoesNotChangeOrders(NhnFastExitBase):
    def test_exit_decision_time_reason_and_price_lines_are_unchanged(self):
        pos = self.run_nhn_like_path()
        self.assertRegex(pos['exit_signal_at'], r'^2026-11-06T14:17:\d\d')
        self.assertEqual(pos['exit_reason'], 'FAST_PROFIT_LOCK_EXIT')
        self.assertEqual(pos['stop'], 61600)
        self.assertEqual(pos['protection_stop'], 63300)
        self.assertEqual(sum(o['side'] == 'SELL' for o in self.broker.orders), 1)

    def test_no_history_without_a_raised_micro_stop(self):
        pos = self.open_position()
        for price in (62200, 62300, 62400):
            self.step(price, price - 100, price)
        self.assertFalse(pos.get('micro_stop_history'))
        self.assertFalse(pos.get('exit_requested'))

    def test_broken_instrumentation_cannot_block_the_exit_order(self):
        def boom(*a, **k):
            raise RuntimeError('instrumentation failure')
        self.engine.fast_strategy.snapshot_decision_tape = boom
        pos = self.run_nhn_like_path()
        self.assertTrue(pos['exit_requested'])
        self.assertEqual(pos['exit_reason'], 'FAST_PROFIT_LOCK_EXIT')
        self.assertEqual(sum(o['side'] == 'SELL' for o in self.broker.orders), 1)
        self.assertEqual(pos['fast_exit_tape'], {'unavailable': 'RuntimeError'})

    def test_repeated_ticks_after_exit_request_do_not_duplicate_sell_or_tape(self):
        pos = self.run_nhn_like_path()
        tape = pos['fast_exit_tape']
        for price in (63000, 62900, 62800):
            self.step(price, price - 100, price)
        self.assertEqual(sum(o['side'] == 'SELL' for o in self.broker.orders), 1)
        self.assertIs(pos['fast_exit_tape'], tape)


if __name__ == '__main__':
    unittest.main()

