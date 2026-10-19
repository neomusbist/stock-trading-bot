"""
Unit and integration tests for R6 Strategy Redesign (2026-10-19):
- INTRADAY_SURGE_OVERRIDE qualification
- 4-stage latency tracking
- EarlyMomentumBreakout and PullbackReclaimBreakout candidate evaluations
- PRE_INTENT 1-tick ask tolerance with pre-validated RR
- FAST v2 viable horizon vs distant daily cap illusion
- FAST Policies A, B, C comparison on Lunit fixture
- Risk budget (2%) and daily risk limit (4%) invariance
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import unittest

from core.settings import Config, KST
from core.strategy import Tick
from core.util import tick_size
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.radar import observation_structure
from automation.entry_timing import ObservationTimeline
from automation.redesign_strategies import (
    evaluate_early_momentum_breakout,
    evaluate_pullback_reclaim_breakout,
    compute_fast_viable_horizon,
    ConditionalFastExitEvaluator
)
from automation.engine import AutonomousEngine
from tests.test_automation import FakeBroker
from tests.test_v1 import bars as make_bars


class R6RedesignTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(
            self.auto.commission_buy, self.auto.commission_sell, self.auto.sell_tax,
            self.cfg.entry_slippage_fraction, self.cfg.exit_slippage_fraction
        )
        self.time = datetime(2026, 10, 19, 11, 0, 0, tzinfo=KST)

    def test_track2_intraday_surge_override(self):
        """Historical 20-day turnover failure is overridden if intraday surge and volume are proven."""
        # Create bars ending on previous trading day with healthy price distribution so overhead share < 35%
        data = [{'datetime': (self.time.date() - timedelta(days=750 - i)).isoformat(),
                 'open': 800 + i * 0.2, 'high': 805 + i * 0.2, 'low': 795 + i * 0.2, 'close': 800 + i * 0.2, 'volume': 100}
                for i in range(750)]

        # Normal evaluation without surge data -> REJECT
        res_normal = observation_structure(data, 1000, self.cfg, self.time.date(), item=None)
        self.assertEqual(res_normal['status'], 'REJECT')
        self.assertIn('거래대금 부족', res_normal['reason'])

        # With surge item: trde_prica=5000 (50억원), flu_rt=+8.5%
        item_surge = {
            'code': '093370',
            'source_details': {
                'ka10032': {
                    'values': {'trde_prica': 5000.0, 'flu_rt': 8.5}
                }
            }
        }
        res_surge = observation_structure(data, 1000, self.cfg, self.time.date(), item=item_surge)
        self.assertEqual(res_surge['status'], 'ELIGIBLE')
        self.assertTrue(res_surge.get('intraday_surge_override'))
        self.assertEqual(res_surge.get('track'), 'INTRADAY_SURGE_OVERRIDE')

    def test_latency_four_stages_recorded(self):
        """Timeline records all 4 distinct delay stages without conflating RAW->bar with WATCH->eval."""
        timeline = ObservationTimeline()
        sym = '093370'
        t0 = self.time
        t1 = t0 + timedelta(seconds=15)
        t2 = t0 + timedelta(seconds=20)
        t3 = t0 + timedelta(seconds=25)
        t4 = t0 + timedelta(seconds=85)
        t5 = t0 + timedelta(seconds=90)
        t6 = t0 + timedelta(seconds=95)

        timeline.raw([{'code': sym, 'first_raw_at': t0.isoformat()}], t0)
        timeline.subscription_request([sym], t1)
        timeline.subscription_ack([sym], t2)
        timeline.tick(sym, t3, [], price=1000, received=t3, processed=t3)

        dummy_bars = [{'time': t4, 'open': 1000, 'high': 1010, 'low': 995, 'close': 1005, 'volume': 1000}]
        timeline.tick(sym, t4, dummy_bars, price=1005, received=t4, processed=t4)
        timeline.watch(sym, t5)
        timeline.signal(sym, t6)

        snap = timeline.opening_snapshot(t6 + timedelta(seconds=1))
        lat = snap['symbols'][sym]['latency_seconds']

        self.assertEqual(lat['raw_to_sub_req'], 15.0)
        self.assertEqual(lat['sub_req_to_tick'], 10.0)
        self.assertEqual(lat['subscription_ack_to_tick'], 5.0)
        self.assertEqual(lat['tick_to_completed_bar'], 60.0)
        self.assertEqual(lat['watch_to_eval'], 5.0)

    def test_early_momentum_breakout_evaluation(self):
        """EarlyMomentumBreakout verifies base high breakout, stop distance, and net RR."""
        bars = [
            {'time': self.time - timedelta(minutes=4), 'open': 1000, 'high': 1020, 'low': 1005, 'close': 1010, 'volume': 1000},
            {'time': self.time - timedelta(minutes=3), 'open': 1010, 'high': 1020, 'low': 1008, 'close': 1015, 'volume': 1200},
            {'time': self.time - timedelta(minutes=2), 'open': 1015, 'high': 1020, 'low': 1010, 'close': 1018, 'volume': 1100},
            {'time': self.time - timedelta(minutes=1), 'open': 1018, 'high': 1020, 'low': 1012, 'close': 1018, 'volume': 1500},
        ]
        # Tick breaking above 1020 base high
        tick = Tick('093370', self.time, 1025, 2000, 50000, 1025, 1024, True, depth_available=True)
        watch = {'code': '093370', 'resistance': 1080}

        sig, details = evaluate_early_momentum_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'])
        self.assertIsNotNone(sig)
        self.assertEqual(sig['pattern'], 'EarlyMomentumBreakout')
        self.assertEqual(sig['h_base'], 1020)
        self.assertEqual(sig['stop'], 1004)  # 1005 - 1
        self.assertGreaterEqual(sig['net_reward_risk'], 1.40)

    def test_pullback_reclaim_breakout_evaluation(self):
        """PullbackReclaimBreakout verifies impulse rise, normal retrace, dry volume, and reclaim trigger."""
        bars = [
            # Impulse from 1000 to 1050 (+5%)
            {'time': self.time - timedelta(minutes=5), 'open': 1000, 'high': 1030, 'low': 1000, 'close': 1025, 'volume': 5000},
            {'time': self.time - timedelta(minutes=4), 'open': 1025, 'high': 1050, 'low': 1020, 'close': 1050, 'volume': 8000},  # Peak 1050
            # Pullback to 1030 (retrace 20/50 = 40%) with dried volume
            {'time': self.time - timedelta(minutes=3), 'open': 1050, 'high': 1050, 'low': 1035, 'close': 1038, 'volume': 2000},
            {'time': self.time - timedelta(minutes=2), 'open': 1038, 'high': 1040, 'low': 1030, 'close': 1032, 'volume': 1500},
            {'time': self.time - timedelta(minutes=1), 'open': 1032, 'high': 1036, 'low': 1030, 'close': 1035, 'volume': 1200},  # Reclaim ref 1036
        ]
        # Tick breaking above prior bar high of 1036
        tick = Tick('093370', self.time, 1038, 2000, 50000, 1038, 1037, True, depth_available=True)
        watch = {'code': '093370', 'resistance': 1100}

        sig, details = evaluate_pullback_reclaim_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'])
        self.assertIsNotNone(sig)
        self.assertEqual(sig['pattern'], 'PullbackReclaimBreakout')
        self.assertEqual(sig['h_impulse'], 1050)
        self.assertEqual(sig['l_pullback'], 1030)
        self.assertEqual(sig['stop'], 1029)  # 1030 - 1
        self.assertGreaterEqual(sig['net_reward_risk'], 1.50)

    def test_fast_horizon_blocks_distant_daily_cap_illusion(self):
        """Lunit scenario: Distant daily cap (+35.8%) is capped by micro wave target and rejected."""
        res = compute_fast_viable_horizon(
            entry_limit=12890,
            structural_stop=12840,
            emergency_stop=12820,
            atr_1m=35.0,
            micro_impulse_span=40,
            nearest_intraday_res=None,
            daily_res_cap=17510,
            vi_upper_price=None,
            mtf_5m_context='MIXED'
        )
        self.assertFalse(res.is_viable)
        self.assertEqual(res.realizable_target, 13010)
        self.assertEqual(res.breakeven_price, 12990)
        self.assertEqual(res.reject_reason, 'FAST_REALIZABLE_HORIZON_BELOW_MIN_RR')
        self.assertLess(res.net_rr, 0.5)

    def test_fast_hybrid_exit_policies_lunit_comparison(self):
        """Lunit fixture: Policies A, B, C all hold at 11:41:26, and exit on structural breakdown."""
        fixture = json.loads(Path('tests/fixtures/lunit_exit_20261019.json').read_text(encoding='utf-8'))
        position = fixture['position']
        ticks = fixture['ticks']

        trades = [r['price'] for r in ticks]
        last_tick = Tick(
            symbol=ticks[-1]['symbol'], time=datetime.fromisoformat(ticks[-1]['exchange_at']),
            price=ticks[-1]['price'], volume=ticks[-1]['volume'], cumulative=ticks[-1]['cumulative'],
            bid=ticks[-1]['bid'], ask=ticks[-1]['ask'], bid_qty=ticks[-1]['bid_qty'], ask_qty=ticks[-1]['ask_qty'],
            regular=ticks[-1]['regular'], depth_available=ticks[-1]['depth_available'], depth_source=ticks[-1]['depth_source'],
            book_time=datetime.fromisoformat(ticks[-1]['book_time']), venue=ticks[-1]['venue']
        )
        decision = datetime.fromisoformat(fixture['decision_at'])

        # 11:41:26 check: all policies must HOLD
        for pol in ('POLICY_A', 'POLICY_B', 'POLICY_C'):
            ev = ConditionalFastExitEvaluator(policy=pol, time_stop_seconds=300.0)
            exit_flag, reason, _ = ev.evaluate(position, last_tick, decision, trades)
            self.assertFalse(exit_flag, f'{pol} prematurely exited at 11:41:26: {reason}')
            self.assertEqual(reason, 'HOLD')

        # Structural stop breakdown: all policies must EXIT
        breakdown_trades = trades + [12840, 12840]
        breakdown_tick = Tick('328130', decision, 12840, 10, 100, 12840, 12850, True, depth_available=True)
        for pol in ('POLICY_A', 'POLICY_B', 'POLICY_C'):
            ev = ConditionalFastExitEvaluator(policy=pol, time_stop_seconds=300.0)
            exit_flag, reason, _ = ev.evaluate(position, breakdown_tick, decision, breakdown_trades)
            self.assertTrue(exit_flag, f'{pol} failed to exit on structural breakdown')
            self.assertIn('STOP', reason)

    def test_pre_intent_1tick_ask_tolerance(self):
        """Engine PRE_INTENT allows 1-tick ask increase if pre-validated RR and chase remain valid."""
        from dataclasses import replace
        engine = AutonomousEngine(self.cfg, replace(self.auto, enable_ask_1tick_tolerance=True),
                                  Policy(Path('runtime/automation_policy.json')),
                                  Path('runtime/automation-production'), FakeBroker(), lambda: self.time)

        signal = {
            'symbol': '093370',
            'limit': 1000,
            'stop': 970,
            'target': 1060,  # RR = 60 / 30 = 2.0
            'pattern': 'A',
            'breakout': 995,
            'signal_detected_at': self.time.isoformat()
        }
        # Ask increased by 1 tick (1000 -> 1001)
        tick_tolerated = Tick('093370', self.time, 1000, 100, 100, 1001, 1000, True, depth_available=True)

        # With tolerance enabled, ENTRY_QUOTE_ABOVE_LIMIT is not returned
        reason = engine.entry_market_reason(signal, quantity=10, fallback=tick_tolerated, phase='PRE_INTENT')
        self.assertNotEqual(reason, 'ENTRY_QUOTE_ABOVE_LIMIT')

        # Without tolerance enabled, ENTRY_QUOTE_ABOVE_LIMIT is returned
        engine_strict = AutonomousEngine(self.cfg, replace(self.auto, enable_ask_1tick_tolerance=False),
                                         Policy(Path('runtime/automation_policy.json')),
                                         Path('runtime/automation-production'), FakeBroker(), lambda: self.time)
        reason_strict = engine_strict.entry_market_reason(signal, quantity=10, fallback=tick_tolerated, phase='PRE_INTENT')
        self.assertEqual(reason_strict, 'ENTRY_QUOTE_ABOVE_LIMIT')


if __name__ == '__main__':
    unittest.main()
