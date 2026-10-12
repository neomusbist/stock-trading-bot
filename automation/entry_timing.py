"""Bounded point-in-time opportunity timing. Diagnostics never grant order permission."""
from collections import OrderedDict
from datetime import datetime


def known_stamp(value,asof):
    try:
        stamp=value if isinstance(value,datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is not None and stamp.date()==asof.date() and stamp<=asof:return stamp.isoformat()
    except (TypeError,ValueError):pass
    return None


def watch_started(row,prior,stamp):
    """Preserve a known WATCH transition during same-day metadata refreshes."""
    try:parsed=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(stamp)
    except (TypeError,ValueError):return None
    if parsed.tzinfo is None:return None
    return known_stamp((prior or {}).get('first_watch_at'),parsed) or known_stamp(row.get('first_watch_at'),parsed) or parsed.isoformat()


def annotate_signal(engine,detector,watch,signal):
    asof=datetime.fromisoformat(signal['time']);symbol=signal['symbol']
    state=getattr(engine,'_opportunity_timing',None)
    if not isinstance(state,dict) or state.get('day')!=asof.date():
        state={'day':asof.date(),'symbols':OrderedDict()};engine._opportunity_timing=state
    rows=state['symbols'];row=rows.setdefault(symbol,{})
    first=known_stamp((watch.get('discovery_context') or {}).get('first_raw_at'),asof) or known_stamp(watch.get('first_raw_at'),asof)
    if first and (not row.get('first_raw_at') or first<row['first_raw_at']):row['first_raw_at']=first
    watch_at=known_stamp(watch.get('first_watch_at'),asof)
    if watch_at and (not row.get('first_watch_at') or watch_at<row['first_watch_at']):row['first_watch_at']=watch_at
    row.setdefault('first_detector_signal_at',signal['time'])
    signal['candidate_detected_at']=row.get('first_raw_at')
    signal['latest_radar_cycle_at']=known_stamp(watch.get('observed_at'),asof)
    signal['opportunity_timing']={**row,
        'observation_started_at':known_stamp(getattr(detector,'observation_start',{}).get(symbol),asof),
        'signal_at':signal['time'],'scope':'OBSERVED_THIS_PROCESS_OR_EXPLICIT_DISCOVERY_CONTEXT',
        'missing_earlier_opportunities':'UNKNOWN','diagnostic_only':True}
    rows.move_to_end(symbol)
    while len(rows)>512:rows.popitem(last=False)


def mark_risk_eligible(engine,signal,stamp):
    state=getattr(engine,'_opportunity_timing',{})
    row=state.get('symbols',{}).get(signal['symbol'])
    if row is not None:
        row.setdefault('first_risk_eligible_at',stamp.isoformat())
        signal.setdefault('opportunity_timing',{}).update(first_risk_eligible_at=row['first_risk_eligible_at'])
