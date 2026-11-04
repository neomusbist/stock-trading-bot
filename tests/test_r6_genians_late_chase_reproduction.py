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

    def test_case_f1_genians_regime_buying_climax_blocked(self):
        # Genians at 11:33:28:
        # Massive volume blowoff into 25,250 resistance (75.8M turnover in 10s, 68% of 30s volume)
        # Price surging from 24,800 to 25,225 (+1.71% in 18s).
        # This is a buying climax / volume blowoff at highs and MUST NOT be classified as Route A EARLY_BREAKOUT.
        prices = [24800, 24850, 24900, 24950, 25000, 25050, 25050, 25100, 25150, 25200, 25225]
        for i, p in enumerate(prices):
            # In the final 10 seconds (indices 4..10), volume explodes heavily
            vol = 150 if i < 4 else 500
            t = Tick(
                symbol='263860', time=BASE_TIME - timedelta(seconds=len(prices)-i), price=float(p),
                volume=vol, cumulative=50000 + i * vol,
                ask=float(p + 25), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=BASE_TIME - timedelta(seconds=len(prices)-i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='263860', time=BASE_TIME, price=25225.0,
            volume=500, cumulative=60000,
            ask=25250.0, bid=25200.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        m = self.strategy.get_tape('263860').metrics(last_t.time)
        watch = {
            'code': '263860', 'name': '지니언스', 'status': 'WATCH',
            'day_high': 25200.0,
            'prior_day_high': 29450.0,
            'first_raw_price': 27600.0,
            'reference_price': 25200.0,
        }
        regime, reason, details = self.strategy.classify_regime(last_t, m, 'REGULAR', watch, asof=last_t.time)
        self.assertNotEqual(regime, 'A', f"Genians buying climax must not pass Route A: {reason}")
        self.assertIn(reason, ('BUYING_CLIMAX_VOLUME_EXHAUSTION', 'DOWNTREND_COUNTER_BOUNCE'))

    def test_case_f2_genians_peak_ceiling_equality_blocked(self):
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

    def test_case_f3_genians_unconfirmed_peak_breakout_blocked(self):
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

if __name__ == '__main__':
    unittest.main()
