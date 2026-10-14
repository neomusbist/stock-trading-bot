"""C support proof and the already enabled close/emergency invalidation contract."""
import hashlib,json,math,statistics
from core.util import floor_tick,tick_size
from automation.trade_quality import complete_sequence,fresh_box_basis,box_direction
from automation.entry_context_audit import confirmed_overhead


def structural_box(raw,asof,setup,cfg,auto,entry,bid,ask):
    evidence={'asof':asof.isoformat(),'policy':'CONFIRMED_BOX_FLOOR_C',
              'scope':'PREDECISION_RECEIVED_COMPLETE_BARS'}
    size=min(len(raw or ()),30)
    bars=complete_sequence(raw,asof,size,require_recent=True) if size>=auto.box_min_bars else []
    if not bars:return None,{**evidence,'reason':'BOX_STRUCTURE_DATA_UNAVAILABLE'}
    if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (entry,bid,ask)) or bid>ask:
        return None,{**evidence,'reason':'BOX_QUOTE_UNAVAILABLE'}
    base=bars[-auto.box_min_bars:];top=max(b['high'] for b in base);support=min(b['low'] for b in base)
    if setup.get('breakout')!=top or setup.get('support')!=support or setup.get('projection')!=top+2*(top-support):
        return False,{**evidence,'reason':'BOX_GEOMETRY_MISMATCH'}
    direction,direction_proof=box_direction(bars,asof,auto.box_min_bars)
    origin,origin_proof=fresh_box_basis(bars,asof,auto.box_min_bars,auto.box_max_range)
    evidence.update(direction=direction_proof,origin=origin_proof)
    if direction is not True or origin is not True:return False,{**evidence,'reason':'BOX_ORIGIN_OR_DIRECTION_INVALID'}
    # A completed expanding impulse cannot supply the missing fifth base bar.
    # Use the existing impulse-rise threshold, not a new profit/stop percentage.
    prior,last=base[:-1],base[-1];prior_top=max(b['high'] for b in prior);prior_low=min(b['low'] for b in prior)
    gain=last['close']/last['open']-1
    expanding=(0<(prior_top-prior_low)/prior_low<=auto.box_max_range
               and last['close']>prior_top and gain>=auto.pullback_min_rise
               and last['high']-last['low']>max(b['high']-b['low'] for b in prior))
    if expanding:return False,{**evidence,'reason':'BOX_CONTAINS_EXPANDING_IMPULSE',
        'impulse_at':last['time'].isoformat(),'completed_gain':gain,
        'required_impulse_gain':auto.pullback_min_rise,'preceding_base_bars':len(prior)}
    touches=[i for i,b in enumerate(base) if b['low']==support];touch=touches[-1]
    confirm=next((b for b in base[touch+1:] if b['low']>support and b['close']>base[touch]['high']),None)
    spread=ask-bid;buffer=max(support*cfg.structural_buffer,tick_size(support),spread)
    soft=floor_tick(support-buffer)
    noise=max(statistics.median(b['high']-b['low'] for b in base),spread,tick_size(entry))
    hard=floor_tick(soft-noise)
    ceiling=setup['projection'];overhead=confirmed_overhead(bars,asof,entry)
    if overhead:ceiling=min(ceiling,overhead['price'])
    known=setup.get('prior_session_high')
    if type(known) in (int,float) and math.isfinite(known) and known>entry:ceiling=min(ceiling,known)
    digest=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in bars],separators=(',',':')).encode()).hexdigest()
    evidence.update(basis_sha256=digest,box_top=top,support=support,
        support_touch_times=[base[i]['time'].isoformat() for i in touches],
        confirmation_bar=confirm['time'].isoformat() if confirm else None,
        buffer=buffer,noise_band=noise,structural_stop=soft,emergency_stop=hard,
        projection=setup['projection'],target_ceiling=ceiling,nearest_confirmed_peak=overhead,
        known_prior_session_high=known)
    valid=bool(confirm and 0<hard<soft<support<top<entry and ceiling>entry)
    return valid,{**evidence,'reason':'CONFIRMED_BOX_SUPPORT' if valid else 'BOX_SUPPORT_NOT_CONFIRMED'}
