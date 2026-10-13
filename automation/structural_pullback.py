"""Completed pullback proof; prices are derived before RR and quantity sizing."""
import hashlib
import json
import math
from statistics import median
from types import SimpleNamespace
from core.util import floor_tick, tick_size
from automation.trade_quality import complete_sequence
from automation.price_context import PriceContext


def structural_pullback(raw, asof, setup, cfg, entry, bid, ask):
    evidence={'asof':asof.isoformat(),'entry':entry,'bid':bid,'ask':ask,
              'policy':'CONFIRMED_PULLBACK_FLOOR_AND_KNOWN_OVERHEAD'}
    size=min(len(raw or ()),30)
    bars=complete_sequence(raw,asof,size,require_recent=True) if size>=4 else []
    if not bars:return None,{**evidence,'reason':'PULLBACK_STRUCTURE_DATA_UNAVAILABLE'}
    if any(type(b.get('volume')) not in (int,float) or not math.isfinite(b['volume']) or b['volume']<0 for b in bars):
        return None,{**evidence,'reason':'PULLBACK_STRUCTURE_VOLUME_UNAVAILABLE'}
    if any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (entry,bid,ask)) or bid>ask:
        return None,{**evidence,'reason':'PULLBACK_QUOTE_UNAVAILABLE'}
    seq=bars[-12:];peak_i=max(range(1,len(seq)-1),key=lambda i:seq[i]['high'])
    impulse=seq[:peak_i+1];pull=seq[peak_i+1:]
    peak=seq[peak_i]['high'];base=min(b['low'] for b in impulse);support=min(b['low'] for b in pull)
    if setup.get('support')!=support or setup.get('peak')!=peak or setup.get('projection')!=peak+peak-base:
        return False,{**evidence,'reason':'PULLBACK_GEOMETRY_MISMATCH'}
    if support<=base or impulse[-1]['close']<=impulse[0].get('open',impulse[0]['close']):
        return False,{**evidence,'reason':'PULLBACK_UPWARD_STRUCTURE_INVALID'}
    digest=hashlib.sha256(json.dumps([[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in bars],separators=(',',':')).encode()).hexdigest()
    touches=[i for i,b in enumerate(pull) if b['low']==support];last_touch=touches[-1]
    # A live uptick cannot confirm its own still-forming support candle.
    # A completed recovery must retain the floor and close through its reaction high.
    recovered=next((b for b in pull[last_touch+1:] if b['low']>support and b['close']>pull[last_touch]['high']),None)
    context=PriceContext()
    for b in bars:context.record('proof',b,asof)
    reference=context.snapshot('proof',SimpleNamespace(time=asof),entry)['nearest_confirmed_peak']
    ceiling=min([p for p in (peak,reference['price'] if reference else None) if p is not None and p>entry],default=None)
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
    if entry-stop<=noise:return False,{**evidence,'reason':'PULLBACK_RISK_WITHIN_OBSERVED_NOISE'}
    return True,{**evidence,'reason':'CONFIRMED_PULLBACK_SUPPORT'}
