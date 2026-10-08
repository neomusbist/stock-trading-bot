"""Point-in-time context only. No broker, order state, indicator gates or future bars."""
from collections import OrderedDict,deque
from datetime import datetime,timedelta
import math


def trend_window(bars,tick,minutes):
    completed=[b for b in bars if b.get('time') and b['time']+timedelta(minutes=1)<=tick.time]
    seq=completed[-minutes:]
    out={'minutes':minutes,'status':'UNKNOWN','completed_bars':len(seq),'reason':'INSUFFICIENT_COMPLETE_CONTEXT',
         'return':None,'higher_lows':None,'lower_highs':None}
    if len(seq)<minutes:return out
    if any((b['time']-a['time']).total_seconds()!=60 for a,b in zip(seq,seq[1:])):
        out['reason']='CONTEXT_GAP';return out
    first=seq[0].get('open',seq[0]['close']);last=seq[-1]['close']
    if first<=0:return out
    change=last/first-1
    higher=all(b['low']>=a['low'] for a,b in zip(seq,seq[1:]))
    lower=all(b['high']<=a['high'] for a,b in zip(seq,seq[1:]))
    out.update(status='UP' if change>0 and higher else 'DOWN' if change<0 and lower else 'MIXED',
               reason=None,**{'return':change},higher_lows=higher,lower_highs=lower,
               start=seq[0]['time'].isoformat(),end=(seq[-1]['time']+timedelta(minutes=1)).isoformat())
    return out


def symbol_context(bars,tick,index=None):
    context={'asof':tick.time.isoformat(),'diagnostic_only':True,'actual_order_permission':False,
             'trend_5m':trend_window(bars,tick,5),'trend_15m':trend_window(bars,tick,15),
             'market_regime':'UNKNOWN','market_reason':'INDEX_CONTEXT_UNAVAILABLE',
             'relative_return_5m':None}
    if index:
        for window in ('trend_5m','trend_15m'):
            a=context[window];b=index.get(window,{})
            if a.get('start')==b.get('start') and a.get('end')==b.get('end') and a.get('return') is not None and b.get('return') is not None:
                context['relative_return_'+window[6:]]=a['return']-b['return']
        context['market_regime']=index.get('trend_15m',{}).get('status','UNKNOWN')
        context['market_reason']=index.get('reason')
    return context


class IndexContext:
    """Two official 0J streams. Ratios do not assume a scale for raw index levels."""
    def __init__(self):self.bars={};self.current={};self.last={}
    def clear(self):self.bars.clear();self.current.clear();self.last.clear()
    def ingest(self,code,stamp,raw_value,received,raw_change=None):
        if code not in {'001','101'} or not stamp.tzinfo or stamp.date()!=received.date():return False
        if not '09:00:00'<=stamp.strftime('%H:%M:%S')<'15:30:00' or not 0<=(received-stamp).total_seconds()<=5:return False
        try:value=abs(float(raw_value))
        except (ValueError,TypeError):return False
        if not math.isfinite(value) or value<=0:return False
        prior=self.last.get(code)
        if prior and stamp<prior['time']:return False
        minute=stamp.replace(second=0,microsecond=0);bar=self.current.get(code)
        if bar and minute!=bar['time']:
            if minute-bar['time']==timedelta(minutes=1) and bar['complete_start']:
                self.bars.setdefault(code,deque(maxlen=30)).append(dict(bar))
            elif minute-bar['time']!=timedelta(minutes=1):self.bars.pop(code,None)
            bar=None
        if bar is None:
            bar={'time':minute,'open':value,'low':value,'high':value,'close':value,
                 'complete_start':bool(prior and prior['time'].replace(second=0,microsecond=0)==minute-timedelta(minutes=1)) or stamp.second<=5}
            self.current[code]=bar
        bar.update(low=min(bar['low'],value),high=max(bar['high'],value),close=value)
        self.last[code]={'time':stamp,'raw_level':raw_value,'raw_change_percent':raw_change}
        return True
    def snapshot(self,stamp):
        out={}
        for code,name in [('001','KOSPI'),('101','KOSDAQ')]:
            last=self.last.get(code);age=(stamp-last['time']).total_seconds() if last else None
            fresh=age is not None and 0<=age<=15 and stamp.strftime('%H:%M:%S')<'15:30:00'
            from types import SimpleNamespace
            tick=SimpleNamespace(time=stamp)
            out[name]={'asof':last['time'].isoformat() if last else None,'age_seconds':age,
                'source':'0J','index_code':code,'raw_level':last['raw_level'] if last else None,
                'raw_change_percent':last['raw_change_percent'] if last else None,
                'reason':None if fresh else 'INDEX_STALE_OR_UNAVAILABLE','fresh':fresh,
                'trend_5m':trend_window(self.bars.get(code,[]) if fresh else [],tick,5),
                'trend_15m':trend_window(self.bars.get(code,[]) if fresh else [],tick,15)}
        return out


class DiscoveryContext:
    """Bounded day-specific discovery evidence; morning preparation is never a BUY list."""
    def __init__(self,limit=256):self.limit=limit;self.day=None;self.rows=OrderedDict()
    def update(self,rows,stamp):
        if self.day!=stamp.date():self.rows.clear();self.day=stamp.date()
        out=[]
        for row in rows:
            symbol=row['code'];price=row.get('reference_price');prior=self.rows.get(symbol)
            first=prior['first_raw_at'] if prior else row.get('first_raw_at') or stamp.isoformat()
            first_price=prior['first_raw_price'] if prior else row.get('first_raw_price') or price
            delta=price/prior['price']-1 if prior and price and prior['price'] else None
            gap=(stamp-datetime.fromisoformat(prior['seen_at'])).total_seconds() if prior else None
            context={'first_raw_at':first,'first_raw_price':first_price,'last_raw_at':stamp.isoformat(),
                'since_first_return':price/first_price-1 if price and first_price else None,
                'last_cycle_return':delta,'comparison_seconds':gap,
                'activity':'NEW_DISCOVERY' if prior is None else 'RISING_RECENTLY' if delta is not None and delta>0 else 'FALLING_RECENTLY' if delta is not None and delta<0 else 'UNCHANGED_OR_UNKNOWN',
                'preopen_prepared':bool((prior or {}).get('preopen') or stamp.strftime('%H:%M:%S')<'09:00:00'),
                'diagnostic_only':True,'actual_order_permission':False}
            out.append({**row,'discovery_context':context})
            self.rows[symbol]={'first_raw_at':first,'first_raw_price':first_price,'price':price,'seen_at':stamp.isoformat(),'preopen':context['preopen_prepared']}
            self.rows.move_to_end(symbol)
        while len(self.rows)>self.limit:self.rows.popitem(last=False)
        return out
