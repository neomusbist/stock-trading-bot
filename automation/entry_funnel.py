"""Bounded, process-local entry accounting; never grants order permission.

Counts invocations before event-log throttling. WATCH, SHADOW and probe calls
are separate populations, not additive unique trading opportunities. Snapshots
use the existing heartbeat/summary writer; this module does no hot-path I/O.
"""
from collections import Counter, OrderedDict, defaultdict
import math
import hashlib
import json

SETUPS = ('A', 'B', 'C', 'FAST')


class EntryFunnel:
    def __init__(self, symbol_limit=2048, example_limit=24):
        self.symbol_limit = symbol_limit
        self.example_limit = example_limit
        self.day = None
        self._reset()

    def _reset(self):
        self.stages = Counter()
        self.ids = defaultdict(set)
        self.strategies = defaultdict(Counter)
        self.reasons = defaultdict(Counter)
        self.boundaries = Counter()
        self.examples = OrderedDict()
        self.timeline = OrderedDict()
        self.distinct_overflow = Counter()
        self.discovery_examples = OrderedDict()
        self.signal_ids = defaultdict(set)
        self.execution_ids = defaultdict(set)

    def _identity(self, mapping, group, key):
        ids=mapping[group]
        if key not in ids and len(ids)>=self.symbol_limit:
            self.distinct_overflow[group]+=1
            return
        ids.add(key)

    @staticmethod
    def strategy_name(signal):
        setup=(signal or {}).get('pattern')
        if setup=='A':
            return 'A_continuation' if signal.get('entry_confirmation_basis')=='CONFIRMED_CONTINUATION' else 'A_burst'
        return setup if setup in SETUPS else 'UNKNOWN'

    def _stamp(self, stamp):
        text = stamp.isoformat() if hasattr(stamp, 'isoformat') else str(stamp)
        day = text[:10]
        if self.day != day:
            self.day = day
            self._reset()
        clock=text[11:19]
        phase=('PREOPEN' if '08:00:00'<=clock<'09:00:00' else
               'REGULAR' if '09:00:00'<=clock<'15:40:00' else
               'AFTER_PREP' if '15:40:00'<=clock<'16:00:00' else
               'AFTER' if '16:00:00'<=clock<'20:00:00' else 'OFF_SESSION')
        return text, phase

    def _stage(self, name, symbol, at):
        self.stages[name] += 1
        ids = self.ids[name]
        if symbol not in ids:
            if len(ids) < self.symbol_limit:
                ids.add(symbol)
            else:
                self.distinct_overflow[name] += 1
        if symbol not in self.timeline:
            if len(self.timeline) >= self.symbol_limit:
                return
            self.timeline[symbol] = {}
        self.timeline[symbol].setdefault(name, at)

    def rows(self, rows, stamp, stage):
        at, phase = self._stamp(stamp)
        for row in rows:
            symbol = row.get('code')
            if symbol:
                self._stage(stage, symbol, at)
                if stage=='DISCOVERY_REVIEWED':
                    reason=str(row.get('reason') or row.get('status') or 'UNKNOWN')[:100]
                    self._reason(phase+':DISCOVERY',reason)
                    self.discovery_examples[reason]={'symbol':symbol,'at':at,
                        **{k:row.get(k) for k in ('status','reason','reference_price','history_end',
                            'median_turnover20','recent_support','resistance','range20','source_count')}}
                    self.discovery_examples.move_to_end(reason)
                    while len(self.discovery_examples)>12:self.discovery_examples.popitem(last=False)

    def _reason(self, group, reason):
        reason = str(reason or 'UNSPECIFIED')[:100]
        counts = self.reasons[group]
        if reason not in counts and len(counts) >= 32:
            reason = 'OTHER_REASONS_OVER_LIMIT'
        counts[reason] += 1

    def _record(self, group, symbol, at, status, reason, values):
        self.strategies[group]['evaluated' if status != 'NOT_RUN' else 'not_run'] += 1
        self.strategies[group][status] += 1
        self._reason(group, reason)
        key = group + ':' + status
        self.examples[key] = dict(symbol=symbol, at=at, group=group,
                                  status=status, reason=reason, **values)
        self.examples.move_to_end(key)
        while len(self.examples) > self.example_limit:
            self.examples.popitem(last=False)

    def evaluation(self, symbol, stamp, scope, trials, fast, signal=None,
                   price=None, bid=None, ask=None, fast_enabled=True,trade_ready=None,quote_ready=None):
        at, phase = self._stamp(stamp)
        self._stage(scope + '_EVALUATED', symbol, at)
        for kind,result in (('TRADE',trade_ready),('QUOTE',quote_ready)):
            self.stages[scope+'_'+kind+('_READY' if result is True else '_UNREADY' if result is False else '_UNKNOWN')]+=1
        common = dict(price=price, bid=bid, ask=ask)
        by_setup = {t.get('setup'): t for t in trials or []}
        for name in ('A', 'B', 'C'):
            group = phase + ':' + scope + ':' + name
            trial = by_setup.get(name)
            if trial is None:
                self._record(group, symbol, at, 'NOT_RUN', 'TRIAL_NOT_AVAILABLE', common)
                continue
            gates = trial.get('gates', {})
            missing = (gates.get('warmup', {}).get('result') != 'PASS'
                       or any(v.get('result') == 'UNKNOWN' for v in gates.values()
                              if v.get('required', True))
                       or 'INSUFFICIENT_USABLE_BARS' in trial.get('failed_reasons', []))
            status = 'UNKNOWN' if missing else 'SIGNAL_CANDIDATE' if trial.get('entry_ready') else 'REJECT'
            counts = self.strategies[group]
            counts['geometry_formed'] += bool(trial.get('candidate_generated'))
            rr = gates.get('risk_reward', {}).get('values', {})
            counts['risk_reward_calculated'] += rr.get('target') is not None
            self._record(group, symbol, at, status, trial.get('primary_reason_code'),
                {**common, 'entry': trial.get('virtual_entry'),
                 'invalidation': trial.get('virtual_invalidation'),
                 'target': rr.get('target'),
                 'failed_reasons': list(trial.get('failed_reasons', []))[:16],
                 'gate_results': {k: v.get('result') for k, v in gates.items()}})
            if name=='A':
                route=self.strategy_name({'pattern':'A','entry_confirmation_basis':trial.get('entry_confirmation_basis')})
                self.strategies[phase+':'+scope+':'+route][status]+=1
                self.strategies[phase+':'+scope+':'+route]['evaluated']+=1
        if signal and signal.get('pattern') in SETUPS:
            group=phase+':'+scope+':'+self.strategy_name(signal)
            if all(type(signal.get(k)) in (int,float) and math.isfinite(signal[k]) and signal[k]>0
                   for k in ('limit','stop','target')):
                body=[symbol,signal['pattern'],signal['limit'],signal['stop'],signal['target'],
                      signal.get('entry_confirmation_basis'),signal.get('invalidation_policy')]
                fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
                self._identity(self.signal_ids,group,fingerprint)
            else:self._reason(group,'SIGNAL_IDENTITY_INCOMPLETE')
        group = phase + ':' + scope + ':FAST'
        if not isinstance(fast, dict):
            reason = 'DISABLED' if not fast_enabled else 'ABC_PRIORITY' if signal else 'EVALUATION_NOT_AVAILABLE'
            self._record(group, symbol, at, 'NOT_RUN', reason, common)
        else:
            raw = fast.get('status')
            status = 'SIGNAL_CANDIDATE' if raw == 'SIGNAL' else 'UNKNOWN' if raw == 'UNKNOWN' else 'REJECT'
            metrics = fast.get('metrics') or {}
            geometry = fast.get('geometry') or {}
            self._record(group, symbol, at, status, fast.get('reason'),
                {**common, 'metrics': {k: metrics.get(k) for k in
                    ('ticks_10', 'turnover_10', 'uptick_ratio_30', 'neutral_volume_30', 'spread')},
                 'geometry': {k: geometry.get(k) for k in
                    ('entry', 'stop', 'emergency', 'target', 'quantity', 'net_rr')},
                 'quote_evidence': fast.get('quote_evidence')})

    def boundary(self, symbol, stamp, reason, gates, signal):
        at, phase = self._stamp(stamp)
        setup = (signal or {}).get('pattern')
        stage = 'POST_SIGNAL' if setup in SETUPS else 'PRE_STRATEGY_OR_NO_SIGNAL'
        key = phase + ':' + stage + ':' + str(reason)[:100]
        if key not in self.boundaries and len(self.boundaries) >= 128:
            key = phase + ':OTHER_BOUNDARIES_OVER_LIMIT'
        self.boundaries[key] += 1
        if setup in SETUPS:
            self._stage('SIGNAL_REACHED_BOUNDARY', symbol, at)
        sizing = gates.get('risk_quantity', {}).get('values')
        if isinstance(sizing, dict):
            self._stage('SIZING_REACHED', symbol, at)
            # Risk allowance alone cannot buy a share: cash or a strategy cap
            # can still make final quantity zero. Keep all three populations.
            for field, prefix in (('quantity', 'SIZING'),
                                  ('risk_quantity', 'RISK_QUANTITY'),
                                  ('capital_quantity', 'CAPITAL_QUANTITY')):
                qty = sizing.get(field)
                status = ('UNKNOWN' if type(qty) not in (int, float)
                          or not math.isfinite(qty) or qty < 0
                          else 'POSITIVE' if qty > 0 else 'ZERO')
                self.stages[prefix + '_' + status] += 1

    def execution(self, kind, symbol, stamp, side=None, order=None):
        if kind not in {'ORDER_ACCEPTED', 'FILL', 'ORDER_REJECTED', 'ORDER_UNKNOWN'}:
            return
        at, phase = self._stamp(stamp)
        self._stage(kind + ':' + str(side or 'UNSPECIFIED'), symbol or 'UNKNOWN', at)
        if order and order.get('local'):
            setup=self.strategy_name(order.get('signal'))
            group=phase+':BROKER:'+setup+':'+str(side or order.get('side'))+':'+kind
            self._identity(self.execution_ids,group,str(order['local']))

    def snapshot(self, stamp):
        at, _ = self._stamp(stamp)
        raw = self.ids.get('RAW', set())
        evaluated = set().union(*(v for k, v in self.ids.items() if k.endswith('_EVALUATED')))
        return dict(schema='ENTRY_FUNNEL_V1', at=at, day=self.day,
            count_basis='PROCESS_INVOCATIONS_BEFORE_LOG_THROTTLE; SCOPES_SEPARATE; NOT_UNIQUE_OPPORTUNITIES',
            coverage='PROCESS_LIFETIME_ONLY; NO_RECONSTRUCTION_OF_MISSING_INPUTS',
            stages=dict(self.stages), unique_symbols={k: len(v) for k, v in self.ids.items()},
            distinct_count_basis='EXACT_IF_NO_OVERFLOW_OTHERWISE_LOWER_BOUND',
            distinct_overflow=dict(self.distinct_overflow),
            raw_not_evaluated=sorted(raw-evaluated)[:32],
            strategies={k: dict(v) for k, v in self.strategies.items()},
            unique_signal_fingerprints={k:len(v) for k,v in self.signal_ids.items()},
            unique_execution_orders={k:len(v) for k,v in self.execution_ids.items()},
            signal_identity_basis='SYMBOL_SETUP_ENTRY_INVALIDATION_TARGET_POLICY; NOT_INDEPENDENT_MARKET_OPPORTUNITIES',
            independent_market_opportunities='UNKNOWN_REQUIRES_COMPLETE_TIME_AVAILABLE_TAPE',
            execution_provenance='BROKER_INTERFACE_EVENTS; REAL_EXECUTION_REQUIRES_PRODUCTION_LEDGER_AND_BROKER_RECEIPTS',
            reasons={k: dict(v) for k, v in self.reasons.items()},
            boundaries=dict(self.boundaries), examples=list(self.examples.values()),
            discovery_examples=list(self.discovery_examples.values()),
            first_stage_times={k: dict(v) for k, v in list(self.timeline.items())[:32]})


def observe_evaluation(engine, t, watch, detector, fast, signal, scope):
    """Diagnostic failure must never interrupt an order/position handler."""
    audit = getattr(engine, 'candidate_audit', None)
    funnel = getattr(audit, 'funnel', None)
    if not isinstance(funnel, EntryFunnel):
        return
    try:
        from core.market_time import tick_freshness,book_freshness
        now=engine.clock()
        funnel.evaluation(t.symbol, engine.clock(), scope,
            detector.last_eval.get(t.symbol, {}).get('trials', []), fast, signal,
            price=t.price, bid=t.bid, ask=t.ask,
            fast_enabled=getattr(engine.a, 'fast_momentum_enabled', False),
            trade_ready=tick_freshness(t,now,engine.c.stale_seconds)['fresh'],
            quote_ready=bool(getattr(t,'depth_available',None) is True
                and getattr(t,'ask_qty',0)>0 and getattr(t,'bid_qty',0)>0
                and 0<t.bid<=t.ask and book_freshness(t,now,engine.c.stale_seconds)['fresh']))
    except Exception:
        engine.entry_funnel_errors = getattr(engine, 'entry_funnel_errors', 0) + 1


if __name__ == '__main__':
    import argparse, json
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Read existing local entry counters. No broker/network calls.')
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    path = args.root/'runtime/automation-production/heartbeat.json'
    packet = json.loads(path.read_text(encoding='utf-8-sig'))
    print(json.dumps({'heartbeat_at': packet.get('time'), 'entry_funnel': packet.get('entry_funnel'),
                      'diagnostic_errors': packet.get('entry_funnel_errors', 0)}, ensure_ascii=False, indent=2))
