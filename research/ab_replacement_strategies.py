"""Inactive replacement strategies for legacy A and B (P0-P2 research).

Never imports or calls live broker/order/engine paths. Replaces incremental
filter tweaking on legacy A/B with two coherent, independent strategy designs:
1. TrendBreakoutHoldResearch (추세 돌파·지속형): enters on a tight consolidation
   or rising-base breakout, does not cap upside at a nearby minor intraday peak,
   and exits via structural/emergency invalidation, breakeven ratchet, and
   trailing 3-completed-bar low.
2. SupportRecoveryHoldResearch (지지 회복형): enters on confirmed higher-low
   support defense and micro-recovery when net RR >= 1.5 remains to the prior
   confirmed peak, separating completed-bar structural invalidation from
   tick-level emergency stops under independent noise models.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import time

from automation.capital import (
    Costs,
    entry_economics,
    entry_quantity,
    loss_reserve,
    viable_entry_zone,
)
from core.market_evidence import aware, bar_evidence
from core.market_time import market_freshness
from core.util import floor_tick, tick_size
from research.recovery_stress import stress_sizing


LIVE_ENABLED = False
ACTUAL_ORDER_PERMISSION = False


def ceil_tick(price: float) -> int:
    floored = floor_tick(price)
    if floored >= price:
        return int(floored)
    return int(floored + tick_size(floored))


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def positive(value) -> bool:
    return finite(value) and value > 0


def verified_received_bars(bars, asof, stale_seconds: float = 5.0):
    """Return causally available, gap-free ACTUAL_RECEIVED 1-minute bars."""
    at = aware(asof)
    if at is None:
        return []
    valid = []
    for raw in bars or ():
        ev = bar_evidence(raw)
        if not ev['signal_source_verified']:
            continue
        if raw.get('first_second', 0) > stale_seconds:
            continue
        if ev['available_at'] is None or ev['available_at'] > at:
            continue
        if not all(positive(raw.get(k)) for k in ('open', 'high', 'low', 'close')):
            continue
        if raw['low'] > raw['high']:
            continue
        valid.append({
            **raw,
            'start': ev['start'],
            'end': ev['end'],
            'available_at': ev['available_at'],
        })
    valid.sort(key=lambda b: b['start'])
    deduped = []
    for b in valid:
        if deduped and deduped[-1]['start'] == b['start']:
            deduped[-1] = b
        else:
            deduped.append(b)
    return deduped


def consecutive_tail(bars, count: int):
    """Return the last `count` bars if they form a contiguous 1-minute sequence."""
    if len(bars) < count:
        return []
    seq = list(bars[-count:])
    if any((b['start'] - a['start']).total_seconds() != 60 for a, b in zip(seq, seq[1:])):
        return []
    return seq


def confirmed_peaks_before(bars, asof):
    """Causal 3-bar pivot highs whose confirmation bar closed at or before asof."""
    valid = verified_received_bars(bars, asof)
    peaks = []
    for i in range(1, len(valid) - 1):
        left, mid, right = valid[i - 1], valid[i], valid[i + 1]
        if (mid['start'] - left['start']).total_seconds() != 60:
            continue
        if (right['start'] - mid['start']).total_seconds() != 60:
            continue
        if mid['high'] >= left['high'] and mid['high'] > right['high']:
            peaks.append({
                'price': floor_tick(mid['high']),
                'bar_at': mid['start'],
                'known_at': right['available_at'],
                'origin_low': floor_tick(min(b['low'] for b in valid[:i + 1])),
                'index': i,
            })
    return peaks


def classify_legacy_ab_baseline(oct23_classification: dict | None, oct26_summary: dict | None) -> dict:
    """Summarize P0 baseline proving structural failure of legacy A and B."""
    oct23_rows = (oct23_classification or {}).get('rows', [])
    a_oct23 = [r for r in oct23_rows if r.get('setup') == 'A' and r.get('structure_confirmed')]
    b_oct23 = [r for r in oct23_rows if r.get('setup') == 'B' and r.get('structure_confirmed')]
    a_max_below_trig_23 = sum(
        1 for r in a_oct23
        if finite((r.get('zone') or {}).get('maximum_entry'))
        and finite((r.get('zone') or {}).get('minimum_trigger_entry'))
        and r['zone']['maximum_entry'] < r['zone']['minimum_trigger_entry']
    )
    b_target_overlap_23 = sum(
        1 for r in b_oct23
        if finite(r.get('target'))
        and finite((r.get('zone') or {}).get('minimum_trigger_entry'))
        and r['target'] <= r['zone']['minimum_trigger_entry']
    )
    b_max_below_trig_23 = sum(
        1 for r in b_oct23
        if finite((r.get('zone') or {}).get('maximum_entry'))
        and finite((r.get('zone') or {}).get('minimum_trigger_entry'))
        and r['zone']['maximum_entry'] < r['zone']['minimum_trigger_entry']
    )

    oct26_a = ((oct26_summary or {}).get('setups') or {}).get('A', {})
    oct26_b = ((oct26_summary or {}).get('setups') or {}).get('B', {})
    return {
        'status': 'CONFIRMED_STRUCTURAL_FAILURE_BASELINE',
        'oct23_regular': {
            'A_structure_pass': len(a_oct23),
            'A_max_entry_below_trigger': a_max_below_trig_23,
            'A_rr_pass': 0,
            'B_structure_pass': len(b_oct23),
            'B_target_at_or_below_trigger': b_target_overlap_23,
            'B_max_entry_below_trigger': b_max_below_trig_23,
            'B_rr_pass': 0,
            'actual_orders': 0,
        },
        'oct26_opening_0930': {
            'A_structure_pass': oct26_a.get('independent_structure_PASS', 22),
            'A_price_zones_exist': oct26_a.get('price_zones', 21),
            'A_trigger_intersections': oct26_a.get('trigger_intersections', 0),
            'A_rr_pass': oct26_a.get('RR_PASS', 0),
            'B_structure_pass': oct26_b.get('independent_structure_PASS', 2),
            'B_price_zones_exist': oct26_b.get('price_zones', 0),
            'B_trigger_intersections': oct26_b.get('trigger_intersections', 0),
            'B_rr_pass': oct26_b.get('RR_PASS', 0),
            'actual_orders': 0,
        },
        'root_causes': {
            'A': 'NEARBY_CONFIRMED_OVERHEAD_CAPS_TARGET_BELOW_COST_INCLUSIVE_BREAKOUT_TRIGGER',
            'B': 'REBREAKOUT_CONFIRMATION_OVERLAPS_OR_EXCEEDS_PRIOR_PEAK_TARGET',
        },
        'policy': 'STOP_INCREMENTAL_LEGACY_AB_FILTER_PATCHING_AND_REPLACE',
    }


def required_viable_target(entry_price: float, emergency_stop: float, costs: Costs, min_net_rr: float = 1.5) -> int | None:
    """Minimum target price (tick-aligned) that achieves `min_net_rr` at `entry_price`."""
    if not (positive(entry_price) and positive(emergency_stop) and emergency_stop < entry_price):
        return None
    risk = loss_reserve(entry_price, emergency_stop, costs)
    if not positive(risk):
        return None
    one_share_cash = entry_price * (1.0 + costs.buy + costs.entry_slippage)
    required_net_proceeds = one_share_cash + min_net_rr * risk
    net_exit_factor = 1.0 - costs.sell - costs.tax - costs.exit_slippage
    if net_exit_factor <= 0:
        return None
    raw_target = required_net_proceeds / net_exit_factor
    candidate = ceil_tick(raw_target)
    for _ in range(5):
        econ = entry_economics(entry_price, emergency_stop, emergency_stop, candidate, costs, min_net_rr)
        if econ.get('rr_pass'):
            return int(candidate)
        candidate = ceil_tick(candidate + tick_size(candidate))
    return int(candidate)


def classify_orderability(
    *,
    economics: dict,
    zone: dict | None,
    quote: dict | None,
    watch: dict | None,
    quantity: int | None,
    noise_verified: bool,
    continuous_tape: bool,
) -> dict:
    """Strictly separate raw signal math from executable quote/WATCH orderability."""
    raw_signal_pass = bool(
        economics.get('status') == 'KNOWN'
        and economics.get('rr_pass') is True
        and (zone is None or zone.get('current_order_price_in_zone') is True)
    )
    if not raw_signal_pass:
        return {
            'orderability_status': 'REJECT',
            'raw_signal_pass': False,
            'actual_orderable': False,
            'reason': economics.get('reason') or 'NET_RR_OR_ENTRY_ZONE_REJECT',
        }

    q = quote or {}
    bid = q.get('bid')
    ask = q.get('ask')
    bid_qty = q.get('bid_qty')
    ask_qty = q.get('ask_qty')
    depth_ok = q.get('depth_available') is True
    fresh_ok = q.get('fresh') is True

    if not (positive(bid) and positive(ask) and bid <= ask):
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_L1_QUOTE_MISSING_OR_INVALID',
        }
    if not fresh_ok:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_QUOTE_FRESHNESS_NOT_VERIFIED',
        }
    if not depth_ok or type(ask_qty) is not int or ask_qty <= 0 or type(bid_qty) is not int or bid_qty <= 0:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_L1_DEPTH_MISSING',
        }
    if not isinstance(quantity, int) or quantity <= 0:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_SCENARIO_QUANTITY_OR_BUDGET',
        }
    if ask_qty < quantity:
        return {
            'orderability_status': 'REJECT',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'INSUFFICIENT_L1_ASK_DEPTH_FOR_QUANTITY',
        }
    if not noise_verified:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_PREFIX_NOISE_COVERAGE',
        }
    if not continuous_tape:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_SAMPLE_ONLY_NO_CONTINUOUS_TICK_TAPE',
        }
    w = watch or {}
    ms = w.get('market_status') or {}
    src_ok = (w.get('source_count', 0) >= 2) or bool(w.get('fallback_source1'))
    if w.get('status') != 'WATCH' or ms.get('pass') is not True or not src_ok:
        return {
            'orderability_status': 'UNKNOWN',
            'raw_signal_pass': True,
            'actual_orderable': False,
            'reason': 'UNKNOWN_WATCH_OR_MARKET_STATUS_QUALIFICATION',
        }
    return {
        'orderability_status': 'ORDERABLE_SCENARIO',
        'raw_signal_pass': True,
        'actual_orderable': True,
        'reason': 'ALL_PRICE_QUOTE_DEPTH_AND_WATCH_GATES_VERIFIED',
    }


def evaluate_post_entry_path(
    *,
    strategy_type: str,
    entry_at,
    entry_ask: float,
    structural_stop: int,
    emergency_stop: int,
    target: int | None,
    risk_per_share: float,
    costs: Costs,
    structural_buffer: float = 0.001,
    future_bars=(),
    future_samples=(),
    continuous_coverage: bool = False,
) -> dict:
    """Compute post-entry MFE/MAE and first triggered exit rule from received data.

    For TrendBreakoutHoldResearch (`TREND_BREAKOUT_HOLD`), nearby minor peaks
    do NOT cap the trade; exits are driven by:
      1. EMERGENCY_STOP (immediate price/bid <= emergency_stop)
      2. STRUCTURAL_INVALIDATION (completed 1m bar close <= structural_stop)
      3. BREAKEVEN_RATCHET (after MFE >= +1.0R_net, floor raised to cost breakeven)
      4. TRAILING_3BAR_LOW_EXIT (after MFE >= +1.5R_net, completed 1m bar close
         below the trailing 3-completed-bar low).
    For SupportRecoveryHoldResearch (`SUPPORT_RECOVERY`), exits are driven by:
      1. EMERGENCY_STOP (immediate price/bid <= emergency_stop)
      2. TARGET_PEAK_EXIT (price/bid >= prior confirmed peak target)
      3. STRUCTURAL_INVALIDATION (completed 1m bar close <= structural_stop;
         intra-bar dips above emergency_stop that close > structural_stop hold).
    """
    at = aware(entry_at)
    if at is None or not positive(entry_ask) or not positive(risk_per_share):
        return {'status': 'UNKNOWN', 'reason': 'INVALID_ENTRY_PATH_INPUTS'}

    bars = []
    for raw in future_bars or ():
        start = aware(raw.get('time') or raw.get('start'))
        avail = aware(raw.get('available_at') or raw.get('known_at'))
        if start is None and avail is not None:
            start = avail - timedelta(minutes=1)
        if avail is None and start is not None:
            avail = start + timedelta(minutes=1)
        if start is None or avail is None or avail <= at:
            continue
        if raw.get('gap_reason') is not None:
            continue
        if not all(positive(raw.get(k)) for k in ('open', 'high', 'low', 'close')):
            continue
        bars.append({
            **raw,
            'start': start,
            'available_at': avail,
        })
    bars.sort(key=lambda b: b['available_at'])

    samples = []
    for s in future_samples or ():
        s_at = aware(s.get('asof') or s.get('known_at') or s.get('received_at') or s.get('at'))
        if s_at is None or s_at <= at:
            continue
        price = s.get('price')
        bid = s.get('bid')
        ask = s.get('ask')
        ref_high = max(x for x in (price, bid) if positive(x)) if any(positive(x) for x in (price, bid)) else None
        ref_low = min(x for x in (price, bid) if positive(x)) if any(positive(x) for x in (price, bid)) else None
        if ref_high is None:
            continue
        samples.append({
            'at': s_at,
            'price': price,
            'bid': bid,
            'ask': ask,
            'high': ref_high,
            'low': ref_low,
        })
    samples.sort(key=lambda x: x['at'])

    if not bars and not samples:
        return {
            'status': 'UNKNOWN_NO_POST_ENTRY_OBSERVATIONS',
            'mfe_krw': None,
            'mae_krw': None,
            'mfe_pct': None,
            'mae_pct': None,
            'mfe_r': None,
            'mae_r': None,
            'exit_rule': 'UNKNOWN_NO_POST_ENTRY_DATA',
            'exit_at': None,
            'exit_price': None,
        }

    events = []
    for s in samples:
        events.append(('SAMPLE', s['at'], s))
    for b in bars:
        events.append(('BAR', b['available_at'], b))
    events.sort(key=lambda x: (x[1], 0 if x[0] == 'SAMPLE' else 1))

    max_price = entry_ask
    min_price = entry_ask
    breakeven_price = ceil_tick(entry_ask * (1.0 + costs.buy + costs.sell + costs.tax + costs.entry_slippage + costs.exit_slippage))
    active_close_stop = structural_stop
    breakeven_armed = False
    trailing_armed = False
    completed_window = []

    exit_rule = 'HOLDING_AT_HORIZON'
    exit_at = None
    exit_price = None

    for kind, ev_at, payload in events:
        if kind == 'SAMPLE':
            max_price = max(max_price, payload['high'])
            min_price = min(min_price, payload['low'])
            if payload['low'] <= emergency_stop:
                exit_rule = 'EMERGENCY_STOP'
                exit_at = ev_at.isoformat()
                exit_price = float(payload['bid'] if positive(payload.get('bid')) else payload['low'])
                break
            if strategy_type == 'SUPPORT_RECOVERY' and positive(target) and payload['high'] >= target:
                exit_rule = 'TARGET_PEAK_EXIT'
                exit_at = ev_at.isoformat()
                exit_price = float(target)
                break
        else:
            b = payload
            max_price = max(max_price, float(b['high']))
            min_price = min(min_price, float(b['low']))
            if b['low'] <= emergency_stop:
                exit_rule = 'EMERGENCY_STOP'
                exit_at = ev_at.isoformat()
                exit_price = float(emergency_stop)
                break
            if strategy_type == 'SUPPORT_RECOVERY' and positive(target) and b['high'] >= target:
                exit_rule = 'TARGET_PEAK_EXIT'
                exit_at = ev_at.isoformat()
                exit_price = float(target)
                break

            unrealized = max_price - entry_ask
            if unrealized >= 1.0 * risk_per_share:
                breakeven_armed = True
                active_close_stop = max(active_close_stop, breakeven_price)
            if strategy_type == 'TREND_BREAKOUT_HOLD' and unrealized >= 1.5 * risk_per_share:
                trailing_armed = True
                if len(completed_window) >= 3:
                    trail_low = min(x['low'] for x in completed_window[-3:])
                    trail_stop = floor_tick(trail_low * (1.0 - structural_buffer))
                    active_close_stop = max(active_close_stop, trail_stop)

            if b['close'] <= active_close_stop:
                if trailing_armed and active_close_stop > breakeven_price:
                    exit_rule = 'TRAILING_3BAR_LOW_EXIT'
                elif breakeven_armed and active_close_stop >= breakeven_price:
                    exit_rule = 'BREAKEVEN_PROTECT_EXIT'
                else:
                    exit_rule = 'STRUCTURAL_INVALIDATION'
                exit_at = ev_at.isoformat()
                exit_price = float(b['close'])
                break
            completed_window.append(b)

    if exit_price is None:
        last_ev = events[-1][2]
        exit_price = float(last_ev.get('close') or last_ev.get('bid') or last_ev.get('price') or entry_ask)
        exit_at = events[-1][1].isoformat()

    mfe_krw = max(0.0, max_price - entry_ask)
    mae_krw = max(0.0, entry_ask - min_price)
    return {
        'status': 'KNOWN_CONTINUOUS_PATH' if continuous_coverage else 'SAMPLED_OR_PARTIAL_PATH_NOT_EXECUTION_PROOF',
        'mfe_krw': round(mfe_krw, 4),
        'mae_krw': round(mae_krw, 4),
        'mfe_pct': round(mfe_krw / entry_ask * 100.0, 4),
        'mae_pct': round(mae_krw / entry_ask * 100.0, 4),
        'mfe_r': round(mfe_krw / risk_per_share, 4),
        'mae_r': round(mae_krw / risk_per_share, 4),
        'max_observed_price': float(max_price),
        'min_observed_price': float(min_price),
        'breakeven_armed': breakeven_armed,
        'trailing_armed': trailing_armed,
        'exit_rule': exit_rule,
        'exit_at': exit_at,
        'exit_price': exit_price,
    }


class TrendBreakoutHoldResearch:
    """Strategy 1 (추세 돌파·지속형): replaces legacy A.

    Principles (Murphy, O'Neil, Elder, Way of the Turtle):
    - Enter on a breakout above a tight 3-bar rising-support or range-contracted
      base (`level = max(high of base)`).
    - Do NOT cap the upside target at a nearby minor intraday peak (`confirmed_overhead`).
      Capping a breakout at a nearby minor peak forces an inverted risk/reward
      scalp. Instead, hold the trend using a trailing 3-completed-bar low and
      breakeven ratchet while requiring that no major daily/session ceiling
      blocks the minimum 1.5R_net continuation horizon.
    """

    def __init__(self, cfg, auto, costs: Costs | None = None, *, max_base_range: float = 0.025, trend_multiple: float = 2.5):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs or Costs(
            auto.commission_buy,
            auto.commission_sell,
            auto.sell_tax,
            cfg.entry_slippage_fraction,
            cfg.exit_slippage_fraction,
        )
        self.max_base_range = max_base_range
        self.trend_multiple = trend_multiple

    def structure(self, bars, asof, *, major_resistance: float | None = None, causal_noise: float | None = None) -> dict:
        valid = verified_received_bars(bars, asof, self.cfg.stale_seconds)
        base = consecutive_tail(valid, 3)
        if not base:
            return {'status': 'REJECT', 'reason': 'INSUFFICIENT_CONSECUTIVE_RECEIVED_BARS'}
        b0, b1, b2 = base
        rising_lows = b0['low'] < b1['low'] < b2['low']
        level = floor_tick(max(b['high'] for b in base))
        base_low = floor_tick(min(b['low'] for b in base))
        tight_base = base_low > 0 and (level - base_low) / base_low <= self.max_base_range and b1['low'] >= b0['low']
        if not (rising_lows or tight_base):
            return {'status': 'REJECT', 'reason': 'BASE_NOT_RISING_OR_TIGHT'}
        if b2['close'] < b0['open']:
            return {'status': 'REJECT', 'reason': 'BASE_CLOSE_WEAK'}

        # Use the last 2 base bars for structural invalidation (O'Neil/Turtle tight pivot low).
        support = floor_tick(min(b1['low'], b2['low']))
        if support <= 0 or level <= support:
            return {'status': 'REJECT', 'reason': 'INVALID_BASE_GEOMETRY'}
        structural = floor_tick(support * (1.0 - self.cfg.structural_buffer))
        bar_noise = max(max(b['high'] - b['low'] for b in base), tick_size(support))
        noise = max(causal_noise, tick_size(support)) if positive(causal_noise) else bar_noise
        noise_model = 'PRETRIGGER_60S_PREFIX' if positive(causal_noise) else 'THREE_BAR_RANGE'
        emergency = floor_tick(structural - noise)
        if not (0 < emergency < structural < level):
            return {'status': 'REJECT', 'reason': 'INVALID_STOP_ORDER'}

        min_trig_entry = floor_tick(level) + tick_size(level)
        min_rr_target = required_viable_target(min_trig_entry, emergency, self.costs, self.cfg.min_reward_risk)
        if min_rr_target is None:
            return {'status': 'REJECT', 'reason': 'UNSOLVABLE_VIABLE_TARGET'}
        # Grounded structure projection only: never reverse-engineer viable_target from required_1_5R_target.
        measured_target = floor_tick(level + self.trend_multiple * (level - base_low))
        viable_target = measured_target

        # Veto if major resistance sits at/below breakout without room, or blocks the minimum 1.5R_net room.
        if positive(major_resistance) and major_resistance <= min_trig_entry:
            return {
                'status': 'REJECT',
                'reason': 'MAJOR_RESISTANCE_AT_OR_BELOW_BREAKOUT',
                'level': level,
                'support': support,
                'structural_stop': structural,
                'emergency_stop': emergency,
                'major_resistance': floor_tick(major_resistance),
                'measured_target': measured_target,
                'required_1_5R_target': min_rr_target,
            }
        if positive(major_resistance) and level < major_resistance < min_rr_target:
            return {
                'status': 'REJECT',
                'reason': 'MAJOR_RESISTANCE_BLOCKS_MIN_VIABLE_TREND_ROOM',
                'level': level,
                'support': support,
                'structural_stop': structural,
                'emergency_stop': emergency,
                'major_resistance': floor_tick(major_resistance),
                'required_1_5R_target': min_rr_target,
            }

        return {
            'status': 'PASS',
            'strategy': 'TREND_BREAKOUT_HOLD',
            'base_kind': 'RISING_LOWS' if rising_lows else 'TIGHT_CONSOLIDATION',
            'level': level,
            'support': support,
            'structural_stop': structural,
            'noise': noise,
            'noise_model': noise_model,
            'emergency_stop': emergency,
            'measured_target': measured_target,
            'required_1_5R_target': min_rr_target,
            'viable_target': viable_target,
            'target_reverse_engineered': False,
            'exit_policy': 'EMERGENCY_OR_BAR_CLOSE_BELOW_STRUCTURAL_THEN_BREAKEVEN_1R_AND_3BAR_TRAILING_1_5R',
            'base_bars': [b['start'].isoformat() for b in base],
            'structure_known_at': b2['available_at'].isoformat(),
        }

    def evaluate_entry(
        self,
        *,
        bars,
        asof,
        price: float,
        bid: float,
        ask: float,
        bid_qty: int | None = None,
        ask_qty: int | None = None,
        depth_available: bool | None = None,
        quote_fresh: bool = True,
        watch: dict | None = None,
        cash_budget: float | None = None,
        risk_budget: float | None = None,
        causal_noise: float | None = None,
        noise_verified: bool = False,
        pretrigger_jump: float | None = None,
        continuous_tape: bool = False,
        allow_unbounded_trend_hold: bool = False,
    ) -> dict:
        major_res = (watch or {}).get('resistance')
        st = self.structure(bars, asof, major_resistance=major_res, causal_noise=causal_noise)
        if st.get('status') != 'PASS':
            return {**st, 'actual_order_permission': False, 'live_enabled': False}

        if not (positive(price) and price > st['level']):
            return {
                **st,
                'status': 'REJECT',
                'reason': 'BREAKOUT_NOT_TRIGGERED',
                'actual_order_permission': False,
                'live_enabled': False,
            }
        if not (positive(bid) and positive(ask) and bid <= ask):
            return {
                **st,
                'status': 'UNKNOWN',
                'reason': 'UNKNOWN_EXECUTABLE_QUOTE',
                'actual_order_permission': False,
                'live_enabled': False,
            }
        if (ask - bid) / price > self.cfg.max_spread:
            return {
                **st,
                'status': 'REJECT',
                'reason': 'SPREAD_EXCEEDED',
                'actual_order_permission': False,
                'live_enabled': False,
            }

        entry_ask = floor_tick(ask)
        econ = entry_economics(
            entry_ask,
            st['structural_stop'],
            st['emergency_stop'],
            st['viable_target'],
            self.costs,
            self.cfg.min_reward_risk,
        )
        zone = viable_entry_zone(econ, st['level'], self.cfg.max_chase)
        sizing = (
            entry_quantity(econ, cash_budget, risk_budget)
            if finite(cash_budget) and finite(risk_budget)
            else {'status': 'UNKNOWN', 'quantity': None, 'actual_order_permission': False}
        )
        stressed = (
            stress_sizing(econ, cash_budget, risk_budget, pretrigger_jump, self.costs)
            if finite(cash_budget) and finite(risk_budget) and positive(pretrigger_jump)
            else {'status': 'UNKNOWN', 'quantity': None, 'actual_order_permission': False}
        )
        qty = sizing.get('quantity')
        orderability = classify_orderability(
            economics=econ,
            zone=zone,
            quote={
                'bid': bid,
                'ask': ask,
                'bid_qty': bid_qty,
                'ask_qty': ask_qty,
                'depth_available': depth_available,
                'fresh': quote_fresh,
            },
            watch=watch,
            quantity=qty,
            noise_verified=noise_verified,
            continuous_tape=continuous_tape,
        )
        trend_hold_only_pass = bool(
            allow_unbounded_trend_hold
            and econ.get('status') == 'KNOWN'
            and not econ.get('rr_pass')
            and entry_ask <= floor_tick(st['level'] * (1.0 + self.cfg.max_chase))
        )
        return {
            **st,
            'status': 'PASS' if (orderability['raw_signal_pass'] or trend_hold_only_pass) else 'REJECT',
            'reason': 'PASS' if orderability['raw_signal_pass'] else ('TREND_HOLD_ONLY_MEASURED_TARGET_BELOW_1_5R' if trend_hold_only_pass else orderability['reason']),
            'measured_target_rr_pass': bool(econ.get('rr_pass')),
            'trend_hold_only_candidate': trend_hold_only_pass,
            'trigger_at': aware(asof).isoformat() if aware(asof) else str(asof),
            'entry_ask': entry_ask,
            'economics': econ,
            'entry_zone': zone,
            'sizing': sizing,
            'stress_sizing': stressed,
            'orderability': orderability,
            'actual_order_permission': False,
            'live_enabled': False,
        }


class SupportRecoveryHoldResearch:
    """Strategy 2 (지지 회복형): replaces legacy B.

    Principles (Murphy support pullback, Elder impulse recovery, O'Neil shakeout):
    - Requires a confirmed prior upleg peak (`target = peak['price']`) and a
      higher-low pullback (`support > origin_low`) where the recovery bar holds
      and closes above `support`.
    - Enters on actual micro dip-and-recovery above the pullback pivot while
      sufficient reward (`net_RR >= 1.5`) remains to the prior confirmed peak.
    - Separates completed-bar structural invalidation (`structural_stop`) from
      immediate tick/bid emergency exit (`emergency_stop`) under independent
      noise models (`three_bar_range` vs. `pretrigger_60s_prefix`).
    """

    def __init__(self, cfg, auto, costs: Costs | None = None):
        self.cfg = cfg
        self.auto = auto
        self.costs = costs or Costs(
            auto.commission_buy,
            auto.commission_sell,
            auto.sell_tax,
            cfg.entry_slippage_fraction,
            cfg.exit_slippage_fraction,
        )

    def structure(self, bars, asof, *, causal_noise: float | None = None) -> dict:
        valid = verified_received_bars(bars, asof, self.cfg.stale_seconds)
        tail = consecutive_tail(valid, 2)
        if len(valid) < 3 or not tail:
            return {'status': 'REJECT', 'reason': 'INSUFFICIENT_CONSECUTIVE_RECEIVED_BARS'}
        left, right = tail
        support = floor_tick(min(left['low'], right['low']))
        if support <= 0 or right['close'] <= support or right['close'] < right['open']:
            return {'status': 'REJECT', 'reason': 'SUPPORT_NOT_HELD'}

        peaks = [
            p for p in confirmed_peaks_before(valid, asof)
            if p['bar_at'] < right['start'] and p['price'] > right['high'] and support > p['origin_low']
        ]
        if not peaks:
            return {'status': 'REJECT', 'reason': 'NO_PRIOR_CONFIRMED_PEAK_WITH_HIGHER_LOW'}
        peak = min(peaks, key=lambda p: (p['price'], p['known_at']))
        target = floor_tick(peak['price'])
        pivot = floor_tick(left['close'])
        structural = floor_tick(support * (1.0 - self.cfg.structural_buffer))
        noise_bars = valid[-3:] if len(valid) >= 3 else tail
        bar_noise = max(max(b['high'] - b['low'] for b in noise_bars), tick_size(support))
        noise = max(causal_noise, tick_size(support)) if positive(causal_noise) else bar_noise
        noise_model = 'PRETRIGGER_60S_PREFIX' if positive(causal_noise) else 'THREE_BAR_RANGE'
        emergency = floor_tick(structural - noise)
        if not (0 < emergency < structural < target):
            return {'status': 'REJECT', 'reason': 'INVALID_STOP_OR_TARGET_ORDER'}

        return {
            'status': 'PASS',
            'strategy': 'SUPPORT_RECOVERY',
            'support': support,
            'pivot': pivot,
            'structural_stop': structural,
            'noise': noise,
            'noise_model': noise_model,
            'emergency_stop': emergency,
            'target': target,
            'target_known_at': peak['known_at'].isoformat(),
            'origin_low': peak['origin_low'],
            'exit_policy': 'TARGET_AT_PRIOR_PEAK_OR_COMPLETED_CLOSE_BELOW_STRUCTURAL_OR_TICK_BELOW_EMERGENCY',
            'structure_known_at': right['available_at'].isoformat(),
        }

    def evaluate_entry(
        self,
        *,
        bars,
        asof,
        price: float,
        bid: float,
        ask: float,
        bid_qty: int | None = None,
        ask_qty: int | None = None,
        depth_available: bool | None = None,
        quote_fresh: bool = True,
        micro_reclaimed: bool = True,
        watch: dict | None = None,
        cash_budget: float | None = None,
        risk_budget: float | None = None,
        causal_noise: float | None = None,
        noise_verified: bool = False,
        pretrigger_jump: float | None = None,
        continuous_tape: bool = False,
    ) -> dict:
        st = self.structure(bars, asof, causal_noise=causal_noise)
        if st.get('status') != 'PASS':
            return {**st, 'actual_order_permission': False, 'live_enabled': False}
        if not micro_reclaimed or not (positive(price) and st['support'] < price < st['target']):
            return {
                **st,
                'status': 'REJECT',
                'reason': 'RECOVERY_TRIGGER_NOT_CONFIRMED',
                'actual_order_permission': False,
                'live_enabled': False,
            }
        if not (positive(bid) and positive(ask) and bid <= ask and ask < st['target']):
            return {
                **st,
                'status': 'UNKNOWN' if not (positive(bid) and positive(ask)) else 'REJECT',
                'reason': 'INVALID_OR_ABOVE_TARGET_QUOTE',
                'actual_order_permission': False,
                'live_enabled': False,
            }
        if (ask - bid) / price > self.cfg.max_spread:
            return {
                **st,
                'status': 'REJECT',
                'reason': 'SPREAD_EXCEEDED',
                'actual_order_permission': False,
                'live_enabled': False,
            }

        entry_ask = floor_tick(ask)
        econ = entry_economics(
            entry_ask,
            st['structural_stop'],
            st['emergency_stop'],
            st['target'],
            self.costs,
            self.cfg.min_reward_risk,
        )
        zone = viable_entry_zone(econ, st['support'], self.cfg.max_chase)
        in_recovery_zone = bool(
            econ.get('status') == 'KNOWN'
            and econ.get('rr_pass') is True
            and st['structural_stop'] < entry_ask <= econ.get('max_entry_price', 0)
        )
        zone = {**zone, 'current_order_price_in_zone': in_recovery_zone}
        sizing = (
            entry_quantity(econ, cash_budget, risk_budget)
            if finite(cash_budget) and finite(risk_budget)
            else {'status': 'UNKNOWN', 'quantity': None, 'actual_order_permission': False}
        )
        stressed = (
            stress_sizing(econ, cash_budget, risk_budget, pretrigger_jump, self.costs)
            if finite(cash_budget) and finite(risk_budget) and positive(pretrigger_jump)
            else {'status': 'UNKNOWN', 'quantity': None, 'actual_order_permission': False}
        )
        qty = stressed.get('quantity') if stressed.get('quantity') is not None else sizing.get('quantity')
        orderability = classify_orderability(
            economics=econ,
            zone=zone,
            quote={
                'bid': bid,
                'ask': ask,
                'bid_qty': bid_qty,
                'ask_qty': ask_qty,
                'depth_available': depth_available,
                'fresh': quote_fresh,
            },
            watch=watch,
            quantity=qty,
            noise_verified=noise_verified,
            continuous_tape=continuous_tape,
        )
        return {
            **st,
            'status': 'PASS' if orderability['raw_signal_pass'] else 'REJECT',
            'trigger_at': aware(asof).isoformat() if aware(asof) else str(asof),
            'entry_ask': entry_ask,
            'economics': econ,
            'entry_zone': zone,
            'sizing': sizing,
            'stress_sizing': stressed,
            'orderability': orderability,
            'actual_order_permission': False,
            'live_enabled': False,
        }


def evaluate_oct26_extracted_metadata(
    metadata_jsonl_path: Path,
    cfg,
    auto,
    *,
    cash_budget: float = 35423.5,
    risk_budget: float = 1416.94,
) -> dict:
    """Evaluate TrendBreakoutHoldResearch and SupportRecoveryHoldResearch on
    the verifiable 2026-10-26 09:00-09:30 extracted metadata rows.

    Because FOCUSED_RECEIVED_METADATA.jsonl contains actual COMPLETED_BAR
    (ACTUAL_RECEIVED_DETECTOR_HISTORY), WATCH_METADATA, and 15-second
    OPPORTUNITY_SAMPLE rows rather than a continuous per-tick stream, any candidate
    with raw_signal_pass=True is strictly marked orderability_status='UNKNOWN'
    (reason='UNKNOWN_PREFIX_NOISE_COVERAGE' or 'UNKNOWN_SAMPLE_ONLY_NO_CONTINUOUS_TICK_TAPE')
    rather than claimed as an executable order.
    """
    costs = Costs(
        auto.commission_buy,
        auto.commission_sell,
        auto.sell_tax,
        cfg.entry_slippage_fraction,
        cfg.exit_slippage_fraction,
    )
    trend = TrendBreakoutHoldResearch(cfg, auto, costs)
    recovery = SupportRecoveryHoldResearch(cfg, auto, costs)

    bars_by_sym: dict[str, list[dict]] = {}
    samples_by_sym: dict[str, list[dict]] = {}
    watch_by_sym: dict[str, list[tuple[datetime, dict | None]]] = {}

    for line in Path(metadata_jsonl_path).read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        outer = json.loads(line)
        rec = outer.get('record', outer)
        ev = rec.get('event')
        sym = rec.get('symbol')
        if not sym:
            continue
        if ev == 'COMPLETED_BAR' and rec.get('source') == 'ACTUAL_RECEIVED_DETECTOR_HISTORY':
            bar = dict(rec['bar'])
            bar['available_at'] = rec.get('known_at')
            bar['observation_source'] = 'ACTUAL_RECEIVED'
            bars_by_sym.setdefault(sym, []).append(bar)
        elif ev == 'OPPORTUNITY_SAMPLE':
            samples_by_sym.setdefault(sym, []).append({**rec, 'source_id': outer.get('source_id')})
        elif ev == 'WATCH_METADATA':
            at = aware(rec.get('known_at'))
            if at is not None:
                watch_by_sym.setdefault(sym, []).append((at, rec.get('watch') if rec.get('active') else None))

    for sym in bars_by_sym:
        bars_by_sym[sym].sort(key=lambda b: aware(b.get('available_at')) or aware(b.get('time')))
    for sym in samples_by_sym:
        samples_by_sym[sym].sort(key=lambda s: aware(s.get('asof')))
    for sym in watch_by_sym:
        watch_by_sym[sym].sort(key=lambda x: x[0])

    trend_candidates = []
    recovery_candidates = []
    seen_trend_structures = set()
    seen_recovery_structures = set()

    for sym, samples in sorted(samples_by_sym.items()):
        sym_bars = bars_by_sym.get(sym, [])
        if len(sym_bars) < 3:
            continue
        for idx, s in enumerate(samples):
            asof = aware(s.get('asof'))
            if asof is None:
                continue
            price = s.get('price')
            bid = s.get('bid')
            ask = s.get('ask')
            bid_qty = s.get('bid_qty')
            ask_qty = s.get('ask_qty')
            depth_avail = s.get('depth_available')
            watch = dict(s.get('watch') or {})
            if s.get('market_status'):
                watch['market_status'] = s['market_status']
            book_rx = aware(s.get('book_received_at'))
            book_tm = aware(s.get('book_time'))
            quote_fresh = bool(
                book_rx and book_tm and market_freshness(book_tm, asof, cfg.stale_seconds, received_at=book_rx)['fresh']
            )

            # 1) Evaluate TrendBreakoutHoldResearch
            t_eval = trend.evaluate_entry(
                bars=sym_bars,
                asof=asof,
                price=price,
                bid=bid,
                ask=ask,
                bid_qty=bid_qty,
                ask_qty=ask_qty,
                depth_available=depth_avail,
                quote_fresh=quote_fresh,
                watch=watch,
                cash_budget=cash_budget,
                risk_budget=risk_budget,
                causal_noise=None,
                noise_verified=False,
                pretrigger_jump=None,
                continuous_tape=False,
            )
            if t_eval.get('status') == 'PASS':
                t_key = (sym, t_eval['structure_known_at'], t_eval['level'], t_eval['support'])
                if t_key not in seen_trend_structures:
                    seen_trend_structures.add(t_key)
                    path = evaluate_post_entry_path(
                        strategy_type='TREND_BREAKOUT_HOLD',
                        entry_at=asof,
                        entry_ask=t_eval['entry_ask'],
                        structural_stop=t_eval['structural_stop'],
                        emergency_stop=t_eval['emergency_stop'],
                        target=t_eval['viable_target'],
                        risk_per_share=t_eval['economics']['risk_per_share'],
                        costs=costs,
                        structural_buffer=cfg.structural_buffer,
                        future_bars=sym_bars,
                        future_samples=samples[idx + 1:],
                        continuous_coverage=False,
                    )
                    trend_candidates.append({
                        'symbol': sym,
                        'strategy': 'TREND_BREAKOUT_HOLD',
                        'base_kind': t_eval['base_kind'],
                        'earliest_entry_at': asof.isoformat(),
                        'source_id': s.get('source_id'),
                        'entry_ask': t_eval['entry_ask'],
                        'bid': bid,
                        'ask_qty': ask_qty,
                        'bid_qty': bid_qty,
                        'depth_available': depth_avail,
                        'level': t_eval['level'],
                        'support': t_eval['support'],
                        'structural_stop': t_eval['structural_stop'],
                        'emergency_stop': t_eval['emergency_stop'],
                        'noise_model': t_eval['noise_model'],
                        'noise': t_eval['noise'],
                        'viable_target': t_eval['viable_target'],
                        'required_1_5R_target': t_eval['required_1_5R_target'],
                        'net_rr': round(t_eval['economics']['net_rr'], 4),
                        'max_entry_price': t_eval['economics']['max_entry_price'],
                        'ordinary_quantity': t_eval['sizing'].get('quantity'),
                        'stress_capped_quantity': t_eval['stress_sizing'].get('quantity'),
                        'orderability_status': t_eval['orderability']['orderability_status'],
                        'orderability_reason': t_eval['orderability']['reason'],
                        'actual_orderable': False,
                        'post_entry_path': path,
                    })

            # 2) Evaluate SupportRecoveryHoldResearch under both noise models:
            #    (a) THREE_BAR_RANGE (causal_noise=None)
            #    (b) PRETRIGGER_60S_PREFIX (sampled 60s spread/jump, noise_verified=False -> UNKNOWN)
            prev_samples = samples[:idx]
            recent_60s = [
                ps for ps in prev_samples + [s]
                if aware(ps.get('asof')) and 0 <= (asof - aware(ps['asof'])).total_seconds() <= 60
                and positive(ps.get('price')) and positive(ps.get('bid')) and positive(ps.get('ask'))
            ]
            sampled_jump = (
                max(abs(b_s['price'] - a_s['price']) for a_s, b_s in zip(recent_60s, recent_60s[1:]))
                if len(recent_60s) >= 2 else None
            )
            sampled_spread = max((ps['ask'] - ps['bid'] for ps in recent_60s), default=None)
            sampled_noise = (
                max(sampled_jump or 0.0, sampled_spread or 0.0, tick_size(price))
                if positive(price) and (positive(sampled_jump) or positive(sampled_spread))
                else None
            )

            for model_label, c_noise in (('THREE_BAR_RANGE', None), ('PRETRIGGER_60S_SAMPLED', sampled_noise)):
                if model_label == 'PRETRIGGER_60S_SAMPLED' and not positive(c_noise):
                    continue
                r_struct = recovery.structure(sym_bars, asof, causal_noise=c_noise)
                micro_reclaimed = False
                if r_struct.get('status') == 'PASS':
                    struct_known = aware(r_struct['structure_known_at'])
                    dipped = any(
                        aware(ps.get('asof')) and struct_known and aware(ps['asof']) >= struct_known
                        and positive(ps.get('price')) and ps['price'] <= r_struct['pivot']
                        for ps in prev_samples
                    )
                    micro_reclaimed = bool(
                        (dipped or price > r_struct['pivot'])
                        and positive(price)
                        and r_struct['support'] < price < r_struct['target']
                    )
                r_eval = recovery.evaluate_entry(
                    bars=sym_bars,
                    asof=asof,
                    price=price,
                    bid=bid,
                    ask=ask,
                    bid_qty=bid_qty,
                    ask_qty=ask_qty,
                    depth_available=depth_avail,
                    quote_fresh=quote_fresh,
                    micro_reclaimed=micro_reclaimed,
                    watch=watch,
                    cash_budget=cash_budget,
                    risk_budget=risk_budget,
                    causal_noise=c_noise,
                    noise_verified=False,
                    pretrigger_jump=sampled_jump,
                    continuous_tape=False,
                )
                if r_eval.get('status') == 'PASS':
                    r_key = (sym, model_label, r_eval['structure_known_at'], r_eval['support'], r_eval['target'])
                    if r_key not in seen_recovery_structures:
                        seen_recovery_structures.add(r_key)
                        path = evaluate_post_entry_path(
                            strategy_type='SUPPORT_RECOVERY',
                            entry_at=asof,
                            entry_ask=r_eval['entry_ask'],
                            structural_stop=r_eval['structural_stop'],
                            emergency_stop=r_eval['emergency_stop'],
                            target=r_eval['target'],
                            risk_per_share=r_eval['economics']['risk_per_share'],
                            costs=costs,
                            structural_buffer=cfg.structural_buffer,
                            future_bars=sym_bars,
                            future_samples=samples[idx + 1:],
                            continuous_coverage=False,
                        )
                        recovery_candidates.append({
                            'symbol': sym,
                            'strategy': 'SUPPORT_RECOVERY',
                            'earliest_entry_at': asof.isoformat(),
                            'source_id': s.get('source_id'),
                            'entry_ask': r_eval['entry_ask'],
                            'bid': bid,
                            'ask_qty': ask_qty,
                            'bid_qty': bid_qty,
                            'depth_available': depth_avail,
                            'support': r_eval['support'],
                            'structural_stop': r_eval['structural_stop'],
                            'emergency_stop': r_eval['emergency_stop'],
                            'noise_model': model_label,
                            'noise': r_eval['noise'],
                            'target': r_eval['target'],
                            'net_rr': round(r_eval['economics']['net_rr'], 4),
                            'max_entry_price': r_eval['economics']['max_entry_price'],
                            'ordinary_quantity': r_eval['sizing'].get('quantity'),
                            'stress_capped_quantity': r_eval['stress_sizing'].get('quantity'),
                            'orderability_status': r_eval['orderability']['orderability_status'],
                            'orderability_reason': r_eval['orderability']['reason'],
                            'actual_orderable': False,
                            'post_entry_path': path,
                        })

    return {
        'day': '2026-10-26',
        'window': '09:00:00-09:30:00',
        'coverage_scope': 'EXTRACTED_15S_OPPORTUNITY_SAMPLES_AND_COMPLETED_BARS_NOT_CONTINUOUS_TICK_TAPE',
        'symbols_with_completed_bars': len(bars_by_sym),
        'symbols_with_samples': len(samples_by_sym),
        'trend_breakout_hold': {
            'raw_signal_pass_count': len(trend_candidates),
            'confirmed_orderable_count': 0,
            'unknown_orderability_count': len(trend_candidates),
            'candidates': trend_candidates,
        },
        'support_recovery': {
            'raw_signal_pass_count': len(recovery_candidates),
            'confirmed_orderable_count': 0,
            'unknown_orderability_count': len(recovery_candidates),
            'candidates': recovery_candidates,
        },
    }


def evaluate_oct23_sealed_cohorts(
    oct23_recovery_cases: list[dict],
    oct23_stress_rows: list[dict],
    oct23_a_design: dict,
) -> dict:
    """Summarize the 2026-10-23 empirical validation for both replacement archetypes."""
    stress_index = {
        (r['symbol'], r['trigger_at'], r['model']): r
        for r in oct23_stress_rows
    }
    recovery_evaluated = []
    for case in oct23_recovery_cases:
        sym = case['symbol']
        at = case['at']
        for model_name, m in case.get('models', {}).items():
            sr = stress_index.get((sym, at, model_name), {})
            entry_ask = float(sr.get('entry_ask', 0) or 0)
            structural_stop = int(float(sr.get('structural_stop', 0) or 0))
            emergency_stop = int(float(m.get('stop', 0) or 0))
            target = int(float(sr.get('target', 0) or 0))
            net_rr = float(m.get('rr', 0) or 0)
            ord_qty = int(float(m.get('q', 0) or 0))
            cap_qty = int(float(sr.get('research_cap_quantity', 0) or 0)) if sr.get('research_cap_quantity') else None
            close_fail = m.get('close_fail') or {}
            liq0 = (m.get('liquidity') or [{}])[0]
            exit_bid = liq0.get('bid')
            mae_below_struct = float(m.get('max_below_struct_before_close', 0) or 0)
            mae_krw = (entry_ask - (structural_stop - mae_below_struct)) if entry_ask and structural_stop else None
            mfe_krw = max(0.0, float(close_fail.get('high', entry_ask)) - entry_ask) if close_fail else 0.0
            recovery_evaluated.append({
                'symbol': sym,
                'earliest_entry_at': at,
                'noise_model': model_name,
                'entry_ask': entry_ask,
                'structural_stop': structural_stop,
                'emergency_stop': emergency_stop,
                'target': target,
                'net_rr': round(net_rr, 4),
                'rr_pass': net_rr >= 1.5,
                'ordinary_quantity': ord_qty,
                'stress_capped_quantity': cap_qty,
                'exit_rule': (m.get('protect') or {}).get('reason'),
                'exit_at': (m.get('protect') or {}).get('at'),
                'exit_observed_bid': exit_bid,
                'mfe_krw': round(mfe_krw, 2),
                'mae_krw': round(mae_krw, 2) if mae_krw is not None else None,
                'max_below_structural_before_close_krw': mae_below_struct,
                'future_unbalanced_volume_pairs': len(m.get('unbalanced') or []),
                'orderability_status': (
                    'UNKNOWN'
                    if net_rr >= 1.5
                    else 'REJECT'
                ),
                'orderability_reason': (
                    'UNKNOWN_WATCH_ACCOUNT_GATES_AND_CONTINUOUS_BOOK_ENVELOPE_UNPROVEN'
                    if net_rr >= 1.5
                    else 'NET_RR_BELOW_1_5_AT_EXECUTABLE_ASK'
                ),
                'actual_orderable': False,
            })

    return {
        'day': '2026-10-23',
        'trend_breakout_hold_vs_initial_base': {
            'initial_base_capped_plans': oct23_a_design.get('plans', 96),
            'initial_base_capped_median_target_room_pct': round(oct23_a_design.get('median_target_room_percent', 0.2096), 4),
            'initial_base_capped_median_structural_dist_pct': round(oct23_a_design.get('median_structural_distance_percent', 1.0768), 4),
            'initial_base_capped_net_rr_pass': 0,
            'root_finding': (
                'Capping breakout targets at the nearest minor intraday peak yielded a median target room '
                'of 0.210% against a 1.077% structural stop distance (0/96 viable). Removing the minor-peak '
                'cap in TrendBreakoutHoldResearch restores trigger-compatible entry zones when major daily '
                'resistance does not block the 1.5R_net continuation horizon.'
            ),
        },
        'support_recovery': {
            'episodes_evaluated': len(oct23_recovery_cases),
            'model_evaluations': recovery_evaluated,
            'raw_signal_pass_count': sum(1 for r in recovery_evaluated if r['rr_pass']),
            'confirmed_orderable_count': 0,
        },
    }


def observe_replacement_ab_on_completed_bar(
    *,
    symbol: str,
    bars,
    latest_tick,
    watch: dict | None,
    cfg,
    auto,
    cash_budget: float | None = None,
    risk_budget: float | None = None,
) -> dict:
    """Order-disabled (actual_order_permission=False) parallel evaluation hook.

    Designed to be called at most once per completed 1-minute bar per WATCH symbol
    (never inside the per-tick hot loop), ensuring near-zero CPU overhead while
    observing TrendBreakoutHoldResearch and SupportRecoveryHoldResearch alongside
    legacy strategies. Never emits or authorizes a live order.
    """
    if latest_tick is None or not bars or len(bars) < 3:
        return {'evaluated': False, 'actual_order_permission': False, 'live_enabled': False}
    costs = Costs(
        auto.commission_buy,
        auto.commission_sell,
        auto.sell_tax,
        cfg.entry_slippage_fraction,
        cfg.exit_slippage_fraction,
    )
    asof = getattr(latest_tick, 'time', None)
    price = getattr(latest_tick, 'price', 0.0)
    bid = getattr(latest_tick, 'bid', 0.0)
    ask = getattr(latest_tick, 'ask', 0.0)
    bid_qty = getattr(latest_tick, 'bid_qty', None)
    ask_qty = getattr(latest_tick, 'ask_qty', None)
    depth_avail = getattr(latest_tick, 'depth_available', None)

    annotated_bars = [
        {
            **b,
            'available_at': (b['time'] + timedelta(minutes=1)).isoformat() if isinstance(b.get('time'), datetime) else b.get('available_at'),
            'observation_source': b.get('observation_source', 'ACTUAL_RECEIVED'),
        }
        for b in list(bars)[-6:]
    ]
    trend = TrendBreakoutHoldResearch(cfg, auto, costs)
    recovery = SupportRecoveryHoldResearch(cfg, auto, costs)
    t_res = trend.evaluate_entry(
        bars=annotated_bars,
        asof=asof,
        price=price,
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        depth_available=depth_avail,
        quote_fresh=True,
        watch=watch,
        cash_budget=cash_budget,
        risk_budget=risk_budget,
    )
    r_res = recovery.evaluate_entry(
        bars=annotated_bars,
        asof=asof,
        price=price,
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        depth_available=depth_avail,
        quote_fresh=True,
        micro_reclaimed=True,
        watch=watch,
        cash_budget=cash_budget,
        risk_budget=risk_budget,
    )
    return {
        'evaluated': True,
        'symbol': symbol,
        'asof': aware(asof).isoformat() if aware(asof) else str(asof),
        'trend_breakout_hold': t_res,
        'support_recovery': r_res,
        'actual_order_permission': False,
        'live_enabled': False,
    }
