"""Bounded, observed price references; never used as A/B/C warmup bars.

Confirmed references survive role changes and a transport reset. They expire at
the trading date boundary. Unknown earlier history is never filled from a later
chart. Keeping references does not assert continuity of current signal data.
"""
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime,timedelta
import math
from automation.entry_context_audit import confirmed_overhead
from automation.market_context import trend_window


class PriceContext:
    def __init__(self, limit=256):
        self.limit=limit;self.rows=OrderedDict();self.day=None

    def bootstrap(self,signals,asof,stale_seconds=5):
        """Recover only already stored, pre-entry, complete references on this day.

        This does not recover missing ticks, current signal bars or warmup.
        A later decision snapshot cannot be used to confirm an earlier decision.
        """
        usable=[]
        for sig in signals:
            try:
                stamp=datetime.fromisoformat(sig['time'])
                if not stamp.tzinfo or stamp.date()!=asof.date() or stamp>asof:continue
                usable.append((stamp,sig))
            except (KeyError,TypeError,ValueError):continue
        added=0
        for stamp,sig in sorted(usable,key=lambda p:p[0]):
            for raw in sorted(sig.get('decision_bar_snapshot',[]),key=lambda b:b.get('time','')):
                try:
                    bar={**raw,'time':datetime.fromisoformat(raw['time'])}
                    added+=bool(self.record(sig['symbol'],bar,stamp,stale_seconds))
                except (KeyError,TypeError,ValueError):continue
        return {'recovered_complete_references':added,'symbols':len(self.rows),
            'scope':'SAVED_PRE_ENTRY_SNAPSHOTS_ONLY','restored_signal_bars':0,'restored_warmup':False}

    def record(self,symbol,bar,asof,stale_seconds=5):
        if self.day!=asof.date():self.rows.clear();self.day=asof.date()
        stamp=bar.get('time')
        if (not isinstance(stamp,datetime) or stamp.tzinfo is None or asof.tzinfo is None
                or stamp.second or stamp.microsecond or stamp.date()!=asof.date() or stamp+timedelta(minutes=1)>asof
                or bar.get('gap_reason') or bar.get('first_second',0)>stale_seconds):return False
        if any(type(bar.get(k)) not in (int,float) or not math.isfinite(bar[k]) or bar[k]<=0
               for k in ('open','high','low','close')):return False
        if not bar['low']<=min(bar['open'],bar['close'])<=max(bar['open'],bar['close'])<=bar['high']:return False
        row=self.rows.setdefault(symbol,{'bars':deque(maxlen=30),'peaks':deque(maxlen=128),
            'first_observed_at':stamp.isoformat(),'last_complete_at':None})
        if row['bars'] and stamp<=row['bars'][-1]['time']:return False
        row['bars'].append({k:deepcopy(bar[k]) for k in ('time','open','high','low','close')})
        row['last_complete_at']=(stamp+timedelta(minutes=1)).isoformat()
        # Retain the reference and its clearance evidence. A completed close over
        # an already confirmed peak retires that resistance; a later pullback
        # below it cannot revive the old pivot without a newly formed peak.
        for peak in row['peaks']:
            if (not peak.get('cleared_at') and bar['close']>peak['price']
                    and stamp.isoformat()>=peak['confirmed_at']):
                peak.update(cleared_at=row['last_complete_at'],cleared_by_close=bar['close'])
        if len(row['bars'])>=3:
            trio=list(row['bars'])[-3:]
            peak=confirmed_overhead(trio,asof,0)
            if peak and (not row['peaks'] or row['peaks'][-1]['time']!=peak['time']):row['peaks'].append(peak)
        self.rows.move_to_end(symbol)
        while len(self.rows)>self.limit:self.rows.popitem(last=False)
        return True

    def snapshot(self,symbol,tick,entry):
        row=self.rows.get(symbol) if self.day==tick.time.date() else None
        peaks=[p for p in row['peaks'] if p['price']>entry and p['confirmed_at']<=tick.time.isoformat()
            and (not p.get('cleared_at') or p['cleared_at']>tick.time.isoformat())] if row else []
        peak=min(peaks,key=lambda p:p['price']) if peaks else None
        return {'asof':tick.time.isoformat(),'nearest_confirmed_peak':deepcopy(peak),
            'known_peak_count':len(peaks),
            'cleared_peak_count':sum(bool(p.get('cleared_at') and p['cleared_at']<=tick.time.isoformat()) for p in row['peaks']) if row else 0,
            'first_observed_at':row['first_observed_at'] if row else None,
            'last_complete_at':row['last_complete_at'] if row else None,
            'coverage':'OBSERVED_BOUNDED_HISTORY_ONLY','missing_earlier_history':'UNKNOWN',
            'trend_5m':trend_window(row['bars'] if row else [],tick,5),
            'trend_15m':trend_window(row['bars'] if row else [],tick,15),
            'signal_bars_reused':False}

    def transfer(self,source,symbol):
        if source is self or symbol not in source.rows:return
        src=source.rows[symbol]
        if self.day is not None and self.day!=source.day:return
        self.day=source.day;dst=self.rows.get(symbol)
        if dst is None:self.rows[symbol]=deepcopy(src)
        else:
            bars={b['time']:b for b in list(dst['bars'])+list(src['bars'])}
            peaks={}
            for p in list(dst['peaks'])+list(src['peaks']):
                prior=peaks.get(p['time'])
                # Role handoff may carry an older pre-clearance copy.
                if prior and prior.get('cleared_at') and not p.get('cleared_at'):continue
                if (prior and prior.get('cleared_at') and p.get('cleared_at')
                        and prior['cleared_at']<=p['cleared_at']):continue
                peaks[p['time']]=deepcopy(p)
            # A reset source may know a later completed close without carrying
            # the destination's older peak. Apply those closes before trimming
            # the merged history; a handoff must not leave cleared resistance.
            for peak in peaks.values():
                for key in sorted(bars):
                    bar=bars[key]
                    cleared=(key+timedelta(minutes=1)).isoformat()
                    if (key>=datetime.fromisoformat(peak['confirmed_at']) and bar['close']>peak['price']
                            and (not peak.get('cleared_at') or cleared<peak['cleared_at'])):
                        peak.update(cleared_at=cleared,cleared_by_close=bar['close'])
                        break
            dst['bars']=deque([bars[k] for k in sorted(bars)][-30:],maxlen=30)
            dst['peaks']=deque([peaks[k] for k in sorted(peaks)][-128:],maxlen=128)
            dst['first_observed_at']=min(dst['first_observed_at'],src['first_observed_at'])
            dst['last_complete_at']=max(dst['last_complete_at'],src['last_complete_at'])
        self.rows.move_to_end(symbol)
        while len(self.rows)>self.limit:self.rows.popitem(last=False)
