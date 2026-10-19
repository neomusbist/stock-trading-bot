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
        """EarlyMomentumBreakout verifies base high breakout, structural stop at l_base - ts, and net RR >= 1.40."""
        bars = [
            {'time': self.time - timedelta(minutes=14 - i), 'open': 9900, 'high': 10080, 'low': 9900, 'close': 10050, 'volume': 8000}
            for i in range(10)
        ] + [
            {'time': self.time - timedelta(minutes=4), 'open': 10020, 'high': 10100, 'low': 10000, 'close': 10040, 'volume': 5000},
            {'time': self.time - timedelta(minutes=3), 'open': 10040, 'high': 10100, 'low': 10010, 'close': 10070, 'volume': 6000},
            {'time': self.time - timedelta(minutes=2), 'open': 10070, 'high': 10100, 'low': 10020, 'close': 10090, 'volume': 5500},
            {'time': self.time - timedelta(minutes=1), 'open': 10090, 'high': 10100, 'low': 10030, 'close': 10095, 'volume': 9000},
        ]
        # In this active scenario with 14-bar ATR ~ 145 KRW, wave target expands to 10110 + 3*145 = 10540
        tick = Tick('093370', self.time, 10110, 3000, 100000, 10110, 10105, True, ask_qty=5000, bid_qty=5000, depth_available=True)
        watch = {'code': '093370', 'resistance': 10800}

        sig, details = evaluate_early_momentum_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'], f"Rejected on: {details.get('reason')}")
        self.assertIsNotNone(sig)
        self.assertEqual(sig['pattern'], 'EarlyMomentumBreakout')
        self.assertEqual(sig['h_base'], 10100)
        self.assertEqual(sig['l_base'], 10000)
        self.assertEqual(sig['stop'], 9990)  # Structural invalidation: floor_tick(10000 - 10) = 9990
        self.assertEqual(sig['emergency'], 9980)  # Emergency floor: 9990 - 10 = 9980
        self.assertEqual(sig['structural_stop'], 9990)
        self.assertEqual(sig['emergency_stop'], 9980)
        self.assertGreaterEqual(sig['net_reward_risk'], 1.40)
        self.assertGreaterEqual(sig['net_rr_structural'], 1.40)
        self.assertGreater(sig['risk_per_share_sized'], 0)

    def test_target_strictly_capped_by_near_resistance(self):
        """Target must NEVER exceed overhead resistance; if overhead is too near, NET_RR_LOW rejects trade."""
        bars = [
            {'time': self.time - timedelta(minutes=6), 'open': 9900, 'high': 10080, 'low': 9900, 'close': 10050, 'volume': 8000},
            {'time': self.time - timedelta(minutes=5), 'open': 10050, 'high': 10100, 'low': 9920, 'close': 10020, 'volume': 7000},
            {'time': self.time - timedelta(minutes=4), 'open': 10020, 'high': 10100, 'low': 10000, 'close': 10040, 'volume': 5000},
            {'time': self.time - timedelta(minutes=3), 'open': 10040, 'high': 10100, 'low': 10010, 'close': 10070, 'volume': 6000},
            {'time': self.time - timedelta(minutes=2), 'open': 10070, 'high': 10100, 'low': 10020, 'close': 10090, 'volume': 5500},
            {'time': self.time - timedelta(minutes=1), 'open': 10090, 'high': 10100, 'low': 10030, 'close': 10095, 'volume': 9000},
        ]
        tick = Tick('093370', self.time, 10110, 3000, 100000, 10110, 10105, True, ask_qty=5000, bid_qty=5000, depth_available=True)
        # Overhead at 10250 (clears breakeven at 10190, but caps target so net RR < 1.40)
        watch_tight = {'code': '093370', 'resistance': 10250}

        sig, details = evaluate_early_momentum_breakout(bars, tick, watch_tight, self.cfg, self.auto, self.costs.__dict__)
        self.assertFalse(details['pass'])
        self.assertEqual(details['reason'], 'NET_RR_LOW')

    def test_boundary_tick_sizes(self):
        """Verifies KRX tick size boundaries and floor_tick precision."""
        from core.util import tick_size, floor_tick
        self.assertEqual(tick_size(1999), 1)
        self.assertEqual(tick_size(2000), 5)
        self.assertEqual(tick_size(4995), 5)
        self.assertEqual(tick_size(5000), 10)
        self.assertEqual(tick_size(19990), 10)
        self.assertEqual(tick_size(20000), 50)
        self.assertEqual(tick_size(49950), 50)
        self.assertEqual(tick_size(50000), 100)

        # Check floor_tick
        self.assertEqual(floor_tick(1999), 1999)
        self.assertEqual(floor_tick(2004), 2000)
        self.assertEqual(floor_tick(5008), 5000)
        self.assertEqual(floor_tick(20045), 20000)

    def test_formal_config_loading_and_shadow_isolation(self):
        """Verifies formal validation prevents enabling live redesign without shadow trials and costs."""
        # Valid trials-only config
        cfg_valid = Config(enable_redesign_trials=True, enable_redesign_live=False).validate()
        self.assertTrue(cfg_valid.enable_redesign_trials)
        self.assertFalse(cfg_valid.enable_redesign_live)

        # Live without trials must raise ValueError
        with self.assertRaises(ValueError):
            Config(enable_redesign_trials=False, enable_redesign_live=True).validate()

        # AutomationConfig live without trials or unconfirmed costs must raise ValueError
        with self.assertRaises(ValueError):
            AutomationConfig(enable_redesign_trials=False, enable_redesign_live=True, costs_confirmed=True).validate()
        with self.assertRaises(ValueError):
            AutomationConfig(enable_redesign_trials=True, enable_redesign_live=True, costs_confirmed=False).validate()

    def test_pullback_reclaim_breakout_evaluation(self):
        """PullbackReclaimBreakout verifies impulse rise, normal retrace, dry volume, and reclaim trigger."""
        bars = [
            # Impulse from 10000 to 10500 (+5%)
            {'time': self.time - timedelta(minutes=5), 'open': 10000, 'high': 10300, 'low': 10000, 'close': 10250, 'volume': 5000},
            {'time': self.time - timedelta(minutes=4), 'open': 10250, 'high': 10500, 'low': 10200, 'close': 10500, 'volume': 8000},  # Peak 10500
            # Pullback to 10300 (retrace 200/500 = 40%) with dried volume
            {'time': self.time - timedelta(minutes=3), 'open': 10500, 'high': 10500, 'low': 10350, 'close': 10380, 'volume': 2000},
            {'time': self.time - timedelta(minutes=2), 'open': 10380, 'high': 10400, 'low': 10300, 'close': 10320, 'volume': 1500},
            {'time': self.time - timedelta(minutes=1), 'open': 10320, 'high': 10360, 'low': 10300, 'close': 10350, 'volume': 1200},  # Reclaim ref 10360
        ]
        # Tick breaking above prior bar high of 10360
        tick = Tick('093370', self.time, 10380, 2000, 50000, 10380, 10370, True, ask_qty=5000, bid_qty=5000, depth_available=True)
        watch = {'code': '093370', 'resistance': 11000}

        sig, details = evaluate_pullback_reclaim_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'], f"Rejected on: {details.get('reason')}")
        self.assertIsNotNone(sig)
        self.assertEqual(sig['pattern'], 'PullbackReclaimBreakout')
        self.assertEqual(sig['h_impulse'], 10500)
        self.assertEqual(sig['l_pullback'], 10300)
        self.assertEqual(sig['stop'], 10290)  # 10300 - 10
        self.assertEqual(sig['structural_stop'], 10290)
        self.assertGreaterEqual(sig['net_reward_risk'], 1.50)
        self.assertGreaterEqual(sig['net_rr_structural'], 1.50)
        self.assertGreater(sig['risk_per_share_sized'], 0)

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
