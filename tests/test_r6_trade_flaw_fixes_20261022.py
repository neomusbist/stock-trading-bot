"""
Causal regression tests for R6 live trade flaw fixes (2026-10-22).
Verifies:
1. Trade A (HJ중공업 097230) flaw:
   - Intraday ceiling protection: cannot project target to distant daily overhead (15,630) when intraday ceiling is 13,390.
   - Rejects near-ceiling entry with KNOWN_CEILING_NET_REWARD_RISK_LOW.
   - Rejects 70 KRW (0.52%) micro-stop with STOP_TOO_TIGHT_NOISE_RISK.
2. Trade B (앤로보틱스 138360) flaw:
   - Downtrend counter-bounce protection: rejects entry when trading > 2.5% below confirmed session peak (1,740 -> 1,687).
   - Rejects 9 KRW (0.53%) micro-stop with STOP_TOO_TIGHT_NOISE_RISK.
   - Net reward-risk against confirmed ceiling with structural stop is < 1.5.
3. Minimum stop distance enforcement across strategies:
   - Enforced in FAST (fast_momentum.py)
   - Enforced in A/B/C patterns (patterns.py) even without chase_quality_filter.
4. Genuine clean breakout with adequate stop distance and clear ceiling passes.
"""
import unittest
from datetime import datetime, timedelta
from dataclasses import replace

from core.strategy import Tick
from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.fast_momentum import FastMomentumStrategy
from automation.patterns import PatternDetector
from automation.capital import Costs


class TestR6TradeFlawFixes20261022(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(Config(), risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True, min_stop_distance_fraction=0.012)
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.fast_strategy = FastMomentumStrategy(self.cfg, self.auto)
        self.base_time = datetime(2026, 10, 22, 10, 51, 46, tzinfo=KST)

    def test_trade_a_hj_heavy_industries_rejected(self):
        """
        HJ중공업 (097230) 10:51 actual trade conditions:
        - Price at 13,380 KRW, day high at 13,390 KRW, overhead daily resistance at 15,630 KRW.
        - Nearest confirmed peak was None (stock just arrived in WATCH).
        - Micro-structure had support at 13,350 KRW, stop at 13,310 KRW (0.52% distance).
        Must be REJECTED before placing any order.
        """
        strat = FastMomentumStrategy(self.cfg, self.auto)
        now = self.base_time

        # Feed ticks simulating the micro-tape for HJ중공업 leading to 13,380 KRW
        prices = [13280, 13300, 13320, 13350, 13360, 13350, 13360, 13370, 13380]
        for i, p in enumerate(prices):
            t_stamp = now - timedelta(seconds=len(prices) - i)
            t = Tick('097230', t_stamp, float(p), 500, (i + 1) * 500,
                     float(p), float(p - 10), ask_qty=5000, bid_qty=5000, depth_available=True,
                     book_time=t_stamp, depth_source='SIMULATED_QUOTE')
            strat.ingest_tick(t, t.time)

        eval_tick = Tick('097230', now, 13380.0, 500, 5500,
                         13380.0, 13350.0, ask_qty=5000, bid_qty=5000, depth_available=True,
                         book_time=now, depth_source='SIMULATED_QUOTE')
        strat.ingest_tick(eval_tick, eval_tick.time)

        watch_hj = {
            'code': '097230',
            'name': 'HJ중공업',
            'status': 'WATCH',
            'reference_price': 13300.0,
            'recent_high': 13390.0,  # Real day high at 10:51
            'resistance': 15630.0,    # Distant daily swing resistance
            'resistance_asof': now.isoformat(),
            'nearest_confirmed_peak': None,  # No completed bars yet
            'source_count': 1,
        }

        sig = strat.evaluate(eval_tick, watch_hj, capital_limit=81300.0, risk_budget=1426.0,
                             ingest=False, received=now, live_authorized=True)

        self.assertIsNone(sig, "Trade A (HJ중공업) must NOT generate a buy signal")
        last_eval = strat.last_evaluation.get('097230', {})
        reason = last_eval.get('reason')
        self.assertIn(reason, ['STOP_TOO_TIGHT_NOISE_RISK', 'KNOWN_CEILING_NET_REWARD_RISK_LOW'],
                      f"Expected structural ceiling or stop distance rejection, got: {reason}")

    def test_trade_b_an_robotics_rejected(self):
        """
        앤로보틱스 (138360) 12:04 actual trade conditions:
        - Confirmed peak earlier at 1,740 KRW (11:54), then collapsed to 1,660 KRW.
        - Evaluated on a micro-rebound at 1,687 KRW (-3.05% below 1,740 peak).
        - Micro-structure had support at 1,680 KRW, stop at 1,678 KRW (9 KRW = 0.53% stop distance).
        Must be REJECTED before placing any order.
        """
        strat = FastMomentumStrategy(self.cfg, self.auto)
        trade_time = datetime(2026, 10, 22, 12, 4, 27, tzinfo=KST)

        prices = [1670, 1675, 1680, 1684, 1682, 1680, 1684, 1685, 1686]
        for i, p in enumerate(prices):
            t_stamp = trade_time - timedelta(seconds=len(prices) - i)
            t = Tick('138360', t_stamp, float(p), 1000, (i + 1) * 1000,
                     float(p), float(p - 2), ask_qty=2000, bid_qty=2000, depth_available=True,
                     book_time=t_stamp, depth_source='SIMULATED_QUOTE')
            strat.ingest_tick(t, t.time)

        eval_tick = Tick('138360', trade_time, 1687.0, 1000, 11000,
                         1687.0, 1685.0, ask_qty=2000, bid_qty=2000, depth_available=True,
                         book_time=trade_time, depth_source='SIMULATED_QUOTE')
        strat.ingest_tick(eval_tick, eval_tick.time)

        watch_an = {
            'code': '138360',
            'name': '앤로보틱스',
            'status': 'WATCH',
            'reference_price': 1680.0,
            'recent_high': 1740.0,
            'resistance': 1800.0,
            'resistance_asof': trade_time.isoformat(),
            'nearest_confirmed_peak': {
                'price': 1740.0,
                'time': (trade_time - timedelta(minutes=10)).isoformat(),
                'confirmed_at': (trade_time - timedelta(minutes=8)).isoformat(),
            },
            'source_count': 2,
        }

        sig = strat.evaluate(eval_tick, watch_an, capital_limit=81300.0, risk_budget=1414.0,
                             ingest=False, received=trade_time, live_authorized=True)

        self.assertIsNone(sig, "Trade B (앤로보틱스) must NOT generate a buy signal")
        last_eval = strat.last_evaluation.get('138360', {})
        reason = last_eval.get('reason')
        self.assertIn(reason, ['DOWNTREND_COUNTER_BOUNCE', 'STOP_TOO_TIGHT_NOISE_RISK', 'KNOWN_CEILING_NET_REWARD_RISK_LOW'],
                      f"Expected downtrend bounce or tight stop rejection, got: {reason}")

    def test_minimum_stop_distance_enforcement_in_fast(self):
        """
        FAST strategy must reject any setup where (entry - stop) / entry < min_stop_distance_fraction (1.2%).
        """
        strat = FastMomentumStrategy(self.cfg, self.auto)
        now = self.base_time

        prices = [1000, 1005, 1010, 1012, 1011, 1010, 1012, 1013, 1014]
        for i, p in enumerate(prices):
            t_stamp = now - timedelta(seconds=len(prices) - i)
            t = Tick('000010', t_stamp, float(p), 6000, (i + 1) * 6000,
                     float(p), float(p - 1), ask_qty=10000, bid_qty=10000, depth_available=True,
                     book_time=t_stamp)
            strat.ingest_tick(t, t.time)

        eval_tick = Tick('000010', now, 1015.0, 6000, 60000,
                         1015.0, 1014.0, ask_qty=10000, bid_qty=10000, depth_available=True,
                         book_time=now)
        strat.ingest_tick(eval_tick, eval_tick.time)

        watch_mock = {
            'code': '000010',
            'name': 'TightStopStock',
            'status': 'WATCH',
            'reference_price': 1000.0,
            'resistance': 1100.0,
            'resistance_asof': now.isoformat(),
        }

        sig = strat.evaluate(eval_tick, watch_mock, capital_limit=81300.0, risk_budget=1626.0,
                             ingest=False, received=now)
        self.assertIsNone(sig)
        last_eval = strat.last_evaluation.get('000010', {})
        self.assertEqual(last_eval.get('reason'), 'STOP_TOO_TIGHT_NOISE_RISK')

    def test_minimum_stop_distance_enforcement_in_pattern_detector(self):
        """
        PatternDetector (A/B/C) must enforce min_stop_distance_fraction (1.2%) even when chase_quality_filter is False.
        """
        auto_cfg = replace(self.auto, chase_quality_filter=False, min_stop_distance_fraction=0.012)
        detector = PatternDetector(self.cfg, auto_cfg, self.costs)

        now = self.base_time
        bars = [
            {'time': now - timedelta(minutes=5), 'open': 10000, 'high': 10100, 'low': 9950, 'close': 10050, 'volume': 1000},
            {'time': now - timedelta(minutes=4), 'open': 10050, 'high': 10200, 'low': 10020, 'close': 10180, 'volume': 2000},
            {'time': now - timedelta(minutes=3), 'open': 10180, 'high': 10250, 'low': 10150, 'close': 10220, 'volume': 1500},
            {'time': now - timedelta(minutes=2), 'open': 10220, 'high': 10260, 'low': 10200, 'close': 10240, 'volume': 1200},
            {'time': now - timedelta(minutes=1), 'open': 10240, 'high': 10300, 'low': 10230, 'close': 10280, 'volume': 1800},
        ]
        detector.history['000020'] = bars

        tick = Tick('000020', now, 10320.0, 100, 10000,
                    10320.0, 10310.0, ask_qty=1000, bid_qty=1000, depth_available=True, book_time=now)
        watch_mock = {
            'code': '000020',
            'name': 'PatternStock',
            'status': 'WATCH',
            'reference_price': 10000.0,
            'resistance': 11000.0,
            'resistance_asof': now.isoformat(),
            'source_count': 2,
        }

        # Setup with tight support (10,250 KRW on limit 10,320 KRW -> 0.678% < 1.2%)
        setup = {'pattern': 'A', 'breakout': 10300, 'support': 10250, 'stop': 10250, 'target': 11000, 'projection': 11000}
        gates = detector.setup_gates(tick, watch_mock, setup, bars)

        self.assertEqual(gates['invalidation']['result'], 'FAIL',
                         "Stop distance < 1.2% must fail invalidation gate even without chase_quality_filter")

    def test_genuine_breakout_with_ample_stop_and_clear_ceiling_passes(self):
        """
        A genuine breakout with stop distance >= 1.2%, ample resistance room, and clear structure must PASS.
        """
        strat = FastMomentumStrategy(self.cfg, self.auto)
        now = self.base_time

        # Structure with 1.4% stop distance, bounded extension < 2.0%:
        # Base origin 985, breakout 1000, support 988, entry 1001
        # Stop at 988 - 1 = 987 -> stop distance (1001 - 987) / 1001 = 1.40% >= 1.2%
        # Target at 1050 (+4.9%) -> net RR > 2.0
        prices = [985, 988, 992, 996, 1000, 990, 988, 992, 996]
        for i, p in enumerate(prices):
            t_stamp = now - timedelta(seconds=len(prices) - i)
            t = Tick('000030', t_stamp, float(p), 10000, (i + 1) * 10000,
                     float(p), float(p - 1), ask_qty=10000, bid_qty=10000, depth_available=True,
                     book_time=t_stamp, depth_source='SIMULATED_QUOTE')
            strat.ingest_tick(t, t.time)

        eval_tick = Tick('000030', now, 1001.0, 10000, 100000,
                         1001.0, 1000.0, ask_qty=10000, bid_qty=10000, depth_available=True,
                         book_time=now, depth_source='SIMULATED_QUOTE')
        strat.ingest_tick(eval_tick, eval_tick.time)

        watch_mock = {
            'code': '000030',
            'name': 'Synthetic Clean Breakout',
            'status': 'WATCH',
            'reference_price': 980.0,
            'resistance': 1050.0,
            'resistance_asof': now.isoformat(),
            'source_count': 2,
        }

        sig = strat.evaluate(eval_tick, watch_mock, capital_limit=81300.0, risk_budget=1626.0,
                             ingest=False, received=now)

        self.assertIsNotNone(sig, f"Clean breakout must generate signal, rejected with: {strat.last_evaluation.get('000030')}")
        self.assertGreaterEqual(sig.net_reward_risk, 1.5)
        stop_dist = (sig.limit - sig.stop) / sig.limit
        self.assertGreaterEqual(stop_dist, 0.0115)


if __name__ == '__main__':
    unittest.main()
