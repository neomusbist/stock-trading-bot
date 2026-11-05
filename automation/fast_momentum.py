"""Causal FAST research only: no broker, LIVE authorization or price forecasts."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any
from collections import deque, Counter
from statistics import median
import math
import threading
from core.util import floor_tick, tick_size
from core.market_time import market_freshness,tick_freshness,book_freshness,MAX_EXCHANGE_LEAD_SECONDS
from automation.session import supported, after_supported


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def stamp(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value if isinstance(value, datetime) and value.tzinfo is not None else None
    except ValueError:
        return None


def active_confirmed_peak(watch, now):
    """Only an available, same-session, uncleared peak can veto an entry."""
    peak=watch.get('nearest_confirmed_peak')
    if not isinstance(peak,dict) or not number(peak.get('price')) or peak['price']<=0:return None
    at=stamp(peak.get('time'));confirmed=stamp(peak.get('confirmed_at'))
    available=stamp(peak.get('available_at',peak.get('confirmed_at')))
    cleared=stamp(peak.get('cleared_at')) if peak.get('cleared_at') else None
    if (at is None or confirmed is None or available is None or now is None
            or not at<confirmed<=now or available>now
            or not at.date()==confirmed.date()==available.date()==now.date()
            or (peak.get('cleared_at') and cleared is None)
            or (cleared is not None and cleared<=now)):return None
    return peak


def saved_evidence(value):
    """Detach point-in-time evidence and keep the INTENT ledger JSON-safe."""
    if isinstance(value,datetime):return value.isoformat()
    if isinstance(value,dict):return {k:saved_evidence(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [saved_evidence(v) for v in value]
    return value


@dataclass
class FastSignal:
    symbol: str
    name: str
    limit: float
    stop: float
    target: float
    emergency: float
    quantity: int
    net_reward_risk: float
    gross_reward_risk: float
    reward_net_per_share: float
    risk_net_per_share: float
    regime: str
    phase: str
    created_at: datetime
    ttl_seconds: float = 15.0
    max_chase_fraction: float = 0.003
    max_chase_price: float = 0.0
    reason: str = 'EARLY_MOMENTUM_ENTRY'
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.max_chase_price:
            self.max_chase_price = self.limit * (1 + self.max_chase_fraction)

    def is_expired(self, current_time, current_ask):
        now = stamp(current_time)
        if now is None or stamp(self.created_at) is None or not number(current_ask) or current_ask <= 0:
            return True, 'SIGNAL_CLOCK_OR_QUOTE_UNKNOWN'
        elapsed = (now - self.created_at).total_seconds()
        if elapsed < 0 or now.date() != self.created_at.date():
            return True, 'SIGNAL_CLOCK_OR_DAY_MISMATCH'
        if elapsed > self.ttl_seconds:
            return True, f'SIGNAL_TTL_EXPIRED ({elapsed:.1f}s > {self.ttl_seconds}s)'
        if current_ask > self.max_chase_price:
            return True, f'MAX_CHASE_EXCEEDED (ask {current_ask:.0f} > max {self.max_chase_price:.0f})'
        return False, ''

    def to_engine_signal(self, watch=None):
        watch = watch or {}
        g = self.extra.get('geometry', {})
        structure = g.get('structural_evidence', {})
        metrics = self.extra.get('metrics', {})
        eff = {
            'decision': 'TRADE',
            'score': self.net_reward_risk,
            'basis': 'CAUSAL_FAST_MICRO_REACTION',
            # Keep the existing net reward/ratio, but report the proportional
            # fees/slippage drag rather than labelling the reward itself a fee.
            'cost_per_share': (self.target - self.limit) - self.reward_net_per_share,
            'reward_after_cost': self.reward_net_per_share,
            'risk_per_share': self.risk_net_per_share,
            'inputs': {
                'entry': self.limit,
                'stop': self.stop,
                'target': self.target,
                'confidence': 0.8,
                'planned_minutes': 10
            }
        }
        gates = {
            'breakout': {'result': 'PASS', 'values': {'breakout': structure.get('breakout', self.limit)}},
            'invalidation': {'result': 'PASS', 'values': {'entry': self.limit, 'stop': self.stop, 'emergency': self.emergency}},
            'risk_reward': {'result': 'PASS', 'values': {'target': self.target, 'net_rr': self.net_reward_risk}},
            'liquidity': {'result': 'PASS', 'values': {'turnover': metrics.get('turnover_10'),
                'window_seconds': 10, 'basis': 'ACCEPTED_REAL_TICKS'}},
            'acceleration': {'result': 'PASS', 'values': {'mode': 'FAST_MICRO_MOMENTUM'}, 'required': False},
            'pattern': {'result': 'PASS', 'values': {'setup': 'FAST'}, 'required': False},
        }
        invalidation_policy = {
            'policy_name': 'FAST_MICRO_STRUCTURE',
            'enabled': True,
            'structural_stop': self.stop,
            'emergency_stop': self.emergency,
            'noise_band': g.get('noise_band', max(1.0, self.limit - self.stop)),
            'breakout_level': structure.get('breakout', self.limit),
            'support_level': structure.get('support', self.stop),
            'origin_level': structure.get('origin', self.stop),
        }
        return {
            'symbol': self.symbol,
            'name': self.name,
            'pattern': 'FAST',
            'limit': self.limit,
            'stop': self.stop,
            'emergency': self.emergency,
            'target': self.target,
            'planned_qty': self.quantity,
            'net_reward_risk': self.net_reward_risk,
            'gross_reward_risk': self.gross_reward_risk,
            'time': self.created_at.isoformat(),
            'signal_detected_at': self.created_at.isoformat(),
            # Preserve the evidence actually evaluated, rather than an
            # unrepeatable PASS with zero turnover or invented bar history.
            'fast_entry_evidence': {'geometry': saved_evidence(g), 'metrics': saved_evidence(metrics),
                'basis': 'OBSERVED_MICRO_TAPE_NOT_COMPLETED_BAR_TREND'},
            'ttl_seconds': self.ttl_seconds,
            'max_chase_price': self.max_chase_price,
            'regime': self.regime,
            'phase': self.phase,
            'efficiency': eff,
            'evaluation_gates': gates,
            'invalidation_policy': invalidation_policy,
            'source_count': watch.get('source_count', 1),
            'fallback_source1': bool(watch.get('fallback_source1', True)),
            'market_state': '3',
            'session': self.phase if self.phase in ('REGULAR', 'AFTER') else 'REGULAR',
            'venue': watch.get('venue') or 'KRX',
            'SHADOW_ONLY': self.extra.get('SHADOW_ONLY', False),
            'actual_order_permission': self.extra.get('actual_order_permission', True),
            'diagnostic_only': False,
            'profitability_proven': False,
            'strategy_version': 'V1.6-fast-momentum-live',
        }


class MicroTape:
    """Accepted regular prints; neutral prints are never invented buying volume."""
    def __init__(self, max_seconds=120.0, stale_seconds=5):
        self.max_seconds = max_seconds
        self.stale_seconds = stale_seconds
        self.ticks = deque()  # time,price,volume,direction,bid,ask; legacy six-field footprint
        self.metadata = deque()
        self.rejections = Counter()
        self.last_rejection = None
        self.last_cumulative = None
        self._last_tick_direction = 0
        self.day = None
        self.symbol = None
        self.reset_reason = None

    def reset(self, reason='EXPLICIT_GAP'):
        self.ticks.clear(); self.metadata.clear()
        self.last_cumulative = None; self._last_tick_direction = 0; self.day = None; self.symbol = None
        self.reset_reason = reason

    def _reject(self, reason):
        self.last_rejection = reason; self.rejections[reason] += 1
        return False

    def ingest(self, t, received=None, coverage_gap=False):
        at = stamp(getattr(t, 'time', None)); receipt = stamp(received) if received is not None else at
        price = getattr(t, 'price', None); volume = getattr(t, 'volume', None)
        if at is None or receipt is None or at.date() != receipt.date():
            return self._reject('TRADE_OR_RECEIPT_TIME_UNKNOWN')
        if not getattr(t, 'regular', True) or not number(price) or price <= 0 or not number(volume) or volume <= 0:
            return self._reject('INVALID_REGULAR_TRADE')
        received_at=getattr(t,'received_at',None) or receipt
        received_monotonic=getattr(t,'received_monotonic',None)
        # Admission asks whether the print was timely AT RECEIPT. Replaying a
        # receiver queue must retain that genuine history, just as ABC does.
        # Comparing its counter to the consumer's current monotonic clock here
        # erased the earlier rise/pullback during catch-up. Current eligibility
        # still checks the original counter in metrics() and _quote().
        if not market_freshness(at,receipt,self.stale_seconds,received_at=received_at,
                                received_monotonic=received_monotonic,
                                monotonic_now=received_monotonic)['fresh']:
            return self._reject('STALE_OR_FUTURE_RECEIPT')
        if self.ticks and at < self.ticks[-1][0]:return self._reject('OUT_OF_ORDER_TRADE')
        if self.symbol is not None and self.symbol != t.symbol:return self._reject('SYMBOL_MISMATCH')
        if self.day is not None and at.date() != self.day:self.reset('TRADING_DAY_CHANGED')
        if coverage_gap:self.reset('EXPLICIT_COVERAGE_GAP')
        cumulative = getattr(t, 'cumulative', None)
        if cumulative is not None:
            if not number(cumulative) or cumulative <= 0:return self._reject('INVALID_CUMULATIVE_VOLUME')
            if self.last_cumulative is not None and cumulative <= self.last_cumulative:
                return self._reject('DUPLICATE_OR_REVERSED_CUMULATIVE_VOLUME')
        bid = getattr(t, 'bid', None); ask = getattr(t, 'ask', None)
        fingerprint = (at, price, volume, bid, ask, cumulative)
        if self.metadata and self.metadata[-1]['fingerprint'] == fingerprint:return self._reject('DUPLICATE_TRADE')
        previous = self.ticks[-1][1] if self.ticks else None
        if previous is None:
            direction = None
            self._last_tick_direction = 0
        elif price > previous:
            direction = 1
            self._last_tick_direction = 1
        elif price < previous:
            direction = -1
            self._last_tick_direction = -1
        else:
            direction = self._last_tick_direction
        self.ticks.append((at, price, volume, direction, bid, ask))
        self.metadata.append(dict(received_at=stamp(received_at),received_monotonic=received_monotonic,
            book_time=stamp(getattr(t, 'book_time', None)),
            bid_qty=getattr(t, 'bid_qty', None), ask_qty=getattr(t, 'ask_qty', None),
            depth_available=getattr(t, 'depth_available', None), fingerprint=fingerprint))
        self.day = at.date(); self.symbol = t.symbol; self.last_rejection = None
        if cumulative is not None:self.last_cumulative = cumulative
        self._prune(at)
        return True

    def _prune(self, current_time):
        cutoff = current_time - timedelta(seconds=self.max_seconds)
        while self.ticks and self.ticks[0][0] < cutoff:
            self.ticks.popleft(); self.metadata.popleft()

    def metrics(self, current_time):
        now = stamp(current_time)
        if now is None:return {}
        self._prune(now)
        eligible = self._observed_rows(now)
        if not eligible:return {}
        result = {'ticks_count': len(eligible), 'latest_time': eligible[-1][0][0],
            'receipt_time': eligible[-1][1]['received_at'], 'last_price': eligible[-1][0][1],
            'last_bid': eligible[-1][0][4], 'last_ask': eligible[-1][0][5],
            'book_time': eligible[-1][1]['book_time'],
            'data_fresh': market_freshness(eligible[-1][0][0],now,self.stale_seconds,
                received_at=eligible[-1][1]['received_at'],
                received_monotonic=eligible[-1][1].get('received_monotonic'))['fresh'],
            'direction_basis': 'TICK_RULE_PROXY_NOT_AGGRESSOR_ID', 'reset_reason': self.reset_reason}
        for seconds in (10, 30, 60):
            cutoff=now-timedelta(seconds=seconds)
            rows = [r for r, _ in eligible if r[0] >= cutoff]
            volume = sum(r[2] for r in rows); turnover = sum(r[1] * r[2] for r in rows)
            result.update({f'ticks_{seconds}': len(rows), f'volume_{seconds}': volume, f'turnover_{seconds}': turnover})
            if seconds in (10, 30, 60):
                # Direction uses Lee-Ready tick test with zero-tick inheritance.
                # Uptick dominance is measured against directional trade volume (up / (up + down)).
                up = sum(r[2] for r in rows if r[3] == 1); down = sum(r[2] for r in rows if r[3] == -1)
                directional = up + down
                result.update({f'uptick_ratio_{seconds}': up / directional if directional else 0.,
                    f'downtick_ratio_{seconds}': down / directional if directional else 0.,
                    f'neutral_volume_{seconds}': sum(r[2] for r in rows if r[3] == 0),
                    f'p_min_{seconds}': min((r[1] for r in rows), default=eligible[-1][0][1]),
                    f'p_max_{seconds}': max((r[1] for r in rows), default=eligible[-1][0][1])})
            if seconds == 30:result['vwap_30'] = turnover / volume if volume else eligible[-1][0][1]
        bid, ask = result['last_bid'], result['last_ask']
        result['spread'] = (ask - bid) / bid if number(bid) and number(ask) and 0 < bid <= ask else None
        return result

    def _observed_rows(self,asof):
        # Exchange time is second-resolution. A bounded clock lead cannot
        # erase a print which has actually arrived; unreceived prints remain
        # unavailable, and old prints still count in their existing windows.
        day=asof.date();latest=asof+timedelta(seconds=MAX_EXCHANGE_LEAD_SECONDS)
        return [(row,meta) for row,meta in zip(self.ticks,self.metadata)
                if meta['received_at']<=asof and row[0].date()==day and row[0]<=latest]

    def structure(self, asof, exclude_decision=True):
        """An earlier rise and higher micro-low must already have rebounded."""
        cache_key = (len(self.ticks), self.ticks[-1] if self.ticks else None, self.ticks[0][0] if self.ticks else None, asof, exclude_decision)
        cached = getattr(self, '_structure_cache', None)
        if cached is not None and cached[0] == cache_key:
            return dict(cached[1])
        rows = [r for r,_ in self._observed_rows(asof)]
        if exclude_decision and rows:rows = rows[:-1]
        turns = []
        for row in rows:
            if not turns or row[1] != turns[-1][1]:turns.append(row)
        evidence = {'basis': 'PREDECISION_OBSERVED_PULLBACK_REBOUND', 'asof': asof.isoformat(),
                    'price_points': len(turns), 'synthetic_bars_used': False}
        # Preserve first-tie max/min semantics while building each prefix once.
        # Repeating max(range(i)) for every low made a declining busy tape O(n²).
        peaks=[];origins=[];peak_i=origin_i=0
        for j,row in enumerate(turns):
            peaks.append(peak_i);origins.append(origin_i)
            if row[1]>turns[peak_i][1]:peak_i=j
            if row[1]<turns[origin_i][1]:origin_i=j
        suffix=[None]*len(turns)
        for j in range(len(turns)-1,-1,-1):
            suffix[j]=turns[j][1] if j==len(turns)-1 or turns[j][1]<suffix[j+1] else suffix[j+1]
        for i in range(len(turns) - 2, 0, -1):
            left, low, right = turns[i - 1:i + 2]
            if not left[1] > low[1] < right[1]:continue
            peak_i=peaks[i];peak=turns[peak_i]
            if peak_i == 0:continue
            origin=turns[origins[peak_i]]
            if low[1]<origin[1] or suffix[peak_i+1]<low[1]:continue
            noise = median([abs(b[1] - a[1]) for a, b in zip(turns, turns[1:])])
            res = {**evidence, 'pass': True, 'support': low[1], 'support_at': low[0].isoformat(),
                'confirmation_at': right[0].isoformat(), 'breakout': peak[1], 'breakout_at': peak[0].isoformat(),
                'origin': origin[1], 'origin_at': origin[0].isoformat(), 'observed_noise': noise}
            self._structure_cache = (cache_key, res)
            return dict(res)
        res = {**evidence, 'pass': False, 'reason': 'CONFIRMED_MICRO_SUPPORT_UNKNOWN'}
        self._structure_cache = (cache_key, res)
        return dict(res)


class FastMomentumStrategy:
    """SHADOW hypothesis; a signal is not order authorization, fill or expectancy."""
    def __init__(self, cfg=None, auto=None, costs=None):
        self.cfg = cfg; self.auto = auto
        self.costs = dict(buy=getattr(auto, 'commission_buy', .00015),
            sell=getattr(auto, 'commission_sell', .00015), tax=getattr(auto, 'sell_tax', .002),
            entry_slippage=getattr(cfg, 'entry_slippage_fraction', .002), exit_slippage=getattr(cfg, 'exit_slippage_fraction', .003))
        if costs is not None:
            self.costs.update(costs if isinstance(costs, dict) else {key: getattr(costs, key) for key in self.costs})
        if any(not number(v) or not 0 <= v < 1 for v in self.costs.values()):raise ValueError('invalid proportional cost assumption')
        if sum(self.costs[k] for k in ('sell', 'tax', 'exit_slippage')) >= 1:raise ValueError('invalid sell proceeds model')
        self._lock = threading.RLock()
        self.tapes = {}; self.active_signals = {}; self.last_exit_by_symbol = {}; self.last_evaluation = {}
        self.day_high = {}
        self.prior_high = {}
        self.high_day = {}
        self.rejection_counts = Counter(); self.signal_history = deque(maxlen=256)
        self.cooldown_seconds = getattr(auto, 'cooldown_seconds', 900.)

    def get_tape(self, symbol):
        with self._lock:
            if symbol not in self.tapes:self.tapes[symbol] = MicroTape(stale_seconds=getattr(self.cfg, 'stale_seconds', 5))
            return self.tapes[symbol]

    def ingest_tick(self, t, received=None, coverage_gap=False):
        with self._lock:
            tape = self.get_tape(t.symbol)
            accepted = tape.ingest(t, received, coverage_gap)
            if accepted and number(getattr(t, 'price', None)):
                if self.high_day.get(t.symbol)!=t.time.date():
                    self.day_high.pop(t.symbol,None);self.prior_high.pop(t.symbol,None)
                    self.high_day[t.symbol]=t.time.date()
                tape.metadata[-1]['prior_observed_day_high']=self.day_high.get(t.symbol)
                if t.symbol in self.day_high and t.price > self.day_high[t.symbol]:
                    self.prior_high[t.symbol] = self.day_high[t.symbol]
                    self.day_high[t.symbol] = t.price
                elif t.symbol not in self.day_high:
                    self.day_high[t.symbol] = t.price
            if not accepted:self._note(t, 'UNKNOWN', tape.last_rejection)
            return accepted

    def snapshot_tape(self, symbol):
        with self._lock:
            tape = self.tapes.get(symbol)
            if tape is None or not tape.ticks:
                return None
            return dict(
                ticks=deque(tape.ticks),
                metadata=deque(dict(m) for m in tape.metadata),
                last_cumulative=tape.last_cumulative,
                day=tape.day,
                symbol=tape.symbol,
                reset_reason=tape.reset_reason,
                day_high=self.day_high.get(symbol),prior_high=self.prior_high.get(symbol),
                high_day=self.high_day.get(symbol),
            )

    def snapshot_decision_tape(self, symbol, asof, max_rows=256):
        """Bounded audit copy of arrived prints, never a new signal or warmup.

        A SHADOW-to-WATCH handoff can precede the recorder's next scope update.
        Its WATCH ring may then be empty even though FAST used genuine history.
        Copy that existing history at the signal's receipt-time cutoff, without
        replaying it into the detector or treating later arrivals as evidence.
        """
        at = stamp(asof)
        if at is None or type(max_rows) is not int or not 0 < max_rows <= 256:
            return None
        with self._lock:
            tape = self.tapes.get(symbol)
            if tape is None:return None
            rows = deque(maxlen=max_rows); eligible = 0
            cutoff = at - timedelta(seconds=tape.max_seconds)
            for row, meta in zip(tape.ticks, tape.metadata):
                if not (cutoff <= row[0] <= at + timedelta(seconds=MAX_EXCHANGE_LEAD_SECONDS)
                        and row[0].date() == at.date() and meta['received_at'] <= at):
                    continue
                eligible += 1
                rows.append(dict(exchange_at=row[0], received_at=meta['received_at'],
                    received_monotonic=meta.get('received_monotonic'), price=row[1],
                    volume=row[2], direction=row[3], bid=row[4], ask=row[5],
                    book_time=meta.get('book_time'), bid_qty=meta.get('bid_qty'),
                    ask_qty=meta.get('ask_qty'), depth_available=meta.get('depth_available'),
                    cumulative=meta['fingerprint'][5]))
            return saved_evidence(dict(asof=at, rows=list(rows), eligible_rows=eligible,
                truncated=eligible > len(rows), max_rows=max_rows,
                reset_reason=tape.reset_reason,
                source='ACCEPTED_FAST_MICRO_TAPE_AT_SIGNAL_RECEIPT_CUTOFF',
                coverage='BOUNDED_OBSERVED_PRINTS_NOT_CONTINUOUS_MARKET_COVERAGE',
                direction_basis='TICK_RULE_PROXY_NOT_AGGRESSOR_ID'))

    def restore_tape(self, symbol, snapshot):
        if not snapshot or not snapshot.get('ticks'):
            return False
        with self._lock:
            src_ticks = snapshot['ticks']
            if snapshot.get('symbol',symbol)!=symbol:return False
            tgt = self.tapes.get(symbol)
            if tgt is not None and tgt.ticks:
                src_last = src_ticks[-1][0]
                tgt_last = tgt.ticks[-1][0]
                if src_last.date()<tgt_last.date():return False
                if src_last.date() == tgt_last.date():
                    if (src_last, snapshot.get('last_cumulative') or 0) < (tgt_last, tgt.last_cumulative or 0):
                        return False
                    if len(tgt.ticks) > len(src_ticks) and (src_last - tgt_last).total_seconds() <= 0:
                        return False
            tape = self.get_tape(symbol)
            tape.ticks = deque(src_ticks)
            tape.metadata = deque(dict(m) for m in snapshot['metadata'])
            tape.last_cumulative = snapshot.get('last_cumulative')
            tape.day = snapshot.get('day')
            tape.symbol = snapshot.get('symbol', symbol)
            tape.reset_reason = snapshot.get('reset_reason')
            self.day_high.pop(symbol,None);self.prior_high.pop(symbol,None)
            self.high_day[symbol]=src_ticks[-1][0].date()
            # A flat repeat at the high is not a new distinct record high.
            for row in src_ticks:
                if row[0].date()!=self.high_day[symbol] or not number(row[1]):continue
                if symbol not in self.day_high:self.day_high[symbol]=row[1]
                elif row[1]>self.day_high[symbol]:
                    self.prior_high[symbol]=self.day_high[symbol];self.day_high[symbol]=row[1]
            high=snapshot.get('day_high');prior=snapshot.get('prior_high')
            if (snapshot.get('high_day')==self.high_day[symbol] and number(high)
                    and high>=self.day_high.get(symbol,0)):
                self.day_high[symbol]=high
                self.prior_high.pop(symbol,None)
                if number(prior) and 0<prior<high:self.prior_high[symbol]=prior
            return True

    def handoff_from(self, source, symbol):
        if source is None or source is self or not hasattr(source, 'snapshot_tape'):
            return False
        return self.restore_tape(symbol, source.snapshot_tape(symbol))

    def forget(self, symbol, reason='OBSERVATION_COVERAGE_LOST'):
        with self._lock:
            self.tapes.pop(symbol, None); self.active_signals.pop(symbol, None); self.last_evaluation.pop(symbol, None)
            self.day_high.pop(symbol,None);self.prior_high.pop(symbol,None);self.high_day.pop(symbol,None)

    def reset(self, reason='OBSERVATION_COVERAGE_LOST'):
        with self._lock:
            for symbol in list(self.tapes):self.forget(symbol, reason)

    def record_exit(self, symbol, filled_at):
        at = stamp(filled_at)
        if at is None:raise ValueError('full exit fill requires aware timestamp')
        with self._lock:
            self.last_exit_by_symbol[symbol] = at; self.active_signals.pop(symbol, None)

    def _note(self, t, status, reason, decision_at=None, **details):
        record = dict(time=t.time.isoformat(), status=status, reason=reason,
                      actual_order_permission=False, SHADOW_ONLY=True)
        record.update(details)
        if decision_at is not None:
            record['quote_evidence']={'decision_at':decision_at.isoformat(),
                'trade_at':t.time.isoformat(),'received_at':getattr(t,'received_at',None),
                'book_time':saved_evidence(getattr(t,'book_time',None)),
                'book_received_at':getattr(t,'book_received_at',None),
                'bid':t.bid,'ask':t.ask,'bid_qty':getattr(t,'bid_qty',None),
                'ask_qty':getattr(t,'ask_qty',None),'depth_available':getattr(t,'depth_available',None),
                'depth_source':getattr(t,'depth_source','UNKNOWN'),
                'accepted_trade_bid':(details.get('metrics') or {}).get('last_bid'),
                'accepted_trade_ask':(details.get('metrics') or {}).get('last_ask'),
                'book_freshness':book_freshness(t,decision_at,getattr(self.cfg,'stale_seconds',5))}
        self.last_evaluation[t.symbol] = record
        if status in {'REJECT', 'UNKNOWN'} and reason:
            self.rejection_counts[reason] += 1

    def determine_phase(self, at):
        s = at.strftime('%H:%M:%S')
        if '09:00:00' <= s < '09:02:00':return 'OPENING_EARLY'
        if '09:02:00' <= s < '09:05:00':return 'OPENING_BUILD'
        if '09:05:00' <= s < '09:15:00':return 'OPENING_STABILIZE'
        if '09:15:00' <= s < '15:10:00':return 'REGULAR'
        if '15:40:00' <= s < '20:00:00':return 'AFTER'
        return 'OFF_SESSION'

    def classify_regime(self, t, m, phase, watch, asof=None):
        spread = m.get('spread')
        is_opening = phase in ('OPENING_EARLY', 'OPENING_BUILD')
        maximum = max(getattr(self.cfg, 'max_spread', 0.004), 0.004 if is_opening else 0.0035)
        if spread is None:return 'C', 'EXECUTABLE_QUOTE_UNKNOWN', {}
        step = tick_size(t.price)
        if (t.ask - t.bid > step) and spread > maximum:
            return 'C', 'FALSE_BREAKOUT_WIDE_SPREAD', {'spread': spread, 'max_spread': maximum, 'tick_size': step}
        minimum = 3 if is_opening else 3
        if m.get('ticks_10', 0) < minimum:return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TICK_VELOCITY', {'min_ticks': minimum}
        turnover = 500000. if is_opening else 800000.
        if m.get('turnover_10', 0) < turnover:return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER', {'min_turnover': turnover}
        reference = watch.get('reference_price') or watch.get('first_raw_price')
        if number(reference) and reference > 0 and t.price < reference * .985 and m['p_max_60'] < reference * .990:
            return 'D', 'DOWNTREND_COUNTER_BOUNCE', {'reference': reference}
        # Multi-minute context: check completed trend if available, without assuming missing history
        trend_5m = watch.get('trend_5m') or (watch.get('fast_context_snapshot') or {}).get('trend_5m')
        if isinstance(trend_5m, dict) and trend_5m.get('status') == 'DOWN' and not trend_5m.get('higher_lows', False):
            return 'D', 'DOWNTREND_COUNTER_BOUNCE', {'trend_5m': trend_5m}
        # Drawdown check: sharp falling knife in the last 60 seconds of tape
        ref_high = m.get('p_max_60')
        if ref_high and ref_high > 0 and t.price < ref_high * .970:
            return 'D', 'DOWNTREND_COUNTER_BOUNCE', {'peak_price': ref_high, 'drawdown': 1 - t.price / ref_high}
        extension = t.price / m['p_min_60'] - 1; limit = .035 if is_opening else .028
        if extension > limit:return 'B', 'EXTENDED_IMPULSE_CHASE_OVEREXTENDED', {'extension': extension}

        # Observable volume exhaustion vs breakout:
        # High volume is not inherently a defect. It is only an exhaustion when accompanied by observable
        # price failure / rejection at the high (e.g. price dropping off 10s high or failing uptick dominance).
        turnover_10 = m.get('turnover_10', 0)
        volume_10 = m.get('volume_10', 0)
        volume_30 = m.get('volume_30', 0)
        if turnover_10 >= 20000000. and volume_30 > 0:
            vol_ratio_10 = volume_10 / volume_30
            if vol_ratio_10 >= 0.60 and (t.price < m.get('p_max_10', t.price) - step or m.get('uptick_ratio_10', 0.5) < 0.40):
                return 'B', 'BUYING_CLIMAX_VOLUME_EXHAUSTION', {
                    'turnover_10': turnover_10,
                    'volume_10_share': vol_ratio_10,
                    'p_max_10': m.get('p_max_10', t.price),
                    'price': t.price
                }

        # --- ROUTE B: FIRST PULLBACK SUPPORT RECOVERY ---
        # Prior impulse rise >= 1.2% in 60s, healthy pullback to higher low, and price recovering
        impulse_rise = m['p_max_60'] / m['p_min_60'] - 1
        pullback_depth = 1.0 - m['p_min_30'] / m['p_max_60']
        if (impulse_rise >= 0.012 and 0.005 <= pullback_depth <= 0.028 and m['p_min_30'] > m['p_min_60']
                and t.price >= m['p_min_30'] + step and (t.price >= m.get('vwap_30', t.price) or m.get('uptick_ratio_10', 0.5) >= 0.45)
                and t.ask <= m['p_max_60']):
            return 'A', 'PULLBACK_SUPPORT_RECOVERY', {'entry_mode': 'PULLBACK_RECOVERY', 'support': m['p_min_30'], 'peak': m['p_max_60']}

        # --- ROUTE A: EARLY COMPRESSION BREAKOUT ---
        # Base compression <= 2.2% in 30s, breaking out at or above base high
        base_range = (m['p_max_30'] - m['p_min_30']) / m['p_min_30']
        if base_range <= 0.022 and t.price >= m['p_max_30'] - step:
            if m.get('uptick_ratio_30', 0) >= 0.50 or m.get('uptick_ratio_10', 0.5) >= 0.50:
                return 'A', 'EARLY_MOMENTUM_FLOW_PROXY', {'entry_mode': 'EARLY_BREAKOUT', 'support': m['p_min_30'], 'breakout': m['p_max_30']}

        # Fallback standard checks
        if m['uptick_ratio_30'] < .50:return 'B', 'EXTENDED_IMPULSE_MOMENTUM_EXHAUSTION', {'uptick_ratio_30': m['uptick_ratio_30']}
        if m['uptick_ratio_30'] < (.58 if is_opening else .60):
            return 'C', 'INSUFFICIENT_UPTICK_DOMINANCE', {'uptick_ratio_30': m['uptick_ratio_30']}
        if t.price < m['p_max_30'] - step:return 'C', 'MICRO_BREAKOUT_NOT_AT_PEAK', {}
        return 'A', 'EARLY_MOMENTUM_FLOW_PROXY', {'entry_mode': 'SWING_BREAKOUT'}

    def _quote(self, t, now, quantity=1, side='BOTH'):
        at = stamp(getattr(t, 'time', None)); book = stamp(getattr(t, 'book_time', None)); age = getattr(self.cfg, 'stale_seconds', 5)
        if not getattr(t, 'regular', True):return 'NONREGULAR_TRADE_NOT_EXECUTABLE'
        if at is None or not tick_freshness(t,now,age)['fresh']:return 'CURRENT_TRADE_STALE_OR_FUTURE'
        bid, ask = getattr(t, 'bid', None), getattr(t, 'ask', None)
        if not number(bid) or not number(ask) or not 0 < bid <= ask:return 'EXECUTABLE_QUOTE_UNKNOWN'
        if book is None or not book_freshness(t,now,age)['fresh']:
            return 'BOOK_TIMESTAMP_UNKNOWN_OR_STALE'
        if getattr(t, 'depth_available', None) is not True:return 'EXECUTABLE_DEPTH_UNKNOWN'
        names = ('bid_qty',) if side == 'SELL' else ('ask_qty', 'bid_qty')
        if any(not number(getattr(t, name, None)) or getattr(t, name) < quantity for name in names):
            return 'FULL_QUANTITY_DEPTH_INSUFFICIENT'
        return None

    def calculate_invalidation_and_target(self, t, m, watch, capital_limit=81300., risk_budget=1626., asof=None, entry_mode=None, regime_details=None):
        reject = lambda reason, **extra: dict(**{'pass': False}, reason=reason, **extra)
        now = stamp(asof) if asof is not None else stamp(t.time)
        if now is None:return reject('DECISION_TIME_UNKNOWN')
        if not number(capital_limit) or not number(risk_budget) or capital_limit <= 0 or risk_budget <= 0:
            return reject('AVAILABLE_CAPITAL_OR_RISK_UNKNOWN')
        reason = self._quote(t, now)
        if reason:return reject(reason)

        structure = self.get_tape(t.symbol).structure(now)
        if not structure['pass']:
            if entry_mode == 'PULLBACK_RECOVERY' and regime_details and regime_details.get('support'):
                support = regime_details['support']
                peak = regime_details.get('peak', m.get('p_max_60'))
                structure = {'pass': True, 'support': support, 'breakout': peak, 'basis': 'PULLBACK_SUPPORT_RECOVERY', 'origin': support}
            elif entry_mode == 'EARLY_BREAKOUT' and regime_details and regime_details.get('support'):
                tape = self.get_tape(t.symbol)
                rows = [r for r, _ in tape._observed_rows(now)]
                if rows:
                    rows = rows[:-1]
                turns = []
                for row in rows:
                    if not turns or row[1] != turns[-1][1]:
                        turns.append(row)
                is_monotonic = len(turns) >= 2 and all(b[1] > a[1] for a, b in zip(turns, turns[1:]))
                support = regime_details['support']
                breakout = regime_details.get('breakout', m.get('p_max_30'))
                if not is_monotonic and len(turns) >= 3 and support > 0 and breakout > support:
                    noise = median([abs(b[1] - a[1]) for a, b in zip(turns, turns[1:])]) if len(turns) >= 2 else tick_size(t.price)
                    structure = {'pass': True, 'support': support, 'breakout': breakout, 'basis': 'EARLY_COMPRESSION_BASE', 'origin': support, 'observed_noise': noise}
                else:
                    return reject(structure['reason'], structure=structure)
            else:
                return reject(structure['reason'], structure=structure)

        min_fraction = getattr(self.auto, 'min_stop_distance_fraction', None) or getattr(self.cfg, 'min_stop_distance_fraction', .012)
        min_stop_distance_fraction = float(min_fraction)
        step = tick_size(t.ask)
        required_stop_ticks = max(1, round((t.ask * min_stop_distance_fraction) / step))
        min_stop_distance_krw = required_stop_ticks * step

        # --- ROUTE B: PULLBACK RECOVERY GEOMETRY ---
        if (entry_mode == 'PULLBACK_RECOVERY' or t.ask < structure['breakout']) and t.price >= structure['support'] + step and t.ask < structure['breakout']:
            support = structure['support']
            breakout = structure['breakout']
            entry = t.ask
            # In pullback recovery below the swing peak, the swing peak (breakout) is the primary overhead ceiling.
            # Do NOT leap over the unconfirmed swing peak to an unconfirmed 20-day daily high!
            buffer = max(step, t.ask - t.bid, support * getattr(self.cfg, 'structural_buffer', .001))
            primary_res = watch.get('resistance')
            if number(primary_res) and primary_res <= entry:
                if support < primary_res - buffer:
                    return reject('UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED',
                                  entry=entry, resistance=primary_res, support=support,
                                  min_required_support=primary_res - buffer, structure=structure)
            overhead = watch.get('resistance') or watch.get('recent_high')
            if number(overhead) and entry < overhead < breakout:
                target = floor_tick(overhead)
            else:
                target = floor_tick(breakout)
            if target <= entry:
                return reject('TARGET_BELOW_OR_AT_ENTRY', entry=entry, target=target)
            min_headroom = max(0.012, min_stop_distance_fraction * 1.5)
            if (target - entry) / entry < min_headroom:
                return reject('INSUFFICIENT_ROOM_TO_RESISTANCE', entry=entry, target=target,
                              room_pct=(target - entry) / entry, min_headroom=min_headroom)
            stop = floor_tick(support - buffer)
            if entry - stop < min_stop_distance_krw:
                stop = floor_tick(entry - min_stop_distance_krw)
            noise = max(step * 2, t.ask - t.bid)
            emergency = floor_tick(stop - noise)
            if not 0 < emergency < stop < entry < target:
                return reject('INVALID_STRUCTURAL_PRICE_GEOMETRY')
            entry_fill = entry * (1 + self.costs['entry_slippage'])
            buy_outlay = entry_fill * (1 + self.costs['buy'])
            sell_multiplier = (1 - self.costs['exit_slippage']) * (1 - self.costs['sell'] - self.costs['tax'])
            reward = target * sell_multiplier - buy_outlay
            risk = buy_outlay - emergency * sell_multiplier
            net_rr = reward / risk if risk > 0 else None
            minimum = getattr(self.cfg, 'min_reward_risk', 1.5)
            if net_rr is None or net_rr < minimum:
                return reject('NET_REWARD_RISK_LOW', min_net_rr=minimum, net_rr=net_rr)
            risk_qty = math.floor(risk_budget / risk); cash_qty = math.floor(capital_limit / buy_outlay)
            quantity = min(risk_qty, cash_qty)
            if quantity <= 0:
                return reject('RISK_ZERO_QUANTITY', max_shares_by_risk=risk_qty, max_shares_by_cap=cash_qty)
            reason = self._quote(t, now, quantity)
            if reason:return reject(reason, quantity=quantity)
            struct_ev = {'pass': True, 'breakout': breakout, 'support': support, 'basis': 'PULLBACK_SUPPORT_RECOVERY'}
            return dict(**{'pass': True}, entry=entry, stop=stop, emergency=emergency, target=target,
                        net_rr=net_rr, gross_rr=(target-entry)/(entry-emergency),
                        reward_net_per_share=reward, risk_net_per_share=risk, risk_budget=risk_budget,
                        target_basis='PULLBACK_RECOVERY_SWING_PEAK', target_known_at=now.isoformat(),
                        quantity=quantity, structural_evidence=struct_ev, entry_mode='PULLBACK_RECOVERY',
                        costs=dict(self.costs), modeled_entry=entry_fill)
        entry = t.ask; breakout = structure['breakout']; support = structure['support']
        if t.price <= breakout:return reject('PREDECISION_REACTION_HIGH_NOT_BROKEN', structure=structure)
        if entry <= breakout:return reject('EXECUTABLE_ASK_NOT_ABOVE_BREAKOUT', structure=structure)
        max_chase = getattr(self.cfg, 'max_chase', .005)
        if entry / breakout - 1 > max_chase:return reject('MAX_CHASE_EXCEEDED', breakout=breakout, entry=entry, max_chase=max_chase)
        min_headroom = max(0.012, min_stop_distance_fraction * 1.5)
        pullback_depth = max(0., breakout - support)
        min_pullback_room = min(max(0., support - structure['origin'] - tick_size(support)), tick_size(support)) if pullback_depth <= tick_size(support) else 0.
        buffer = max(tick_size(support), t.ask - t.bid, support * getattr(self.cfg, 'structural_buffer', .001)) + min_pullback_room

        primary_res = watch.get('resistance')
        known_at = stamp(watch.get('resistance_asof') or watch.get('observed_at'))
        if number(primary_res):
            if entry < primary_res:
                room = (primary_res - entry) / entry
                if room < min_headroom:
                    return reject('INSUFFICIENT_ROOM_TO_RESISTANCE', entry=entry, target=primary_res,
                                  room_pct=room, min_headroom=min_headroom, structure=structure)
                overhead = primary_res
            else:
                # entry >= primary_res: require structural confirmation that primary resistance was broken and supported
                # A simple 1-tick poke or unconfirmed impulse with support below primary_res is NOT a confirmed breakout.
                confirmed_breakout_support = (support >= primary_res - buffer and breakout > primary_res)
                if not confirmed_breakout_support:
                    return reject('UNCONFIRMED_RESISTANCE_BREAKOUT_HEADROOM_EXHAUSTED',
                                  entry=entry, resistance=primary_res, support=support,
                                  min_required_support=primary_res - buffer, structure=structure)
                next_res = watch.get('recent_high') or watch.get('daily_high_20d')
                if number(next_res) and next_res > entry:
                    next_room = (next_res - entry) / entry
                    if next_room < min_headroom:
                        return reject('INSUFFICIENT_ROOM_TO_NEXT_RESISTANCE', entry=entry, target=next_res,
                                      room_pct=next_room, min_headroom=min_headroom, structure=structure)
                    overhead = next_res
                else:
                    return reject('NO_VALID_NEXT_RESISTANCE_ABOVE_BREAKOUT',
                                  entry=entry, broken_resistance=primary_res, structure=structure)
        else:
            overhead = watch.get('recent_high') or watch.get('daily_high_20d')
            if not (number(overhead) and overhead > entry):
                return reject('KNOWN_OVERHEAD_TARGET_UNAVAILABLE', structure=structure)
            room = (overhead - entry) / entry
            if room < min_headroom:
                return reject('INSUFFICIENT_ROOM_TO_RESISTANCE', entry=entry, target=overhead,
                              room_pct=room, min_headroom=min_headroom, structure=structure)

        if not number(overhead) or overhead <= entry or known_at is None or known_at > now or known_at.date() != now.date():
            return reject('KNOWN_OVERHEAD_TARGET_UNAVAILABLE', structure=structure)

        intraday_ceiling = None
        ceiling_source = None
        intraday_cap = None

        peak = active_confirmed_peak(watch, now)
        if peak and number(peak.get('price')):
            peak_price = peak['price']
            if entry <= peak_price:
                room = (peak_price - entry) / entry
                if room < min_headroom:
                    return reject('KNOWN_CEILING_NET_REWARD_RISK_LOW',
                                  entry=entry, target=peak_price, room_pct=room,
                                  min_headroom=min_headroom, nearest_confirmed_peak=peak,
                                  intraday_cap=peak_price, ceiling_source='CONFIRMED_SWING_PEAK',
                                  structure=structure)
                intraday_ceiling = peak_price
                ceiling_source = 'CONFIRMED_SWING_PEAK'
            else:
                # entry > peak_price: requires structural confirmation that peak was broken and held as support
                confirmed_peak_support = (support >= peak_price - buffer and breakout > peak_price)
                if not confirmed_peak_support:
                    return reject('UNCONFIRMED_PEAK_BREAKOUT_HEADROOM_EXHAUSTED',
                                  entry=entry, peak=peak_price, support=support,
                                  min_required_support=peak_price - buffer, structure=structure)

        if intraday_ceiling is None:
            dh = watch.get('day_high') or self.day_high.get(t.symbol)
            if number(dh) and dh > entry:
                intraday_ceiling = dh
                ceiling_source = 'INTRADAY_DAY_HIGH'

        if intraday_ceiling is not None:
            intraday_cap = intraday_ceiling
            target = floor_tick(min(overhead, intraday_ceiling))
            target_basis = 'MIN_KNOWN_INTRADAY_CEILING_AND_DAILY_REFERENCE'
        else:
            target = floor_tick(overhead)
            ceiling_source = 'CONFIRMED_HIGHER_TIMEFRAME_RESISTANCE'
            target_basis = 'CONFIRMED_HIGHER_TIMEFRAME_RESISTANCE'

        if target <= entry or (target - entry) / entry < min_headroom:
            reason = 'KNOWN_CEILING_NET_REWARD_RISK_LOW' if (intraday_cap and target <= floor_tick(intraday_cap)) else 'INSUFFICIENT_ROOM_TO_RESISTANCE'
            return reject(reason, entry=entry, target=target, room_pct=(target - entry) / entry,
                          min_headroom=min_headroom, nearest_confirmed_peak=peak if intraday_cap else None,
                          intraday_cap=intraday_cap, ceiling_source=ceiling_source, structure=structure)
        # Never move a structural line to a fixed percentage or solve a target
        # backwards from desired RR. Size from the separately derived hard line.
        stop = floor_tick(support - buffer)
        noise = max(structure['observed_noise'], t.ask - t.bid, 2 * tick_size(support))
        emergency = floor_tick(stop - noise)
        if not 0 < emergency < stop < entry < target:return reject('INVALID_STRUCTURAL_PRICE_GEOMETRY', structure=structure)
        min_fraction=getattr(self.auto,'min_stop_distance_fraction',None)
        if min_fraction is None:min_fraction=getattr(self.cfg,'min_stop_distance_fraction',.012)
        min_stop_distance_fraction=float(min_fraction)
        step = tick_size(entry)
        required_stop_ticks = max(1, round((entry * min_stop_distance_fraction) / step))
        min_stop_distance_krw = required_stop_ticks * step
        actual_stop_distance_krw = entry - stop
        if actual_stop_distance_krw < min_stop_distance_krw:
            return reject('STOP_TOO_TIGHT_NOISE_RISK', entry=entry, stop=stop,
                          stop_distance_fraction=actual_stop_distance_krw / entry,
                          min_stop_distance_fraction=min_stop_distance_fraction,
                          required_stop_ticks=required_stop_ticks,
                          actual_stop_ticks=round(actual_stop_distance_krw / step),
                          min_stop_distance_krw=min_stop_distance_krw,
                          structure=structure)
        entry_fill = entry * (1 + self.costs['entry_slippage'])
        buy_outlay = entry_fill * (1 + self.costs['buy'])
        sell_multiplier = (1 - self.costs['exit_slippage']) * (1 - self.costs['sell'] - self.costs['tax'])
        # Prices use ask and exit-bid hypotheses; spread is already inside these
        # prices and is not charged again as a separate percentage fee.
        reward = target * sell_multiplier - buy_outlay
        risk = buy_outlay - emergency * sell_multiplier
        net_rr = reward / risk if risk > 0 else None
        target_known=max(known_at,stamp(peak.get('available_at',peak['confirmed_at']))) if peak and intraday_cap else known_at
        geometry = dict(entry=entry, stop=stop, emergency=emergency, target=target,
            net_rr=net_rr, gross_rr=(target - entry) / (entry - emergency),
            reward_net_per_share=reward, risk_net_per_share=risk, risk_budget=risk_budget,
            target_basis=target_basis, target_known_at=target_known.isoformat(),
            pattern_projection=None,
            predecision_day_high=self.get_tape(t.symbol).metadata[-1].get('prior_observed_day_high') if self.get_tape(t.symbol).metadata else None,
            decision_trade_price=t.price,intraday_ceiling=intraday_ceiling,ceiling_source=ceiling_source,
            nearest_confirmed_peak=peak if intraday_cap else None,
            structural_evidence=structure, buffer=buffer, noise_band=noise,
            cost_basis='ASK_AND_EXIT_BID_PLUS_PROPORTIONAL_FEES_TAX_SLIPPAGE_ONCE',
            costs=dict(self.costs), entry_is_filled=False, modeled_entry=entry_fill)
        minimum = getattr(self.cfg, 'min_reward_risk', 1.5)
        if net_rr is None or net_rr < minimum:
            return reject('NET_REWARD_RISK_LOW', min_net_rr=minimum, **geometry)
        risk_qty = math.floor(risk_budget / risk); cash_qty = math.floor(capital_limit / buy_outlay)
        quantity = min(risk_qty, cash_qty)
        if quantity <= 0:return reject('RISK_ZERO_QUANTITY', max_shares_by_risk=risk_qty, max_shares_by_cap=cash_qty, **geometry)
        reason = self._quote(t, now, quantity)
        if reason:return reject(reason, quantity=quantity, **geometry)
        return dict(**{'pass': True}, quantity=quantity, **geometry)

    def evaluate(self, t, watch, capital_limit=81300., risk_budget=1626., ingest=True, received=None, live_authorized=False):
        now = stamp(received) if received is not None else stamp(t.time)
        if now is None:return None
        if ingest and not self.ingest_tick(t, received):return None
        tape = self.get_tape(t.symbol); m = tape.metrics(now)
        # Flow/history remain based on accepted 0B prints. Execution spread is
        # the current paired quote, including a later 0D price change.
        if m:
            spread=(t.ask-t.bid)/t.bid if number(t.bid) and number(t.ask) and 0<t.bid<=t.ask else None
            m={**m,'spread':spread}
        note_metrics=saved_evidence(m)
        if not m or not m['data_fresh']:
            self._note(t, 'UNKNOWN', 'CURRENT_MICRO_TAPE_UNAVAILABLE',metrics=note_metrics,decision_at=now); return None
        reason = self._quote(t, now)
        if reason:self._note(t, 'UNKNOWN', reason,metrics=note_metrics,decision_at=now); return None
        if m['latest_time'] != t.time or m['last_price'] != t.price:
            self._note(t, 'UNKNOWN', 'CURRENT_TRADE_NOT_IN_ACCEPTED_TAPE',metrics=note_metrics,decision_at=now); return None
        exited = self.last_exit_by_symbol.get(t.symbol)
        if exited and exited.date() == now.date() and 0 <= (now - exited).total_seconds() < self.cooldown_seconds:
            self._note(t, 'REJECT', 'SAME_SYMBOL_FILLED_EXIT_COOLDOWN',metrics=note_metrics,decision_at=now); return None
        phase = self.determine_phase(t.time)
        if phase == 'OFF_SESSION' or not (supported(t.time) or after_supported(t.time)):
            self._note(t, 'REJECT', 'OFF_SESSION',metrics=note_metrics,decision_at=now); return None
        regime, reason, details = self.classify_regime(t, m, phase, watch, asof=now)
        if regime != 'A':self._note(t, 'REJECT', reason, regime=regime, details=details,metrics=note_metrics,decision_at=now); return None
        geometry = self.calculate_invalidation_and_target(t, m, watch, capital_limit, risk_budget, asof=now, entry_mode=details.get('entry_mode'), regime_details=details)
        if not geometry.get('pass'):
            self._note(t, 'UNKNOWN' if 'UNKNOWN' in geometry['reason'] or 'UNAVAILABLE' in geometry['reason'] else 'REJECT', geometry['reason'], geometry=geometry,metrics=note_metrics,decision_at=now)
            return None
        actual_order = bool(live_authorized)
        max_chase = getattr(self.cfg, 'max_chase', .005)
        signal = FastSignal(symbol=t.symbol, name=watch.get('name', t.symbol), limit=geometry['entry'],
            stop=geometry['stop'], emergency=geometry['emergency'], target=geometry['target'], quantity=geometry['quantity'],
            net_reward_risk=geometry['net_rr'], gross_reward_risk=geometry['gross_rr'], reward_net_per_share=geometry['reward_net_per_share'],
            risk_net_per_share=geometry['risk_net_per_share'], regime='A', phase=phase, created_at=now,
            max_chase_fraction=max_chase,
            max_chase_price=geometry['structural_evidence']['breakout'] * (1 + max_chase),
            extra=dict(metrics=m, geometry=geometry,
                       actual_order_permission=actual_order,
                       SHADOW_ONLY=not actual_order,
                       profitability_proven=False,
                       strategy_policy='FAST_LIVE_CANDIDATE' if actual_order else 'CAUSAL_FAST_RESEARCH'))
        self.active_signals[t.symbol] = signal
        self.signal_history.append(dict(
            symbol=t.symbol,
            created_at=now.isoformat(),
            limit=signal.limit,
            stop=signal.stop,
            emergency=signal.emergency,
            target=signal.target,
            quantity=signal.quantity,
            net_reward_risk=signal.net_reward_risk,
            phase=phase,
            actual_order_permission=actual_order,
            SHADOW_ONLY=not actual_order,
        ))
        self._note(t, 'SIGNAL', 'FAST_LIVE_CANDIDATE' if actual_order else 'CAUSAL_FAST_RESEARCH_CANDIDATE',
                    geometry=geometry, metrics=note_metrics, actual_order_permission=actual_order, SHADOW_ONLY=not actual_order,decision_at=now)
        return signal

    def evaluate_exit(self, position, t, current_time):
        now = stamp(current_time); opened = stamp(position.get('opened'))
        entry = position.get('avg', position.get('entry')); qty = position.get('qty')
        initial_stop = position.get('initial_stop', position.get('stop')); emergency = position.get('emergency')
        evidence = dict(actual_order_permission=False, SHADOW_ONLY=True, profitability_proven=False)
        if now is None or opened is None or now < opened or not all(number(v) and v > 0 for v in (entry, qty, initial_stop, emergency)):
            return False, 'EXIT_PRICE_OR_RISK_UNKNOWN', evidence
        if position.get('symbol', t.symbol) != t.symbol:return False, 'EXIT_SYMBOL_MISMATCH', evidence
        reason = self._quote(t, now, qty, side='SELL')
        if reason:return False, reason, evidence
        bid = t.bid
        if bid <= emergency:return True, 'EMERGENCY_PLUNGE_EXIT', {**evidence, 'bid': bid, 'emergency': emergency}
        allowance = position.get('risk_budget')
        # Executed avg already includes entry slippage. Current bid is executable;
        # additional exit slippage is a hypothesis, not a second spread deduction.
        modeled_exit = bid * (1 - self.costs['exit_slippage'])
        modeled_loss = (entry * (1 + self.costs['buy']) - modeled_exit * (1 - self.costs['sell'] - self.costs['tax'])) * qty
        if number(allowance) and allowance > 0 and modeled_loss >= allowance:
            return True, 'ACCOUNT_RISK_LIMIT_EXIT', {**evidence, 'modeled_loss': modeled_loss, 'risk_budget': allowance, 'bid': bid}
        rows = [r for r in self.get_tape(t.symbol).ticks if opened <= r[0] <= t.time]
        def below(line):
            return (len(rows) >= 2 and rows[-2][0] < rows[-1][0] == t.time
                    and rows[-1][1] <= line and rows[-2][1] <= line and bid <= line)
        protection = position.get('protection_stop')
        if number(protection) and below(protection):
            return True, 'PROFIT_LOCK_EXIT', {**evidence, 'bid': bid, 'stop': protection, 'basis': 'CONFIRMED_POST_FILL_HIGHER_LOW_FAILED'}
        if below(initial_stop):return True, 'INVALIDATION_STOP_EXIT', {**evidence, 'bid': bid, 'stop': initial_stop, 'basis': 'TWO_INDEPENDENT_TRADE_BREACHES'}
        turns = []
        for row in rows:
            if not turns or row[1] != turns[-1][1]:turns.append(row)
        highs = []; lows = []
        for left, center, right in zip(turns, turns[1:], turns[2:]):
            if center[1] > max(left[1], right[1]):highs.append(center)
            if center[1] < min(left[1], right[1]):lows.append(center)
        holding_seconds = (now - opened).total_seconds()
        diffs = [abs(b[1] - a[1]) for a, b in zip(turns, turns[1:]) if abs(b[1] - a[1]) > 0]
        # A received print difference is not the executable quote unit. Lunit's
        # 12,905 print halved this guard to 5, so a one-quote-step 12,890 ->
        # 12,900 rebound passed the existing two-step requirement. Keep the raw
        # print as evidence, but never let it shrink the entry quote unit.
        min_step = tick_size(entry)
        swing = {'swing_tick_size': min_step, 'swing_tick_basis': 'ENTRY_QUOTE_UNIT',
                 'observed_min_price_change': min(diffs) if diffs else None}
        if (holding_seconds >= 45.0 and len(highs) >= 2 and lows and highs[-1][1] < highs[-2][1]
                and (highs[-2][1] - lows[-1][1]) >= 2 * min_step
                and (highs[-1][1] - lows[-1][1]) >= 2 * min_step
                and below(lows[-1][1])):
            return True, 'MOMENTUM_FAILURE_EXIT', {**evidence, **swing, 'bid': bid,
                'previous_high': highs[-2][1], 'previous_high_at': highs[-2][0].isoformat(),
                'failed_rebound_high': highs[-1][1], 'failed_rebound_at': highs[-1][0].isoformat(),
                'floor': lows[-1][1], 'floor_at': lows[-1][0].isoformat(),
                'confirmation_trades': [{'time': r[0].isoformat(), 'price': r[1]} for r in rows[-2:]]}
        initial_r = entry - emergency
        if initial_r > 0 and t.price - entry >= initial_r and lows:
            floor = lows[-1][1]
            buffer = max(tick_size(floor), t.ask - t.bid, floor * getattr(self.cfg, 'structural_buffer', .001))
            line = floor_tick(floor - buffer)
            cost_floor = entry * (1 + self.costs['buy']) / ((1 - self.costs['sell'] - self.costs['tax']) * (1 - self.costs['exit_slippage']))
            if cost_floor < line < bid and line > (protection if number(protection) else 0):
                position['protection_stop'] = line; position['profit_locked'] = True
        return False, 'HOLD', {**evidence, **swing, 'holding_seconds': (now - opened).total_seconds(), 'bid': bid,
                               'risk_budget_known': number(allowance) and allowance > 0}
