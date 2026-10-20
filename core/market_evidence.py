"""Pure provenance gates for research signals; no broker or filesystem access."""
from datetime import datetime,timedelta
import math


def aware(value):
    try:
        value=datetime.fromisoformat(value) if isinstance(value,str) else value
        return value if isinstance(value,datetime) and value.tzinfo is not None else None
    except (TypeError,ValueError):return None


def finite(value,positive=True):
    return type(value) in (int,float) and math.isfinite(value) and (value>0 if positive else value>=0)


def bar_evidence(raw,*,row=None):
    row=row or {};start=aware(raw.get('time'))
    end=start+timedelta(minutes=1) if start and not (start.second or start.microsecond) else None
    source=raw.get('observation_source','UNKNOWN')
    known=aware(raw.get('available_at') or raw.get('known_at') or raw.get('recorded_at'))
    basis='EXPLICIT_BAR_AVAILABILITY' if known else 'UNKNOWN'
    if known is None and source=='REST_REFERENCE':
        known=aware(row.get('history_received_at'));basis='ROW_REST_RECEIPT' if known else 'UNKNOWN'
    reason=None
    if not end:reason='BAR_START_LABEL_INVALID'
    elif any(not finite(raw.get(k)) for k in ('open','high','low','close')):reason='OHLC_INVALID'
    elif not raw['low']<=min(raw['open'],raw['close'])<=max(raw['open'],raw['close'])<=raw['high']:reason='OHLC_ORDER_INVALID'
    elif known is not None and known<end:reason='BAR_AVAILABILITY_BEFORE_CLOSE'
    elif not known:reason='BAR_AVAILABILITY_UNKNOWN'
    elif raw.get('gap_reason') or raw.get('local_drop_count',0):reason='BAR_COVERAGE_GAP'
    elif raw.get('first_second',0)>5:reason='BAR_COVERAGE_START_UNKNOWN'
    elif source not in {'ACTUAL_RECEIVED','REST_REFERENCE'}:reason='BAR_SOURCE_UNKNOWN'
    return {'start':start,'end':end,'available_at':known,'source':source,
            'availability_basis':basis,'reason':reason,'valid':reason is None,
            'signal_source_verified':reason is None and source=='ACTUAL_RECEIVED'}


def known_signal_bars(bars,asof,required=5):
    if not aware(asof):return [],'AWARE_DECISION_TIME_REQUIRED'
    checked=[]
    for raw in bars:
        ev=bar_evidence(raw)
        if not ev['signal_source_verified']:return [],ev['reason'] or 'REFERENCE_IS_NOT_SIGNAL_COVERAGE'
        if ev['available_at']>asof or ev['end']>asof:return [],'FUTURE_OR_UNAVAILABLE_BAR'
        checked.append({**raw,'time':ev['start']})
    if len(checked)<required:return [],'INSUFFICIENT_KNOWN_BARS'
    if asof-(checked[-1]['time']+timedelta(minutes=1))>=timedelta(minutes=1):return [],'LATEST_COMPLETED_BAR_MISSING'
    if any((b['time']-a['time']).total_seconds()!=60 for a,b in zip(checked,checked[1:])):return [],'NONCONSECUTIVE_KNOWN_BARS'
    return checked,None
