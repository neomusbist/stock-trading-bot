import os
import tempfile
import unittest
from datetime import datetime, date
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.settings import Config, KST
from core.strategy import Tick
from core.util import atomic_json
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs, signal_economics
from automation.allocation import efficiency
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import DynamicRadar
from automation.runner import evaluate_actual_entry
from tests.test_automation import FakeBroker


class BreakoutFreshStructureOrderPathTests(unittest.TestCase):
    def setUp(self):
        legacy = patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED', True)
        legacy.start()
        self.addCleanup(legacy.stop)
        
        integrity_patch = patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []})
        integrity_patch.start()
        self.addCleanup(integrity_patch.stop)

        self.cfg = Config()
        object.__setattr__(self.cfg, 'live_mode', True)
        object.__setattr__(self.cfg, 'min_window_turnover', 30_000_000)
        object.__setattr__(self.cfg, 'max_positions', 3)
        object.__setattr__(self.cfg, 'stale_seconds', 10.0)
        
        self.auto = AutomationConfig(AUTO_TRADING_CAP_KRW=10_000_000)
        object.__setattr__(self.auto, 'structural_breakout_enabled', True)
        object.__setattr__(self.auto, 'continuous_observation', True)
        object.__setattr__(self.auto, 'costs_confirmed', True)
        object.__setattr__(self.auto, 'min_source_count', 1)
        object.__setattr__(self.auto, 'fallback_source1', True)
        
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)

    def test_setup_a_fresh_structure_preserves_target_and_submits_buy(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            broker = FakeBroker()
            
            t_now = datetime(2026, 10, 27, 11, 46, 7, 247000, tzinfo=KST)
            policy = Policy(folder / 'policy.json')
            policy.command('LIVE', confirmation='LIVE')

            engine = AutonomousEngine(self.cfg, self.auto, policy, folder / 'book', broker, lambda: t_now)
            snapshot = {'cash': 1000000, 'equity': 1000000, 'orders': [], 'holdings': {}}
            engine.reconcile(snapshot)
            engine.connected = True
            engine.account_ok = True
            engine.market = '3'
            engine.is_supported_now = lambda: True
            
            detector = PatternDetector(self.cfg, self.auto, self.costs)
            detector.continuous_observation = True
            
            sym = '138360'
            engine.market_status[sym] = {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'}
            
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0,
                        ask_qty=5000, bid_qty=5000,
                        received_at=t_now.isoformat(), received_monotonic=100.0)
            
            watch = {
                'code': sym,
                'name': '엔로보틱스',
                'status': 'WATCH',
                'resistance': 2090.0,  # Pre-breakout resistance broken by 2130 entry
                'source_count': 3,
                'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}
            }
            
            # Setup candidate signal for Setup A
            econ = signal_economics(self.cfg, self.auto, {
                'pattern': 'A', 'limit': 2130, 'stop': 2030, 'target': 2325,
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045}
            })
            eff = efficiency(2130, 2030, 2325, 0.8, 30, self.costs, economics=econ)
            
            gates = {
                'spread': {'result': 'PASS', 'values': {'bid': 2125.0, 'ask': 2130.0}},
                'breakout_quote': {'result': 'PASS', 'values': {'price': 2130, 'ask': 2130.0, 'entry': 2130}},
                'breakout_structure': {'result': 'PASS', 'values': {'enabled': True, 'emergency_stop': 2030, 'target_ceiling': 2325, 'proof_level': 'PASS'}},
            }
            
            base_sig = {
                'pattern': 'A',
                'symbol': sym,
                'time': t_now.isoformat(),
                'limit': 2130,
                'stop': 2030,
                'target': 2325,
                'projection': 2325,
                'breakout': 2125,
                'support': 2050,
                'entry_basis': 'TREND_BREAKOUT_BASE',
                'entry_confirmation_basis': 'ACCELERATED_BREAKOUT',
                'candidate_confirmation_basis': 'CONFIRMED_CONTINUATION',
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045, 'target_ceiling': 2325, 'proof_level': 'PASS'},
                'evaluation_gates': gates,
                'efficiency': eff,
                'signal_detected_at': t_now.isoformat(),
            }
            
            # Radar with daily bars
            radar = DynamicRadar(self.cfg, self.auto, policy)
            radar.cache = {}
            dummy_daily_bars = [
                {'datetime': '2026-10-26', 'open': 2100.0, 'high': 2325.0, 'low': 1996.0, 'close': 2040.0, 'volume': 33041209.0}
            ]
            radar.cache[(t_now.date(), sym)] = dummy_daily_bars
            
            # Mock structure() returning ELIGIBLE with overhead resistance 2205.0
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal') as mock_signal, \
                 patch('automation.engine.AutonomousEngine.entry_market_reason', return_value=None):
                
                mock_structure.return_value = {
                    'status': 'ELIGIBLE',
                    'reason': 'A/C 허용',
                    'resistance': 2205.0,
                    'overhead_share_estimate': 0.08,
                    'recent_high': 2325.0
                }
                
                # First call returns base_sig; second call (with fresh_watch) must receive fresh_watch with resistance=2090.0 (not 2205.0)
                def signal_side_effect(t, w):
                    if w.get('status') == 'ELIGIBLE':
                        # Verify that fresh_watch did NOT have resistance overwritten with 2205.0
                        self.assertEqual(w.get('resistance'), 2090.0)
                        return dict(base_sig)
                    return dict(base_sig)
                
                mock_signal.side_effect = signal_side_effect
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                
                self.assertIsNotNone(order, "Order should be generated")
                self.assertEqual(order['symbol'], sym)
                self.assertEqual(order['side'], 'BUY')
                self.assertEqual(order['limit'], 2130)
                self.assertGreater(order['qty'], 0)
                self.assertIn(order['local'], engine.s['orders'])

    def test_setup_a_fresh_structure_rejected_emits_structure_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            broker = FakeBroker()
            
            t_now = datetime(2026, 10, 27, 11, 46, 7, 247000, tzinfo=KST)
            policy = Policy(folder / 'policy.json')
            policy.command('LIVE', confirmation='LIVE')

            engine = AutonomousEngine(self.cfg, self.auto, policy, folder / 'book', broker, lambda: t_now)
            snapshot = {'cash': 1000000, 'equity': 1000000, 'orders': [], 'holdings': {}}
            engine.reconcile(snapshot)
            engine.connected = True
            engine.account_ok = True
            
            detector = PatternDetector(self.cfg, self.auto, self.costs)
            sym = '138360'
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0,
                        ask_qty=5000, bid_qty=5000, received_at=t_now.isoformat())
            watch = {'code': sym, 'name': '엔로보틱스', 'status': 'WATCH', 'resistance': 2090.0}
            base_sig = {'pattern': 'A', 'symbol': sym, 'time': t_now.isoformat(), 'limit': 2130, 'stop': 2030, 'target': 2325}
            
            radar = DynamicRadar(self.cfg, self.auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal', return_value=base_sig):
                
                # Mock structure returning REJECT
                mock_structure.return_value = {
                    'status': 'REJECT',
                    'reason': 'OVERHEAD_TOO_HEAVY',
                    'overhead_share_estimate': 0.45
                }
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                
                self.assertIsNone(order)
                self.assertEqual(len(broker.orders), 0)


if __name__ == '__main__':
    unittest.main()
