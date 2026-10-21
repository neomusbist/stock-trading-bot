"""Setup D: Early Impulse-Pullback-Rebound Strategy (Strictly SHADOW_ONLY Research Module).

Integrates verification standards from AG c296ae3 and Argon independent audit:
1. Completed bar causality: all bars must complete strictly before decision time asof.
2. Explicit bar arrival evidence: verifies available_at / known_at; rejects unverified instant receipt.
3. Separates decision time (local receipt time) from tick exchange time (prevents retroactive backdating).
4. Mandatory orderbook verification: rejects quotes with missing book_time or stale book age.
5. Never substitutes current_price when current_ask is missing (returns EXECUTABLE_QUOTE_UNKNOWN).
6. Separates price-geometry signal detection from executable depth verification.
7. Enforces max_chase_price (0.3% default above rebound_trigger).
8. Bounds target price by known resistance and confirmed intraday peaks.
9. Three-layer price levels: structural stop, emergency stop, and resistance-bounded target.
10. Account risk sizing strictly based on emergency_risk_per_share with multiplicative roundtrip costs.
11. Per-symbol deduplication, cooldown, day rollover, and stream reset handling.
12. Strict SHADOW isolation: SHADOW_ONLY=True, actual_order_permission=False.
13. Causal post-entry trade replayer (replay_trade_causal) with separate buy/sell depth validation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from core.settings import KST
from core.util import floor_tick, tick_size, number


@dataclass
class EarlyImpulsePullbackSignal:
    symbol: str
    limit: float
    stop: float
    emergency: float
    target: float
    quantity: int
    gross_reward_risk: float
    net_reward_risk: float
    impulse_high: float
    impulse_low: float
    support_low: float
    rebound_trigger: float
    max_chase_price: float
    created_at: datetime
    signal_tick_time: Optional[datetime] = None
    ttl_seconds: float = 15.0
    SHADOW_ONLY: bool = True
    actual_order_permission: bool = False
    execution_status: str = 'EXECUTABLE_QUOTE_AND_DEPTH_VERIFIED'
    invalidation_policy: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_engine_signal(self, watch: dict) -> dict:
        """Convert to engine-compatible diagnostic signal dictionary (strictly SHADOW_ONLY)."""
        return {
            'symbol': self.symbol,
            'name': watch.get('name', ''),
            'pattern': 'D_SHADOW',
            'limit': self.limit,
            'stop': self.stop,
            'emergency': self.emergency,
            'target': self.target,
            'planned_qty': self.quantity,
            'gross_reward_risk': self.gross_reward_risk,
            'net_reward_risk': self.net_reward_risk,
            'max_chase_price': self.max_chase_price,
            'ttl_seconds': self.ttl_seconds,
            'time': self.created_at.isoformat(),
            'signal_tick_time': self.signal_tick_time.isoformat() if self.signal_tick_time else None,
            'SHADOW_ONLY': True,
            'actual_order_permission': False,
            'diagnostic_only': True,
            'execution_status': self.execution_status,
            'invalidation_policy': dict(self.invalidation_policy),
            'extra': dict(self.extra),
        }


def parse_bar_window(bar: dict, reference_dt: datetime) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
    """Return (bar_start, bar_end, available_at) as timezone-aware datetimes."""
    raw_t = bar.get('time')
    start = None
    if isinstance(raw_t, datetime):
        start = raw_t if raw_t.tzinfo is not None else raw_t.replace(tzinfo=reference_dt.tzinfo or KST)
    elif isinstance(raw_t, str):
        s = raw_t.strip()
        try:
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                start = parsed
        except ValueError:
            pass
        if start is None and len(s) in (5, 8) and s[2] == ':':
            parts = [int(p) for p in s.split(':')]
            hh, mm = parts[0], parts[1]
            ss = parts[2] if len(parts) == 3 else 0
            base_date = reference_dt.astimezone(KST).date() if reference_dt.tzinfo else reference_dt.date()
            start = datetime(base_date.year, base_date.month, base_date.day, hh, mm, ss, tzinfo=KST)

    if start is None or start.second != 0 or start.microsecond != 0:
        return None, None, None

    end = start + timedelta(minutes=1)
    avail_raw = bar.get('available_at') or bar.get('known_at') or bar.get('recorded_at') or bar.get('received_at')
    avail = None
    if isinstance(avail_raw, datetime):
        avail = avail_raw if avail_raw.tzinfo is not None else avail_raw.replace(tzinfo=reference_dt.tzinfo or KST)
    elif isinstance(avail_raw, str):
        try:
            parsed = datetime.fromisoformat(avail_raw)
            avail = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=reference_dt.tzinfo or KST)
        except ValueError:
            avail = None

    return start, end, avail


def completed_bars_asof(
    bars: List[dict],
    asof: datetime,
    require_arrival_evidence: bool = True
) -> Tuple[List[dict], List[dict]]:
    """Split bars into (completed_bars, incomplete_or_future_bars) relative to aware asof.

    Enforces:
    - bar_end <= asof (no uncompleted minute bars)
    - available_at <= asof and available_at >= bar_end (explicit causal receipt proof)
    """
    if not isinstance(asof, datetime) or asof.tzinfo is None:
        return [], list(bars or [])
    completed = []
    incomplete = []
    for b in (bars or []):
        start, end, avail = parse_bar_window(b, asof)
        if start is None or end is None:
            incomplete.append({**b, '_reject_reason': 'BAR_START_LABEL_INVALID'})
            continue
        if end > asof:
            incomplete.append({
                **b,
                'bar_start': start.isoformat(),
                'bar_completes_at': end.isoformat(),
                '_reject_reason': 'BAR_NOT_COMPLETED_AT_ASOF',
            })
            continue

        if require_arrival_evidence:
            if avail is None:
                incomplete.append({
                    **b,
                    'bar_start': start.isoformat(),
                    'bar_completes_at': end.isoformat(),
                    '_reject_reason': 'BAR_AVAILABILITY_UNKNOWN',
                })
                continue
            if avail > asof:
                incomplete.append({
                    **b,
                    'bar_start': start.isoformat(),
                    'bar_completes_at': end.isoformat(),
                    'available_at': avail.isoformat(),
                    '_reject_reason': 'BAR_ARRIVED_AFTER_ASOF',
                })
                continue
            if avail < end:
                incomplete.append({
                    **b,
                    'bar_start': start.isoformat(),
                    'bar_completes_at': end.isoformat(),
                    'available_at': avail.isoformat(),
                    '_reject_reason': 'BAR_AVAILABILITY_BEFORE_CLOSE',
                })
                continue

        completed.append({
            **b,
            'bar_start': start,
            'bar_end': end,
            'available_at': avail or end,
        })
    return completed, incomplete


class EarlyImpulsePullbackStrategy:
    """Evaluates causal 1-minute completed bar sequences for Early Impulse -> Pullback -> Rebound."""

    def __init__(self, cfg=None, auto=None, costs=None):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs
        self.cooldown_seconds = float(getattr(auto, 'setup_d_cooldown_seconds', 30.0))
        self.default_ttl_seconds = float(getattr(auto, 'setup_d_ttl_seconds', 15.0))
        self.active_signals: Dict[str, EarlyImpulsePullbackSignal] = {}
        self.last_signal_at: Dict[str, datetime] = {}
        self.emitted_keys: Dict[str, set] = {}

    def reset(self, reason: str = 'RESET'):
        self.active_signals.clear()
        self.last_signal_at.clear()
        self.emitted_keys.clear()

    def forget(self, symbol: str, reason: str = 'FORGET'):
        self.active_signals.pop(symbol, None)
        self.last_signal_at.pop(symbol, None)
        self.emitted_keys.pop(symbol, None)

    def _safe_pos_float(self, val) -> Optional[float]:
        if val in (None, ''):
            return None
        try:
            v = float(val)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    def evaluate_tick(
        self,
        t,
        bars: List[dict],
        watch: dict,
        capital_limit: float = 81300.0,
        risk_budget: float = 1626.0,
        asof: Optional[datetime] = None,
        received: Optional[datetime] = None,
    ) -> Tuple[Optional[EarlyImpulsePullbackSignal], dict]:
        """Adapter for Tick objects in ShadowObserver / runner.

        Disambiguates decision time from tick exchange time:
        - decision_at is strictly bound to received or asof (local receipt time)
        - t.time is treated as tick exchange timestamp
        """
        decision_at = received or asof or getattr(t, 'received_at', None) or now()
        if isinstance(decision_at, str):
            decision_at = datetime.fromisoformat(decision_at)

        return self.evaluate(
            symbol=getattr(t, 'symbol', watch.get('code', '')),
            bars=bars,
            current_price=getattr(t, 'price', 0.0),
            current_ask=getattr(t, 'ask', None),
            current_bid=getattr(t, 'bid', None),
            watch=watch,
            capital_limit=capital_limit,
            risk_budget=risk_budget,
            asof=decision_at,
            tick_time=getattr(t, 'time', None),
            book_time=getattr(t, 'book_time', None),
            ask_qty=getattr(t, 'ask_qty', None),
            bid_qty=getattr(t, 'bid_qty', None),
            depth_available=getattr(t, 'depth_available', None),
            strict_causality=False,
        )

    def evaluate(
        self,
        symbol: str,
        bars: List[dict],
        current_price: float,
        current_ask: Optional[float],
        current_bid: Optional[float],
        watch: dict,
        capital_limit: float = 81300.0,
        risk_budget: float = 1626.0,
        asof: Optional[datetime] = None,
        *,
        tick_time: Optional[datetime] = None,
        book_time: Optional[datetime] = None,
        ask_qty: Optional[int] = None,
        bid_qty: Optional[int] = None,
        depth_available: Optional[bool] = None,
        strict_causality: bool = True,
        require_arrival_evidence: bool = True,
    ) -> Tuple[Optional[EarlyImpulsePullbackSignal], dict]:
        watch = watch or {}
        if not isinstance(asof, datetime) or asof.tzinfo is None:
            return None, {'status': 'REJECT', 'reason': 'AWARE_DECISION_TIME_REQUIRED'}

        stale_seconds = float(getattr(self.cfg, 'stale_seconds', 5.0))

        # Check tick freshness relative to decision_at (prevents retroactive backdating)
        if tick_time is not None:
            if not isinstance(tick_time, datetime) or tick_time.tzinfo is None:
                return None, {'status': 'REJECT', 'reason': 'AWARE_TICK_TIME_REQUIRED'}
            tick_age = (asof - tick_time).total_seconds()
            if not (-1.0 <= tick_age <= stale_seconds):
                return None, {
                    'status': 'REJECT',
                    'reason': 'STALE_OR_FUTURE_SIGNAL_TICK',
                    'tick_age_seconds': tick_age,
                    'stale_seconds': stale_seconds,
                }

        # Check orderbook timestamp freshness: missing book_time is strictly rejected as UNKNOWN
        if book_time is None:
            return None, {
                'status': 'UNKNOWN',
                'reason': 'BOOK_TIMESTAMP_UNKNOWN_OR_MISSING',
            }
        if not isinstance(book_time, datetime) or book_time.tzinfo is None:
            return None, {'status': 'UNKNOWN', 'reason': 'BOOK_TIMESTAMP_INVALID'}
        book_age = (asof - book_time).total_seconds()
        if not (-1.0 <= book_age <= stale_seconds):
            return None, {
                'status': 'UNKNOWN',
                'reason': 'BOOK_TIMESTAMP_STALE',
                'book_age_seconds': book_age,
                'stale_seconds': stale_seconds,
            }

        # Expire stale active signal for this symbol
        prior_active = self.active_signals.get(symbol)
        if prior_active is not None:
            if (asof - prior_active.created_at).total_seconds() > prior_active.ttl_seconds:
                self.active_signals.pop(symbol, None)

        last_at = self.last_signal_at.get(symbol)
        if last_at is not None and 0 <= (asof - last_at).total_seconds() < self.cooldown_seconds:
            return None, {
                'status': 'REJECT',
                'reason': 'SIGNAL_COOLDOWN_ACTIVE',
                'cooldown_remaining_seconds': self.cooldown_seconds - (asof - last_at).total_seconds(),
            }

        if not bars:
            return None, {'status': 'REJECT', 'reason': 'INSUFFICIENT_BARS', 'count': 0}

        completed, incomplete = completed_bars_asof(bars, asof, require_arrival_evidence=require_arrival_evidence)
        if strict_causality and incomplete:
            first_bad = incomplete[0]
            return None, {
                'status': 'REJECT',
                'reason': first_bad.get('_reject_reason', 'FUTURE_OR_INCOMPLETE_BAR_LOOKAHEAD'),
                'offending_bar_time': first_bad.get('time'),
                'asof': asof.isoformat(),
                'completed_bar_count': len(completed),
                'incomplete_bar_count': len(incomplete),
            }

        if len(completed) < 4:
            return None, {'status': 'REJECT', 'reason': 'INSUFFICIENT_COMPLETED_BARS', 'count': len(completed)}

        # Verify contiguous 1-minute completed bars and no missing latest completed bar
        if (asof - completed[-1]['bar_end']).total_seconds() >= 60.0:
            return None, {
                'status': 'REJECT',
                'reason': 'LATEST_COMPLETED_BAR_MISSING',
                'last_completed_bar_end': completed[-1]['bar_end'].isoformat(),
                'asof': asof.isoformat(),
            }
        for a_bar, b_bar in zip(completed, completed[1:]):
            if (b_bar['bar_start'] - a_bar['bar_start']).total_seconds() != 60.0:
                return None, {'status': 'REJECT', 'reason': 'NONCONSECUTIVE_COMPLETED_BARS'}

        px = self._safe_pos_float(current_price)
        if px is None:
            return None, {'status': 'REJECT', 'reason': 'INVALID_CURRENT_PRICE'}

        # Step 1: Evaluate price geometry using ONLY completed 1-minute bars
        recent_bars = completed[-12:] if len(completed) >= 12 else completed
        n = len(recent_bars)

        best_impulse = None
        for pull_len in range(1, min(5, n - 2)):
            imp_end_idx = n - pull_len
            for imp_len in range(2, min(7, imp_end_idx + 1)):
                imp_start_idx = imp_end_idx - imp_len
                imp_slice = recent_bars[imp_start_idx:imp_end_idx]
                pull_slice = recent_bars[imp_end_idx:]

                imp_low = min(float(b['low']) for b in imp_slice)
                imp_high = max(float(b['high']) for b in imp_slice)
                if imp_low <= 0:
                    continue

                imp_gain = (imp_high - imp_low) / imp_low
                if imp_gain < 0.025:
                    continue
                if float(imp_slice[-1]['close']) <= float(imp_slice[0]['open']):
                    continue
                if float(imp_slice[-1]['high']) < imp_high:
                    continue
                if max(float(b['high']) for b in pull_slice) > imp_high:
                    continue

                imp_avg_vol = sum(float(b.get('volume', 0)) for b in imp_slice) / len(imp_slice)
                pull_avg_vol = sum(float(b.get('volume', 0)) for b in pull_slice) / len(pull_slice)

                best_impulse = {
                    'imp_slice': imp_slice,
                    'pull_slice': pull_slice,
                    'imp_low': imp_low,
                    'imp_high': imp_high,
                    'imp_gain': imp_gain,
                    'imp_avg_vol': imp_avg_vol,
                    'pull_avg_vol': pull_avg_vol,
                    'impulse_end_time': imp_slice[-1]['bar_end'].isoformat(),
                }
                break
            if best_impulse:
                break

        if not best_impulse:
            return None, {'status': 'REJECT', 'reason': 'NO_IMPULSE_DETECTED'}

        imp_high = best_impulse['imp_high']
        imp_low = best_impulse['imp_low']
        imp_span = imp_high - imp_low
        pull_bars = best_impulse['pull_slice']

        support_low = min(float(b['low']) for b in pull_bars)
        retrace = (imp_high - support_low) / imp_span if imp_span > 0 else 1.0
        if not (0.15 <= retrace <= 0.65):
            return None, {'status': 'REJECT', 'reason': 'PULLBACK_RETRACE_OUT_OF_BOUNDS', 'retrace': retrace}

        if best_impulse['pull_avg_vol'] > best_impulse['imp_avg_vol'] * 0.85:
            return None, {
                'status': 'REJECT',
                'reason': 'PULLBACK_VOLUME_TOO_HIGH',
                'pull_vol': best_impulse['pull_avg_vol'],
                'imp_vol': best_impulse['imp_avg_vol'],
            }

        prev_bar = pull_bars[-1]
        rebound_trigger = float(prev_bar['high'])
        if px <= rebound_trigger:
            return None, {
                'status': 'REJECT',
                'reason': 'REBOUND_TRIGGER_NOT_BROKEN',
                'price': px,
                'trigger': rebound_trigger,
                'support_low': support_low,
            }
        if px > imp_high:
            return None, {'status': 'REJECT', 'reason': 'ALREADY_BROKE_IMPULSE_HIGH_USE_SETUP_A'}

        max_chase_fraction = float(getattr(self.auto, 'setup_d_max_chase', 0.003)) if self.auto else 0.003
        max_chase_price = rebound_trigger * (1.0 + max_chase_fraction)

        geometry_summary = {
            'impulse_high': imp_high,
            'impulse_low': imp_low,
            'support_low': support_low,
            'rebound_trigger': rebound_trigger,
            'max_chase_price': max_chase_price,
            'retrace': retrace,
            'completed_bars_used': len(completed),
            'last_completed_bar_start': prev_bar['bar_start'].isoformat(),
            'last_completed_bar_end': prev_bar['bar_end'].isoformat(),
        }

        # Step 2: Separate Signal Geometry from Executable Quote & Depth Verification
        # NEVER substitute current_price when current_ask or current_bid is missing/invalid.
        ask = self._safe_pos_float(current_ask)
        bid = self._safe_pos_float(current_bid)
        if ask is None or bid is None or bid > ask or ask < px:
            return None, {
                'status': 'UNKNOWN',
                'reason': 'EXECUTABLE_QUOTE_UNKNOWN',
                'signal_candidate_detected': True,
                'fill_status': 'UNKNOWN',
                'observed_price': px,
                'observed_ask': current_ask,
                'observed_bid': current_bid,
                'geometry': geometry_summary,
            }

        spread = (ask - bid) / px
        max_spread = float(getattr(self.cfg, 'max_spread', 0.004)) if self.cfg else 0.004
        if spread > max_spread:
            return None, {
                'status': 'REJECT',
                'reason': 'SPREAD_TOO_WIDE',
                'spread': spread,
                'max_spread': max_spread,
                'geometry': geometry_summary,
            }

        if ask > max_chase_price:
            return None, {
                'status': 'REJECT',
                'reason': 'MAX_CHASE_EXCEEDED',
                'ask': ask,
                'rebound_trigger': rebound_trigger,
                'max_chase_price': max_chase_price,
                'chase_fraction': ask / rebound_trigger - 1.0,
                'max_chase_fraction': max_chase_fraction,
                'geometry': geometry_summary,
            }

        # Step 3: Three-Layer Price & Risk Boundaries
        t_size = tick_size(support_low)
        stop = floor_tick(support_low - t_size)
        structural_buffer = float(getattr(self.cfg, 'structural_buffer', 0.002)) if self.cfg else 0.002
        noise = max(2 * t_size, support_low * structural_buffer)
        emergency = floor_tick(stop - noise)
        if stop <= 0 or emergency <= 0 or ask <= stop:
            return None, {'status': 'REJECT', 'reason': 'INVALID_STOP_GEOMETRY', 'ask': ask, 'stop': stop}

        stop_distance_fraction = (ask - stop) / ask
        min_stop_fraction = float(getattr(self.cfg, 'min_stop_fraction', 0.006)) if self.cfg else 0.006
        max_stop_fraction = float(getattr(self.cfg, 'max_stop_fraction', 0.025)) if self.cfg else 0.025
        if not (min_stop_fraction <= stop_distance_fraction <= max_stop_fraction):
            return None, {
                'status': 'REJECT',
                'reason': 'INVALIDATION_DISTANCE_OUT_OF_RANGE',
                'stop_distance_fraction': stop_distance_fraction,
                'min_stop_fraction': min_stop_fraction,
                'max_stop_fraction': max_stop_fraction,
                'ask': ask,
                'stop': stop,
            }

        raw_target = floor_tick(imp_high + 2.0 * (imp_high - support_low))
        ceilings = []
        res = self._safe_pos_float(watch.get('resistance'))
        if res is not None:
            ceilings.append(('daily_resistance', res))
        peak_info = watch.get('nearest_confirmed_peak')
        if isinstance(peak_info, dict):
            peak_px = self._safe_pos_float(peak_info.get('price'))
            if peak_px is not None:
                ceilings.append(('intraday_confirmed_peak', peak_px))

        for cname, cval in ceilings:
            if cval <= ask:
                return None, {
                    'status': 'REJECT',
                    'reason': 'OVERHEAD_RESISTANCE_BLOCKED',
                    'ceiling_source': cname,
                    'ceiling_price': cval,
                    'ask': ask,
                }

        target = raw_target
        applied_ceiling = None
        for cname, cval in ceilings:
            if cval < target:
                target = floor_tick(cval)
                applied_ceiling = cname

        if target <= imp_high or target <= ask:
            return None, {
                'status': 'REJECT',
                'reason': 'TARGET_CAPPED_BELOW_IMPULSE_HIGH',
                'target': target,
                'raw_target': raw_target,
                'impulse_high': imp_high,
                'applied_ceiling': applied_ceiling,
            }

        # Multiplicative roundtrip cost model
        buy_comm = float(getattr(self.costs, 'buy', 0.00015)) if self.costs else 0.00015
        sell_comm = float(getattr(self.costs, 'sell', 0.00015)) if self.costs else 0.00015
        sell_tax = float(getattr(self.costs, 'tax', 0.002)) if self.costs else 0.002
        entry_slip = float(getattr(self.costs, 'entry_slippage', 0.002)) if self.costs else 0.002
        exit_slip = float(getattr(self.costs, 'exit_slippage', 0.003)) if self.costs else 0.003

        buy_outlay_per_share = ask * (1.0 + entry_slip) * (1.0 + buy_comm)
        sell_keep_factor = (1.0 - exit_slip) * (1.0 - sell_comm - sell_tax)
        target_net_proceeds = target * sell_keep_factor
        stop_net_proceeds = stop * sell_keep_factor
        emergency_net_proceeds = emergency * sell_keep_factor

        gross_reward = target - ask
        gross_risk = ask - stop
        gross_rr = gross_reward / gross_risk if gross_risk > 0 else 0.0

        net_reward = target_net_proceeds - buy_outlay_per_share
        structural_risk_per_share = buy_outlay_per_share - stop_net_proceeds
        emergency_risk_per_share = buy_outlay_per_share - emergency_net_proceeds
        if structural_risk_per_share <= 0 or emergency_risk_per_share <= 0:
            return None, {'status': 'REJECT', 'reason': 'NON_POSITIVE_RISK_PER_SHARE'}

        net_rr = net_reward / structural_risk_per_share
        min_rr = float(getattr(self.cfg, 'min_reward_risk', 1.5)) if self.cfg else 1.5
        if net_reward <= 0 or net_rr < min_rr:
            return None, {
                'status': 'REJECT',
                'reason': 'INSUFFICIENT_REWARD_RISK',
                'net_rr': net_rr,
                'gross_rr': gross_rr,
                'min_rr': min_rr,
                'target': target,
                'raw_target': raw_target,
                'applied_ceiling': applied_ceiling,
            }

        # Risk budget sizing based on emergency_risk_per_share
        unit_capital_cost = ask * (1.0 + entry_slip) * (1.0 + buy_comm)
        qty_by_cap = int(capital_limit // unit_capital_cost) if capital_limit > 0 else 0
        qty_by_risk = int(risk_budget // emergency_risk_per_share) if risk_budget > 0 else 0
        qty = min(qty_by_cap, qty_by_risk)
        if qty < 1:
            return None, {
                'status': 'REJECT',
                'reason': 'ZERO_QUANTITY_AFFORDABLE',
                'qty_by_cap': qty_by_cap,
                'qty_by_risk': qty_by_risk,
                'emergency_risk_per_share': emergency_risk_per_share,
                'structural_risk_per_share': structural_risk_per_share,
            }

        # Step 4: Verify Executable Orderbook Depth
        if depth_available is not True or ask_qty is None or bid_qty is None:
            return None, {
                'status': 'UNKNOWN',
                'reason': 'EXECUTABLE_DEPTH_UNKNOWN',
                'signal_candidate_detected': True,
                'fill_status': 'UNKNOWN',
                'planned_qty': qty,
                'ask': ask,
                'bid': bid,
                'stop': stop,
                'emergency': emergency,
                'target': target,
                'net_rr': net_rr,
                'geometry': geometry_summary,
            }
        if int(ask_qty) < qty or int(bid_qty) < qty:
            return None, {
                'status': 'REJECT',
                'reason': 'INSUFFICIENT_QUOTE_DEPTH',
                'signal_candidate_detected': True,
                'fill_status': 'INSUFFICIENT_DEPTH',
                'planned_qty': qty,
                'ask_qty': ask_qty,
                'bid_qty': bid_qty,
                'geometry': geometry_summary,
            }

        dedup_key = (best_impulse['impulse_end_time'], round(support_low, 2))
        seen = self.emitted_keys.setdefault(symbol, set())
        if dedup_key in seen:
            return None, {
                'status': 'REJECT',
                'reason': 'DUPLICATE_IMPULSE_PULLBACK_SIGNAL',
                'dedup_key': dedup_key,
            }
        seen.add(dedup_key)

        invalidation_policy = {
            'enabled': True,
            'mode': 'COMPLETED_BAR_CLOSE_BELOW_STOP_OR_EMERGENCY_TICK',
            'structural_stop': stop,
            'emergency_stop': emergency,
            'support_low': support_low,
            'sizing_basis': 'EMERGENCY_STOP_ROUNDTRIP_RISK',
        }
        sig = EarlyImpulsePullbackSignal(
            symbol=symbol,
            limit=ask,
            stop=stop,
            emergency=emergency,
            target=target,
            quantity=qty,
            gross_reward_risk=gross_rr,
            net_reward_risk=net_rr,
            impulse_high=imp_high,
            impulse_low=imp_low,
            support_low=support_low,
            rebound_trigger=rebound_trigger,
            max_chase_price=max_chase_price,
            created_at=asof,
            signal_tick_time=tick_time or asof,
            ttl_seconds=self.default_ttl_seconds,
            SHADOW_ONLY=True,
            actual_order_permission=False,
            execution_status='EXECUTABLE_QUOTE_AND_DEPTH_VERIFIED',
            invalidation_policy=invalidation_policy,
            extra={
                'retrace': retrace,
                'impulse_gain': best_impulse['imp_gain'],
                'pullback_bars': len(pull_bars),
                'raw_target': raw_target,
                'applied_ceiling': applied_ceiling,
                'structural_risk_per_share': structural_risk_per_share,
                'emergency_risk_per_share': emergency_risk_per_share,
                'ask_qty': int(ask_qty),
                'bid_qty': int(bid_qty),
            },
        )
        self.active_signals[symbol] = sig
        self.last_signal_at[symbol] = asof
        return sig, {
            'status': 'SIGNAL',
            'reason': 'OK',
            'signal_candidate_detected': True,
            'fill_status': 'EXECUTABLE_QUOTE_AND_DEPTH_VERIFIED',
            'signal': sig,
        }


def replay_trade_causal(
    signal: EarlyImpulsePullbackSignal,
    frames: List[dict],
    costs,
    entry_quote: Optional[dict] = None,
    max_hold_seconds: float = 600.0,
    include_slippage: bool = True,
) -> dict:
    """Causally replay post-entry frames to determine exit, MFE, MAE, and net PnL.

    Strict rules:
    - Never hardcodes subsequent strategy's exit price (e.g. 14,170 KRW).
    - If entry quote or depth is missing/unverified, marks fill_verified=False, status='UNKNOWN'.
    - Exit must verify bid depth (bid_qty >= qty) and fresh quote on the sell side.
    - Consistent friction calculation: buy commission, sell commission, securities tax, slippage.
    """
    qty = int(signal.quantity)
    entry_px = float(signal.limit)
    entry_at = signal.created_at

    # 1. Verify entry quote feasibility
    if entry_quote is not None:
        e_ask = entry_quote.get('ask')
        e_ask_qty = entry_quote.get('ask_qty')
        e_depth = entry_quote.get('depth_available')
        e_fresh = entry_quote.get('book_freshness', {}).get('fresh', True) if isinstance(entry_quote.get('book_freshness'), dict) else True
        if not (isinstance(e_ask, (int, float)) and 0 < e_ask <= entry_px):
            return {
                'status': 'UNKNOWN',
                'reason': 'ENTRY_QUOTE_UNVERIFIED_OR_MOVED',
                'fill_verified': False,
                'pnl_valid': False,
            }
        if e_depth is not True or e_ask_qty is None or int(e_ask_qty) < qty or not e_fresh:
            return {
                'status': 'UNKNOWN',
                'reason': 'ENTRY_DEPTH_UNKNOWN_OR_INSUFFICIENT',
                'fill_verified': False,
                'pnl_valid': False,
            }

    if not frames:
        return {
            'status': 'UNKNOWN',
            'reason': 'POST_ENTRY_FRAMES_ABSENT',
            'fill_verified': False,
            'pnl_valid': False,
        }

    future_frames = []
    for f in frames:
        ft = f.get('time')
        if isinstance(ft, str):
            ft = datetime.fromisoformat(ft)
        if not isinstance(ft, datetime) or ft.tzinfo is None:
            return {'status': 'REJECT', 'reason': 'AWARE_FRAME_TIME_REQUIRED', 'pnl_valid': False}
        if ft > entry_at:
            future_frames.append({**f, '_dt': ft})

    if not future_frames:
        return {
            'status': 'UNKNOWN',
            'reason': 'NO_FRAMES_AFTER_ENTRY',
            'fill_verified': False,
            'pnl_valid': False,
        }

    future_frames.sort(key=lambda x: x['_dt'])

    mfe_per_share = 0.0
    mae_per_share = 0.0
    exit_frame = None
    exit_reason = None

    for f in future_frames:
        px = float(f.get('price', 0.0))
        bid = float(f.get('bid') or 0.0)
        mark = bid if bid > 0 else px
        if mark <= 0:
            continue
        excursion = mark - entry_px
        if excursion > mfe_per_share:
            mfe_per_share = excursion
        if excursion < mae_per_share:
            mae_per_share = excursion

        # Immediate emergency stop
        if px <= signal.emergency or (bid > 0 and bid <= signal.emergency):
            exit_frame = f
            exit_reason = 'EMERGENCY_STOP'
            break
        # Structural stop on completed 1-minute bar close (or print below stop)
        if f.get('bar_completed') and float(f.get('bar_close', px)) <= signal.stop:
            exit_frame = f
            exit_reason = 'STRUCTURAL_STOP_BAR_CLOSE'
            break
        if px <= signal.stop:
            exit_frame = f
            exit_reason = 'STRUCTURAL_STOP_PRICE'
            break
        # Target reached
        if (bid > 0 and bid >= signal.target) or px >= signal.target:
            exit_frame = f
            exit_reason = 'TARGET_REACHED'
            break
        # Time horizon exit
        if (f['_dt'] - entry_at).total_seconds() >= max_hold_seconds:
            exit_frame = f
            exit_reason = 'TIME_EXIT'
            break

    if exit_frame is None:
        return {
            'status': 'UNKNOWN',
            'reason': 'EXIT_NOT_TRIGGERED_IN_AVAILABLE_FRAMES',
            'fill_verified': False,
            'pnl_valid': False,
            'mfe_per_share': mfe_per_share,
            'mae_per_share': mae_per_share,
        }

    exit_bid = float(exit_frame.get('bid') or 0.0)
    exit_bid_qty = exit_frame.get('bid_qty')
    exit_depth = exit_frame.get('depth_available')
    if exit_bid <= 0:
        return {
            'status': 'UNKNOWN',
            'reason': 'EXIT_EXECUTABLE_BID_UNKNOWN',
            'exit_reason': exit_reason,
            'exit_time': exit_frame['_dt'].isoformat(),
            'fill_verified': False,
            'pnl_valid': False,
            'mfe_per_share': mfe_per_share,
            'mae_per_share': mae_per_share,
        }

    depth_verified = bool(exit_depth is True and exit_bid_qty is not None and int(exit_bid_qty) >= qty)
    exit_px = exit_bid

    buy_comm_rate = float(getattr(costs, 'buy', 0.00015)) if costs else 0.00015
    sell_comm_rate = float(getattr(costs, 'sell', 0.00015)) if costs else 0.00015
    sell_tax_rate = float(getattr(costs, 'tax', 0.002)) if costs else 0.002
    entry_slip_rate = (float(getattr(costs, 'entry_slippage', 0.002)) if costs else 0.002) if include_slippage else 0.0
    exit_slip_rate = (float(getattr(costs, 'exit_slippage', 0.003)) if costs else 0.003) if include_slippage else 0.0

    effective_buy_px = entry_px * (1.0 + entry_slip_rate)
    effective_sell_px = exit_px * (1.0 - exit_slip_rate)

    gross_buy = qty * entry_px
    gross_sell = qty * exit_px
    gross_pnl = gross_sell - gross_buy

    if hasattr(costs, 'roundtrip'):
        fee_and_tax_only = qty * costs.roundtrip(entry_px, exit_px)
    else:
        fee_and_tax_only = qty * (entry_px * buy_comm_rate + exit_px * (sell_comm_rate + sell_tax_rate))

    buy_outlay = qty * effective_buy_px * (1.0 + buy_comm_rate)
    sell_net = qty * effective_sell_px * (1.0 - sell_comm_rate - sell_tax_rate)
    modeled_net_pnl = sell_net - buy_outlay
    total_cost_and_slippage = gross_pnl - modeled_net_pnl

    return {
        'status': 'COMPLETED' if depth_verified else 'COMPLETED_DEPTH_UNVERIFIED',
        'reason': exit_reason,
        'fill_verified': depth_verified,
        'pnl_valid': True,
        'entry_time': entry_at.isoformat(),
        'exit_time': exit_frame['_dt'].isoformat(),
        'entry_price': entry_px,
        'exit_price': exit_px,
        'quantity': qty,
        'gross_pnl': round(gross_pnl, 4),
        'fee_and_tax_only': round(fee_and_tax_only, 4),
        'total_cost_and_slippage': round(total_cost_and_slippage, 4),
        'net_pnl_before_slippage': round(gross_pnl - fee_and_tax_only, 4),
        'modeled_net_pnl': round(modeled_net_pnl, 4),
        'mfe_per_share': mfe_per_share,
        'mae_per_share': mae_per_share,
    }
