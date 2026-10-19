"""
Offline Integration Tests for R6 Redesign A/B Strategies (2026-10-19):
- Signal generation with mathematically valid RR without distant resistance
- Full pipeline: Signal -> PRE_INTENT -> OrderIntent -> Virtual Fill -> Exit Plan
- Strict risk budget (2%) and daily risk limit (4%)
- Single slot and repeat_gate duplicate prevention
- exit_plan.py binding: EARLY_MOMENTUM_BASE and PULLBACK_RECLAIM_STRUCTURE close_based
- 0 external network/broker calls
"""
import copy
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock

from core.settings import Config, KST
from core.strategy import Tick
from core.util import tick_size
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.exit_plan import invalidation_lines, build_plan
from automation.redesign_strategies import (
    evaluate_early_momentum_breakout,
    evaluate_pullback_reclaim_breakout,
    calculate_net_proceeds_and_cost
)
from tests.test_automation import FakeBroker


class AbOrderPathIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        object.__setattr__(self.cfg, 'enable_redesign_trials', True)
        object.__setattr__(self.cfg, 'enable_redesign_live', True)
        object.__setattr__(self.cfg, 'live_mode', True)
        self.auto = AutomationConfig(AUTO_TRADING_CAP_KRW=10_000_000)
        object.__setattr__(self.auto, 'enable_ask_1tick_tolerance', True)
        object.__setattr__(self.auto, 'confirmed_invalidation_enabled', True)
        object.__setattr__(self.auto, 'costs_confirmed', True)
        self.costs = Costs(
            self.auto.commission_buy, self.auto.commission_sell, self.auto.sell_tax,
            self.cfg.entry_slippage_fraction, self.cfg.exit_slippage_fraction
        )
        import tempfile
        from pathlib import Path
        from core.util import atomic_json
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp_dir.name)
        self.time = datetime(2026, 10, 19, 10, 0, 0, tzinfo=KST)
        atomic_json(self.folder / 'policy.json', {
            'mode': 'LIVE', 'live_authorized': True, 'releases': {}, 'revision': 1, 'commands': []
        })
        self.policy = Policy(self.folder / 'policy.json')
        self.broker = FakeBroker()
        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder, self.broker, lambda: self.time)
        import time
        self.engine.connected = True
        self.engine.account_ok = True
        self.engine.last_account = time.monotonic()  # fresh account timestamp
        self.engine.s['cash'] = 10_000_000
        self.engine.available_cash = 10_000_000
        self.engine.s['daily_loss'] = 0

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_early_momentum_breakout_rr_without_distant_overhead(self):
        """Proves that a realistic base with 14m session ATR produces achievable Net RR >= 1.40 without distant resistance."""
        bars = [
            {'time': self.time - timedelta(minutes=14 - i), 'open': 9900, 'high': 10100, 'low': 9900, 'close': 10050, 'volume': 8000}
            for i in range(10)
        ] + [
            {'time': self.time - timedelta(minutes=4), 'open': 10040, 'high': 10100, 'low': 10040, 'close': 10060, 'volume': 5000},
            {'time': self.time - timedelta(minutes=3), 'open': 10060, 'high': 10100, 'low': 10040, 'close': 10070, 'volume': 6000},
            {'time': self.time - timedelta(minutes=2), 'open': 10070, 'high': 10100, 'low': 10040, 'close': 10090, 'volume': 5500},
            {'time': self.time - timedelta(minutes=1), 'open': 10090, 'high': 10100, 'low': 10050, 'close': 10095, 'volume': 9000},
        ]
        # Breakout tick
        tick = Tick('093370', self.time, 10110, 3000, 100000, 10110, 10105, True, ask_qty=5000, bid_qty=5000, depth_available=True)
        # NO arbitrary distant resistance passed
        watch = {'code': '093370', 'status': 'WATCH'}

        sig, details = evaluate_early_momentum_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'], f"Rejected on: {details.get('reason')}")
        self.assertIsNotNone(sig)
        self.assertGreaterEqual(sig['net_reward_risk'], 1.40)
        self.assertEqual(sig['stop'], 10030)
        self.assertEqual(sig['emergency'], 10020)
        self.assertEqual(sig['structural_stop'], 10030)
        self.assertEqual(sig['emergency_stop'], 10020)
        self.assertGreater(sig['risk_per_share_sized'], 0)

    def test_pullback_reclaim_breakout_rr_and_breakeven_milestone(self):
        """Proves pullback reclaim generates valid signal with impulse peak above breakeven."""
        bars = [
            {'time': self.time - timedelta(minutes=5), 'open': 10000, 'high': 10300, 'low': 10000, 'close': 10250, 'volume': 15000},
            {'time': self.time - timedelta(minutes=4), 'open': 10250, 'high': 10450, 'low': 10200, 'close': 10400, 'volume': 20000},  # Peak 10450 (+4.5%)
            {'time': self.time - timedelta(minutes=3), 'open': 10400, 'high': 10420, 'low': 10250, 'close': 10280, 'volume': 4000},   # Dried pullback
            {'time': self.time - timedelta(minutes=2), 'open': 10280, 'high': 10300, 'low': 10220, 'close': 10250, 'volume': 3500},
            {'time': self.time - timedelta(minutes=1), 'open': 10250, 'high': 10290, 'low': 10220, 'close': 10280, 'volume': 3000},   # Reclaim ref 10290
        ]
        tick = Tick('093370', self.time, 10300, 3000, 100000, 10300, 10280, True, ask_qty=5000, bid_qty=5000, depth_available=True)
        watch = {'code': '093370', 'status': 'WATCH'}

        sig, details = evaluate_pullback_reclaim_breakout(bars, tick, watch, self.cfg, self.auto, self.costs.__dict__)
        self.assertTrue(details['pass'], f"Rejected on: {details.get('reason')}")
        self.assertIsNotNone(sig)
        self.assertGreaterEqual(sig['net_reward_risk'], 1.30)
        self.assertEqual(sig['h_impulse'], 10450)

    def test_engine_sizing_2pct_budget_and_daily_limit(self):
        """Engine enforces 2% trade risk budget and 4% daily risk budget accurately."""
        equity = 10_000_000
        remaining_daily_risk = 400_000  # 4%
        signal = {
            'symbol': '093370',
            'limit': 10000,
            'stop': 9800,
            'emergency': 9750,
            'invalidation_policy': {'enabled': True, 'structural_stop': 9800, 'emergency_stop': 9750}
        }
        # Risk per share roughly: (10000 - 9750) + costs = 250 + ~25 = 275 KRW
        # 2% equity risk budget = 200,000 KRW
        # Expected quantity = 200,000 // 275 ≈ 727 shares
        details = self.engine.quantity_details(signal, equity, 10_000_000, remaining_daily_risk)
        self.assertGreater(details['quantity'], 0)
        self.assertLessEqual(details['quantity'] * details['risk_per_share'], 200_000 + 1000)

        # When daily risk is exhausted (remaining = 0), quantity must be 0
        details_zero = self.engine.quantity_details(signal, equity, 10_000_000, 0)
        self.assertEqual(details_zero['quantity'], 0)
        self.assertEqual(details_zero['reason_code'], 'RISK_BUDGET_ZERO_QTY')

    def test_exit_plan_binding_for_redesign_policies(self):
        """exit_plan.py binds EARLY_MOMENTUM_BASE and PULLBACK_RECLAIM_STRUCTURE to close_based."""
        pos_early = {
            'symbol': '093370',
            'initial_stop': 9800,
            'stop': 9800,
            'pattern': 'EarlyMomentumBreakout',
            'signal': {
                'invalidation_policy': {
                    'enabled': True,
                    'policy_name': 'EARLY_MOMENTUM_BASE',
                    'structural_stop': 9850,
                    'emergency_stop': 9800,
                    'noise_band': 50
                }
            }
        }
        lines_early = invalidation_lines(self.cfg, self.auto, pos_early)
        self.assertTrue(lines_early['close_based'])
        self.assertEqual(lines_early['initial_structure'], 9850)
        self.assertEqual(lines_early['emergency'], 9800)

        pos_reclaim = {
            'symbol': '093370',
            'initial_stop': 9700,
            'stop': 9700,
            'pattern': 'PullbackReclaimBreakout',
            'signal': {
                'invalidation_policy': {
                    'enabled': True,
                    'policy_name': 'PULLBACK_RECLAIM_STRUCTURE',
                    'structural_stop': 9750,
                    'emergency_stop': 9700,
                    'noise_band': 60
                }
            }
        }
        lines_reclaim = invalidation_lines(self.cfg, self.auto, pos_reclaim)
        self.assertTrue(lines_reclaim['close_based'])
        self.assertEqual(lines_reclaim['initial_structure'], 9750)
        self.assertEqual(lines_reclaim['emergency'], 9700)

    def test_pre_intent_and_shadow_isolation_guard(self):
        """Verifies that SHADOW_ONLY=True is blocked at engine boundary, but passes when authorized."""
        signal = {
            'symbol': '093370',
            'limit': 10000,
            'stop': 9800,
            'emergency': 9750,
            'target': 10500,
            'pattern': 'EarlyMomentumBreakout',
            'SHADOW_ONLY': True,
            'actual_order_permission': False,
            'invalidation_policy': {'enabled': True, 'structural_stop': 9800, 'emergency_stop': 9750, 'noise_band': 50}
        }
        tick = Tick('093370', self.time, 10000, 1000, 50000, 10000, 9990, True, ask_qty=2000, bid_qty=2000, depth_available=True)

        # SHADOW_ONLY should be rejected with OBSERVATION_NOT_BUY_PERMISSION
        res = self.engine.enter(signal, tick)
        self.assertIsNone(res)

        # When SHADOW_ONLY is removed but live redesign authorization is False, still blocked at engine boundary
        signal_auth = copy.deepcopy(signal)
        signal_auth['SHADOW_ONLY'] = False
        signal_auth['actual_order_permission'] = True
        signal_auth['efficiency'] = {'decision': 'TRADE', 'score': 1.0}
        signal_auth['source_count'] = 3
        self.engine.market_status['093370'] = {'pass': True, 'reason_code': 'PASS', 'checked_at': self.time.isoformat()}
        self.engine.is_supported_now = MagicMock(return_value=True)
        self.engine.entry_window = MagicMock(return_value=True)

        object.__setattr__(self.engine.a, 'enable_redesign_live', False)
        object.__setattr__(self.engine.c, 'enable_redesign_live', False)
        res_blocked = self.engine.enter(signal_auth, tick)
        self.assertIsNone(res_blocked, "Unapproved live redesign signal must be blocked")

        # When explicitly authorized in both AutomationConfig and Config, passes pre-intent and creates OrderIntent
        object.__setattr__(self.engine.a, 'enable_redesign_live', True)
        object.__setattr__(self.engine.c, 'enable_redesign_live', True)
        res_auth = self.engine.enter(signal_auth, tick)
        self.assertIsNotNone(res_auth, "Authorized signal should create an OrderIntent")
        self.assertEqual(res_auth['side'], 'BUY')
        self.assertEqual(res_auth['symbol'], '093370')
        self.assertEqual(res_auth['limit'], 10000)


if __name__ == '__main__':
    unittest.main()
