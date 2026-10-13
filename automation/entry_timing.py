"""Bounded point-in-time opportunity timing. Diagnostics never grant order permission."""
from collections import OrderedDict
from datetime import datetime


class ObservationTimeline:
    """Small day-specific evidence file, never a restored trading/approval state."""
    def __init__(self,path=None):
        self.path=path;self.day=None;self.rows=OrderedDict();self.saved_at=None
        if path:
            import json
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                self.day=datetime.fromisoformat(data['day']).date()
                self.rows=OrderedDict(list(data['symbols'].items())[-8192:])
            except (OSError,ValueError,KeyError,TypeError):self.day=None;self.rows.clear()

    def row(self,symbol,stamp):
        if self.day!=stamp.date():self.rows.clear();self.day=stamp.date();self.saved_at=None
        row=self.rows.setdefault(symbol,{})
        # Reject corrupt/future/previous-day persisted times, rather than
        # projecting them into today's chronology.
        for key in list(row):
            if key.endswith('_at') and known_stamp(row[key],stamp) is None:row.pop(key)
        self.rows.move_to_end(symbol)
        while len(self.rows)>8192:self.rows.popitem(last=False)
        return row

    def raw(self,rows,stamp):
        for item in rows:
            row=self.row(item['code'],stamp)
            first=known_stamp((item.get('discovery_context') or {}).get('first_raw_at'),stamp) or known_stamp(item.get('first_raw_at'),stamp)
            if first and (not row.get('first_raw_at') or first<row['first_raw_at']):row['first_raw_at']=first

    def watch(self,symbol,stamp,rejoined=True):
        row=self.row(symbol,stamp);row.setdefault('first_watch_at',stamp.isoformat())
        if rejoined:row['last_watch_rejoin_at']=stamp.isoformat()
        return dict(row)

    def tick(self,symbol,stamp,bars,price=None):
        row=self.row(symbol,stamp);row['last_normal_tick_at']=stamp.isoformat()
        if price is not None:row['last_trade_price']=price
        if bars:row['last_valid_completed_bar_at']=bars[-1]['time'].isoformat()

    def signal(self,symbol,stamp):
        row=self.row(symbol,stamp);row.setdefault('first_valid_signal_at',stamp.isoformat())

    def save(self,stamp):
        if self.path and self.day==stamp.date() and (self.saved_at is None or (stamp-self.saved_at).total_seconds()>=5):
            from core.util import atomic_json
            atomic_json(self.path,{'day':self.day.isoformat(),'symbols':self.rows,'diagnostic_only':True,
                                   'actual_order_permission':False,'restores_ticks_or_bars':False})
            self.saved_at=stamp


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
    timeline=getattr(engine,'observation_timeline',None)
    persisted={}
    if timeline is not None:
        timeline.signal(symbol,asof)
        persisted=timeline.row(symbol,asof)
    state=getattr(engine,'_opportunity_timing',None)
    if not isinstance(state,dict) or state.get('day')!=asof.date():
        state={'day':asof.date(),'symbols':OrderedDict()};engine._opportunity_timing=state
    rows=state['symbols'];row=rows.setdefault(symbol,{})
    known=[known_stamp(v,asof) for v in ((watch.get('discovery_context') or {}).get('first_raw_at'),watch.get('first_raw_at'),persisted.get('first_raw_at'))]
    first=min((v for v in known if v),default=None)
    if first and (not row.get('first_raw_at') or first<row['first_raw_at']):row['first_raw_at']=first
    known=[known_stamp(v,asof) for v in (watch.get('first_watch_at'),persisted.get('first_watch_at'))]
    watch_at=min((v for v in known if v),default=None)
    if watch_at and (not row.get('first_watch_at') or watch_at<row['first_watch_at']):row['first_watch_at']=watch_at
    # Persisted chronology is evidence, never restored trading/warmup state.
    earlier=known_stamp(persisted.get('first_valid_signal_at'),asof)
    row['first_detector_signal_at']=min(v for v in (row.get('first_detector_signal_at'),earlier,signal['time']) if v)
    signal['candidate_detected_at']=row.get('first_raw_at')
    signal['latest_radar_cycle_at']=known_stamp(watch.get('observed_at'),asof)
    signal['opportunity_timing']={**row,
        'observation_started_at':known_stamp(getattr(detector,'observation_start',{}).get(symbol),asof),
        'signal_at':signal['time'],'scope':'DAY_TIMELINE_AND_CURRENT_PROCESS' if timeline is not None else 'OBSERVED_THIS_PROCESS_OR_EXPLICIT_DISCOVERY_CONTEXT',
        'missing_earlier_opportunities':'UNKNOWN','diagnostic_only':True}
    rows.move_to_end(symbol)
    while len(rows)>512:rows.popitem(last=False)


def mark_risk_eligible(engine,signal,stamp):
    state=getattr(engine,'_opportunity_timing',{})
    row=state.get('symbols',{}).get(signal['symbol'])
    if row is not None:
        row.setdefault('first_risk_eligible_at',stamp.isoformat())
        signal.setdefault('opportunity_timing',{}).update(first_risk_eligible_at=row['first_risk_eligible_at'])
