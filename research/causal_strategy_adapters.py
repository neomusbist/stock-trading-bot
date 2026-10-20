"""Unified Causal Strategy Adapters for R6 Paper Account Replay.

Adapts existing and research strategies to execute under the exact same
causal timeline, market data, and AutonomousEngine risk/order/fill paths:
1. STRATEGY_A: Existing R6 Strategy A (Breakout with acceleration) via PatternDetector
2. STRATEGY_B: Existing R6 Strategy B (Pullback reclaim) via PatternDetector
3. STRATEGY_C: Existing R6 Strategy C (Fresh box breakout) via PatternDetector
4. STRATEGY_FAST: Existing R6 Strategy FAST via FastMomentumStrategy
5. LOCAL_BREAKOUT: Local resistance breakout via LocalBreakoutStrategy
6. RETEST_EXPERIMENT: Confirmed breakout & retest hypothesis via ConfirmedRetestStrategy
"""
from collections import deque, Counter
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

from core.settings import Config
from core.strategy import Tick
from core.util import tick_size, floor_tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.fast_momentum import FastMomentumStrategy
from automation.local_breakout import LocalBreakoutStrategy
from research.retest_strategy import ConfirmedRetestStrategy, paper_engine_signal
from research.causal_data import aware, finite, known_signal_bars


class BaseStrategyAdapter:
    """Common interface for all strategy adapters."""
    name: str = "BASE"

    def __init__(self, cfg: Config, auto: AutomationConfig, costs: Costs):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs
        self.rejection_counts = Counter()

    def ingest_bar(self, known: datetime, sym: str, b: Dict[str, Any]) -> None:
        """Called when a completed bar is causality-confirmed in the replay loop."""
        pass

    def ingest_tick(self, t: Tick, received: datetime) -> None:
        """Called when a real trade/quote tick arrives."""
        pass

    def evaluate(self, t: Tick, bars: list, watch: Dict[str, Any],
                 capital_limit: float, risk_budget: float, asof: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Evaluate strategy. Returns (engine_signal, detail)."""
        raise NotImplementedError


class _BaseABCAdapter(BaseStrategyAdapter):
    """Shared adapter logic for PatternDetector setups A, B, and C."""
    setup_name: str = "A"

    def __init__(self, cfg: Config, auto: AutomationConfig, costs: Costs):
        super().__init__(cfg, auto, costs)
        self.detector = PatternDetector(cfg, auto, costs)
        self.detector.continuous_observation = True

    def ingest_bar(self, known: datetime, sym: str, b: Dict[str, Any]) -> None:
        self.detector.history.setdefault(sym, deque(maxlen=30)).append(b)
        self.detector.price_context.record(sym, b, b['time'], self.cfg.stale_seconds)

    def ingest_tick(self, t: Tick, received: datetime) -> None:
        ingested = self.detector.ingest(t, received)
        if not ingested:
            # Fallback for synthetic/out-of-session test ticks: accumulate into current bar without destroying OHLC
            minute = t.time.replace(second=0, microsecond=0)
            self.detector.buffers.setdefault(t.symbol, deque(maxlen=300)).append(t)
            self.detector.observation_start.setdefault(t.symbol, t.time)
            self.detector.day_high[t.symbol] = max(t.price, self.detector.day_high.get(t.symbol, t.price))
            self.detector.prior_high.setdefault(t.symbol, t.price)
            b = self.detector.current.get(t.symbol)
            if b is None or b.get('time') != minute:
                if b is not None and (minute - b['time']).total_seconds() == 60:
                    self.detector.history.setdefault(t.symbol, deque(maxlen=30)).append(b)
                    self.detector.price_context.record(t.symbol, {**b, 'venue': 'KRX'}, t.time, self.cfg.stale_seconds)
                b = {
                    'time': minute, 'open': t.price, 'high': t.price, 'low': t.price, 'close': t.price,
                    'volume': t.volume, 'observation_source': 'ACTUAL_RECEIVED', 'tick_count': 1,
                    'first_second': t.time.second, 'last_second': t.time.second, 'gap_reason': None
                }
                self.detector.current[t.symbol] = b
            else:
                b['high'] = max(b['high'], t.price)
                b['low'] = min(b['low'], t.price)
                b['close'] = t.price
                b['volume'] += t.volume
                b['tick_count'] = b.get('tick_count', 0) + 1
                b['last_second'] = t.time.second

    def evaluate(self, t: Tick, bars: list, watch: Dict[str, Any],
                 capital_limit: float, risk_budget: float, asof: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        def _reject(reason, status="REJECT", **extra):
            self.rejection_counts[reason] += 1
            return None, {'pass': False, 'status': status, 'reason': reason, 'setup': self.setup_name, **extra}

        # 1. Base evaluation
        gates, usable_bars, f_cnt, b_cnt = self.detector._base_evaluation(t, watch)
        if gates.get('warmup', {}).get('result') != 'PASS':
            return _reject('WARMUP_INSUFFICIENT', 'REJECT', gates=gates)
        if gates.get('spread', {}).get('result') != 'PASS':
            return _reject('SPREAD_TOO_WIDE', 'REJECT', gates=gates)
        if gates.get('structure', {}).get('result') != 'PASS':
            return _reject('WATCH_STATUS_NOT_ELIGIBLE', 'REJECT', gates=gates)
        if f_cnt < 2 or not b_cnt or not (0 < t.bid <= t.ask):
            return _reject('TICK_BUFFER_INSUFFICIENT', 'UNKNOWN')

        # 2. Setup trials
        trials = self.detector.setup_trials(t, watch, gates, _bars=usable_bars)
        trial = next((x for x in trials if x['setup'] == self.setup_name), None)
        if not trial:
            return _reject(f'SETUP_{self.setup_name}_ABSENT', 'REJECT')

        if not trial.get('entry_ready'):
            reasons = trial.get('failed_reasons', [])
            prim = reasons[0] if reasons else f'SETUP_{self.setup_name}_NOT_READY'
            status = 'UNKNOWN' if ('UNKNOWN' in prim or 'INSUFFICIENT' in prim or 'ABSENT' in prim) else 'REJECT'
            return _reject(prim, status, failed_reasons=reasons, trial_gates=trial.get('gates'))

        # 3. Virtual entry validation against ask
        entry_price = trial['virtual_entry']
        if entry_price < t.ask:
            return _reject('ASK_ABOVE_VIRTUAL_ENTRY', 'REJECT', entry_price=entry_price, ask=t.ask)

        stop_price = trial['virtual_invalidation']
        price_gates = trial.get('gates', {})
        target_price = price_gates.get('risk_reward', {}).get('values', {}).get('target', entry_price * 1.02)
        score = price_gates.get('economic', {}).get('values', {}).get('score', {})

        # 4. Construct engine signal
        source_evidence = {'source_count': watch.get('source_count', 2), 'timestamp': asof.isoformat()}
        engine_sig = {
            'symbol': t.symbol,
            'name': watch.get('name', t.symbol),
            'pattern': self.setup_name,
            'time': asof.isoformat(),
            'signal_detected_at': asof.isoformat(),
            'limit': entry_price,
            'stop': stop_price,
            'emergency': stop_price,
            'target': target_price,
            'ttl_seconds': self.cfg.stale_seconds,
            'max_chase_price': entry_price * (1.0 + getattr(self.cfg, 'max_chase', 0.005)),
            'session': 'REGULAR',
            'venue': 'KRX',
            'source_evidence': source_evidence,
            'invalidation_policy': {
                'enabled': True,
                'structural_stop': stop_price,
                'emergency_stop': stop_price,
                'noise_band': max(0.0, entry_price - stop_price)
            },
            'efficiency': score,
            'heuristic_basis': 'EXISTING_R6_WEIGHT_ASSUMPTION_NOT_EMPIRICAL_WIN_PROBABILITY',
            'SHADOW_ONLY': True,
            'actual_order_permission': False
        }
        return engine_sig, {'pass': True, 'status': 'PASS', 'reason': f'SETUP_{self.setup_name}_ENTRY_READY'}


class StrategyAAdapter(_BaseABCAdapter):
    name = "STRATEGY_A"
    setup_name = "A"


class StrategyBAdapter(_BaseABCAdapter):
    name = "STRATEGY_B"
    setup_name = "B"


class StrategyCAdapter(_BaseABCAdapter):
    name = "STRATEGY_C"
    setup_name = "C"


class StrategyFastAdapter(BaseStrategyAdapter):
    name = "STRATEGY_FAST"

    def __init__(self, cfg: Config, auto: AutomationConfig, costs: Costs):
        super().__init__(cfg, auto, costs)
        self.fast_strategy = FastMomentumStrategy(cfg, auto, costs)

    def ingest_tick(self, t: Tick, received: datetime) -> None:
        self.fast_strategy.ingest_tick(t, received)

    def evaluate(self, t: Tick, bars: list, watch: Dict[str, Any],
                 capital_limit: float, risk_budget: float, asof: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        sig = self.fast_strategy.evaluate(
            t, watch, capital_limit=capital_limit, risk_budget=risk_budget, ingest=False, received=asof
        )
        if sig is None:
            note = self.fast_strategy.last_evaluation.get(t.symbol, {})
            reason = note.get('reason', 'FAST_NO_SIGNAL')
            status = note.get('status', 'REJECT')
            self.rejection_counts[reason] += 1
            return None, {'pass': False, 'status': status, 'reason': reason, 'details': note}

        engine_sig = sig.to_engine_signal(watch=watch)
        engine_sig['source_evidence'] = {'source_count': watch.get('source_count', 2), 'timestamp': asof.isoformat()}
        engine_sig['SHADOW_ONLY'] = True
        engine_sig['actual_order_permission'] = False
        return engine_sig, {'pass': True, 'status': 'PASS', 'reason': sig.reason}


class StrategyLocalBreakoutAdapter(BaseStrategyAdapter):
    name = "LOCAL_BREAKOUT"

    def __init__(self, cfg: Config, auto: AutomationConfig, costs: Costs):
        super().__init__(cfg, auto, costs)
        self.lb_strategy = LocalBreakoutStrategy(cfg, auto, costs)

    def evaluate(self, t: Tick, bars: list, watch: Dict[str, Any],
                 capital_limit: float, risk_budget: float, asof: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        sig, det = self.lb_strategy.evaluate(t, bars, watch, capital_limit, risk_budget, asof=asof)
        if sig is None:
            reason = det.get('reason', 'UNKNOWN')
            status = det.get('status', 'REJECT')
            self.rejection_counts[reason] += 1
            return None, {'pass': False, 'status': status, 'reason': reason, 'details': det}

        source = {'source_count': watch.get('source_count', 2), 'timestamp': asof.isoformat()}
        engine_sig = paper_engine_signal(sig, self.cfg, self.auto, self.costs, source_evidence=source)
        return engine_sig, {'pass': True, 'status': 'PASS', 'reason': sig.reason}


class StrategyRetestAdapter(BaseStrategyAdapter):
    name = "RETEST_EXPERIMENT"

    def __init__(self, cfg: Config, auto: AutomationConfig, costs: Costs):
        super().__init__(cfg, auto, costs)
        self.retest_strategy = ConfirmedRetestStrategy(cfg, auto, costs)

    def evaluate(self, t: Tick, bars: list, watch: Dict[str, Any],
                 capital_limit: float, risk_budget: float, asof: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        sig, det = self.retest_strategy.evaluate(t, bars, watch, capital_limit, risk_budget, asof=asof)
        if sig is None:
            reason = det.get('reason', 'UNKNOWN')
            status = det.get('status', 'REJECT')
            self.rejection_counts[reason] += 1
            return None, {'pass': False, 'status': status, 'reason': reason, 'details': det}

        source = {'source_count': watch.get('source_count', 2), 'timestamp': asof.isoformat()}
        engine_sig = paper_engine_signal(sig, self.cfg, self.auto, self.costs, source_evidence=source)
        return engine_sig, {'pass': True, 'status': 'PASS', 'reason': sig.reason}


def build_all_strategy_adapters(cfg: Config, auto: AutomationConfig, costs: Costs) -> Dict[str, BaseStrategyAdapter]:
    """Factory returning all 6 strategy adapters initialized identically."""
    return {
        'STRATEGY_A': StrategyAAdapter(cfg, auto, costs),
        'STRATEGY_B': StrategyBAdapter(cfg, auto, costs),
        'STRATEGY_C': StrategyCAdapter(cfg, auto, costs),
        'STRATEGY_FAST': StrategyFastAdapter(cfg, auto, costs),
        'LOCAL_BREAKOUT': StrategyLocalBreakoutAdapter(cfg, auto, costs),
        'RETEST_EXPERIMENT': StrategyRetestAdapter(cfg, auto, costs)
    }
