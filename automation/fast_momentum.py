"""Causal FAST research only: no broker, LIVE authorization or price forecasts."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any
from collections import deque, Counter
from statistics import median
import math
from core.util import floor_tick, tick_size
from core.market_time import market_freshness,tick_freshness,MAX_EXCHANGE_LEAD_SECONDS
from automation.session import supported, after_supported


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def stamp(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value if isinstance(value, datetime) and value.tzinfo is not None else None
    except ValueError:
        return None


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
            'cost_per_share': self.reward_net_per_share,
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
        self.day = None
        self.symbol = None
        self.reset_reason = None

    def reset(self, reason='EXPLICIT_GAP'):
        self.ticks.clear(); self.metadata.clear()
        self.last_cumulative = None; self.day = None; self.symbol = None
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
        if not market_freshness(at,receipt,self.stale_seconds,received_at=received_at,
                                received_monotonic=received_monotonic)['fresh']:
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
        direction = None if previous is None else 1 if price > previous else -1 if price < previous else 0
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
            rows = [r for r, _ in eligible if r[0] >= now - timedelta(seconds=seconds)]
            volume = sum(r[2] for r in rows); turnover = sum(r[1] * r[2] for r in rows)
            result.update({f'ticks_{seconds}': len(rows), f'volume_{seconds}': volume, f'turnover_{seconds}': turnover})
            if seconds in (30, 60):
                # First print has unknown direction. Subsequent neutral prints
                # remain in the denominator and count as neither buying nor selling.
                comparable = sum(r[2] for r in rows if r[3] is not None)
                up = sum(r[2] for r in rows if r[3] == 1); down = sum(r[2] for r in rows if r[3] == -1)
                result.update({f'uptick_ratio_{seconds}': up / comparable if comparable else 0.,
                    f'downtick_ratio_{seconds}': down / comparable if comparable else 0.,
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
        return [(row,meta) for row,meta in zip(self.ticks,self.metadata)
                if meta['received_at']<=asof and row[0].date()==asof.date()
                and row[0]<=asof+timedelta(seconds=MAX_EXCHANGE_LEAD_SECONDS)]

    def structure(self, asof, exclude_decision=True):
        """An earlier rise and higher micro-low must already have rebounded."""
        rows = [r for r,_ in self._observed_rows(asof)]
        if exclude_decision and rows:rows = rows[:-1]
        turns = []
        for row in rows:
            if not turns or row[1] != turns[-1][1]:turns.append(row)
        evidence = {'basis': 'PREDECISION_OBSERVED_PULLBACK_REBOUND', 'asof': asof.isoformat(),
                    'price_points': len(turns), 'synthetic_bars_used': False}
        for i in range(len(turns) - 2, 0, -1):
            left, low, right = turns[i - 1:i + 2]
            if not left[1] > low[1] < right[1]:continue
            peak_i = max(range(i), key=lambda n: turns[n][1]); peak = turns[peak_i]
            if peak_i == 0:continue
            origin = min(turns[:peak_i], key=lambda r: r[1])
            if low[1] <= origin[1] or any(r[1] < low[1] for r in turns[i + 1:]):continue
            noise = median([abs(b[1] - a[1]) for a, b in zip(turns, turns[1:])])
            return {**evidence, 'pass': True, 'support': low[1], 'support_at': low[0].isoformat(),
                'confirmation_at': right[0].isoformat(), 'breakout': peak[1], 'breakout_at': peak[0].isoformat(),
                'origin': origin[1], 'origin_at': origin[0].isoformat(), 'observed_noise': noise}
        return {**evidence, 'pass': False, 'reason': 'CONFIRMED_MICRO_SUPPORT_UNKNOWN'}


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
        self.tapes = {}; self.active_signals = {}; self.last_exit_by_symbol = {}; self.last_evaluation = {}
        self.cooldown_seconds = getattr(auto, 'cooldown_seconds', 900.)

    def get_tape(self, symbol):
        if symbol not in self.tapes:self.tapes[symbol] = MicroTape(stale_seconds=getattr(self.cfg, 'stale_seconds', 5))
        return self.tapes[symbol]

    def ingest_tick(self, t, received=None, coverage_gap=False):
        accepted = self.get_tape(t.symbol).ingest(t, received, coverage_gap)
        if not accepted:self._note(t, 'UNKNOWN', self.get_tape(t.symbol).last_rejection)
        return accepted

    def forget(self, symbol, reason='OBSERVATION_COVERAGE_LOST'):
        self.tapes.pop(symbol, None); self.active_signals.pop(symbol, None); self.last_evaluation.pop(symbol, None)

    def reset(self, reason='OBSERVATION_COVERAGE_LOST'):
        for symbol in list(self.tapes):self.forget(symbol, reason)

    def record_exit(self, symbol, filled_at):
        at = stamp(filled_at)
        if at is None:raise ValueError('full exit fill requires aware timestamp')
        self.last_exit_by_symbol[symbol] = at; self.active_signals.pop(symbol, None)

    def _note(self, t, status, reason, **details):
        record = dict(time=t.time.isoformat(), status=status, reason=reason,
                      actual_order_permission=False, SHADOW_ONLY=True)
        record.update(details)
        self.last_evaluation[t.symbol] = record

    def determine_phase(self, at):
        s = at.strftime('%H:%M:%S')
        if '09:00:00' <= s < '09:02:00':return 'OPENING_EARLY'
        if '09:02:00' <= s < '09:05:00':return 'OPENING_BUILD'
        if '09:05:00' <= s < '09:15:00':return 'OPENING_STABILIZE'
        if '09:15:00' <= s < '15:10:00':return 'REGULAR'
        if '15:40:00' <= s < '20:00:00':return 'AFTER'
        return 'OFF_SESSION'

    def classify_regime(self, t, m, phase, watch):
        spread = m.get('spread'); maximum = .004 if phase == 'OPENING_EARLY' else .0035
        if spread is None:return 'C', 'EXECUTABLE_QUOTE_UNKNOWN', {}
        if spread > maximum:return 'C', 'FALSE_BREAKOUT_WIDE_SPREAD', {'spread': spread, 'max_spread': maximum}
        minimum = 4 if phase == 'OPENING_EARLY' else 6
        if m.get('ticks_10', 0) < minimum:return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TICK_VELOCITY', {'min_ticks': minimum}
        turnover = 3000000. if phase == 'OPENING_EARLY' else 5000000.
        if m.get('turnover_10', 0) < turnover:return 'C', 'FALSE_BREAKOUT_INSUFFICIENT_TURNOVER', {'min_turnover': turnover}
        reference = watch.get('reference_price') or watch.get('first_raw_price')
        if number(reference) and reference > 0 and t.price < reference * .985 and m['p_max_60'] < reference * .990:
            return 'D', 'DOWNTREND_COUNTER_BOUNCE', {'reference': reference}
        extension = t.price / m['p_min_60'] - 1; limit = .022 if phase == 'OPENING_EARLY' else .020
        if extension > limit:return 'B', 'EXTENDED_IMPULSE_CHASE_OVEREXTENDED', {'extension': extension}
        if m['uptick_ratio_30'] < .55:return 'B', 'EXTENDED_IMPULSE_MOMENTUM_EXHAUSTION', {'uptick_ratio_30': m['uptick_ratio_30']}
        if m['uptick_ratio_30'] < (.62 if phase == 'OPENING_EARLY' else .65):
            return 'C', 'INSUFFICIENT_UPTICK_DOMINANCE', {'uptick_ratio_30': m['uptick_ratio_30']}
        if t.price < m['p_max_30'] - tick_size(t.price):return 'C', 'MICRO_BREAKOUT_NOT_AT_PEAK', {}
        return 'A', 'EARLY_MOMENTUM_FLOW_PROXY', {}

    def _quote(self, t, now, quantity=1, side='BOTH'):
        at = stamp(getattr(t, 'time', None)); book = stamp(getattr(t, 'book_time', None)); age = getattr(self.cfg, 'stale_seconds', 5)
        if not getattr(t, 'regular', True):return 'NONREGULAR_TRADE_NOT_EXECUTABLE'
        if at is None or not tick_freshness(t,now,age)['fresh']:return 'CURRENT_TRADE_STALE_OR_FUTURE'
        bid, ask = getattr(t, 'bid', None), getattr(t, 'ask', None)
        if not number(bid) or not number(ask) or not 0 < bid <= ask:return 'EXECUTABLE_QUOTE_UNKNOWN'
        if book is None or not market_freshness(book,now,age)['fresh']:
            return 'BOOK_TIMESTAMP_UNKNOWN_OR_STALE'
        if getattr(t, 'depth_available', None) is not True:return 'EXECUTABLE_DEPTH_UNKNOWN'
        names = ('bid_qty',) if side == 'SELL' else ('ask_qty', 'bid_qty')
        if any(not number(getattr(t, name, None)) or getattr(t, name) < quantity for name in names):
            return 'FULL_QUANTITY_DEPTH_INSUFFICIENT'
        return None

    def calculate_invalidation_and_target(self, t, m, watch, capital_limit=81300., risk_budget=1626., asof=None):
        reject = lambda reason, **extra: dict(**{'pass': False}, reason=reason, **extra)
        now = stamp(asof) if asof is not None else stamp(t.time)
        if now is None:return reject('DECISION_TIME_UNKNOWN')
        if not number(capital_limit) or not number(risk_budget) or capital_limit <= 0 or risk_budget <= 0:
            return reject('AVAILABLE_CAPITAL_OR_RISK_UNKNOWN')
        reason = self._quote(t, now)
        if reason:return reject(reason)
        structure = self.get_tape(t.symbol).structure(now)
        if not structure['pass']:return reject(structure['reason'], structure=structure)
        entry = t.ask; breakout = structure['breakout']; support = structure['support']
        if t.price <= breakout or entry <= breakout:return reject('PREDECISION_REACTION_HIGH_NOT_BROKEN', structure=structure)
        if entry / breakout - 1 > .003:return reject('MAX_CHASE_EXCEEDED', breakout=breakout, entry=entry)
        overhead = watch.get('resistance'); known_at = stamp(watch.get('resistance_asof') or watch.get('observed_at'))
        if not number(overhead) or overhead <= entry or known_at is None or known_at > now or known_at.date() != now.date():
            return reject('KNOWN_OVERHEAD_TARGET_UNAVAILABLE', structure=structure)
        peak = watch.get('nearest_confirmed_peak')
        intraday_cap = None
        if peak and isinstance(peak, dict):
            peak_price = peak.get('price')
            peak_conf = stamp(peak.get('confirmed_at'))
            peak_cleared = stamp(peak.get('cleared_at')) if peak.get('cleared_at') else None
            if (number(peak_price) and peak_price > entry
                    and peak_conf and peak_conf <= now and peak_conf.date() == now.date()
                    and (peak_cleared is None or peak_cleared > now)):
                intraday_cap = peak_price
                target = floor_tick(min(overhead, peak_price))
            else:
                target = floor_tick(overhead)
        else:
            target = floor_tick(overhead)
        # Never move a structural line to a fixed percentage or solve a target
        # backwards from desired RR. Size from the separately derived hard line.
        buffer = max(tick_size(support), t.ask - t.bid, support * getattr(self.cfg, 'structural_buffer', .001))
        stop = floor_tick(support - buffer)
        noise = max(structure['observed_noise'], t.ask - t.bid, tick_size(support))
        emergency = floor_tick(stop - noise)
        if not 0 < emergency < stop < entry < target:return reject('INVALID_STRUCTURAL_PRICE_GEOMETRY', structure=structure)
        entry_fill = entry * (1 + self.costs['entry_slippage'])
        buy_outlay = entry_fill * (1 + self.costs['buy'])
        sell_multiplier = (1 - self.costs['exit_slippage']) * (1 - self.costs['sell'] - self.costs['tax'])
        # Prices use ask and exit-bid hypotheses; spread is already inside these
        # prices and is not charged again as a separate percentage fee.
        reward = target * sell_multiplier - buy_outlay
        risk = buy_outlay - emergency * sell_multiplier
        net_rr = reward / risk if risk > 0 else None
        geometry = dict(entry=entry, stop=stop, emergency=emergency, target=target,
            net_rr=net_rr, gross_rr=(target - entry) / (entry - emergency),
            reward_net_per_share=reward, risk_net_per_share=risk, risk_budget=risk_budget,
            target_basis='KNOWN_RESISTANCE_CEILING_NOT_PRICE_FORECAST', target_known_at=known_at.isoformat(),
            nearest_confirmed_peak=peak if intraday_cap else None,
            structural_evidence=structure, buffer=buffer, noise_band=noise,
            cost_basis='ASK_AND_EXIT_BID_PLUS_PROPORTIONAL_FEES_TAX_SLIPPAGE_ONCE',
            costs=dict(self.costs), entry_is_filled=False, modeled_entry=entry_fill)
        minimum = getattr(self.cfg, 'min_reward_risk', 1.5)
        if net_rr is None or net_rr < minimum:
            reason = 'KNOWN_CEILING_NET_REWARD_RISK_LOW' if (intraday_cap and target == floor_tick(intraday_cap)) else 'NET_REWARD_RISK_LOW'
            return reject(reason, min_net_rr=minimum, **geometry)
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
        if not m or not m['data_fresh']:
            self._note(t, 'UNKNOWN', 'CURRENT_MICRO_TAPE_UNAVAILABLE'); return None
        reason = self._quote(t, now)
        if reason:self._note(t, 'UNKNOWN', reason); return None
        if m['latest_time'] != t.time or m['last_price'] != t.price:
            self._note(t, 'UNKNOWN', 'CURRENT_TRADE_NOT_IN_ACCEPTED_TAPE'); return None
        exited = self.last_exit_by_symbol.get(t.symbol)
        if exited and exited.date() == now.date() and 0 <= (now - exited).total_seconds() < self.cooldown_seconds:
            self._note(t, 'REJECT', 'SAME_SYMBOL_FILLED_EXIT_COOLDOWN'); return None
        phase = self.determine_phase(t.time)
        if phase == 'OFF_SESSION' or not (supported(t.time) or after_supported(t.time)):
            self._note(t, 'REJECT', 'OFF_SESSION'); return None
        regime, reason, details = self.classify_regime(t, m, phase, watch)
        if regime != 'A':self._note(t, 'REJECT', reason, regime=regime, details=details); return None
        geometry = self.calculate_invalidation_and_target(t, m, watch, capital_limit, risk_budget, asof=now)
        if not geometry.get('pass'):
            self._note(t, 'UNKNOWN' if 'UNKNOWN' in geometry['reason'] or 'UNAVAILABLE' in geometry['reason'] else 'REJECT', geometry['reason'], geometry=geometry)
            return None
        actual_order = bool(live_authorized)
        signal = FastSignal(symbol=t.symbol, name=watch.get('name', t.symbol), limit=geometry['entry'],
            stop=geometry['stop'], emergency=geometry['emergency'], target=geometry['target'], quantity=geometry['quantity'],
            net_reward_risk=geometry['net_rr'], gross_reward_risk=geometry['gross_rr'], reward_net_per_share=geometry['reward_net_per_share'],
            risk_net_per_share=geometry['risk_net_per_share'], regime='A', phase=phase, created_at=now,
            max_chase_price=geometry['structural_evidence']['breakout'] * 1.003,
            extra=dict(metrics=m, geometry=geometry,
                       actual_order_permission=actual_order,
                       SHADOW_ONLY=not actual_order,
                       profitability_proven=False,
                       strategy_policy='FAST_LIVE_CANDIDATE' if actual_order else 'CAUSAL_FAST_RESEARCH'))
        self.active_signals[t.symbol] = signal
        self._note(t, 'SIGNAL', 'FAST_LIVE_CANDIDATE' if actual_order else 'CAUSAL_FAST_RESEARCH_CANDIDATE',
                   geometry=geometry, actual_order_permission=actual_order, SHADOW_ONLY=not actual_order)
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
        min_step = min(tick_size(entry), min(diffs)) if diffs else tick_size(entry)
        if (holding_seconds >= 45.0 and len(highs) >= 2 and lows and highs[-1][1] < highs[-2][1]
                and (highs[-2][1] - lows[-1][1]) >= 2 * min_step
                and (highs[-1][1] - lows[-1][1]) >= 2 * min_step
                and below(lows[-1][1])):
            return True, 'MOMENTUM_FAILURE_EXIT', {**evidence, 'bid': bid, 'failed_rebound_high': highs[-1][1], 'floor': lows[-1][1]}
        initial_r = entry - emergency
        if initial_r > 0 and t.price - entry >= initial_r and lows:
            floor = lows[-1][1]
            buffer = max(tick_size(floor), t.ask - t.bid, floor * getattr(self.cfg, 'structural_buffer', .001))
            line = floor_tick(floor - buffer)
            cost_floor = entry * (1 + self.costs['buy']) / ((1 - self.costs['sell'] - self.costs['tax']) * (1 - self.costs['exit_slippage']))
            if cost_floor < line < bid and line > (protection if number(protection) else 0):
                position['protection_stop'] = line; position['profit_locked'] = True
        return False, 'HOLD', {**evidence, 'holding_seconds': (now - opened).total_seconds(), 'bid': bid,
                               'risk_budget_known': number(allowance) and allowance > 0}
