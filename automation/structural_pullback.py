"""Completed pullback proof; prices are derived before RR and quantity sizing."""
import hashlib
import json
import math
from datetime import timedelta
from statistics import median
from types import SimpleNamespace
from core.util import floor_tick, tick_size
from automation.trade_quality import complete_sequence
from automation.price_context import PriceContext


def pullback_peak_index(seq):
    """One peak definition for B detection and its independent floor proof."""
    first=0 if seq[0].get('open',0)>0 else 1
    return max(range(first,len(seq)-1),key=lambda i:seq[i]['high'])


def support_reclaim_setup(raw,asof,setup):
    """Earlier B trigger, same independently confirmed floor/peak/target.

    The completed recovery CLOSE already reclaimed the floor candle's high.
    Requiring its potentially much higher upper wick again duplicates that
    confirmation. A fresh executable print must still hold above the original
    reaction high; it does not confirm a still-forming support candle.
    """
    size=min(len(raw or ()),30)
    bars=complete_sequence(raw,asof,size,require_recent=True) if size>=4 else []
    if not bars or not setup:return None
    seq=bars[-12:];peak_i=pullback_peak_index(seq);pull=seq[peak_i+1:]
    if not pull:return None
    support=min(b['low'] for b in pull);touch=max(i for i,b in enumerate(pull) if b['low']==support)
    recovered=next((b for b in pull[touch+1:] if b['low']>support and b['close']>pull[touch]['high']),None)
    if recovered is None or pull[touch]['high']>=setup['peak']:return None
    return {**setup,'breakout':pull[touch]['high'],'entry_basis':'CONFIRMED_SUPPORT_RECLAIM',
        'support_touch_at':pull[touch]['time'].isoformat(),'recovery_bar_at':recovered['time'].isoformat(),
        'recovery_close':recovered['close'],'recovery_wick_high':recovered['high'],
        'confirmation_closed_at':(recovered['time']+timedelta(minutes=1)).isoformat()}


def structural_pullback(raw, asof, setup, cfg, entry, bid, ask,close_policy=False,auto=None):
    evidence={'asof':asof.isoformat(),'entry':entry,'bid':bid,'ask':ask,
              'policy':'CONFIRMED_PULLBACK_FLOOR_AND_KNOWN_OVERHEAD'}
    size=min(len(raw or ()),30)
    bars=complete_sequence(raw,asof,size,require_recent=True) if size>=4 else []
    if not bars:return None,{**evidence,'reason':'PULLBACK_STRUCTURE_DATA_UNAVAILABLE'}
    if any(type(b.get('volume')) not in (int,float) or not math.isfinite(b['volume']) or b['volume']<0 for b in bars):
        return None,{**evidence,'reason':'PULLBACK_STRUCTURE_VOLUME_UNAVAILABLE'}
    if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (entry,bid,ask)) or bid>ask:
        return None,{**evidence,'reason':'PULLBACK_QUOTE_UNAVAILABLE'}
    seq=bars[-min(len(bars),24):];peak_i=pullback_peak_index(seq)
    impulse=seq[:peak_i+1];pull=seq[peak_i+1:]
    peak=seq[peak_i]['high'];base=min(b['low'] for b in impulse);support=min(b['low'] for b in pull)
    if setup.get('support')!=support or setup.get('peak')!=peak:
        return False,{**evidence,'reason':'PULLBACK_GEOMETRY_MISMATCH'}
    if support<=base or impulse[-1]['close']<=impulse[0].get('open',impulse[0]['close']):
        return False,{**evidence,'reason':'PULLBACK_UPWARD_STRUCTURE_INVALID'}
    digest=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in bars],separators=(',',':')).encode()).hexdigest()
    touches=[i for i,b in enumerate(pull) if b['low']==support];last_touch=touches[-1]
    # A live uptick cannot confirm its own still-forming support candle.
    # A completed recovery must retain the floor and close through its reaction high.
    recovered=next((b for b in pull[last_touch+1:] if b['low']>support and b['close']>pull[last_touch]['high']),None)
    if setup.get('entry_basis')=='CONFIRMED_SUPPORT_RECLAIM':
        revised=support_reclaim_setup(bars,asof,setup)
        keys=('breakout','support_touch_at','recovery_bar_at','recovery_close','recovery_wick_high','confirmation_closed_at')
        if (not getattr(auto,'b_support_reclaim_entry_enabled',False) or revised is None
                or any(setup.get(k)!=revised.get(k) for k in keys)):
            return False,{**evidence,'reason':'PULLBACK_RECLAIM_TRIGGER_PROOF_INVALID'}
    context=PriceContext()
    for b in bars:context.record('proof',b,asof)
    reference=context.snapshot('proof',SimpleNamespace(time=asof),setup['breakout'])['nearest_confirmed_peak']
    peak_time = seq[peak_i]['time']
    peak_iso = peak_time.isoformat() if hasattr(peak_time, 'isoformat') else str(peak_time)
    overhead_peaks = [p for p in context.rows['proof']['peaks'] if p['price'] > entry and (p['price'] > peak or str(p['time']) <= peak_iso)]
    nearest_overhead = min(overhead_peaks, key=lambda p: p['price']) if overhead_peaks else None
    overhead_ceiling = nearest_overhead['price'] if nearest_overhead else None
    if auto and getattr(auto, 'pullback_expansion_target_enabled', False):
        impulse_rise = peak - base
        expansion_target = floor_tick(peak + 0.5 * impulse_rise)
        ceiling = min([p for p in (expansion_target, overhead_ceiling) if p is not None and p > entry], default=expansion_target)
    else:
        ceiling = min([p for p in (peak, overhead_ceiling) if p is not None and p > entry], default=peak)
    spread=ask-bid;buffer=max(support*cfg.structural_buffer,tick_size(support),spread)
    stop=floor_tick(support-buffer)
    ranges=[b['high']-b['low'] for b in pull]
    noise=max(median(ranges),spread+stop*cfg.exit_slippage_fraction)
    evidence.update(basis_sha256=digest,support=support,impulse_base=base,impulse_peak=peak,
        impulse_bars=[b['time'].isoformat() for b in impulse],pullback_bars=[b['time'].isoformat() for b in pull],
        support_touch_times=[pull[i]['time'].isoformat() for i in touches],
        confirmation_bar=recovered['time'].isoformat() if recovered else None,
        buffer_amount=buffer,buffered_stop=stop,spread=spread,median_pullback_range=median(ranges),
        noise_risk_floor=noise,stop_distance=entry-stop,nearest_confirmed_peak=reference,target_ceiling=ceiling,
        coverage='PREDECISION_COMPLETED_BARS_ONLY',missing_earlier_history='UNKNOWN')
    if setup.get('entry_basis')=='CONFIRMED_SUPPORT_RECLAIM':
        evidence.update(entry_basis=setup['entry_basis'],reaction_trigger=pull[last_touch]['high'],
            recovery_close=recovered['close'],recovery_wick_high=recovered['high'],
            confirmation_closed_at=setup['confirmation_closed_at'],target_expansion_used=False)
    if close_policy:
        band=max(median(ranges),spread,tick_size(entry))
        evidence.update(structural_stop=stop,noise_band=band,emergency_stop=floor_tick(stop-band))
    is_support_recovery = (setup.get('entry_basis') == 'SUPPORT_RECOVERY')
    if is_support_recovery:
        if last_touch >= len(pull) - 1 or any(b['low'] < support for b in pull[last_touch+1:]):
            return False, {**evidence, 'reason': 'PULLBACK_SUPPORT_NOT_CONFIRMED'}
        if entry < support:
            return False, {**evidence, 'reason': 'PULLBACK_SUPPORT_BREACHED'}
        if entry < setup.get('recovery_trigger', support):
            return False, {**evidence, 'reason': 'PULLBACK_RECOVERY_TRIGGER_NOT_REACHED'}
    else:
        if recovered is None:return False,{**evidence,'reason':'PULLBACK_SUPPORT_NOT_CONFIRMED'}
        # A failed earlier floor and descending pivots require a completed reclaim.
        lows=[]
        for left,center,right in zip(bars,bars[1:],bars[2:]):
            if center['low']<left['low'] and center['low']<right['low']:lows.append(center['low'])
        peaks=list(context.rows['proof']['peaks'])
        declining=(len(lows)>=2 and lows[-1]<lows[-2] and len(peaks)>=2 and peaks[-1]['price']<peaks[-2]['price'])
        evidence.update(declining_swing_structure=declining,
                        trend_reclaim_reference=peaks[-1]['price'] if declining else None)
        if declining and recovered['close']<=peaks[-1]['price']:
            return False,{**evidence,'reason':'PULLBACK_LOWER_SWINGS_NOT_RECLAIMED'}
    reclaim_close_policy=close_policy and (setup.get('entry_basis') in {'CONFIRMED_SUPPORT_RECLAIM','SUPPORT_RECOVERY'} or getattr(auto,'confirmed_invalidation_enabled',False))
    risk_stop=evidence['emergency_stop'] if reclaim_close_policy else stop
    evidence.update(noise_guard_basis='CONFIRMED_CLOSE_WITH_SEPARATE_EMERGENCY' if reclaim_close_policy else 'STRUCTURAL_PRICE_DISTANCE',
                    noise_guard_risk_distance=entry-risk_stop)
    if entry-risk_stop<=noise:return False,{**evidence,'reason':'PULLBACK_RISK_WITHIN_OBSERVED_NOISE'}
    if auto and getattr(auto,'b_chase_filter_enabled',False) and peak>support and entry>support+0.55*(peak-support):
        return False,{**evidence,'reason':'ENTRY_CHASING_LATE','entry':entry,'support':support,'peak':peak}
    if auto and getattr(auto,'b_min_target_room_enabled',False):
        if ceiling<=entry or (ceiling-entry)/entry<0.015:
            return False,{**evidence,'reason':'TARGET_STRUCTURALLY_NARROW','entry':entry,'target_ceiling':ceiling}
    return True,{**evidence,'reason':'CONFIRMED_SUPPORT_RECOVERY' if is_support_recovery else 'CONFIRMED_PULLBACK_SUPPORT'}
