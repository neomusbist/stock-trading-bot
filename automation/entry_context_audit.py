"""Point-in-time entry diagnostics. No broker, state mutation or BUY permission."""
import math
from datetime import datetime,timedelta
from automation.allocation import efficiency

def confirmed_overhead(bars,asof,entry):
    """A local peak needs both neighboring complete, consecutive minute bars."""
    complete=[]
    for bar in bars or []:
        try:
            stamp=bar['time']
            if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
            high=float(bar['high'])
            if stamp.date()!=asof.date() or stamp+timedelta(minutes=1)>asof:continue
            if not math.isfinite(high) or high<=0:continue
            complete.append((stamp,high))
        except (KeyError,TypeError,ValueError):continue
    complete.sort()
    peaks=[]
    for left,center,right in zip(complete,complete[1:],complete[2:]):
        if center[0]-left[0]!=timedelta(minutes=1) or right[0]-center[0]!=timedelta(minutes=1):continue
        if center[1]>entry and center[1]>=left[1] and center[1]>=right[1] and center[1]>min(left[1],right[1]):
            peaks.append({'price':center[1],'time':center[0].isoformat(),'confirmed_at':(right[0]+timedelta(minutes=1)).isoformat()})
    return min(peaks,key=lambda p:p['price']) if peaks else None

def review_entry_context(signal,costs,minimum_rr,minimum_efficiency):
    result={'diagnostic_only':True,'actual_order_permission':False,'live_gate_enabled':False,
        'known_day_high':None,'day_high_basis':'NOT_RECONSTRUCTED_FROM_TRUNCATED_SNAPSHOT'}
    try:
        entry=float(signal['limit']);stop=float(signal['stop']);target=float(signal['target'])
        stamp=signal.get('time') or signal.get('timestamp')
        asof=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(stamp)
        if not 0<stop<entry<target:return {**result,'status':'UNKNOWN','reason':'INVALID_STRUCTURE'}
        peak=confirmed_overhead(signal.get('decision_bar_snapshot',[]),asof,entry)
        reviewed=min(target,peak['price']) if peak else target
        inputs=signal.get('efficiency',{}).get('inputs',{})
        score=efficiency(entry,stop,reviewed,inputs.get('confidence'),inputs.get('planned_minutes'),costs,minimum_efficiency)
        original_rr=(target-entry)/(entry-stop);reviewed_rr=(reviewed-entry)/(entry-stop)
        return {**result,'status':'CONFIRMED_LOCAL_OVERHEAD' if peak else 'NO_CONFIRMED_PEAK_IN_SNAPSHOT',
            'asof':asof.isoformat(),'entry':entry,'invalidation':stop,'original_target':target,'reviewed_target':reviewed,
            'nearest_confirmed_peak':peak,'original_rr':original_rr,'reviewed_rr':reviewed_rr,'required_rr':minimum_rr,
            'reviewed_rr_pass':reviewed_rr>=minimum_rr,'reviewed_economic':score,
            'trend_5m':signal.get('market_context',{}).get('trend_5m'),
            'trend_15m':signal.get('market_context',{}).get('trend_15m'),
            'limitation':'Snapshot is bounded; missing earlier/session peaks are UNKNOWN, not absent.'}
    except (KeyError,TypeError,ValueError,OverflowError):return {**result,'status':'UNKNOWN','reason':'INCOMPLETE_ENTRY_EVIDENCE'}
