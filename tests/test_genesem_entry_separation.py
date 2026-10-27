"""Empirical test and causal verification for Genesem (217190) 09:43 case.
Verifies separation of concerns:
1. Price Structure Candidate Generation
2. Trigger Satisfaction
3. Transaction Costs & Net RR Economics
4. Invalidation, Emergency Stop, and Sizing
5. Final Order Eligibility
6. Target Capping Evaluation (Prior Peak H vs Trend Expansion)
"""
import unittest
from datetime import datetime
from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.capital import signal_economics
from core.util import tick_size

class GenesemCausalSeparationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = {
            'buy': 0.00015,
            'sell': 0.00015,
            'tax': 0.002,
            'entry_slippage': 0.002,
            'exit_slippage': 0.003
        }

    def test_reproduce_exact_0943_genesem_values(self):
        """09:43:06 제너셈(217190)의 실제 입력값을 정확히 재현하고 단계별 탈락 원인을 분리 검증."""
        # 1. 입력값 (09:43:06 ENTRY_EVAL 로그와 100% 일치)
        entry_ask = 8370
        bid = 8330
        breakout = 8340
        structural_stop = 8280
        emergency_stop = 8240
        target = 8400.0  # nearest_confirmed_peak (09:37 형성)
        window_turnover = 2045970.0  # 204.5만 원
        gain_5s = 0.00119617  # 0.12%

        # 2. 호가 스프레드 검증
        spread = (entry_ask - bid) / entry_ask
        self.assertAlmostEqual(spread, 0.00477897, places=5)
        spread_pass = spread <= self.cfg.max_spread  # 0.40%
        self.assertFalse(spread_pass, "Spread 0.478% must FAIL threshold 0.40%")

        # 3. 유동성 검증
        liq_pass = window_turnover >= self.cfg.min_window_turnover  # 30,000,000
        self.assertFalse(liq_pass, "Turnover 2.04M must FAIL 30M threshold")

        # 4. 5초 가속도 검증
        accel_pass = gain_5s >= self.cfg.min_price_gain  # 0.30%
        self.assertFalse(accel_pass, "5s gain 0.12% must FAIL 0.30% threshold")

        # 5. 가격 구조 및 돌파 트리거 검증 (돌파 자체는 충족)
        breakout_pass = entry_ask >= breakout
        self.assertTrue(breakout_pass, "Breakout price 8370 >= 8340 PASS")
        chase = (entry_ask / breakout) - 1
        self.assertLess(chase, self.cfg.max_chase, "Chase 0.36% <= 0.8% PASS")

        # 6. 경제성 및 순손익비(Net RR) 검증
        val_signal = {
            'pattern': 'A',
            'limit': entry_ask,
            'stop': emergency_stop,
            'target': target,
            'invalidation_policy': {
                'enabled': True,
                'structural_stop': structural_stop,
                'emergency_stop': emergency_stop,
                'target_ceiling': target
            }
        }
        econ = signal_economics(self.cfg, self.auto, val_signal)

        # 비용 상세 재현
        gross_reward = target - entry_ask  # 8400 - 8370 = 30 KRW
        self.assertEqual(gross_reward, 30.0)

        # 1주당 수수료 + 세금 + 슬리피지 = 61.26 KRW
        cost_per_share = econ['cost_per_share']
        self.assertAlmostEqual(cost_per_share, 61.2555, places=4)

        # 비용 차감 후 순보상 = -31.26 KRW (음수!)
        reward_after_cost = econ['reward_after_cost']
        self.assertAlmostEqual(reward_after_cost, -31.2555, places=4)
        self.assertLess(reward_after_cost, 0, "Reward after cost is NEGATIVE!")

        # 순손익비 = -0.164 (기준치 1.5 미달)
        net_rr = econ['net_rr']
        self.assertAlmostEqual(net_rr, -0.1641, places=3)
        self.assertFalse(econ['rr_pass'], "Net RR < 1.5 must FAIL")

    def test_target_model_comparison_prior_peak_vs_trend_expansion(self):
        """[연구 비교] 최근 고점(H) 캡핑 vs 추세 확장 목표가(H + 1.5*ATR / Measured Move) 비교.
        LIVE 규칙을 변경하지 않고, 추세 추종 전략 관점에서의 효과와 한계를 실증 비교.
        """
        entry_ask = 8370
        breakout = 8340
        structural_stop = 8280
        emergency_stop = 8240
        box_width = breakout - structural_stop  # 60 KRW
        atr = 100.0  # 1분봉 변동폭 ~100 KRW

        # 모델 1 (현재 LIVE): 최근 고점 8400에 캡핑
        target_live = 8400.0
        econ_live = signal_economics(self.cfg, self.auto, {
            'pattern': 'A', 'limit': entry_ask, 'stop': emergency_stop, 'target': target_live,
            'invalidation_policy': {'enabled': True, 'structural_stop': structural_stop, 'emergency_stop': emergency_stop}
        })

        # 모델 2 (추세 확장): 돌파 후 변동폭 확장 목표가 (8,400 + 1.5*ATR = 8,550)
        target_expanded = 8550.0
        econ_expanded = signal_economics(self.cfg, self.auto, {
            'pattern': 'A', 'limit': entry_ask, 'stop': emergency_stop, 'target': target_expanded,
            'invalidation_policy': {'enabled': True, 'structural_stop': structural_stop, 'emergency_stop': emergency_stop}
        })

        # 비교 결과
        # LIVE 모델: 순보상 -31.26원 (진입 시 마이너스 확정)
        self.assertLess(econ_live['reward_after_cost'], 0)
        self.assertLess(econ_live['net_rr'], 0)

        # 확장 모델: 순보상 +117.8원 (양의 순보상 회복)
        self.assertGreater(econ_expanded['reward_after_cost'], 0)
        self.assertGreater(econ_expanded['net_rr'], 0.5)

        # 그러나 제너셈의 본질적 결함:
        # 목표가를 아무리 확장하더라도 제너셈(217190)은 호가 스프레드(0.478% > 0.40%)와
        # 거래대금(204만 원 < 2000만 원)으로 인해 실제 진입은 정당하게 차단되어야 함!
        self.assertFalse(econ_expanded['net_rr'] >= 1.5, "At 8370 chase, Net RR 0.61 still below 1.5 threshold")


if __name__ == '__main__':
    unittest.main()
