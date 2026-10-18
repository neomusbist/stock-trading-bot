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
from core.util import tick_size


class PriceContext:
    def __init__(self, limit=256,bar_limit=30):
        self.limit=limit;self.bar_limit=bar_limit;self.rows=OrderedDict();self.day=None

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
            snapshots=sig.get('decision_bar_snapshot') or sig.get('fast_reference_bar_snapshot') or []
            for raw in sorted(snapshots,key=lambda b:b.get('time','')):
                try:
                    bar={**raw,'time':datetime.fromisoformat(raw['time']),'venue':sig.get('venue')}
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
        row=self.rows.setdefault(symbol,{'bars':deque(maxlen=self.bar_limit),'peaks':deque(maxlen=128),
            'first_observed_at':stamp.isoformat(),'last_complete_at':None})
        venue=bar.get('venue')
        if venue:
            if row.get('context_venue') not in (None,venue):
                self.rows.pop(symbol,None)
                return self.record(symbol,bar,asof,stale_seconds)
            row['context_venue']=venue
        if row['bars'] and stamp<=row['bars'][-1]['time']:return False
        row['bars'].append({**{k:deepcopy(bar[k]) for k in ('time','open','high','low','close')},
            'first_second':bar.get('first_second',0),'gap_reason':bar.get('gap_reason')})
        if self.bar_limit>30 and 'volume' in bar:row['bars'][-1]['volume']=bar['volume']
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

    def merge_history(self,symbol,bars,received,venue):
        """REST references do not populate Detector.history, buffers or observation_start."""
        if self.day!=received.date():self.rows.clear();self.day=received.date()
        prior=self.rows.get(symbol)
        if prior and prior.get('context_venue')!=venue:prior=None
        merged={b['time']:dict(b) for b in bars}
        last_rest=max(merged,default=None)
        if prior:merged.update({b['time']:dict(b) for b in prior['bars'] if last_rest is None or b['time']>last_rest})
        self.rows.pop(symbol,None)
        for stamp in sorted(merged):self.record(symbol,merged[stamp],received)
        if symbol in self.rows:
            self.rows[symbol].update(context_venue=venue,history_received_at=received.isoformat())

    def reference_bars(self,symbol,asof,venue=None):
        row=self.rows.get(symbol) if self.day==asof.date() else None
        if not row or (venue and row.get('context_venue')!=venue):return []
        received=row.get('history_received_at')
        if received and datetime.fromisoformat(received)>asof:return []
        return [deepcopy(b) for b in row['bars'] if b['time']+timedelta(minutes=1)<=asof]

    def snapshot(self,symbol,tick,entry,support=None,breakout=None):
        row=self.rows.get(symbol) if self.day==tick.time.date() else None
        def _active_peak(p):
            if p['confirmed_at']>tick.time.isoformat() or (p.get('cleared_at') and p['cleared_at']<=tick.time.isoformat()):
                return False
            if p['price']>=entry:
                return True
            step=tick_size(p['price'])
            if entry<=p['price']+step:
                return True
            if entry<=p['price']*1.005 and not (support is not None and support>=p['price'] and breakout is not None and breakout>p['price']):
                return True
            return False
        peaks=[p for p in row['peaks'] if _active_peak(p)] if row else []
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
        if dst and src.get('context_venue') and dst.get('context_venue')!=src['context_venue']:
            self.rows.pop(symbol,None);dst=None
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
            dst['bars']=deque([bars[k] for k in sorted(bars)][-self.bar_limit:],maxlen=self.bar_limit)
            dst['peaks']=deque([peaks[k] for k in sorted(peaks)][-128:],maxlen=128)
            dst['first_observed_at']=min(dst['first_observed_at'],src['first_observed_at'])
            dst['last_complete_at']=max(dst['last_complete_at'],src['last_complete_at'])
            if src.get('history_received_at'):
                dst['history_received_at']=max(dst.get('history_received_at',''),src['history_received_at'])
                dst['context_venue']=src.get('context_venue')
        self.rows.move_to_end(symbol)
        while len(self.rows)>self.limit:self.rows.popitem(last=False)

    def dump_cache(self):
        """Serialize current day's observed reference bars and confirmed peaks."""
        if not self.day:return {}
        return {
            'day': self.day.isoformat(),
            'rows': {
                sym: {
                    'bars': [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in b.items()} for b in row['bars']],
                    'peaks': list(row['peaks']),
                    'first_observed_at': row['first_observed_at'],
                    'last_complete_at': row['last_complete_at'],
                    'context_venue': row.get('context_venue'),
                    'history_received_at': row.get('history_received_at')
                }
                for sym, row in self.rows.items()
            }
        }

    def load_cache(self, data, asof):
        """Restore reference bars and peaks from same-day cache without touching tick warmup."""
        if not data or not data.get('day'):return 0
        try:
            cache_day = datetime.fromisoformat(data['day']).date() if isinstance(data['day'], str) else data['day']
            if cache_day != asof.date():return 0
        except (TypeError, ValueError):
            return 0
        added = 0
        for sym, row_data in data.get('rows', {}).items():
            for raw_bar in row_data.get('bars', []):
                try:
                    bar = dict(raw_bar)
                    if isinstance(bar.get('time'), str):
                        bar['time'] = datetime.fromisoformat(bar['time'])
                    if bar['time'].date() == asof.date() and bar['time'] + timedelta(minutes=1) <= asof:
                        added += bool(self.record(sym, bar, asof))
                except (KeyError, TypeError, ValueError):
                    continue
        return added
