"""Existing B setup: a complete first candle may contain the actual impulse."""
import unittest
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from core.settings import Config


class FirstCandlePullbackTests(unittest.TestCase):
    def setUp(self):
        self.detector = PatternDetector(Config(), AutomationConfig(), {})

    def bars(self):
        return [
            dict(open=1000, high=1012, low=1000, close=1010, volume=10000),
            dict(open=1009, high=1009, low=1006, close=1006, volume=4000),
            dict(open=1006, high=1008, low=1005, close=1007, volume=3000),
            dict(open=1007, high=1008, low=1006, close=1007, volume=3500),
        ]

    def test_complete_first_body_and_controlled_pullback_uses_actual_peak(self):
        setup, reason, evidence = self.detector.pullback_analysis(self.bars())
        self.assertIsNotNone(setup, (reason, evidence))
        self.assertEqual(setup['peak'], 1012)
        self.assertEqual(setup['support'], 1005)
        self.assertEqual(setup['projection'], 1024)
        self.assertEqual(setup['pullback_bars'], 3)

    def test_lower_later_high_must_not_relabel_peak_or_dilute_impulse_volume(self):
        bars = self.bars()
        bars[1]['volume'] = 10
        bars[2]['volume'] = bars[3]['volume'] = 5000
        setup, reason, evidence = self.detector.pullback_analysis(bars)
        self.assertIsNotNone(setup, (reason, evidence))
        self.assertEqual(setup['impulse_peak'], 1012)
        self.assertAlmostEqual(setup['pullback_volume_ratio'], 10010/3/10000)

    def test_first_candle_wick_without_close_progress_still_fails(self):
        bars = self.bars(); bars[0]['close'] = 1001
        setup, reason, _ = self.detector.pullback_analysis(bars)
        self.assertIsNone(setup)
        self.assertEqual(reason, 'IMPULSE_CLOSE_PROGRESS_INSUFFICIENT')

    def test_deep_or_high_volume_pullback_stays_blocked(self):
        for key, value in [('low', 1001), ('volume', 30000)]:
            with self.subTest(key=key):
                bars = self.bars(); bars[2][key] = value
                self.assertIsNone(self.detector.pullback_analysis(bars)[0])

    def test_missing_first_open_cannot_prove_first_candle_impulse(self):
        bars = self.bars(); bars[0].pop('open')
        self.assertIsNone(self.detector.pullback_analysis(bars)[0])

    def test_existing_B_order_path_accepts_complete_first_impulse_and_confirmed_floor(self):
        from tests.test_live_ready_20261021 import LiveReadyLifecycle,AT,SYM
        from datetime import timedelta
        from core.util import tick_size
        h=LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.watch('B')
        raw=[(1000,1050,1000,1045,10000),(1046,1046,1041,1042,10),
             (1042,1044,1040,1041,5000),(1042,1047,1042,1046,5000)]
        for i,(o,hi,lo,c,v) in enumerate(raw):
            start=AT-timedelta(minutes=len(raw)-i)
            for sec,price in ((0,o),(15,hi),(30,lo),(59,c)):
                h.feed(start+timedelta(seconds=sec),price,v)
        self.assertEqual(h.broker.orders,[])
        h.feed(AT,1046);h.feed(AT+timedelta(seconds=5),1051,100000)
        buys=[o for o in h.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1,h.detector.last_eval[SYM]['trials'][1])
        buy=buys[0];self.assertEqual(buy['signal']['pattern'],'B')
        self.assertEqual(buy['signal']['peak'],1050)
        h.fill(buy,buy['qty'],buy['limit'])
        position=h.e.s['positions'][SYM]
        hard=position['signal']['invalidation_policy']['emergency_stop']
        sell_price=hard-tick_size(hard)
        h.feed(h.clock+timedelta(seconds=1),sell_price)
        sells=[o for o in h.e.s['orders'].values() if o['side']=='SELL']
        self.assertEqual(len(sells),1)
        # A market order has limit=0; its simulated fill must use the actual
        # supplied executable bid, never that zero placeholder.
        h.fill(sells[0],buy['qty'],sell_price)
        self.assertEqual(h.e.s['positions'],{})
        self.assertEqual([x['side'] for x in h.broker.intent_checks],['BUY','SELL'])


if __name__ == '__main__':
    unittest.main()
