import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from core.broker import Rest
from core.settings import KST


class AfterKrxPositionBook(unittest.TestCase):
    def test_krx_after_requests_same_symbol_and_preserves_quote_timestamp(self):
        rest = Rest.__new__(Rest)
        rest.query = Mock(return_value={'bid_req_base_tm': '175000', 'buy_fpr_bid': '+13460',
                                       'sel_fpr_bid': '+13470', 'buy_fpr_req': '20', 'sel_fpr_req': '15'})
        now = datetime(2026, 11, 5, 17, 50, 2, tzinfo=KST)
        with patch('core.broker.now', return_value=now):
            quote = rest.position_book('109670', 'AFTER', 'KRX')
        self.assertEqual(rest.query.call_args.args[1], {'stk_cd': '109670'})
        self.assertEqual(quote['venue'], 'KRX')
        self.assertEqual(quote['time'], now.replace(second=0).isoformat())
        self.assertEqual((quote['bid_qty'], quote['ask_qty']), (20, 15))
        self.assertFalse(quote['regular'])

    def test_invalid_krx_book_is_still_rejected(self):
        rest = Rest.__new__(Rest)
        rest.query = Mock(return_value={'bid_req_base_tm': '175000', 'buy_fpr_bid': '0',
                                       'sel_fpr_bid': '13470', 'buy_fpr_req': '20', 'sel_fpr_req': '15'})
        with patch('core.broker.now', return_value=datetime(2026, 11, 5, 17, 50, 2, tzinfo=KST)):
            with self.assertRaises(ValueError):
                rest.position_book('109670', 'AFTER', 'KRX')

if __name__ == '__main__':
    unittest.main()
