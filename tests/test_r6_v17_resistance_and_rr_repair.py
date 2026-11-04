import unittest
from datetime import datetime, timedelta
from core.settings import KST, Config
from automation.policy import AutomationConfig
from core.strategy import Tick
from automation.fast_momentum import FastMomentumStrategy

BASE_TIME = datetime(2026, 11, 4, 9, 48, 22, tzinfo=KST)

class V17ResistanceAndRewardRiskRepairTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(live_mode=True, min_reward_risk=1.5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(fast_momentum_enabled=True, min_stop_distance_fraction=0.012)
        self.strategy = FastMomentumStrategy(self.cfg, self.auto)

    def test_case_a_miree_0948_blocked(self):
        # Case A: Miree (025560) at 09:48:22
        # Price surging 7,710 -> 7,770 (breakout) -> 7,750 (support) -> 7,810 (bounce)
        # Entry ask 7,820 >= resistance 7,810. Support is 7,750 (< 7,810 - buffer).
        # Resistance was NOT confirmed as broken/supported. Target must NOT leap to 8,990!
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
            'resistance': 7810.0,
            'resistance_asof': BASE_TIME.isoformat(),
            'observed_at': BASE_TIME.isoformat(),
            'recent_high': 8990.0,
            'daily_high_20d': 8990.0,
            'resistance_room_pass': False,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1398.0, asof=last_t.time
        )
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED')
        self.assertNotEqual(geo.get('target'), 8990.0)

    def test_case_b_dongjin_1424_blocked(self):
        # Case B: Dongjin Semichem (005290) at 14:24:24
        # Peak at 14:20 was 39,250 KRW. Entry ask 39,100.
        # Headroom to peak is only 0.38% (< 1.2%). Target must not leap to 53,800.
        dongjin_time = datetime(2026, 11, 3, 14, 24, 24, tzinfo=KST)
        for i in range(10):
            p = 38300.0 + i * 65.0
            tick = Tick(
                symbol='005290', time=dongjin_time + timedelta(seconds=i), price=p, volume=100, cumulative=200000+i*100,
                ask=p + 50.0, bid=p, regular=True, ask_qty=50, bid_qty=50,
                depth_available=True, book_time=dongjin_time + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(tick, tick.time)

        t = Tick(
            symbol='005290', time=dongjin_time + timedelta(seconds=10), price=38950.0, volume=100, cumulative=201000,
            ask=39100.0, bid=38950.0, regular=True, ask_qty=50, bid_qty=50,
            depth_available=True, book_time=dongjin_time + timedelta(seconds=10), depth_source='0D'
        )
        m = self.strategy.get_tape('005290').metrics(t.time)
        watch = {
            'code': '005290', 'name': '동진쎄미켐', 'status': 'WATCH',
            'daily_high_20d': 53800.0,
            'resistance': None, 'recent_high': None,
        }
        regime_details = {'entry_mode': 'PULLBACK_RECOVERY', 'support': 38300.0, 'peak': 39250.0}
        geo = self.strategy.calculate_invalidation_and_target(
            t, m, watch, capital_limit=70596.0, risk_budget=1411.0, asof=t.time,
            entry_mode='PULLBACK_RECOVERY', regime_details=regime_details
        )
        self.assertFalse(geo['pass'])
        self.assertIn(geo['reason'], ('INSUFFICIENT_ROOM_TO_RESISTANCE', 'NET_REWARD_RISK_LOW'))

    def test_case_c_ap_satellite_1310_allowed(self):
        # Case C: AP Satellite (211270) at 13:10:45
        # Entry at 9,320 KRW, overhead ceiling at 10,010 KRW (+7.4% headroom)
        # Structural support at 9,230, breakout at 9,270. Net RR 3.13 >= 1.5. Must PASS.
        ap_time = datetime(2026, 11, 3, 13, 10, 45, tzinfo=KST)
        prices = [9200, 9220, 9240, 9270, 9230, 9250, 9280, 9300, 9310]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='211270', time=ap_time + timedelta(seconds=i), price=float(p),
                volume=300, cumulative=30000 + i * 300,
                ask=float(p + 10), bid=float(p), regular=True, ask_qty=300, bid_qty=300,
                depth_available=True, book_time=ap_time + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='211270', time=ap_time + timedelta(seconds=len(prices)), price=9310.0,
            volume=300, cumulative=30000 + len(prices) * 300,
            ask=9320.0, bid=9300.0, regular=True, ask_qty=300, bid_qty=300,
            depth_available=True, book_time=ap_time + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('211270').metrics(last_t.time)
        watch = {
            'code': '211270', 'name': 'AP위성', 'status': 'WATCH',
            'resistance': 10010.0,
            'resistance_asof': ap_time.isoformat(),
            'observed_at': ap_time.isoformat(),
            'prior_day_high': 10010.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1392.0, asof=last_t.time
        )
        self.assertTrue(geo['pass'], geo.get('reason'))
        self.assertEqual(geo['target'], 10010.0)
        self.assertGreaterEqual(geo['net_rr'], 1.5)

    def test_case_d_confirmed_breakout_and_support_retest_allowed(self):
        # Case D: Genuine confirmed breakout and support retest above resistance
        # Primary resistance R1 was 10,000 KRW.
        # Price broke out above 10,000 to 10,200, pulled back and held support at 10,020 (>= 10,000 - buffer).
        # Breakout level is 10,200 (> 10,000). Current ask is 10,220.
        # Next resistance R2 is 11,000 KRW (+7.6% headroom).
        # This legitimate structural breakout MUST PASS!
        d_time = datetime(2026, 11, 4, 10, 15, 0, tzinfo=KST)
        prices = [9950, 10000, 10100, 10200, 10050, 10020, 10050, 10150, 10210]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='099990', time=d_time + timedelta(seconds=i), price=float(p),
                volume=500, cumulative=50000 + i * 500,
                ask=float(p + 10), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=d_time + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='099990', time=d_time + timedelta(seconds=len(prices)), price=10210.0,
            volume=500, cumulative=50000 + len(prices) * 500,
            ask=10220.0, bid=10200.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=d_time + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('099990').metrics(last_t.time)
        watch = {
            'code': '099990', 'name': '돌파성공주', 'status': 'WATCH',
            'resistance': 10000.0,
            'resistance_asof': d_time.isoformat(),
            'observed_at': d_time.isoformat(),
            'daily_high_20d': 11000.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time
        )
        self.assertTrue(geo['pass'], geo.get('reason'))
        self.assertEqual(geo['target'], 11000.0)
        self.assertGreaterEqual(geo['net_rr'], 1.5)

    def test_case_e_failed_breakout_reattempt_1tick_poke_blocked(self):
        # Case E: 1-tick poke or failed retest without support confirmation
        # Primary resistance R1 was 10,000 KRW.
        # Price poked 10,010, but pulled back deeply to support 9,850 (< 10,000 - buffer).
        # Now price surges again to 10,020 (ask 10,030).
        # Since support (9,850) is below resistance, resistance is NOT confirmed as support!
        # Target must NOT leap to 20-day high (12,000). Must be rejected!
        e_time = datetime(2026, 11, 4, 10, 20, 0, tzinfo=KST)
        prices = [9800, 9900, 10010, 9900, 9850, 9880, 9950, 10000, 10020]
        for i, p in enumerate(prices):
            t = Tick(
                symbol='088880', time=e_time + timedelta(seconds=i), price=float(p),
                volume=500, cumulative=50000 + i * 500,
                ask=float(p + 10), bid=float(p), regular=True, ask_qty=500, bid_qty=500,
                depth_available=True, book_time=e_time + timedelta(seconds=i), depth_source='0D'
            )
            self.strategy.ingest_tick(t, t.time)

        last_t = Tick(
            symbol='088880', time=e_time + timedelta(seconds=len(prices)), price=10020.0,
            volume=500, cumulative=50000 + len(prices) * 500,
            ask=10030.0, bid=10010.0, regular=True, ask_qty=500, bid_qty=500,
            depth_available=True, book_time=e_time + timedelta(seconds=len(prices)), depth_source='0D'
        )
        m = self.strategy.get_tape('088880').metrics(last_t.time)
        watch = {
            'code': '088880', 'name': '재돌파시도주', 'status': 'WATCH',
            'resistance': 10000.0,
            'resistance_asof': e_time.isoformat(),
            'observed_at': e_time.isoformat(),
            'daily_high_20d': 12000.0,
        }
        geo = self.strategy.calculate_invalidation_and_target(
            last_t, m, watch, capital_limit=81300.0, risk_budget=1626.0, asof=last_t.time
        )
        self.assertFalse(geo['pass'])
        self.assertEqual(geo['reason'], 'UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED')
        self.assertNotEqual(geo.get('target'), 12000.0)

if __name__ == '__main__':
    unittest.main()
