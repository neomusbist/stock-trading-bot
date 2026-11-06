import unittest
from datetime import datetime, timedelta
from core.settings import KST, Config
from automation.policy import AutomationConfig
from core.strategy import Tick
from automation.fast_momentum import FastMomentumStrategy

BASE_TIME = datetime(2026, 11, 4, 11, 33, 28, tzinfo=KST)

class GeniansLateChaseReproductionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(fast_momentum_enabled=True, min_stop_distance_fraction=0.012)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_case_f1_genians_peak_ceiling_equality_blocked(self):
        # Genians at 11:33:28:
        # Nearest confirmed peak is 25,250 KRW (confirmed at 10:32).
        # Executable ask is 25,250 KRW.
        # Headroom to peak is (25,250 - 25,250) / 25,250 = 0.00%.
        # Invalidation & target MUST NOT leap over the confirmed peak to prior_day_high (29,450)!
        prices = [24800, 24850, 24900, 25100, 25050, 25050, 25100, 25150, 25200, 25225]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='263860', time=BASE_TIME - timedelta(seconds=len(prices)-i), price=float(p),
                volume=300, cumulative=50000 + i * 300,
                ask=float(p + 25), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=BASE_TIME - timedelta(seconds=len(prices)-i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='263860', time=BASE_TIME, price=25225.0,
            volume=300, cumulative=60000,
            ask=25250.0, bid=25200.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        m = self.strategy.get_tape('263860').metrics(last_t.time)
        watch = {
            'code': '263860', 'name': '지니언스', 'status': 'WATCH',
            'resistance': None,
            'day_high': 25200.0,
            'prior_day_high': 29450.0,
            'daily_high_20d': 32000.0,
            'nearest_confirmed_peak': {
                'price': 25250.0,
                'time': '2026-11-04T10:30:00+09:00',
                'confirmed_at': '2026-11-04T10:32:00+09:00'
            },
            'observed_at': BASE_TIME.isoformat(),
            'first_raw_price': 27600.0,
            'reference_price': 25200.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1381.0, asof=last_t.time
        )
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'KNOWN_CEILING_NET_REWARD_RISK_LOW')
        self.assertNotEqual(geo.get('target'), 29450.0)

    def test_case_f2_genians_unconfirmed_peak_breakout_blocked(self):
        # Even if ask reaches 25,300 (above peak 25,250):
        # Support is 25,050 (< 25,250 - buffer). Peak breakout is NOT confirmed by support.
        # Target must NOT leap to 29,450 or 32,000! Must reject UNCONFIRMED_PEAK_BREAKOUT_HEADROOM_EXHAUSTED.
        prices = [24800, 24850, 24900, 25100, 25050, 25050, 25100, 25200, 25250, 25275]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='263860', time=BASE_TIME - timedelta(seconds=len(prices)-i), price=float(p),
                volume=300, cumulative=50000 + i * 300,
                ask=float(p + 25), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=BASE_TIME - timedelta(seconds=len(prices)-i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='263860', time=BASE_TIME, price=25275.0,
            volume=300, cumulative=60000,
            ask=25300.0, bid=25250.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        m = self.strategy.get_tape('263860').metrics(last_t.time)
        watch = {
            'code': '263860', 'name': '지니언스', 'status': 'WATCH',
            'resistance': None,
            'day_high': 25200.0,
            'prior_day_high': 29450.0,
            'daily_high_20d': 32000.0,
            'nearest_confirmed_peak': {
                'price': 25250.0,
                'time': '2026-11-04T10:30:00+09:00',
                'confirmed_at': '2026-11-04T10:32:00+09:00'
            },
            'observed_at': BASE_TIME.isoformat(),
            'first_raw_price': 27600.0,
            'reference_price': 25200.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1381.0, asof=last_t.time
        )
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'UNCONFIRMED_PEAK_BREAKOUT_HEADROOM_EXHAUSTED')
        self.assertNotEqual(geo.get('target'), 29450.0)

    def test_case_f3_multi_minute_downtrend_context_blocks_counter_bounce(self):
        # When 5-minute trend data is available and shows a confirmed downtrend without higher lows,
        # it is properly identified as DOWNTREND_COUNTER_BOUNCE without assuming missing data.
        last_t = Tick(
            symbol='263860', time=BASE_TIME, price=25225.0,
            volume=500, cumulative=60000,
            ask=25250.0, bid=25200.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        m = {'spread': 0.002, 'ticks_10': 10, 'turnover_10': 15000000.0, 'p_min_60': 24800.0, 'p_max_60': 25225.0,
             'p_min_30': 25000.0, 'p_max_30': 25225.0, 'uptick_ratio_30': 0.8, 'uptick_ratio_10': 0.8}
        watch = {
            'code': '263860', 'name': '지니언스', 'status': 'WATCH',
            'reference_price': 25200.0,
            'trend_5m': {'status': 'DOWN', 'higher_lows': False, 'return': -0.04}
        }
        regime, reason, _ = self.strategy.classify_regime(last_t, m, 'REGULAR', watch, asof=last_t.time)
        self.assertEqual(regime, 'D')
        self.assertEqual(reason, 'DOWNTREND_COUNTER_BOUNCE')

    def test_case_f4_strong_breakout_with_high_volume_and_upticks_not_falsely_blocked(self):
        # A powerful, genuine breakout with 50M turnover in 10s where price is cleanly breaking out
        # at its highs with strong upticks must NOT be falsely classified as exhaustion.
        last_t = Tick(
            symbol='099990', time=BASE_TIME, price=10500.0,
            volume=2000, cumulative=100000,
            ask=10510.0, bid=10500.0, regular=True, ask_qty=5000, bid_qty=5000,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        m = {
            'spread': 0.00095, 'ticks_10': 15, 'turnover_10': 50000000.0, 'volume_10': 5000,
            'turnover_30': 70000000.0, 'volume_30': 7000,
            'p_min_60': 10300.0, 'p_max_60': 10500.0,
            'p_min_30': 10350.0, 'p_max_30': 10500.0,
            'p_min_10': 10450.0, 'p_max_10': 10500.0,
            'uptick_ratio_30': 0.85, 'uptick_ratio_10': 0.88,
            'downtick_ratio_10': 0.12
        }
        watch = {'code': '099990', 'name': '강한돌파주', 'status': 'WATCH', 'reference_price': 10400.0}
        regime, reason, details = self.strategy.classify_regime(last_t, m, 'REGULAR', watch, asof=last_t.time)
        self.assertEqual(regime, 'A', f"Clean high volume breakout at highs must pass Route A, got {regime} {reason}")
        self.assertEqual(reason, 'EARLY_MOMENTUM_FLOW_PROXY')

    def test_case_f5_confirmed_reversal_from_morning_drawdown_allowed(self):
        # A stock that fell 8% in the early morning, but now has completed a structural higher-low base
        # and breaks out with confirmed support and +6% headroom to resistance must NOT be arbitrarily blocked!
        rev_time = datetime(2026, 11, 4, 10, 30, 0, tzinfo=KST)
        prices = [9200, 9250, 9300, 9400, 9300, 9320, 9350, 9420, 9450]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='077770', time=rev_time + timedelta(seconds=i), price=float(p),
                volume=500, cumulative=50000 + i * 500,
                ask=float(p + 10), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=rev_time + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='077770', time=rev_time + timedelta(seconds=len(prices)), price=9450.0,
            volume=500, cumulative=50000 + len(prices) * 500,
            ask=9460.0, bid=9440.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=rev_time + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('077770').metrics(last_t.time)
        watch = {
            'code': '077770', 'name': '추세반등성공주', 'status': 'WATCH',
            'first_raw_price': 10300.0, # fell 8% from early morning
            'day_high': 10400.0,
            'resistance': 10000.0, # Overhead resistance is at 10,000 (+5.7% room)
            'resistance_asof': rev_time.isoformat(),
            'observed_at': rev_time.isoformat(),
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time
        )
        self.assertTrue(geo['pass'], f"Confirmed reversal with structural support must pass: {geo.get('reason')}")
        self.assertEqual(geo['target'], 10000.0)
        self.assertGreaterEqual(geo['net_rr'], 1.5)

if __name__ == '__main__':
    unittest.main()
