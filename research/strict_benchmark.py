"""Full production decision path in a temporary, typed no-network PAPER account.

Lossy caches may be audited, but cannot acquire WATCH/quote/receipt evidence.
There is deliberately no custom buy, sell, target-hit or end-of-data flatten.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import hashlib
import importlib
import json
import math

from automation.patterns import PatternDetector
from automation.runner import evaluate_actual_entry
from automation.capital import Costs
from core.util import floor_tick
from research.causal_account import PaperAccount
from research.causal_data import Frame, aware, json_values


STRATEGIES = ('A', 'B', 'C', 'D', 'E2')


def normalize_cache_row(raw, symbol=None):
    """Normalize a known alias; never invent volume, quote or packet clocks."""
    row = dict(raw)
    if symbol is not None:
        if row.get('symbol') not in (None, symbol):
            raise ValueError('CACHE_SYMBOL_CONFLICT')
        row['symbol'] = symbol
    if 'vol' in row and 'volume' in row and row['vol'] != row['volume']:
        raise ValueError('VOLUME_ALIAS_CONFLICT')
    if 'volume' not in row and 'vol' in row:
        row['volume'] = row['vol']
    return row


def missing_execution_evidence(row):
    missing = []
    for field in ('symbol', 'time', 'price', 'volume', 'cumulative', 'regular',
                  'ask', 'bid', 'ask_qty', 'bid_qty', 'depth_available',
                  'depth_source', 'book_time', 'book_received_at'):
        if row.get(field) is None:
            missing.append(field)
    if not (row.get('received') or row.get('received_at')):
        missing.append('received_at')
    if not row.get('processed_at'):
        missing.append('processed_at')
    return missing


def valid_tick_price(price):
    return (type(price) in (int, float) and math.isfinite(price)
            and price > 0 and price == floor_tick(price))


def audit_cache(path):
    """Input-quality census, not a market replay or profitability result."""
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    counts = Counter(); fields = Counter(); dates = Counter(); volume = 0
    groups = data.items() if isinstance(data, dict) else [(None, data)]
    for symbol, rows in groups:
        for source in rows:
            row = normalize_cache_row(source, symbol)
            counts['rows'] += 1
            dates[str(row.get('time', ''))[:10]] += 1
            for name in missing_execution_evidence(row):
                fields[name] += 1
            if 'volume' in row:
                volume += row['volume']
            if 'vol' in source and 'volume' not in source:
                counts['volume_alias_restored'] += 1
            for name in ('price', 'ask', 'bid'):
                if name in row and not valid_tick_price(row[name]):
                    counts['off_tick_' + name] += 1
    return dict(input_sha256=hashlib.sha256(raw).hexdigest(), counts=dict(counts),
                dates=dict(dates), missing_fields=dict(fields), observed_volume=volume,
                execution_status='UNKNOWN_MISSING_EVIDENCE' if fields else 'REQUIRES_WATCH_AND_REFERENCE_TIMELINE',
                actual_orderable_signals=None, actual_trades=None, net_pnl=None,
                warning='CACHE_CENSUS_NOT_STRICT_EXECUTION_OR_NO_OPPORTUNITY_PROOF')


class IsolatedDetector(PatternDetector):
    """Use the actual signal constructor, isolating a setup without rebuilding it."""
    def __init__(self, cfg, auto, costs, setup):
        super().__init__(cfg, auto, costs)
        self.isolated_setup = None if setup=='LIVE_ABC' else 'E' if setup == 'E2' else setup
        self.continuous_observation = True

    def setup_trials(self, *args, **kwargs):
        kwargs['_only'] = self.isolated_setup
        return super().setup_trials(*args, **kwargs)


class FullPathReplay:
    """PatternDetector -> production runner -> AutonomousEngine -> L1 fake broker.

    config/automation are explicit scenario inputs. They are never loaded from
    LIVE by this library. E2/D are isolated PAPER options, never deployments.
    """
    def __init__(self, start, strategy, cfg, auto, initial_cash, *, latency_ms=25):
        if strategy not in STRATEGIES+('LIVE_ABC',):
            raise ValueError('UNKNOWN_STRATEGY')
        self.strategy = strategy
        self.cfg = cfg
        if strategy=='LIVE_ABC':
            self.auto=auto
        else:
            # A runtime without research options cannot silently run D/E2;
            # frozen A/B/C and C2 do not need those absent fields.
            available=set(auto.__dataclass_fields__)
            required={'D':{'resistance_breakout_expansion_enabled'},
                      'E2':{'support_rebound_reclaim_enabled','setup_e_reclaim_mode'}}.get(strategy,set())
            if not required<=available:
                raise ValueError('PAPER_RESEARCH_STRATEGY_NOT_IN_RUNTIME: '+strategy)
            requested=dict(resistance_breakout_expansion_enabled=strategy=='D',
                           support_rebound_reclaim_enabled=strategy=='E2',
                           setup_e_reclaim_mode='PULLBACK_HIGH_RECLAIM')
            self.auto=replace(auto,**{k:v for k,v in requested.items() if k in available})
        self.account = PaperAccount(start, cfg=cfg, auto=self.auto,
                                    initial_cash=initial_cash, latency_ms=latency_ms)
        self.account.e.paper_mode = True
        self.detector = IsolatedDetector(cfg, self.auto, self.account.e.costs, strategy)
        self.radar = SimpleNamespace(cache={}, root=self.account.root)
        self.account_view = SimpleNamespace(need_fills=False)
        self.protection_bars = {}
        self.watch = {}; self.watch_times = {}; self.last_eval = {}; self.reasons = Counter()
        self.decisions = []; self.opportunities = {}; self.frames = 0
        self.profile = dict(config=asdict(cfg), automation=asdict(self.auto),
                            initial_cash=initial_cash, latency_ms=latency_ms,
                            requested_strategy=strategy, independent_account=strategy!='LIVE_ABC')
        self.profile_sha256 = hashlib.sha256(json.dumps(self.profile, sort_keys=True).encode()).hexdigest()
        self.source_sha256={name:hashlib.sha256(Path(importlib.import_module(
            name[:-3].replace('/','.')).__file__).read_bytes()).hexdigest()
            for name in ('automation/patterns.py','automation/runner.py','automation/engine.py',
                         'automation/capital.py','automation/allocation.py','automation/exit_plan.py',
                         'core/engine.py','research/causal_account.py','research/strict_benchmark.py')}
        self.account.e.evidence_source_identity={'source_sha256':self.source_sha256,'profile_sha256':self.profile_sha256,
                                                'scope':'PAPER_LOADED_MODULE_FILES'}

    def close(self):
        self.account.close()

    def qualify(self, symbol, watch, known_at):
        known = aware(known_at)
        if known is None or type(watch.get('source_count')) is not int:
            raise ValueError('WATCH_PROVENANCE_REQUIRED')
        prior = self.watch_times.get(symbol)
        if prior and known < prior:
            raise ValueError('NONCAUSAL_WATCH_UPDATE')
        self.watch[symbol] = deepcopy(watch)
        self.watch_times[symbol] = known

    def reference(self, symbol, bars, known_at):
        known = aware(known_at)
        if known is None or any(datetime.fromisoformat(str(b.get('datetime') or b.get('date'))).date() >= known.date()
                                for b in bars):
            raise ValueError('COMPLETED_PRIOR_DAILY_REFERENCE_REQUIRED')
        self.radar.cache[(known.date(), symbol)] = deepcopy(bars)
        self.radar.cache[(known.date(), symbol, 'known_at')] = known

    def step(self, frame, *, quote_retry=False, evaluate=True):
        if not isinstance(frame, Frame):
            raise TypeError('EVIDENCE_FRAME_REQUIRED')
        self.frames += 1
        tick = frame.tick; at = frame.available_at
        self.account.e.evidence_input_basis=frame.basis
        accepted = True if quote_retry else self.detector.ingest(tick, at)
        bar_events = []
        for bar in self.detector.completed.get(tick.symbol, ()):
            previous = self.protection_bars.get(tick.symbol)
            if previous is None or bar['time'] > previous:
                bar_events.append((tick.symbol, bar))
                self.protection_bars[tick.symbol] = bar['time']
        # Actual engine fills/protection run even when a new entry is ineligible.
        self.account.step(frame, bar_events=bar_events)
        if not accepted:
            self.reasons['DETECTOR_INGEST_REJECTED'] += 1
            return None
        if not evaluate:return None
        why = frame.quote_reason(at, self.cfg.stale_seconds)
        if why:
            self.reasons[why] += 1
            return None
        if not all(valid_tick_price(v) for v in (tick.price, tick.ask, tick.bid)):
            self.reasons['OFF_TICK_MARKET_PRICE'] += 1
            return None
        watch = self.watch.get(tick.symbol)
        known = self.watch_times.get(tick.symbol)
        if not watch or known > at:
            self.reasons['POINT_IN_TIME_WATCH_UNKNOWN'] += 1
            return None
        if (at-known).total_seconds() > self.auto.scan_seconds*3:
            self.reasons['POINT_IN_TIME_WATCH_STALE'] += 1
            return None
        ref_known = self.radar.cache.get((at.date(), tick.symbol, 'known_at'))
        if ref_known and ref_known > at:
            self.reasons['REFERENCE_NOT_YET_KNOWN'] += 1
            return None
        e = self.account.e
        # As in PaperAccount.enter(), this is the known simulator balance,
        # never a fabricated historical REST account observation. Refreshing
        # it also prevents wall-clock replay speed from expiring PAPER cash.
        e.reconcile(self.account.broker.snapshot())
        e.source_qualification_seen = True
        e.source_qualification[tick.symbol] = {
            **watch, 'timestamp': known.isoformat()}
        # Production dispatch performs the fresh daily-structure second check.
        order = evaluate_actual_entry(e, self.detector, tick, watch, self.radar,
                                      e.prices, self.account_view, self.last_eval, quote_retry=quote_retry)
        trials = self.detector.last_eval.get(tick.symbol, {}).get('trials', [])
        trial = next((t for t in trials if t.get('setup') == self.detector.isolated_setup), None)
        if self.detector.isolated_setup is None:
            trial=next((t for t in trials if t.get('entry_ready')),trials[0] if trials else None)
        if trial and trial.get('setup_geometry'):
            geometry = trial.get('setup_geometry') or {}
            key = (tick.symbol, geometry.get('pattern'), geometry.get('breakout'),
                   geometry.get('support'), geometry.get('projection'))
            self.opportunities.setdefault(key, {'first_at': at.isoformat(), 'source_id': frame.source_id,
                                                'trial': json_values(deepcopy(trial))})
        if trial:
            self.reasons.update(trial.get('failed_reasons') or [])
        self.decisions.append(dict(at=at.isoformat(), source_id=frame.source_id,
            symbol=tick.symbol, trial_ready=bool(trial and trial.get('entry_ready')),
            order_status=order.get('status') if order else None,
            quantity=order.get('qty') if order else None))
        self.account.broker.advance(frame)
        return order

    def result(self):
        result = self.account.result()
        orders=list(self.account.e.s['orders'].values())
        buys=[o for o in orders if o['side']=='BUY']
        result.update(strategy=self.strategy, mode='FULL_PRODUCTION_PATH_PAPER',
                      profile=self.profile, profile_sha256=self.profile_sha256,
                      source_sha256=self.source_sha256,
                      frames=self.frames, distinct_structure_geometries=len(self.opportunities),
                      structure_identity_basis='SYMBOL_AND_PRICE_LEVELS_NOT_INDEPENDENT_TRADES',
                      structure_records=list(self.opportunities.values()), reasons=dict(self.reasons),
                      entry_funnel=dict(evaluation_calls=len(self.decisions),
                          setup_ready_evaluations=sum(d['trial_ready'] for d in self.decisions),
                          ledger_buy_orders=len(buys),
                          submitted_buy_orders=sum(o['side']=='BUY' for o in self.account.broker.submissions),
                          buy_fill_events=sum(f['side']=='BUY' for f in result['fills'])),
                      paper_order_records=deepcopy(orders),
                      same_as_live_execution=False,
                      scope='Production detector/runner/engine; temporary PAPER policy/account/L1 fills; not historical LIVE approvals or actual fills')
        return result
