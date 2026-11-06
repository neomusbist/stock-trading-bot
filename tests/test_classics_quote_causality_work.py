import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from tools.replay_classics_real_account import RealisticPosition, executable_bid, run_realistic_replay


class QuoteCausalityTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime.fromisoformat('2026-11-05T15:10:00+09:00')
        self.signal = {'symbol': '389260', 'strategy': 'research', 'entry': 100,
                       'stop': 95, 'target': 110, 'net_rr': 1.5, 'gross_rr': 2}

    def test_trade_at_target_is_not_a_target_fill_below_bid(self):
        p = RealisticPosition(self.signal, self.at, 100, 2, 200)
        result = p.update_tick(bid=104, ask=110, price=110, current_time=self.at)
        self.assertTrue(result is None or result[1] != 'TARGET_REACHED')

    def test_quote_must_match_position_and_be_fresh_and_have_depth(self):
        quote = {'symbol': '282720', 'bid': 99, 'bid_qty': 10,
                 'depth_available': True, 'book_received_at': self.at.isoformat()}
        self.assertIsNone(executable_bid(quote, '389260', self.at, 2))
        quote['symbol'] = '389260'
        self.assertEqual(executable_bid(quote, '389260', self.at, 2), 99)
        quote['bid_qty'] = 1
        self.assertIsNone(executable_bid(quote, '389260', self.at, 2))
        quote['bid_qty'] = 10; quote['book_received_at'] = '2026-11-05T15:09:54+09:00'
        self.assertIsNone(executable_bid(quote, '389260', self.at, 2))

    def test_force_close_never_uses_other_symbols_quote(self):
        class Bars:
            def __init__(self):
                self.bars = {'389260': [{'open': 100}] * 8}
            def on_tick(self, tick):
                pass
        class Engine:
            def __init__(self, **kwargs):
                pass
            def evaluate_candidate(self, **kwargs):
                return ({**self_signal, 'quantity': 2}, None, {'risk_net_per_share': 6})
        self_signal = self.signal
        def event(symbol, clock, price):
            stamp = '2026-11-05T' + clock + '+09:00'
            return {'event': 'TRADE', 'symbol': symbol, 'time': stamp, 'received': stamp,
                    'price': price, 'bid': price, 'ask': price, 'bid_qty': 10, 'ask_qty': 10,
                    'book_received_at': stamp, 'depth_available': True}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ticks.jsonl'
            path.write_text('\n'.join(json.dumps(x) for x in [event('389260', '10:00:00', 100),
                                                               event('282720', '15:10:00', 5000)]), encoding='utf8')
            with patch('tools.replay_classics_real_account.CandleAggregator', Bars), patch('tools.replay_classics_real_account.ClassicsStrategyEngine', Engine):
                result = run_realistic_replay([path], initial_cash=1000, initial_equity=1000)
        self.assertEqual(result['open_positions_unknown'], 1)
        self.assertIsNone(result['trades'][0]['net_pnl'])
        self.assertIsNone(result['trades'][0]['exit_price'])
        self.assertEqual(result['execution_performance'], 'UNKNOWN')

if __name__ == '__main__':
    unittest.main()
