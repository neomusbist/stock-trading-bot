"""Explicit A range-base hypothesis; no fixed-percent stop widening."""
import hashlib,json,math,statistics
from datetime import datetime,timedelta
from core.util import floor_tick,tick_size
from automation.trade_quality import complete_sequence
from automation.entry_context_audit import confirmed_overhead


def breakout_retest_setup(raw,asof,cfg):
    """A completed breakout -> held retest -> completed reaction reclaim.

    Geometry and the original range projection are fixed BEFORE economics.
    Receiver-built signal bars own this proof; REST references cannot create
    a retest, observation warmup or a new trigger.
    """
    size=min(len(raw or ()),12)
    bars=complete_sequence(raw,asof,size,require_recent=True) if size>=6 else []
    if not bars or any(b.get('observation_source') in {'REST','REST_REFERENCE','REFERENCE'} for b in bars):return None
    if any(type(b.get('volume')) not in (int,float) or not math.isfinite(b['volume']) or b['volume']<=0 for b in bars):return None
    choices=[]
    for i in range(1,len(bars)-4):
        left,peak,right=bars[i-1:i+2];level=peak['high']
        if not (level>=max(left['high'],right['high']) and level>min(left['high'],right['high'])):continue
        # The three bars defining this confirmed peak are a fixed original
        # range. A bounded history rolling forward must not silently change
        # its support/projection while retaining the same old anchor time.
        base=bars[i-1:i+2];support=min(b['low'] for b in base)
        if support>=level:continue
        crossed=next((j for j in range(i+2,len(bars)-2) if bars[j]['close']>level),None)
        if crossed is None:continue
        impulse=bars[crossed];previous=bars[max(0,crossed-3):crossed]
        if impulse['close']/impulse['open']-1<cfg.min_price_gain or impulse['volume']<sum(b['volume'] for b in previous)/len(previous):continue
        for j in range(crossed+1,len(bars)-1):
            retest=bars[j]
            # A completed loss of the old ceiling retires this role-reversal
            # claim. Do not recast that failure as a higher support later.
            if any(b['low']<level or b['close']<level for b in bars[crossed+1:]):continue
            recovered=next((b for b in bars[j+1:] if b['low']>retest['low'] and b['close']>retest['high']),None)
            if recovered is None or retest['low']>=retest['high'] or retest['high']>=impulse['high']:continue
            if any(b['low']<retest['low'] for b in bars[j+1:]):continue
            choices.append(dict(pattern='A',breakout=retest['high'],support=retest['low'],
                projection=level+2*(level-support),entry_basis='CONFIRMED_BREAKOUT_RETEST',
                old_ceiling=level,original_range_support=support,range_anchor_at=peak['time'].isoformat(),
                breakout_bar_at=impulse['time'].isoformat(),breakout_close=impulse['close'],
                breakout_volume=impulse['volume'],base_volume_mean=sum(b['volume'] for b in previous)/len(previous),
                retest_bar_at=retest['time'].isoformat(),recovery_bar_at=recovered['time'].isoformat(),
                confirmation_closed_at=(recovered['time']+timedelta(minutes=1)).isoformat(),
                projection_confirmed_bar_closed_at=(impulse['time']+timedelta(minutes=1)).isoformat(),
                decision_basis_sha256=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close','volume')] for b in bars],separators=(',',':')).encode()).hexdigest()))
    return max(choices,key=lambda x:(x['retest_bar_at'],x['range_anchor_at'])) if choices else None


def _retest_proof(raw,decision_bars,asof,setup,cfg,entry,bid,ask,auto):
    evidence={'asof':asof.isoformat(),'policy':'CONFIRMED_BREAKOUT_RETEST_A',
        'scope':'RECEIVED_COMPLETE_RETEST_WITH_KNOWN_OVERHEAD','earlier_history':'UNKNOWN'}
    derived=breakout_retest_setup(decision_bars,asof,cfg)
    if not getattr(auto,'a_breakout_retest_entry_enabled',False) or derived is None:
        return False,{**evidence,'reason':'BREAKOUT_RETEST_PROOF_UNAVAILABLE'}
    if any(setup.get(k)!=v for k,v in derived.items()):return False,{**evidence,'reason':'BREAKOUT_RETEST_GEOMETRY_MISMATCH'}
    if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (entry,bid,ask)) or bid>ask:
        return None,{**evidence,'reason':'INVALID_EXECUTABLE_QUOTE'}
    known={}
    for row in list(raw or ())+list(decision_bars or ()):
        valid=complete_sequence([row],asof,1)
        if valid:known[valid[0]['time']]=valid[0]
    rows=sorted(known.values(),key=lambda b:b['time'])[-120:]
    support=derived['support'];spread=ask-bid;buffer=max(support*cfg.structural_buffer,tick_size(support),spread)
    structural=floor_tick(support-buffer)
    # Retain the same reference-noise estimator; no RR-driven tightening.
    noise=max(statistics.median(b['high']-b['low'] for b in rows[-15:]),spread,tick_size(entry))
    emergency=floor_tick(structural-noise)
    peak=confirmed_overhead(rows,asof,derived['breakout']);ceiling=min(derived['projection'],peak['price']) if peak else derived['projection']
    prior=setup.get('prior_session_high');prior_valid=type(prior) in (int,float) and math.isfinite(prior) and prior>derived['breakout']
    if prior_valid:ceiling=min(ceiling,prior)
    digest=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in rows],separators=(',',':')).encode()).hexdigest()
    evidence.update(reference_sha256=digest,known_bars=len(rows),support=support,structural_stop=structural,
        emergency_stop=emergency,noise_band=noise,buffer=buffer,spread=spread,projection=derived['projection'],
        target_ceiling=ceiling,nearest_confirmed_peak=peak,known_prior_session_high=prior if prior_valid else None,
        original_micro_support=support,confirmation_bar=derived['recovery_bar_at'],entry_basis=derived['entry_basis'],
        role_reversal={k:derived[k] for k in ('old_ceiling','original_range_support','range_anchor_at','breakout_bar_at','retest_bar_at','recovery_bar_at','projection_confirmed_bar_closed_at','confirmation_closed_at','decision_basis_sha256')})
    passed=bool(0<emergency<structural<entry and entry>derived['breakout'] and ceiling>entry)
    return passed,{**evidence,'reason':'CONFIRMED_BREAKOUT_RETEST' if passed else 'RETEST_ENTRY_OR_KNOWN_CEILING_INVALID'}


def structural_breakout(raw,asof,setup,cfg,entry,bid,ask,auto=None,decision_bars=None):
    if setup.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST':
        return _retest_proof(raw,decision_bars,asof,setup,cfg,entry,bid,ask,auto)
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
    required=4 if getattr(auto,'a_short_reference_enabled',False) else 10
    if not recent or (not opening and (len(rows)<required or span<(required-1)*60)):
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
    peak=confirmed_overhead(rows,asof,breakout)
    ceiling=min(projection,peak['price']) if peak else projection
    # A locally confirmed new rise does not erase an earlier observed session
    # high merely because that high is outside the bounded reference history.
    known_high=setup.get('prior_session_high') if setup.get('entry_basis')=='LOCAL_CONFIRMED_CONTINUATION' else None
    known_high_valid=type(known_high) in (int,float) and math.isfinite(known_high) and known_high>breakout
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
