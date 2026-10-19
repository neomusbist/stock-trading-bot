"""09:00~09:10 장초반 10분 진입 퍼널 자동 진단 모듈 (automation/opening_diagnostics.py)."""
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from core.util import atomic_json


def _parse_iso(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def build_opening_funnel_diagnostic(engine, detector, shadow, radar, lookup, rows, stamp):
    """09:00~09:10 구간의 발견 -> SHADOW -> WATCH -> 신호 -> 진입/차단 -> 주문 집계를 생성한다."""
    now_dt = _parse_iso(stamp) or engine.clock()
    today = now_dt.date()
    timeline = getattr(engine, 'observation_timeline', None)
    timeline_rows = dict(getattr(timeline, 'rows', {}) or {})

    shadow_obs = getattr(shadow, 'observer', None) if shadow is not None else None
    shadow_first = dict(getattr(shadow_obs, 'first', {}) or {})
    shadow_latest = dict(getattr(shadow_obs, 'latest', {}) or {})
    shadow_rows = dict(getattr(shadow_obs, 'rows', {}) or {})
    shadow_view = shadow.runtime_view() if shadow is not None and hasattr(shadow, 'runtime_view') else {}
    shadow_symbols = set(shadow_view.get('symbols', ()) or ())

    row_map = {r['code']: dict(r) for r in (rows or []) if isinstance(r, dict) and r.get('code')}
    for sym, r in (lookup or {}).items():
        if isinstance(r, dict):
            row_map.setdefault(sym, dict(r))

    discovered_symbols = set(row_map.keys()) | set( getattr(engine, 'source_qualification', {}).keys() )
    for sym, tr in timeline_rows.items():
        raw_dt = _parse_iso(tr.get('first_raw_at'))
        if raw_dt is None or raw_dt.date() == today:
            discovered_symbols.add(sym)
    for sym, sf in shadow_first.items():
        raw_dt = _parse_iso(sf.get('first_raw_at'))
        if raw_dt is None or raw_dt.date() == today:
            discovered_symbols.add(sym)

    shadow_admitted = set(shadow_symbols) | set(shadow_rows.keys())
    for sym, sf in shadow_first.items():
        if sf.get('first_shadow_at'):
            shadow_admitted.add(sym)

    watch_admitted = set((lookup or {}).keys())
    watch_latencies = {}
    tick_latencies = {}
    for sym, tr in timeline_rows.items():
        raw_dt = _parse_iso(tr.get('first_raw_at'))
        watch_dt = _parse_iso(tr.get('first_watch_at'))
        tick_dt = _parse_iso(tr.get('first_valid_tick_at'))
        if watch_dt is not None and (raw_dt is None or watch_dt.date() == today):
            watch_admitted.add(sym)
        if raw_dt is not None and watch_dt is not None and raw_dt.date() == today and watch_dt.date() == today:
            watch_latencies[sym] = round(max(0.0, (watch_dt - raw_dt).total_seconds()), 3)
        if raw_dt is not None and tick_dt is not None and raw_dt.date() == today and tick_dt.date() == today:
            tick_latencies[sym] = round(max(0.0, (tick_dt - raw_dt).total_seconds()), 3)

    for sym, w_row in (lookup or {}).items():
        if sym not in watch_latencies and isinstance(w_row, dict):
            raw_dt = _parse_iso(w_row.get('first_raw_at'))
            watch_dt = _parse_iso(w_row.get('first_watch_at'))
            if raw_dt is not None and watch_dt is not None and raw_dt.date() == today and watch_dt.date() == today:
                watch_latencies[sym] = round(max(0.0, (watch_dt - raw_dt).total_seconds()), 3)

    def _latency_stats(mapping):
        vals = sorted(mapping.values())
        if not vals:
            return {'count': 0, 'min': None, 'median': None, 'max': None, 'by_symbol': {}}
        return {
            'count': len(vals),
            'min': vals[0],
            'median': round(float(median(vals)), 3),
            'max': vals[-1],
            'by_symbol': mapping,
        }

    # Unique signals by setup (WATCH & SHADOW)
    decisions = getattr(engine, 'decisions', None)
    sig_keys = getattr(decisions, 'signal_keys_by_setup', {}) if decisions is not None else {}
    watch_signals = {k: set(sig_keys.get(k, set())) for k in ('FAST', 'A', 'B', 'C')}
    eng_fast = getattr(engine, 'fast_strategy', None)
    if eng_fast is not None:
        for item in getattr(eng_fast, 'signal_history', ()):
            watch_signals['FAST'].add((item.get('symbol'), item.get('limit'), item.get('stop'), item.get('target')))

    shadow_signals = {k: set() for k in ('FAST', 'A', 'B', 'C')}
    if shadow_obs is not None:
        sh_fast = getattr(shadow_obs, 'fast_strategy', None)
        if sh_fast is not None:
            for item in getattr(sh_fast, 'signal_history', ()):
                shadow_signals['FAST'].add((item.get('symbol'), item.get('limit'), item.get('stop'), item.get('target')))
        for sym, ev in shadow_latest.items():
            vsig = ev.get('virtual_signal')
            if isinstance(vsig, dict) and vsig.get('pattern') in shadow_signals:
                pat = vsig['pattern']
                shadow_signals[pat].add((sym, vsig.get('limit'), vsig.get('stop'), vsig.get('target')))

    watch_signal_counts = {k: len(v) for k, v in watch_signals.items()}
    shadow_signal_counts = {k: len(v) for k, v in shadow_signals.items()}
    total_signal_counts = {k: len(watch_signals[k] | shadow_signals[k]) for k in ('FAST', 'A', 'B', 'C')}

    # Rejection reasons
    rejections = Counter()
    if decisions is not None and hasattr(decisions, 'rejection_counts'):
        rejections.update(decisions.rejection_counts)
    if eng_fast is not None and hasattr(eng_fast, 'rejection_counts'):
        for r_key, r_cnt in eng_fast.rejection_counts.items():
            rejections[f'FAST_{r_key}'] += r_cnt
    audit = getattr(engine, 'candidate_audit', None)
    if audit is not None and getattr(audit, 'started', False):
        try:
            snap = audit.summary(now_dt.isoformat(), engine.capital_context())
            for r_key, r_cnt in (snap.get('no_buy_reasons') or {}).items():
                if r_key not in rejections:
                    rejections[r_key] += r_cnt
        except Exception:
            pass

    buy_candidates_count = len(getattr(decisions, 'buy_candidates', set())) if decisions is not None else 0
    buy_candidates_count = max(buy_candidates_count, sum(watch_signal_counts.values()))

    # Orders summary (BUY orders today)
    day_str = today.isoformat()
    buy_orders = [
        o for o in engine.s.get('orders', {}).values()
        if o.get('side') == 'BUY' and str(o.get('created', day_str)).startswith(day_str)
    ]
    orders_sent_count = len(buy_orders)
    orders_filled_count = sum(1 for o in buy_orders if o.get('status') == 'FILLED' or o.get('filled', 0) > 0)
    orders_open_count = sum(1 for o in buy_orders if o.get('status') in {'SUBMITTING', 'OPEN', 'PARTIAL', 'UNKNOWN'})
    canceled_or_rejected = [o for o in buy_orders if o.get('status') in {'CANCELED', 'REJECTED'}]
    cancel_reasons = Counter(
        (o.get('cancel_reason') or o.get('reason') or o.get('status') or 'UNKNOWN')
        for o in canceled_or_rejected
    )

    # Missed rising candidates due to observation/promotion delay
    shadow_fast_symbols = {item[0] for item in shadow_signals['FAST'] if item and item[0]}
    bought_symbols = {o.get('symbol') for o in buy_orders if o.get('symbol')}
    missed_candidates = []
    for sym in discovered_symbols:
        row = row_map.get(sym) or shadow_rows.get(sym) or {}
        sf = shadow_first.get(sym, {})
        tr = timeline_rows.get(sym, {})
        first_price = row.get('first_raw_price') or sf.get('first_raw_price') or row.get('reference_price')
        curr_price = (
            engine.prices.get(sym)
            or (detector.last[sym].price if detector is not None and sym in detector.last else None)
            or shadow_latest.get(sym, {}).get('current_price')
            or row.get('price')
            or row.get('reference_price')
        )
        gain_from_raw_pct = None
        if isinstance(first_price, (int, float)) and first_price > 0 and isinstance(curr_price, (int, float)) and curr_price > 0:
            gain_from_raw_pct = round((curr_price - first_price) / first_price * 100.0, 2)
        day_gain_pct = row.get('gain')
        effective_gain = gain_from_raw_pct if gain_from_raw_pct is not None else (float(day_gain_pct) if isinstance(day_gain_pct, (int, float)) else 0.0)
        in_watch = sym in (lookup or {})
        w_lat = watch_latencies.get(sym)
        had_shadow_fast = sym in shadow_fast_symbols
        if sym in bought_symbols:
            continue
        if effective_gain > 0.0 or had_shadow_fast:
            if not in_watch:
                miss_reason = 'SHADOW_FAST_NOT_IN_WATCH' if had_shadow_fast else 'NOT_PROMOTED_TO_WATCH'
            elif w_lat is not None and w_lat > 5.0:
                miss_reason = 'WATCH_PROMOTION_DELAYED'
            elif had_shadow_fast:
                miss_reason = 'SIGNAL_REJECTED_AT_ENTRY_GATE'
            else:
                continue
            missed_candidates.append({
                'symbol': sym,
                'name': row.get('name') or engine.names.get(sym, sym),
                'status': 'WATCH' if in_watch else row.get('status', 'SHADOW' if sym in shadow_admitted else 'RAW'),
                'gain_from_raw_pct': gain_from_raw_pct,
                'day_gain_pct': day_gain_pct,
                'watch_promotion_latency_seconds': w_lat,
                'had_shadow_fast_signal': had_shadow_fast,
                'last_rejection_reason': (
                    eng_fast.last_evaluation.get(sym, {}).get('reason')
                    if eng_fast is not None and sym in eng_fast.last_evaluation
                    else row.get('reason')
                ),
                'miss_reason': miss_reason,
            })

    missed_candidates.sort(
        key=lambda x: (
            1 if x['had_shadow_fast_signal'] else 0,
            x['gain_from_raw_pct'] if x['gain_from_raw_pct'] is not None else (x['day_gain_pct'] or 0.0),
        ),
        reverse=True,
    )

    zero_buy = (orders_filled_count == 0 and len(engine.s.get('positions', {})) == 0)
    return {
        'timestamp': now_dt.isoformat(),
        'window': '09:00~09:10',
        'zero_buy_alert': zero_buy,
        'discovered_unique_symbols': len(discovered_symbols),
        'shadow_admitted_count': len(shadow_admitted),
        'watch_admitted_count': len(watch_admitted),
        'watch_promotion_latency_seconds': _latency_stats(watch_latencies),
        'raw_to_first_tick_latency_seconds': _latency_stats(tick_latencies),
        'unique_signals_by_setup': total_signal_counts,
        'watch_unique_signals_by_setup': watch_signal_counts,
        'shadow_unique_signals_by_setup': shadow_signal_counts,
        'buy_candidates_count': buy_candidates_count,
        'rejection_counts_by_reason': dict(rejections.most_common(30)),
        'orders_sent_count': orders_sent_count,
        'orders_filled_count': orders_filled_count,
        'orders_open_count': orders_open_count,
        'orders_canceled_or_rejected_count': len(canceled_or_rejected),
        'cancel_or_reject_reasons': dict(cancel_reasons),
        'missed_rising_candidates': missed_candidates[:10],
        'SHADOW_ONLY': True,
        'actual_order_permission': False,
    }


def check_opening_funnel_diagnostic(engine, detector, shadow, radar, lookup, rows, stamp, root):
    """09:10:00 도달 시 매수 체결이 0건이면 자동으로 10분 진입 퍼널 진단을 집계·기록한다."""
    now_dt = _parse_iso(stamp) or engine.clock()
    if getattr(engine, 'opening_funnel_reported_day', None) == now_dt.date():
        return None
    t_str = now_dt.strftime('%H:%M:%S')
    if not ('09:10:00' <= t_str < '09:15:00'):
        return None
    engine.opening_funnel_reported_day = now_dt.date()
    report = build_opening_funnel_diagnostic(engine, detector, shadow, radar, lookup, rows, now_dt)
    try:
        atomic_json(Path(root) / 'runtime' / 'opening_funnel_diagnostic.json', report, fsync=False)
    except Exception:
        pass
    if report['zero_buy_alert']:
        engine.event('OPENING_10M_FUNNEL_DIAGNOSTIC', **report)
    return report
