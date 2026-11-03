import unittest
from datetime import datetime
from core.settings import KST, Config
from automation.policy import AutomationConfig
from core.strategy import Tick
from automation.fast_momentum import FastMomentumStrategy
from automation.price_context import PriceContext

BASE_TIME = datetime(2026, 11, 3, 14, 24, 24, tzinfo=KST)

class DongjinLateChasePreventionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(fast_momentum_enabled=True, min_stop_distance_fraction=0.012)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_dongjin_1424_late_chase_rejected(self):
        # 005290 at 14:24:24: price pulled back to 38,300 and bounced to 39,050 / ask 39,100
        # Peak at 14:20 was 39,250 KRW (only +200 KRW above ask 39,100)
        # Daily high 20d was 53,800 KRW
        t = Tick(
            symbol='005290', time=BASE_TIME, price=38950.0, volume=100, cumulative=200000,
            ask=39100.0, bid=38950.0, regular=True, ask_qty=50, bid_qty=50,
            depth_available=True, book_time=BASE_TIME, depth_source='0D'
        )
        # Ingest 10 ticks around 38,300 -> 38,950 to form structure
        for i in range(10):
            p = 38300.0 + i * 65.0
            tick = Tick(
                symbol='005290', time=BASE_TIME, price=p, volume=100, cumulative=200000+i*100,
                ask=p + 50.0, bid=p, regular=True, ask_qty=50, bid_qty=50,
                depth_available=True, book_time=BASE_TIME, depth_source='0D'
            )
            self.strategy.ingest_tick(tick, BASE_TIME)

        m = self.strategy.get_tape('005290').metrics(BASE_TIME)
        watch = {
            'code': '005290', 'name': '동진쎄미켐', 'status': 'WATCH',
            'daily_high_20d': 53800.0,  # Historical 20-day high that previously caused false RR 13.0
            'resistance': None, 'recent_high': None,
        }
        regime_details = {'entry_mode': 'PULLBACK_RECOVERY', 'support': 38300.0, 'peak': 39250.0}
        geo = self.strategy.calculate_invalidation_and_target(
            t, m, watch, capital_limit=70596.0, risk_budget=1411.0, asof=BASE_TIME,
            entry_mode='PULLBACK_RECOVERY', regime_details=regime_details
        )
        # Under the fix, target MUST NOT leap to 53,800!
        self.assertFalse(geo['pass'])
        # Target must be bounded by swing peak (39,250) or reject due to headroom / net_rr
        if 'target' in geo:
            self.assertLessEqual(geo['target'], 39250.0)
            self.assertNotEqual(geo['target'], 53800.0)
        self.assertIn(geo['reason'], ('INSUFFICIENT_ROOM_TO_RESISTANCE', 'NET_REWARD_RISK_LOW'))

    def test_early_breakout_allowed(self):
        # 14:18 morning high breakout setup:
        # Origin at 37,000, swing high at 38,100, pullback to 37,500, then rebound breaking out at 38,150.
        prices = [37000, 37300, 37600, 37900, 38100, 37500, 37650, 37800, 37950, 38100, 38150]
        from datetime import timedelta
        for i, p in enumerate(prices):
            t = Tick(
                symbol='005290', time=BASE_TIME + timedelta(seconds=i), price=float(p),
                volume=1000, cumulative=100000 + i * 1000,
                ask=float(p), bid=float(p - 50), regular=True, ask_qty=1000, bid_qty=1000,
                depth_available=True, book_time=BASE_TIME + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='005290', time=BASE_TIME + timedelta(seconds=len(prices)), price=38150.0,
            volume=1000, cumulative=100000 + len(prices) * 1000,
            ask=38150.0, bid=38100.0, regular=True, ask_qty=1000, bid_qty=1000,
            depth_available=True, book_time=BASE_TIME + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('005290').metrics(last_t.time)
        watch = {
            'code': '005290', 'name': '동진쎄미켐', 'status': 'WATCH',
            'resistance': 40500.0, 'resistance_asof': BASE_TIME.isoformat(),
            'observed_at': BASE_TIME.isoformat(),
            'daily_high_20d': 53800.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time,
            entry_mode='EARLY_BREAKOUT', regime_details={'entry_mode': 'EARLY_BREAKOUT'}
        )
        # Ample headroom to 40,000 KRW from 38,150 KRW (reward ~1,850 KRW vs risk ~900 KRW)
        self.assertTrue(geo['pass'], geo.get('reason'))
        self.assertEqual(geo['target'], 40500.0)
        self.assertGreaterEqual(geo['net_rr'], 1.5)

    def test_merge_history_stores_session_high(self):
        pc = PriceContext(bar_limit=120)
        bars = [
            {'time': datetime(2026, 11, 3, 14, 18, 0, tzinfo=KST), 'open': 36950, 'high': 38100, 'low': 36900, 'close': 37450, 'volume': 7523},
            {'time': datetime(2026, 11, 3, 14, 19, 0, tzinfo=KST), 'open': 37450, 'high': 38050, 'low': 37250, 'close': 38050, 'volume': 10625},
        ]
        pc.merge_history('005290', bars, datetime(2026, 11, 3, 14, 20, 42, tzinfo=KST), 'KRX', session_high=39250.0)
        self.assertEqual(pc.day_high('005290'), 39250.0)

    def test_no_synthetic_2r_target_fabricated_on_breakout(self):
        # When breaking out without confirmed overhead resistance, do NOT fabricate 2R target
        prices = [37000, 37300, 37600, 37900, 38100, 37500, 37650, 37800, 37950, 38100, 38150]
        from datetime import timedelta
        for i, p in enumerate(prices):
            t = Tick(
                symbol='005290', time=BASE_TIME + timedelta(seconds=i), price=float(p),
                volume=1000, cumulative=100000 + i * 1000,
                ask=float(p), bid=float(p - 50), regular=True, ask_qty=1000, bid_qty=1000,
                depth_available=True, book_time=BASE_TIME + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='005290', time=BASE_TIME + timedelta(seconds=len(prices)), price=38150.0,
            volume=1000, cumulative=100000 + len(prices) * 1000,
            ask=38150.0, bid=38100.0, regular=True, ask_qty=1000, bid_qty=1000,
            depth_available=True, book_time=BASE_TIME + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('005290').metrics(last_t.time)
        # watch has NO known resistance or recent_high
        watch = {
            'code': '005290', 'name': '동진쎄미켐', 'status': 'WATCH',
            'resistance': None, 'recent_high': None, 'daily_high_20d': None,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time,
            entry_mode='EARLY_BREAKOUT', regime_details={'entry_mode': 'EARLY_BREAKOUT'}
        )
        # Must reject with KNOWN_OVERHEAD_TARGET_UNAVAILABLE, NOT synthesize 2R target
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'KNOWN_OVERHEAD_TARGET_UNAVAILABLE')

if __name__ == '__main__':
    unittest.main()
