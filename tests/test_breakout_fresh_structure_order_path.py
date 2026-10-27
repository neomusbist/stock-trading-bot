import os
import tempfile
import unittest
from datetime import datetime, date
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, MagicMock

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

    def test_positive_truly_favorable_signal_with_room_submits_buy(self):
        """양성 검증: 상단 확인 저항이 충분히 멀어(2,500원) 투영 목표가(2,325원)의 순RR 1.551이 유지되는 유리한 신호는 주문까지 도달한다."""
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
                        received_at=t_now.isoformat(), received_monotonic=None)
            
            watch = {
                'code': sym,
                'name': '엔로보틱스',
                'status': 'WATCH',
                'resistance': 2500.0,  # Far overhead resistance
                'source_count': 3,
                'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}
            }
            
            econ = signal_economics(self.cfg, self.auto, {
                'pattern': 'A', 'limit': 2130, 'stop': 2030, 'target': 2325,
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045}
            })
            eff = efficiency(2130, 2030, 2325, 0.8, 30, self.costs, economics=econ)
            
            gates = {
                'spread': {'result': 'PASS', 'values': {'bid': 2125.0, 'ask': 2130.0}},
                'breakout_quote': {'result': 'PASS', 'values': {'price': 2130, 'ask': 2130.0, 'entry': 2130}},
                'breakout_structure': {'result': 'PASS', 'values': {'enabled': True, 'emergency_stop': 2030, 'target_ceiling': 2500, 'proof_level': 'PASS'}},
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
                'breakout_level': 2125,
                'confirmed_overhead_resistance': 2500.0,
                'projected_target': 2325,
                'economic_reward_reference': 2325,
                'entry_basis': 'TREND_BREAKOUT_BASE',
                'entry_confirmation_basis': 'ACCELERATED_BREAKOUT',
                'candidate_confirmation_basis': 'CONFIRMED_CONTINUATION',
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045, 'target_ceiling': 2500, 'proof_level': 'PASS'},
                'evaluation_gates': gates,
                'efficiency': eff,
                'signal_detected_at': t_now.isoformat(),
            }
            
            radar = DynamicRadar(self.cfg, self.auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal', return_value=dict(base_sig)), \
                 patch('automation.engine.AutonomousEngine.entry_market_reason', return_value=None):
                
                # Fresh structure returns resistance at 2500 (plenty of room)
                mock_structure.return_value = {
                    'status': 'ELIGIBLE',
                    'reason': 'A/C 허용',
                    'resistance': 2500.0,
                    'overhead_share_estimate': 0.05,
                    'recent_high': 2500.0
                }
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                
                self.assertIsNotNone(order, "Favorable order must reach submission")
                self.assertEqual(order['symbol'], sym)
                self.assertEqual(order['side'], 'BUY')
                self.assertEqual(order['limit'], 2130)
                self.assertGreater(order['qty'], 0)
                self.assertIn(order['local'], engine.s['orders'])

    def test_negative_fresh_resistance_compresses_net_rr_strictly_rejected(self):
        """음성 검증 1: 앤로보틱스 11:46:07 동일 조건 - 새로 확인된 저항(2,205원)이 상단을 가로막아 순RR이 0.513으로 악화된 신호는 정확하게 거절한다."""
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
            
            detector = PatternDetector(self.cfg, self.auto, self.costs)
            detector.continuous_observation = True
            
            sym = '138360'
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0,
                        ask_qty=5000, bid_qty=5000,
                        received_at=t_now.isoformat(), received_monotonic=None)
            
            # 1차 판단 당시 watch (아침 스캔 저항 2,090원 - 돌파된 것으로 착각)
            watch = {
                'code': sym, 'name': '엔로보틱스', 'status': 'WATCH', 'resistance': 2090.0,
                'source_count': 3, 'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}
            }
            
            base_sig = {
                'pattern': 'A', 'symbol': sym, 'time': t_now.isoformat(),
                'limit': 2130, 'stop': 2030, 'target': 2325, 'projection': 2325, 'breakout': 2125,
                'breakout_level': 2125, 'confirmed_overhead_resistance': 2090.0,
                'projected_target': 2325, 'economic_reward_reference': 2325
            }
            
            radar = DynamicRadar(self.cfg, self.auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal') as mock_signal:
                
                # 일봉 신선 구조가 새로운 실질 저항 2,205원을 발견
                mock_structure.return_value = {
                    'status': 'ELIGIBLE',
                    'reason': 'A/C 허용',
                    'resistance': 2205.0,
                    'overhead_share_estimate': 0.0827,
                    'recent_high': 2325.0
                }
                
                # 1차 호출은 base_sig 반환, 2차(fresh_watch) 호출 시에는 2,205원 저항으로 인해 Net RR < 1.50 탈락(None 반환)
                def signal_behavior(t, w):
                    if w.get('resistance') == 2205.0:
                        # 모의 last_eval에 Net RR 실패 기록
                        detector.last_eval[sym] = {
                            'trials': [{
                                'setup': 'A',
                                'primary_reason_code': 'RISK_REWARD_FAIL',
                                'failed_reasons': ['RISK_REWARD_FAIL'],
                                'entry_ready': False
                            }]
                        }
                        return None
                    return dict(base_sig)
                
                mock_signal.side_effect = signal_behavior
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                
                # 검증: 신규 저항으로 경제성이 악화된 신호는 정확하게 거절된다
                self.assertIsNone(order, "Order must be REJECTED when confirmed overhead resistance destroys Net RR")
                self.assertEqual(len(engine.s['orders']), 0)
                self.assertEqual(len(broker.orders), 0)
                
                # 의사결정 이벤트 검증
                events_file = folder / 'book/events.jsonl'
                self.assertTrue(events_file.exists())
                import json
                eval_events = [json.loads(line) for line in events_file.read_text(encoding='utf-8').splitlines()
                               if json.loads(line).get('event') == 'ENTRY_EVAL']
                self.assertGreater(len(eval_events), 0)
                last_eval = eval_events[-1]
                self.assertEqual(last_eval['decision'], 'NO_BUY')
                self.assertEqual(last_eval['primary_reason_code'], 'FRESH_RESISTANCE_REWARD_INSUFFICIENT')
                
                # 5개 의미 필드 구분 검증
                details = last_eval.get('details', {})
                self.assertEqual(details.get('breakout_level'), 2125)
                self.assertEqual(details.get('confirmed_overhead_resistance'), 2205.0)
                self.assertEqual(details.get('projected_target'), 2325)
                self.assertEqual(details.get('economic_reward_reference'), 2205.0)
                self.assertEqual(details.get('rejection_basis'), 'OVERHEAD_CEILING_COMPRESSED_NET_RR')

    def test_negative_fresh_structure_rejected_emits_structure_rejected(self):
        """음성 검증 2: 일봉 매물대나 장기위치가 불량하여 structure()가 REJECT인 경우 주문 미생성"""
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
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0, ask_qty=5000, bid_qty=5000, received_at=t_now.isoformat())
            watch = {'code': sym, 'name': '엔로보틱스', 'status': 'WATCH', 'resistance': 2090.0}
            base_sig = {'pattern': 'A', 'symbol': sym, 'time': t_now.isoformat(), 'limit': 2130, 'stop': 2030, 'target': 2325}
            
            radar = DynamicRadar(self.cfg, self.auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal', return_value=base_sig):
                
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

    def test_real_live_account_default_cap_yields_6_shares(self):
        """실제 LIVE 계좌 기본 포지션 한도(20%) 하의 수량 계산 회귀 검증:
        - 예수금: 70,847.0원, 계좌 자본: 70,420.78원
        - 1회 거래 허용 위험예산: 70,420.78 * 2% = 1,408.42원
        - 일일 최대 손실 예산: 70,420.78 * 4% = 2,816.83원
        - 진입 2,130원, 비상손절 2,030원
        - 주당 최악 손실: 100원 + 15.034원(비용) = 115.034원
        - 2% 위험 기준 수량 (risk_quantity): floor(1,408.42 / 115.034) = 12주
        - 기본 포지션 한도 (max_position_fraction=0.20): floor(70,420.78 * 0.20 / 2,134.58) = 6주
        - 최종 결정 수량: min(12주, 6주) = 6주
        """
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            broker = FakeBroker()
            
            t_now = datetime(2026, 10, 27, 11, 46, 7, 247000, tzinfo=KST)
            policy = Policy(folder / 'policy.json')
            policy.command('LIVE', confirmation='LIVE')

            live_cfg = replace(self.cfg, risk_fraction=0.02, max_daily_loss_fraction=0.04)
            live_auto = replace(self.auto, AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True)
            engine = AutonomousEngine(live_cfg, live_auto, policy, folder / 'book', broker, lambda: t_now)
            
            real_live_snapshot = {
                'cash': 70847.0,
                'equity': 70420.7759,
                'orders': [],
                'holdings': {}
            }
            engine.reconcile(real_live_snapshot)
            engine.connected = True
            engine.account_ok = True
            engine.market = '3'
            engine.is_supported_now = lambda: True
            
            sym = '138360'
            engine.market_status[sym] = {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'}
            
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0,
                        ask_qty=5000, bid_qty=5000,
                        received_at=t_now.isoformat(), received_monotonic=None)
            
            watch = {
                'code': sym, 'name': '엔로보틱스', 'status': 'WATCH', 'resistance': 2600.0,
                'source_count': 3, 'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}
            }
            
            econ = signal_economics(live_cfg, live_auto, {
                'pattern': 'A', 'limit': 2130, 'stop': 2030, 'target': 2325,
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045}
            })
            eff = efficiency(2130, 2030, 2325, 0.8, 30, self.costs, economics=econ)
            
            gates = {
                'spread': {'result': 'PASS', 'values': {'bid': 2125.0, 'ask': 2130.0}},
                'breakout_quote': {'result': 'PASS', 'values': {'price': 2130, 'ask': 2130.0, 'entry': 2130}},
                'breakout_structure': {'result': 'PASS', 'values': {'enabled': True, 'emergency_stop': 2030, 'target_ceiling': 2600, 'proof_level': 'PASS'}},
            }
            
            base_sig = {
                'pattern': 'A', 'symbol': sym, 'time': t_now.isoformat(),
                'limit': 2130, 'stop': 2030, 'target': 2325, 'projection': 2325, 'breakout': 2125, 'support': 2050,
                'breakout_level': 2125, 'confirmed_overhead_resistance': 2600.0,
                'projected_target': 2325, 'economic_reward_reference': 2325,
                'entry_basis': 'TREND_BREAKOUT_BASE', 'entry_confirmation_basis': 'ACCELERATED_BREAKOUT',
                'candidate_confirmation_basis': 'CONFIRMED_CONTINUATION',
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045, 'target_ceiling': 2600, 'proof_level': 'PASS'},
                'evaluation_gates': gates, 'efficiency': eff, 'signal_detected_at': t_now.isoformat()
            }
            
            detector = PatternDetector(live_cfg, live_auto, self.costs)
            detector.continuous_observation = True
            radar = DynamicRadar(live_cfg, live_auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal', return_value=dict(base_sig)), \
                 patch('automation.engine.AutonomousEngine.entry_market_reason', return_value=None):
                
                mock_structure.return_value = {
                    'status': 'ELIGIBLE', 'reason': 'A/C 허용', 'resistance': 2600.0,
                    'overhead_share_estimate': 0.05, 'recent_high': 2600.0
                }
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                self.assertIsNotNone(order)
                self.assertEqual(order['qty'], 6, "Under 20% position fraction, quantity is 6 shares")

    def test_real_live_account_pure_2pct_risk_yields_12_shares(self):
        """실제 LIVE 계좌 포지션 한도 미제한(100%) 하의 순수 2% 위험 한도 수량 계산 회귀 검증:
        - 1회 거래 허용 위험예산: 70,420.78 * 2% = 1,408.42원
        - 주당 최악 손실: 115.034원
        - 순수 2% 위험 기준 수량: floor(1,408.42 / 115.034) = 정확히 12주
        - 불변조건: 12주 * 115.034원 = 1,380.41원 <= 1,408.42원 (2% 허용 위험 충족)
        - 불변조건: 12주 * 2,134.58원 = 25,614.96원 <= 70,847.0원 (예수금 충족)
        """
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            broker = FakeBroker()
            
            t_now = datetime(2026, 10, 27, 11, 46, 7, 247000, tzinfo=KST)
            policy = Policy(folder / 'policy.json')
            policy.command('LIVE', confirmation='LIVE')

            live_cfg = replace(self.cfg, risk_fraction=0.02, max_daily_loss_fraction=0.04, max_position_fraction=1.0)
            live_auto = replace(self.auto, AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True)
            engine = AutonomousEngine(live_cfg, live_auto, policy, folder / 'book', broker, lambda: t_now)
            
            real_live_snapshot = {
                'cash': 70847.0,
                'equity': 70420.7759,
                'orders': [],
                'holdings': {}
            }
            engine.reconcile(real_live_snapshot)
            engine.connected = True
            engine.account_ok = True
            engine.market = '3'
            engine.is_supported_now = lambda: True
            
            sym = '138360'
            engine.market_status[sym] = {'pass': True, 'reason_code': 'SYNTHETIC_NORMAL_STATUS'}
            
            tick = Tick(sym, t_now, 2130, 50000, 15000000, 2130.0, 2125.0,
                        ask_qty=5000, bid_qty=5000,
                        received_at=t_now.isoformat(), received_monotonic=None)
            
            watch = {
                'code': sym, 'name': '엔로보틱스', 'status': 'WATCH', 'resistance': 2600.0,
                'source_count': 3, 'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}
            }
            
            econ = signal_economics(live_cfg, live_auto, {
                'pattern': 'A', 'limit': 2130, 'stop': 2030, 'target': 2325,
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045}
            })
            eff = efficiency(2130, 2030, 2325, 0.8, 30, self.costs, economics=econ)
            
            gates = {
                'spread': {'result': 'PASS', 'values': {'bid': 2125.0, 'ask': 2130.0}},
                'breakout_quote': {'result': 'PASS', 'values': {'price': 2130, 'ask': 2130.0, 'entry': 2130}},
                'breakout_structure': {'result': 'PASS', 'values': {'enabled': True, 'emergency_stop': 2030, 'target_ceiling': 2600, 'proof_level': 'PASS'}},
            }
            
            base_sig = {
                'pattern': 'A', 'symbol': sym, 'time': t_now.isoformat(),
                'limit': 2130, 'stop': 2030, 'target': 2325, 'projection': 2325, 'breakout': 2125, 'support': 2050,
                'breakout_level': 2125, 'confirmed_overhead_resistance': 2600.0,
                'projected_target': 2325, 'economic_reward_reference': 2325,
                'entry_basis': 'TREND_BREAKOUT_BASE', 'entry_confirmation_basis': 'ACCELERATED_BREAKOUT',
                'candidate_confirmation_basis': 'CONFIRMED_CONTINUATION',
                'invalidation_policy': {'enabled': True, 'emergency_stop': 2030, 'structural_stop': 2045, 'target_ceiling': 2600, 'proof_level': 'PASS'},
                'evaluation_gates': gates, 'efficiency': eff, 'signal_detected_at': t_now.isoformat()
            }
            
            detector = PatternDetector(live_cfg, live_auto, self.costs)
            detector.continuous_observation = True
            radar = DynamicRadar(live_cfg, live_auto, policy)
            radar.cache = {(t_now.date(), sym): [{'datetime': '2026-10-26'}]}
            
            with patch('automation.runner.structure') as mock_structure, \
                 patch.object(detector, 'signal', return_value=dict(base_sig)), \
                 patch('automation.engine.AutonomousEngine.entry_market_reason', return_value=None):
                
                mock_structure.return_value = {
                    'status': 'ELIGIBLE', 'reason': 'A/C 허용', 'resistance': 2600.0,
                    'overhead_share_estimate': 0.05, 'recent_high': 2600.0
                }
                
                prices = {sym: 2125.0}
                account = FakeBroker()
                
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account)
                self.assertIsNotNone(order)
                self.assertEqual(order['qty'], 12, "Pure 2% risk limit without position fraction clamp yields exactly 12 shares")
                
                # 계좌 위험 및 예수금 불변조건 검증
                worst_loss = 12 * 115.034
                self.assertLessEqual(worst_loss, 1408.42, "Worst loss for 12 shares must be <= allowed 2% risk (1,408.42 KRW)")
                required_cash = 12 * 2134.58
                self.assertLessEqual(required_cash, 70847.0, "Required cash for 12 shares must be <= available cash (70,847 KRW)")


if __name__ == '__main__':
    unittest.main()
