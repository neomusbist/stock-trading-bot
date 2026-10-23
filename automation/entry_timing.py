"""Bounded point-in-time opportunity timing. Diagnostics never grant order permission."""
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from threading import RLock
from collections import deque


def timeline_locked(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return call


class ObservationTimeline:
    """Small day-specific evidence file, never a restored trading/approval state."""
    def __init__(self,path=None):
        self.writer=None;self.diagnostic_fsync=False
        self.path=path;self.day=None;self.rows=OrderedDict();self.saved_at=None;self.validated_through=None
        self.lock=RLock();self.events=deque(maxlen=4096)
        if path:
            import json
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                self.day=datetime.fromisoformat(data['day']).date()
                self.rows=OrderedDict(list(data['symbols'].items())[-8192:])
            except (OSError,ValueError,KeyError,TypeError):self.day=None;self.rows.clear()

    @timeline_locked
    def row(self,symbol,stamp):
        if self.day!=stamp.date():
            self.rows.clear();self.events.clear();self.day=stamp.date();self.saved_at=None;self.validated_through=None
        # ACK/RAW callbacks carry their original receiver/source time and can
        # arrive after a later processing timestamp has already been recorded.
        # Only actual callback stamps advance this watermark; persisted fields
        # never make a future timestamp valid on first load.
        if self.validated_through is None or stamp>self.validated_through:self.validated_through=stamp
        row=self.rows.setdefault(symbol,{})
        # Reject corrupt/future/previous-day persisted times, rather than
        # projecting them into today's chronology.
        for key in list(row):
            if key.endswith('_at') and known_stamp(row[key],self.validated_through) is None:row.pop(key)
        self.rows.move_to_end(symbol)
        while len(self.rows)>8192:self.rows.popitem(last=False)
        return row

    def first(self,symbol,row,key,stamp,**evidence):
        if key in row:return
        row[key]=stamp.isoformat()
        if '09:00:00'<=stamp.strftime('%H:%M:%S')<'09:10:00':
            self.events.append({'symbol':symbol,'stage':key,'stage_at':stamp.isoformat(),
                                'diagnostic_only':True,'actual_order_permission':False,**evidence})

    @timeline_locked
    def raw(self,rows,stamp):
        for item in rows:
            row=self.row(item['code'],stamp)
            first=known_stamp((item.get('discovery_context') or {}).get('first_raw_at'),stamp) or known_stamp(item.get('first_raw_at'),stamp)
            if first and (not row.get('first_raw_at') or first<row['first_raw_at']):
                if 'first_raw_at' not in row:self.first(item['code'],row,'first_raw_at',datetime.fromisoformat(first))
                else:row['first_raw_at']=first

    @timeline_locked
    def watch(self,symbol,stamp,rejoined=True):
        row=self.row(symbol,stamp);self.first(symbol,row,'first_watch_at',stamp)
        if rejoined:row['last_watch_rejoin_at']=stamp.isoformat()
        return dict(row)

    @timeline_locked
    def subscription_request(self,symbols,stamp):
        for symbol in symbols:
            row=self.row(symbol,stamp);self.first(symbol,row,'first_subscription_request_at',stamp)
            row['last_subscription_request_at']=stamp.isoformat()

    @timeline_locked
    def subscription_ack(self,symbols,stamp):
        for symbol in symbols:
            row=self.row(symbol,stamp);self.first(symbol,row,'first_subscription_ack_at',stamp,
                ack_scope='GROUP_REG_ACK_NOT_INDIVIDUAL_SYMBOL')
            row['last_subscription_ack_at']=stamp.isoformat()
            row['subscription_ack_scope']='GROUP_REG_ACK_NOT_INDIVIDUAL_SYMBOL'

    @timeline_locked
    def tick(self,symbol,stamp,bars,price=None,received=None,processed=None,execution_fresh=True):
        received=received or stamp;processed=processed or received
        row=self.row(symbol,processed);row['last_normal_tick_at']=stamp.isoformat()
        self.first(symbol,row,'first_normal_tick_at',stamp)
        self.first(symbol,row,'first_tick_received_at',received)
        self.first(symbol,row,'first_tick_processed_at',processed)
        if execution_fresh:self.first(symbol,row,'first_execution_fresh_tick_at',processed,
            trade_time=stamp.isoformat(),received_at=received.isoformat())
        if price is not None:row['last_trade_price']=price
        if bars:
            row['last_valid_completed_bar_at']=bars[-1]['time'].isoformat()
            self.first(symbol,row,'first_completed_bar_start_at',bars[0]['time'])
            self.first(symbol,row,'first_completed_bar_known_at',processed,
                bar_source='RECEIVED_REGULAR_TICKS',bar_start_at=bars[0]['time'].isoformat())

    @timeline_locked
    def signal(self,symbol,stamp):
        row=self.row(symbol,stamp);self.first(symbol,row,'first_valid_signal_at',stamp)

    @timeline_locked
    def risk_eligible(self,symbol,stamp):
        row=self.row(symbol,stamp);self.first(symbol,row,'first_risk_eligible_at',stamp,
            eligibility_scope='LOCAL_ENTRY_GATES_PASS_FINAL_HTTP_GUARDS_STILL_REQUIRED')

    @timeline_locked
    def drain_events(self):
        result=list(self.events);self.events.clear();return result

    @timeline_locked
    def opening_snapshot(self,stamp):
        def delay(row,left,right):
            a=known_stamp(row.get(left),stamp);b=known_stamp(row.get(right),stamp)
            value=(datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds() if a and b else None
            return value if value is not None and value>=0 else None
        def asof_row(row):
            result={k:v for k,v in row.items() if not k.endswith('_at') or known_stamp(v,stamp) is not None}
            if 'last_normal_tick_at' in row and 'last_normal_tick_at' not in result:result.pop('last_trade_price',None)
            return result
        return {'asof':stamp.isoformat(),'regular_epoch':stamp.replace(hour=9,minute=0,second=0,microsecond=0).isoformat(),
            'actual_order_permission':False,'restored_warmup':False,
            'symbols':{s:{**asof_row(r),
                'observed_before_watch':bool(known_stamp(r.get('first_tick_received_at'),stamp) and known_stamp(r.get('first_watch_at'),stamp)
                    and r['first_tick_received_at']<r['first_watch_at']),
                'latency_seconds':{
                'raw_to_watch':delay(r,'first_raw_at','first_watch_at'),
                'watch_to_subscription_ack':delay(r,'first_watch_at','first_subscription_ack_at'),
                'subscription_ack_to_tick':delay(r,'first_subscription_ack_at','first_tick_received_at'),
                'receiver_to_processing':delay(r,'first_tick_received_at','first_tick_processed_at'),
                'tick_to_completed_bar':delay(r,'first_execution_fresh_tick_at','first_completed_bar_known_at'),
                'completed_bar_to_signal':delay(r,'first_completed_bar_known_at','first_valid_signal_at'),
                'signal_to_risk_eligible':delay(r,'first_valid_signal_at','first_risk_eligible_at')}}
                for s,r in self.rows.items()} if self.day==stamp.date() else {}}

    @timeline_locked
    def save(self,stamp):
        if self.path and self.day==stamp.date() and (self.saved_at is None or (stamp-self.saved_at).total_seconds()>=5):
            from core.util import atomic_json
            snapshot={'day':self.day.isoformat(),'symbols':{s:dict(r) for s,r in self.rows.items()},'diagnostic_only':True,
                                   'actual_order_permission':False,'restores_ticks_or_bars':False,
                                   'opening':self.opening_snapshot(stamp) if '09:00:00'<=stamp.strftime('%H:%M:%S')<'09:10:00' else None}
            if self.writer:self.writer(self.path,snapshot)
            else:atomic_json(self.path,snapshot,fsync=self.diagnostic_fsync)
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
    timeline=getattr(engine,'observation_timeline',None)
    if timeline is not None:timeline.risk_eligible(signal['symbol'],stamp)
    state=getattr(engine,'_opportunity_timing',{})
    row=state.get('symbols',{}).get(signal['symbol'])
    if row is not None:
        row.setdefault('first_risk_eligible_at',stamp.isoformat())
        signal.setdefault('opportunity_timing',{}).update(first_risk_eligible_at=row['first_risk_eligible_at'])
