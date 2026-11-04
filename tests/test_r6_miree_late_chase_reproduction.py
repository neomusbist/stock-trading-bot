import unittest
from datetime import datetime, timedelta
from core.settings import KST, Config
from automation.policy import AutomationConfig
from core.strategy import Tick
from automation.fast_momentum import FastMomentumStrategy

BASE_TIME = datetime(2026, 11, 4, 9, 48, 22, tzinfo=KST)

class MireeLateChaseReproductionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(fast_momentum_enabled=True, min_stop_distance_fraction=0.012)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_miree_0948_entry_reproduction(self):
        # 025560 at 09:48:22:
        # Price surging from 7,710 -> 7,770 -> 7,750 -> 7,810, ask 7,820
        # Resistance was 7,810 KRW, recent_high/daily_high_20d was 8,990 KRW
        prices = [7710, 7730, 7750, 7770, 7750, 7760, 7780, 7800, 7810]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='025560', time=BASE_TIME + timedelta(seconds=i), price=float(p),
                volume=500, cumulative=50000 + i * 500,
                ask=float(p + 10), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=BASE_TIME + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='025560', time=BASE_TIME + timedelta(seconds=len(prices)), price=7810.0,
            volume=500, cumulative=50000 + len(prices) * 500,
            ask=7820.0, bid=7800.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=BASE_TIME + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('025560').metrics(last_t.time)
        watch = {
            'code': '025560', 'name': '미래산업', 'status': 'WATCH',
            'resistance': 7810.0,  # Known resistance is 7,810 KRW!
            'resistance_asof': BASE_TIME.isoformat(),
            'observed_at': BASE_TIME.isoformat(),
            'recent_high': 8990.0,
            'daily_high_20d': 8990.0,
            'resistance_room_pass': False,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1398.0, asof=last_t.time
        )
        # Under the fix, this MUST be rejected with UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED')
        self.assertNotEqual(geo.get('target'), 8990.0)

if __name__ == '__main__':
    unittest.main()
