# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timedelta

from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.focus_selection import (
    select_focus, TIER_SIGNAL_READY, TIER_GEOMETRY_BASE_READY, TIER_RANK_ONLY
)

class TestIntradayResistanceAudit(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.policy = AutomationConfig()
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.detector = PatternDetector(self.cfg, self.policy, self.costs)

    def test_kumho_late_breakout_rejected_by_intraday_resistance(self):
        """Replay 11:10 Kumho Construction:
        - 10:38 morning peak at 13,950 KRW
        - 11:05~11:08 box consolidation at 13,660~13,790 KRW
        - 11:09:58 breakout attempt at 13,800 KRW
        - Target must be capped at 13,950 (not 14,050 / 14,450)
        - R:R = (13950 - 13800) / (13800 - 13640) = 0.9375 < 1.5 -> REJECTED
        - Setup C trial must have entry_ready = False due to RISK_REWARD_FAIL
        """
        sym = '002990'
        # 1. Feed morning peak at 10:38:00
        bars = [
            {'open': 13700.0, 'high': 13950.0, 'low': 13680.0, 'close': 13950.0, 'volume': 50000,
             'time': datetime(2026, 10, 12, 10, 38, 0, tzinfo=KST)},
            {'open': 13650.0, 'high': 13700.0, 'low': 13620.0, 'close': 13690.0, 'volume': 8366,
             'time': datetime(2026, 10, 12, 10, 57, 0, tzinfo=KST)},
            {'open': 13670.0, 'high': 13690.0, 'low': 13660.0, 'close': 13690.0, 'volume': 7957,
             'time': datetime(2026, 10, 12, 11, 4, 0, tzinfo=KST)},
            {'open': 13690.0, 'high': 13780.0, 'low': 13680.0, 'close': 13720.0, 'volume': 17862,
             'time': datetime(2026, 10, 12, 11, 5, 0, tzinfo=KST)},
            {'open': 13720.0, 'high': 13740.0, 'low': 13690.0, 'close': 13700.0, 'volume': 19651,
             'time': datetime(2026, 10, 12, 11, 6, 0, tzinfo=KST)},
            {'open': 13690.0, 'high': 13720.0, 'low': 13690.0, 'close': 13720.0, 'volume': 8469,
             'time': datetime(2026, 10, 12, 11, 7, 0, tzinfo=KST)},
            {'open': 13710.0, 'high': 13790.0, 'low': 13710.0, 'close': 13780.0, 'volume': 10235,
             'time': datetime(2026, 10, 12, 11, 8, 0, tzinfo=KST)},
        ]
        
        watch = {
            'symbol': sym,
            'code': sym,
            'status': 'WATCH',
            'resistance': 14450.0,  # Daily historical resistance
            'overhead_share_estimate': 0.13,
            'resistance_room_pass': True,
            'market_status': {'pass': True},
            'source_details': {'ka10032': {}, 'ka10027': {}, 'ka00198': {}}
        }
        
        box_geom = self.detector.box(bars)
        self.assertIsNotNone(box_geom)
        self.assertEqual(box_geom['breakout'], 13790.0)
        self.assertEqual(box_geom['support'], 13660.0)
        
        t = Tick(
            time=datetime(2026, 10, 12, 11, 9, 58, tzinfo=KST),
            symbol=sym,
            price=13800.0,
            volume=500,
            cumulative=10517649,
            bid=13790.0,
            ask=13800.0,
            regular=True
        )
        
        gates = self.detector.setup_gates(t, watch, box_geom, bars=bars)
        
        # Target must be capped at 13,950 (nearest intraday resistance)
        self.assertEqual(gates['risk_reward']['values']['target'], 13950.0)
        
        # Risk-Reward must FAIL (0.9375 < 1.5)
        self.assertEqual(gates['risk_reward']['result'], 'FAIL')
        self.assertAlmostEqual(gates['risk_reward']['values']['calculated_rr'], 0.9375, places=4)
        
        # Full setup trial must have entry_ready = False due to RISK_REWARD_FAIL
        trials = self.detector.setup_trials(t, watch, gates=self.detector.base_gates(t, watch), _bars=bars)
        c_trial = next(x for x in trials if x['setup'] == 'C')
        self.assertFalse(c_trial['entry_ready'])
        self.assertIn('RISK_REWARD_FAIL', c_trial['failed_reasons'])

    def test_day_high_breakout_allowed(self):
        """When price genuinely breaks above the intraday high, target looks to daily resistance."""
        sym = '002990'
        bars = [
            {'open': 13850.0, 'high': 13950.0, 'low': 13800.0, 'close': 13900.0, 'volume': 50000,
             'time': datetime(2026, 10, 12, 10, 38, 0, tzinfo=KST)},
            {'open': 13850.0, 'high': 13950.0, 'low': 13800.0, 'close': 13920.0, 'volume': 30000,
             'time': datetime(2026, 10, 12, 10, 40, 0, tzinfo=KST)},
            {'open': 13900.0, 'high': 13950.0, 'low': 13850.0, 'close': 13930.0, 'volume': 25000,
             'time': datetime(2026, 10, 12, 10, 42, 0, tzinfo=KST)},
            {'open': 13920.0, 'high': 13950.0, 'low': 13880.0, 'close': 13940.0, 'volume': 20000,
             'time': datetime(2026, 10, 12, 10, 44, 0, tzinfo=KST)},
            {'open': 13930.0, 'high': 13950.0, 'low': 13890.0, 'close': 13950.0, 'volume': 40000,
             'time': datetime(2026, 10, 12, 10, 45, 0, tzinfo=KST)},
        ]
        
        watch = {
            'symbol': sym,
            'code': sym,
            'status': 'WATCH',
            'resistance': 14450.0,
            'overhead_share_estimate': 0.13,
            'resistance_room_pass': True,
            'market_status': {'pass': True},
            'source_details': {'ka10032': {}, 'ka10027': {}, 'ka00198': {}}
        }
        
        box_geom = self.detector.box(bars)
        self.assertIsNotNone(box_geom)
        self.assertEqual(box_geom['breakout'], 13950.0)
        self.assertEqual(box_geom['support'], 13800.0)
        
        # Breakout above day high at 13,960
        t = Tick(
            time=datetime(2026, 10, 12, 10, 46, 0, tzinfo=KST),
            symbol=sym,
            price=13960.0,
            volume=500,
            cumulative=10517649,
            bid=13950.0,
            ask=13960.0,
            regular=True
        )
        
        gates = self.detector.setup_gates(t, watch, box_geom, bars=bars)
        
        # When 13,950 is broken, effective resistance is the daily resistance (14,450)
        self.assertEqual(gates['resistance']['values']['resistance'], 14450.0)
        self.assertEqual(gates['risk_reward']['result'], 'PASS')

    def test_focus_selection_freshness_boundary(self):
        """Readiness evaluations within 15s retain tier, while older evaluations drop to rank-only."""
        current_watch = {'002990': {'code': '002990', 'status': 'WATCH', 'score': 20.0}}
        candidates = [{'code': '002990', 'score': 20.0, 'source_count': 2, 'status': 'WATCH'}]
        readiness_map = {
            '002990': {
                'timestamp': '2026-10-12T10:53:35+09:00',
                'tier': TIER_GEOMETRY_BASE_READY
            }
        }
        # Clock is 10s after readiness event (<= 15s)
        clock_iso = '2026-10-12T10:53:45+09:00'
        res = select_focus(current_watch, candidates, readiness_map, clock_iso=clock_iso)
        self.assertEqual(res.tier_map['002990'], TIER_GEOMETRY_BASE_READY)
        self.assertIn('002990', res.next_watch)

if __name__ == '__main__':
    unittest.main()
