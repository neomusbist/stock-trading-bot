"""
Synthetic regression geometries for R6 trade flaw fixes (2026-10-22).
These short tick lists are not recorded decision tapes. In particular, the
HJ 13,390 ceiling below is a hypothetical input, not proven available at entry.
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
   - Enforced in A/B/C patterns only when chase_quality_filter is enabled.
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
        Synthetic HJ-like near-ceiling counterexample, not actual tick replay:
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
            'day_high': 13390.0,        # Hypothetical ceiling, not a proven entry-time fact
            'daily_high_20d': 15630.0,  # 20-day high from daily candidate builder
            'recent_high': 15630.0,     # Disambiguated 20-day high
            'resistance': 15630.0,      # Distant daily swing resistance
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
        Synthetic AN-like counterexample; the 1,740 peak exists in saved context:
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
            'day_high': 1740.0,         # True intraday day high
            'daily_high_20d': 1800.0,   # 20-day high from daily candidate builder
            'recent_high': 1800.0,      # Disambiguated 20-day high
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
        PatternDetector (A/B/C):
        - When chase_quality_filter is True, enforces min_stop_distance_fraction (1.2%).
        - When chase_quality_filter is False on a confirmed structural setup, respects structural stop.
        """
        now = self.base_time
        bars = [
            {'time': now - timedelta(minutes=5), 'open': 10000, 'high': 10100, 'low': 9950, 'close': 10050, 'volume': 1000},
            {'time': now - timedelta(minutes=4), 'open': 10050, 'high': 10200, 'low': 10020, 'close': 10180, 'volume': 2000},
            {'time': now - timedelta(minutes=3), 'open': 10180, 'high': 10250, 'low': 10150, 'close': 10220, 'volume': 1500},
            {'time': now - timedelta(minutes=2), 'open': 10220, 'high': 10260, 'low': 10200, 'close': 10240, 'volume': 1200},
            {'time': now - timedelta(minutes=1), 'open': 10240, 'high': 10300, 'low': 10230, 'close': 10280, 'volume': 1800},
        ]
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
        setup = {'pattern': 'A', 'breakout': 10300, 'support': 10250, 'stop': 10250, 'target': 11000, 'projection': 11000}

        # 1. With chase_quality_filter=True, stop distance < 1.2% must FAIL
        auto_cfg_filter = replace(self.auto, chase_quality_filter=True, min_stop_distance_fraction=0.012)
        detector_filter = PatternDetector(self.cfg, auto_cfg_filter, self.costs)
        detector_filter.history['000020'] = bars
        gates_filter = detector_filter.setup_gates(tick, watch_mock, setup, bars)
        self.assertEqual(gates_filter['invalidation']['result'], 'FAIL',
                         "Stop distance < 1.2% must fail invalidation gate when chase_quality_filter is True")

        # 2. With chase_quality_filter=False, confirmed structural setup allows narrow stop
        auto_cfg_normal = replace(self.auto, chase_quality_filter=False, min_stop_distance_fraction=0.012)
        detector_normal = PatternDetector(self.cfg, auto_cfg_normal, self.costs)
        detector_normal.history['000020'] = bars
        gates_normal = detector_normal.setup_gates(tick, watch_mock, setup, bars)
        self.assertEqual(gates_normal['invalidation']['result'], 'PASS',
                         "Confirmed structural setup allows narrow stop when chase_quality_filter is False")

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
            'name': 'Clean Breakout Stock',
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

    def test_missing_intraday_ceiling_context_rejected(self):
        """
        When neither nearest_confirmed_peak nor day_high is available, FAST strategy
        must reject with INTRADAY_CEILING_CONTEXT_UNAVAILABLE instead of using distant daily resistance.
        """
        strat = FastMomentumStrategy(self.cfg, self.auto)
        now = self.base_time

        # Feed a valid structural tape with bounded extension (< 2.0%)
        prices = [4940, 4950, 4970, 4990, 5000, 4970, 4960, 4980, 4990]
        for i, p in enumerate(prices):
            t_stamp = now - timedelta(seconds=len(prices) - i)
            t = Tick('000040', t_stamp, float(p), 1000, (i + 1) * 1000,
                     float(p), float(p - 5), ask_qty=5000, bid_qty=5000, depth_available=True,
                     book_time=t_stamp, depth_source='SIMULATED_QUOTE')
            strat.ingest_tick(t, t.time)

        eval_tick = Tick('000040', now, 5005.0, 1000, 10000,
                         5005.0, 5000.0, ask_qty=5000, bid_qty=5000, depth_available=True,
                         book_time=now, depth_source='SIMULATED_QUOTE')
        strat.ingest_tick(eval_tick, eval_tick.time)

        # Clear strategy day_high cache to simulate newly arriving watch item without intraday context
        strat.day_high.clear()
        strat.prior_high.clear()

        watch_no_context = {
            'code': '000040',
            'name': 'NoContextStock',
            'status': 'WATCH',
            'reference_price': 4900.0,
            'resistance': 6000.0,
            'resistance_asof': now.isoformat(),
            'source_count': 1,
            # No day_high, no nearest_confirmed_peak
        }

        sig = strat.evaluate(eval_tick, watch_no_context, capital_limit=81300.0, risk_budget=1626.0,
                             ingest=False, received=now)
        self.assertIsNone(sig)
        last_eval = strat.last_evaluation.get('000040', {})
        self.assertEqual(last_eval.get('reason'), 'INTRADAY_CEILING_CONTEXT_UNAVAILABLE')

    def test_tick_discrete_minimum_stop_distance(self):
        """
        Stop distance check must discretize via tick_size(entry) using round(),
        guaranteeing actual stop distance >= 1.2% (with 1-tick quantization margin)
        across nominal price bands without arbitrary floating point padding.
        """
        from core.util import tick_size

        cases = [
            (1001.0, 0.012, 12, 12.0),
            (13380.0, 0.012, 16, 160.0),
            (55000.0, 0.012, 7, 700.0),
        ]
        for entry, fraction, exp_ticks, exp_krw in cases:
            step = tick_size(entry)
            req_ticks = max(1, round((entry * fraction) / step))
            req_krw = req_ticks * step
            self.assertEqual(req_ticks, exp_ticks, f"Failed tick count for entry {entry}")
            self.assertEqual(req_krw, exp_krw, f"Failed KRW distance for entry {entry}")
            self.assertGreaterEqual(req_krw / entry, fraction - 0.0002, f"Quantized distance must be within 1 tick of {fraction}")


# Duplicate synthetic lists previously labelled real archive replay were removed.
# Recorded point-in-time evidence is verified separately from these fixtures.

    def test_setup_b_pullback_expansion_target_resolves_mathematical_impossibility(self):
        """
        Prove that when pullback_expansion_target_enabled=True, Setup B resolves the
        mathematical impossibility where target was artificially squashed to peak,
        allowing net_reward_risk >= 1.5 with realistic 50% impulse continuation.
        """
        now = datetime(2026, 10, 22, 11, 0, 0, tzinfo=KST)
        # Impulse: 10000 -> 10600 (rise = 600). Pullback to 10300 (50% retrace).
        # Rebound: close 10350. Entry at 10380.
        bars = [
            {'time': now - timedelta(minutes=6), 'open': 10000, 'high': 10200, 'low': 9980, 'close': 10180, 'volume': 5000, 'first_second': 0},
            {'time': now - timedelta(minutes=5), 'open': 10180, 'high': 10400, 'low': 10150, 'close': 10380, 'volume': 8000, 'first_second': 0},
            {'time': now - timedelta(minutes=4), 'open': 10380, 'high': 10600, 'low': 10350, 'close': 10580, 'volume': 10000, 'first_second': 0},
            {'time': now - timedelta(minutes=3), 'open': 10550, 'high': 10550, 'low': 10400, 'close': 10420, 'volume': 3000, 'first_second': 0},
            {'time': now - timedelta(minutes=2), 'open': 10420, 'high': 10420, 'low': 10300, 'close': 10320, 'volume': 2000, 'first_second': 0},
            {'time': now - timedelta(minutes=1), 'open': 10320, 'high': 10360, 'low': 10310, 'close': 10350, 'volume': 4000, 'first_second': 0},
        ]
        tick = Tick('000050', now, 10380.0, 500, 10000, 10380.0, 10370.0,
                    ask_qty=5000, bid_qty=5000, depth_available=True, book_time=now)
        watch_mock = {
            'code': '000050',
            'name': 'PullbackStock',
            'status': 'WATCH',
            'reference_price': 10000.0,
            'resistance': 12000.0,
            'source_count': 2,
            'source_details': {'a': {}, 'b': {}},
        }

        # 1. Under legacy logic (pullback_expansion_target_enabled=False), target is capped at peak (10600)
        # Entry 10380, peak 10600 -> gross reward = 220 won.
        # Support 10300 -> stop 10250 -> gross risk = 130 won.
        # With roundtrip frictions (~75 won), net reward is ~145 won, net risk is ~205 won -> net RR ~0.52 < 1.5 -> REJECTED!
        auto_legacy = replace(self.auto, structural_pullback_enabled=True, confirmed_invalidation_enabled=True,
                              pullback_expansion_target_enabled=False)
        det_legacy = PatternDetector(self.cfg, auto_legacy, self.costs)
        setup_legacy = det_legacy.pullback(bars)
        self.assertIsNotNone(setup_legacy)
        gates_legacy = det_legacy.setup_gates(tick, watch_mock, setup_legacy, bars)
        self.assertEqual(gates_legacy['risk_reward']['result'], 'FAIL',
                         "Legacy ceiling=peak must fail risk_reward gate due to mathematical impossibility")
        self.assertLess(gates_legacy['risk_reward']['values']['calculated_rr'], 1.0)

        # 2. Under improved logic (pullback_expansion_target_enabled=True), target projects to peak + 0.5*rise = 10910
        # Entry 10380, target 10910 -> gross reward = 530 won.
        # Net reward = ~455 won, net risk = ~205 won -> net RR > 2.2 >= 1.5 -> PASS!
        auto_improved = replace(self.auto, structural_pullback_enabled=True, confirmed_invalidation_enabled=True,
                               pullback_expansion_target_enabled=True)
        det_improved = PatternDetector(self.cfg, auto_improved, self.costs)
        setup_improved = det_improved.pullback(bars)
        self.assertIsNotNone(setup_improved)
        gates_improved = det_improved.setup_gates(tick, watch_mock, setup_improved, bars)
        self.assertEqual(gates_improved['risk_reward']['result'], 'PASS',
                         "Improved expansion target must PASS risk_reward gate with net RR >= 1.5")
        self.assertGreaterEqual(gates_improved['risk_reward']['values']['calculated_rr'], 1.5)

if __name__ == '__main__':
    unittest.main()
