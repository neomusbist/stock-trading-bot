"""Explicit A range-base hypothesis; no fixed-percent stop widening."""
import hashlib,json,math,statistics
from datetime import datetime,timedelta
from core.util import floor_tick,tick_size
from automation.trade_quality import complete_sequence
from automation.entry_context_audit import confirmed_overhead


def structural_breakout(raw,asof,setup,cfg,entry,bid,ask):
    evidence={'asof':asof.isoformat(),'policy':'CONFIRMED_RANGE_BASE_A',
              'scope':'KNOWN_PRE_DECISION_REFERENCE_BARS','earlier_history':'UNKNOWN'}
    rows=[]
    for row in raw or []:
        valid=complete_sequence([row],asof,1)
        if valid:rows.append(valid[0])
    rows=sorted(rows,key=lambda b:b['time'])[-120:]
    if len({b['time'] for b in rows})!=len(rows):return None,{**evidence,'reason':'DUPLICATE_REFERENCE_BAR'}
    span=(rows[-1]['time']-rows[0]['time']).total_seconds() if rows else 0
    opening=bool(rows and rows[0]['time'].strftime('%H:%M:%S')=='09:00:00')
    recent=complete_sequence(rows,asof,2 if opening else 3,require_recent=True)
    if not recent or (not opening and (len(rows)<10 or span<9*60)):
        return None,{**evidence,'reason':'PRIOR_STRUCTURE_UNAVAILABLE','known_bars':len(rows),'span_seconds':span}
    if any(type(x) not in (float,int) or not math.isfinite(x) or x<=0 for x in (entry,bid,ask)) or bid>ask:
        return None,{**evidence,'reason':'INVALID_EXECUTABLE_QUOTE'}
    breakout=setup['breakout']
    peaks=[]
    for i in range(1,len(rows)-1):
        a,b,c=rows[i-1:i+2]
        if b['time']-a['time']!=timedelta(minutes=1) or c['time']-b['time']!=timedelta(minutes=1):continue
        if b['high']>=max(a['high'],c['high']) and b['high']>min(a['high'],c['high']) and b['high']<=breakout:
            # A prior completed breakout retires that old range, rather than reusing its low.
            if not any(x['close']>b['high'] for x in rows[i+2:]):peaks.append((i,b))
    if peaks:anchor_i,anchor=max(peaks,key=lambda p:(p[1]['high'],p[1]['time']))
    else:
        # During an ongoing rising leg old ceilings may already have been cleared.
        # The last completed pullback-and-reclaim remains a price-defined origin.
        floors=[]
        for i,b in enumerate(rows[:-1]):
            if i and b['low']>rows[i-1]['low']:continue
            recovered=next((c for c in rows[i+1:] if c['low']>b['low'] and c['close']>b['high']),None)
            if recovered and not any(c['low']<b['low'] for c in rows[i+1:]):floors.append((i,b))
        anchor_i,anchor=floors[-1] if floors else (0,rows[0])
    base=rows[anchor_i:]
    support=min(b['low'] for b in base)
    touch=max(i for i,b in enumerate(base) if b['low']==support)
    confirm=next((b for b in base[touch+1:] if b['low']>support and b['close']>base[touch]['high']),None)
    spread=ask-bid
    buffer=max(support*cfg.structural_buffer,tick_size(support),spread)
    structural_stop=floor_tick(support-buffer)
    noise=max(statistics.median(b['high']-b['low'] for b in rows[-15:]),spread,tick_size(entry))
    hard_stop=floor_tick(structural_stop-noise)
    projection=breakout+2*(breakout-support)
    peak=confirmed_overhead(rows,asof,entry)
    ceiling=min(projection,peak['price']) if peak else projection
    # A locally confirmed new rise does not erase an earlier observed session
    # high merely because that high is outside the bounded reference history.
    known_high=setup.get('prior_session_high') if setup.get('entry_basis')=='LOCAL_CONFIRMED_CONTINUATION' else None
    known_high_valid=type(known_high) in (int,float) and math.isfinite(known_high) and known_high>entry
    capped_by_session_high=bool(known_high_valid and known_high<ceiling)
    if known_high_valid:ceiling=min(ceiling,known_high)
    digest=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in rows],separators=(',',':')).encode()).hexdigest()
    evidence.update(reference_sha256=digest,known_bars=len(rows),span_seconds=span,
        range_anchor_at=anchor['time'].isoformat(),range_anchor_high=anchor['high'],
        support=support,last_support_touch=base[touch]['time'].isoformat(),
        confirmation_bar=confirm['time'].isoformat() if confirm else None,
        structural_stop=structural_stop,emergency_stop=hard_stop,buffer=buffer,noise_band=noise,
        projection=projection,target_ceiling=ceiling,nearest_confirmed_peak=peak,
        known_prior_session_high=known_high if known_high_valid else None,
        session_high_ceiling_applied=capped_by_session_high,
        original_micro_support=setup['support'],spread=spread)
    rising=rows[-1]['close']>base[touch]['close'] and support<breakout<entry
    valid=bool(confirm and rising and 0<hard_stop<structural_stop<entry and ceiling>entry)
    return valid,{**evidence,'reason':'CONFIRMED_RANGE_BASE' if valid else 'UNCONFIRMED_OR_BROKEN_RANGE_BASE'}


def confirmed_continuation(raw,asof,minimum_gain):
    bars=complete_sequence(raw,asof,3,require_recent=True)
    evidence={'asof':asof.isoformat(),'basis':'THREE_COMPLETE_HIGHER_LOWS_AND_CLOSES',
              'volume_trend_required':False,'absolute_liquidity_owner':'UNCHANGED_ENTRY_LIQUIDITY_GATE'}
    if not bars:return None,{**evidence,'reason':'CONTINUATION_DATA_UNAVAILABLE'}
    volumes=[b.get('volume') for b in bars]
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in volumes):
        return None,{**evidence,'reason':'CONTINUATION_VOLUME_UNAVAILABLE'}
    first,middle,last=bars
    gain=last['close']/first['open']-1
    valid=(first['low']<middle['low']<last['low'] and first['close']<middle['close']<last['close']
           and last['close']>first['high'] and gain>=minimum_gain)
    return valid,{**evidence,'reason':'CONFIRMED_CONTINUATION' if valid else 'CONTINUATION_NOT_CONFIRMED',
        'times':[b['time'].isoformat() for b in bars],'lows':[b['low'] for b in bars],
        'closes':[b['close'] for b in bars],'volumes':volumes,'completed_gain':gain,'required_gain':minimum_gain}
