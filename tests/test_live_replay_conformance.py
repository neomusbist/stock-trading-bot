import unittest
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import math
from decimal import Decimal

from automation.capital import Costs, entry_economics, entry_quantity

KST = timezone(timedelta(hours=9))

class LiveReplayConformanceAuditor:
    """Audits step-by-step conformance between LIVE execution pipeline and Replay pipeline.
    
    If LIVE and Replay diverge at any step for identical input data, this auditor
    records the exact first divergence step, symbol, timestamp, and root cause.
    """
    
    STAGES = [
        '1_TICK_FRESHNESS',
        '2_GATE_WARMUP',
        '3_GATE_LIQUIDITY',
        '4_GATE_SPREAD',
        '5_GATE_STRUCTURE',
        '6_GATE_BREAKOUT',
        '7_GATE_RISK_REWARD',
        '8_STAGE2_STRUCTURE_REFRESH',
        '9_SESSION_WINDOW',
        '10_SIZING_CAPITAL_AND_RISK',
        '11_PORTFOLIO_CONCURRENCY',
        '12_FINAL_DECISION'
    ]
    
    def __init__(self):
        self.divergences: List[Dict[str, Any]] = []
        self.first_divergence: Optional[Dict[str, Any]] = None

    def record_step(self, symbol: str, timestamp: str, stage: str,
                    live_eval: Dict[str, Any], replay_eval: Dict[str, Any]) -> bool:
        """Compare LIVE and Replay for a single decision stage.
        Returns True if conformant, False if diverged.
        """
        assert stage in self.STAGES, f"Unknown stage: {stage}"
        
        live_result = live_eval.get('result')
        replay_result = replay_eval.get('result')
        live_reason = live_eval.get('reason')
        replay_reason = replay_eval.get('reason')
        
        match = (live_result == replay_result)
        if not match:
            record = {
                'timestamp': timestamp,
                'symbol': symbol,
                'stage': stage,
                'live_result': live_result,
                'replay_result': replay_result,
                'live_reason': live_reason,
                'replay_reason': replay_reason,
                'live_extra': live_eval.get('extra', {}),
                'replay_extra': replay_eval.get('extra', {}),
                'divergence_explanation': (
                    f"Divergence at [{stage}] for {symbol} at {timestamp}: "
                    f"LIVE={live_result} ({live_reason}) vs REPLAY={replay_result} ({replay_reason})"
                )
            }
            self.divergences.append(record)
            if self.first_divergence is None:
                self.first_divergence = record
            return False
        return True


class TestLiveReplayConformance(unittest.TestCase):
    
    def setUp(self):
        self.auditor = LiveReplayConformanceAuditor()
        self.costs = Costs(buy=0.00015, sell=0.00015, tax=0.0020, entry_slippage=0.0020, exit_slippage=0.0030)

    def test_conformance_on_enrobotics_actual_signal(self):
        """11:46:07 앤로보틱스: LIVE와 Replay 모두 2차 저항선 확인에서 FRESH_STRUCTURE_SIGNAL_NOT_READY로 일치해야 함."""
        timestamp = '2026-10-27T11:46:07+09:00'
        sym = '138360'
        
        for stage in LiveReplayConformanceAuditor.STAGES[:7]:
            ok = self.auditor.record_step(sym, timestamp, stage,
                                          {'result': 'PASS', 'reason': None},
                                          {'result': 'PASS', 'reason': None})
            self.assertTrue(ok)
            
        live_stage8 = {
            'result': 'FAIL',
            'reason': 'FRESH_STRUCTURE_SIGNAL_NOT_READY',
            'extra': {'confirmed_resistance': 2205, 'target': 2205, 'net_rr': 0.55}
        }
        replay_stage8 = {
            'result': 'FAIL',
            'reason': 'FRESH_STRUCTURE_SIGNAL_NOT_READY',
            'extra': {'confirmed_resistance': 2205, 'target': 2205, 'net_rr': 0.55}
        }
        ok = self.auditor.record_step(sym, timestamp, '8_STAGE2_STRUCTURE_REFRESH', live_stage8, replay_stage8)
        self.assertTrue(ok)
        self.assertIsNone(self.auditor.first_divergence)

    def test_divergence_caught_when_replay_ignores_fresh_resistance(self):
        """재생기가 2차 저항선을 무시하고 통과시킬 경우, 최초 분기 단계와 이유가 정확히 기록되어야 함."""
        timestamp = '2026-10-27T11:46:07+09:00'
        sym = '138360'
        
        live_stage8 = {
            'result': 'FAIL',
            'reason': 'FRESH_STRUCTURE_SIGNAL_NOT_READY',
            'extra': {'confirmed_resistance': 2205, 'target': 2205, 'net_rr': 0.55}
        }
        naive_replay_stage8 = {
            'result': 'PASS',
            'reason': None,
            'extra': {'target': 2325, 'net_rr': 1.83}
        }
        
        ok = self.auditor.record_step(sym, timestamp, '8_STAGE2_STRUCTURE_REFRESH', live_stage8, naive_replay_stage8)
        self.assertFalse(ok)
        self.assertIsNotNone(self.auditor.first_divergence)
        self.assertEqual(self.auditor.first_divergence['stage'], '8_STAGE2_STRUCTURE_REFRESH')
        self.assertEqual(self.auditor.first_divergence['live_result'], 'FAIL')
        self.assertEqual(self.auditor.first_divergence['replay_result'], 'PASS')

    def test_divergence_caught_on_position_cap_mismatch(self):
        """재생기가 20% 포지션 캡(14,084원)을 적용하는데 LIVE는 100% 캡(70,420원)을 적용하면 Sizing 단계에서 분기 감지."""
        timestamp = '2026-10-27T10:15:51+09:00'
        sym = '253590'
        
        live_sizing = {
            'result': 'PASS',
            'reason': 'QUANTITY_AVAILABLE',
            'extra': {'quantity': 3, 'position_cap_krw': 70420.0}
        }
        replay_20pct_sizing = {
            'result': 'FAIL',
            'reason': 'ONE_SHARE_UNAFFORDABLE',
            'extra': {'quantity': 0, 'position_cap_krw': 14084.0}
        }
        
        ok = self.auditor.record_step(sym, timestamp, '10_SIZING_CAPITAL_AND_RISK', live_sizing, replay_20pct_sizing)
        self.assertFalse(ok)
        self.assertEqual(self.auditor.first_divergence['stage'], '10_SIZING_CAPITAL_AND_RISK')

    def test_divergence_caught_on_session_cutoff_mismatch(self):
        """14:00 vs 14:55 시간 제한 불일치 시 9_SESSION_WINDOW 단계에서 최초 분기 자동 기록."""
        timestamp = '2026-10-27T14:15:00+09:00'
        sym = '138360'
        
        live_session = {'result': 'PASS', 'reason': None, 'extra': {'cutoff': '14:55:00'}}
        legacy_session = {'result': 'FAIL', 'reason': 'NEW_ENTRY_CUTOFF', 'extra': {'cutoff': '14:00:00'}}
        
        ok = self.auditor.record_step(sym, timestamp, '9_SESSION_WINDOW', live_session, legacy_session)
        self.assertFalse(ok)
        self.assertEqual(self.auditor.first_divergence['stage'], '9_SESSION_WINDOW')

    def test_enrobotics_quantity_and_net_rr_regression(self):
        """앤로보틱스 2,200원 주문 수량 및 Net RR 계산의 정밀 재현 회귀시험."""
        entry = 2200.0
        stop = 2178.0
        target = 2255.0
        capital = 70420.0
        risk_budget = 70420.0 * 0.02
        
        # 1. Economics
        econ = entry_economics(entry, stop, stop, target, self.costs, 1.5)
        self.assertAlmostEqual(econ['gross_rr'], 2.50, places=2)
        self.assertAlmostEqual(econ['net_rr'], 1.0187, places=3)
        self.assertFalse(econ['rr_pass'])
        
        # 2. Broker Pure Cash Quantity (without slippage reserve): 32 shares
        broker_one_share = entry * (1 + self.costs.buy)
        broker_qty = int(capital // broker_one_share)
        self.assertEqual(broker_qty, 32)
        total_broker_cash = broker_qty * broker_one_share
        self.assertLessEqual(total_broker_cash, capital)
        self.assertAlmostEqual(total_broker_cash, 70410.56, places=2)
        
        # 3. Conservative Sizing with Pre-trade Slippage Reserve: 31 shares
        sized = entry_quantity(econ, capital, risk_budget)
        self.assertEqual(sized['capital_quantity'], 31)
        self.assertEqual(sized['risk_quantity'], 37)
        self.assertEqual(sized['quantity'], 31)

    def test_setup_d_strict_net_rr_rejection(self):
        """Setup D에서 Gross RR=2.50이어도 Net RR < 1.5인 신호는 동일한 운영규칙에서 전면 탈락해야 함."""
        econ_enro = entry_economics(2200.0, 2178.0, 2178.0, 2255.0, self.costs, 1.5)
        self.assertFalse(econ_enro['rr_pass'])
        
        econ_neo = entry_economics(17680.0, 17487.0, 17487.0, 18162.0, self.costs, 1.5)
        self.assertFalse(econ_neo['rr_pass'])
        
        econ_sands = entry_economics(7590.0, 7479.0, 7479.0, 7867.0, self.costs, 1.5)
        self.assertFalse(econ_sands['rr_pass'])


if __name__ == '__main__':
    unittest.main()
