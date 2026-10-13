"""An AFTER burst thesis can fail before one price-risk unit is earned."""
from datetime import datetime,timedelta
import math
from automation.trade_quality import complete_sequence


def after_momentum_failure(position,asof):
    sig=position.get('signal',{})
    evidence={'asof':asof.isoformat(),'policy':'AFTER_BURST_FAILED_REBOUND',
              'initial_stop':position.get('initial_stop'),'one_r_required':False}
    acceleration=sig.get('evaluation_gates',{}).get('acceleration',{}).get('values',{})
    if sig.get('session')!='AFTER' or sig.get('pattern')!='A' or acceleration.get('mode')=='CONFIRMED_CONTINUATION':
        return False,{**evidence,'reason':'NOT_AFTER_BURST'}
    try:
        opened=datetime.fromisoformat(position['opened'])
        if opened.tzinfo is None or not isinstance(asof,datetime) or asof.tzinfo is None:
            raise ValueError('timezone required')
    except (KeyError,TypeError,ValueError):return None,{**evidence,'reason':'ENTRY_TIME_UNAVAILABLE'}
    raw=position.get('protection_bars',[])
    size=min(len(raw),30)
    first_full=opened.replace(second=0,microsecond=0)+(timedelta(minutes=1) if opened.second or opened.microsecond else timedelta())
    bars=complete_sequence(raw,asof,size,first_full,require_recent=True) if size>=5 else []
    if not bars:return None,{**evidence,'reason':'COMPLETE_POST_ENTRY_SEQUENCE_UNAVAILABLE'}
    # A retest of the same peak also retires the older failed-rebound thesis.
    peak_i=max(range(len(bars)),key=lambda i:(bars[i]['high'],i))
    peak=bars[peak_i]['high'];known=position.get('after_entry_peak')
    if known and known.get('price',0)>peak:
        try:known_at=datetime.fromisoformat(known['time'])
        except (KeyError,TypeError,ValueError):return None,{**evidence,'reason':'PEAK_TIME_UNAVAILABLE'}
        if known_at.tzinfo is None or not opened<=known_at<bars[0]['time']:return None,{**evidence,'reason':'PEAK_OUTSIDE_RECENT_SEQUENCE'}
        peak=known['price'];peak_i=-1
    elif position.get('high',peak)>peak:return None,{**evidence,'reason':'PEAK_OUTSIDE_RECENT_SEQUENCE'}
    if peak<=position['avg']:return False,{**evidence,'reason':'NO_POST_ENTRY_IMPULSE'}
    # A lower rebound must exist before the final decline; a single quiet pause
    # or the first controlled pullback cannot declare a failed continuation.
    rebound=None
    for i in range(max(peak_i+2,1),len(bars)-3):
        before,b=bars[i-1],bars[i]
        if (b['close']>before['high'] and b['high']<peak
                and any(x['close']<bars[max(peak_i,0)]['close'] for x in bars[max(peak_i+1,0):i])):
            rebound=b
    last=bars[-3:]
    falling=all(a['high']>b['high'] and a['low']>b['low'] and a['close']>b['close'] for a,b in zip(last,last[1:]))
    volumes=[b.get('volume') for b in last]
    if not all(type(v) in (int,float) and math.isfinite(v) and v>=0 for v in volumes):
        return None,{**evidence,'reason':'DECLINE_VOLUME_UNAVAILABLE'}
    contracting=volumes[-1]<=volumes[0]
    # Expanding sell volume must not veto an already confirmed price failure.
    # Volume alone also cannot force an exit during a healthy pullback.
    failed=bool(rebound and falling and last[-1]['close']<rebound['low'] and max(b['high'] for b in last)<peak)
    evidence.update(known_peak=peak,rebound_time=rebound['time'].isoformat() if rebound else None,
        rebound_floor=rebound['low'] if rebound else None,
        decline_times=[b['time'].isoformat() for b in last],closes=[b['close'] for b in last],
        highs=[b['high'] for b in last],lows=[b['low'] for b in last],volumes=volumes,
        confirmed_falling=falling,volume_contracting=contracting)
    return failed,{**evidence,'reason':'AFTER_BURST_FAILED_REBOUND' if failed else 'AFTER_THESIS_NOT_CONFIRMED_FAILED'}
