"""Opt-in price-structure experiments. No broker access or future-price inputs."""
import math
from datetime import datetime, timedelta
from core.util import floor_tick


def complete_sequence(raw, asof, count, not_before=None, require_recent=False):
    if (not isinstance(asof,datetime) or asof.tzinfo is None
            or type(count) is not int or count<1):return []
    result=[]
    for bar in raw or ():
        try:
            stamp=bar['time']
            if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
            if not isinstance(stamp,datetime) or stamp.tzinfo is None:continue
            # Minute bars cannot begin at an arbitrary second. A shifted timestamp
            # must not make a partial entry minute look like a completed minute.
            if stamp.second or stamp.microsecond:continue
            if stamp.date()!=asof.date() or stamp+timedelta(minutes=1)>asof:continue
            if not_before is not None and stamp<not_before:continue
            if bar.get('gap_reason') or bar.get('first_second',0)>5:continue
            values=[bar[k] for k in ('open','high','low','close')]
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in values):continue
            opening,high,low,close=values
            if not low<=min(opening,close)<=max(opening,close)<=high:continue
            result.append({**bar,'time':stamp})
        except (KeyError,TypeError,ValueError):continue
    result=sorted(result,key=lambda b:b['time'])[-count:]
    if len(result)!=count:return []
    if any(b['time']-a['time']!=timedelta(minutes=1) for a,b in zip(result,result[1:])):return []
    if require_recent and result[-1]['time']!=asof.replace(second=0,microsecond=0)-timedelta(minutes=1):return []
    return result


def box_direction(raw, asof, count):
    bars=complete_sequence(raw,asof,count,require_recent=True)
    evidence={'asof':asof.isoformat(),'bars_required':count}
    if not bars:return None,{**evidence,'reason':'BOX_DIRECTION_DATA_UNAVAILABLE'}
    first,last=bars[0],bars[-1]
    declining=(last['close']<first['open'] and last['high']<first['high'] and last['low']<first['low'])
    # Falling endpoints can also contain a completed reversal. Require a higher
    # low and a completed close reclaiming the intervening reaction high. A live
    # breakout tick alone cannot supply this confirmation.
    reversal=False
    if declining and len(bars)>=3:
        left,center,right=bars[-3:]
        reversal=(center['low']>left['low'] and right['low']>=center['low']
            and center['close']>left['close'] and right['close']>center['high'])
    reason='CONFIRMED_REVERSAL_BASE' if reversal else 'DECLINING_RANGE' if declining else 'BASE_OR_RISING_RANGE'
    return not declining or reversal,{**evidence,'reason':reason,'endpoint_declining':declining,
        'completed_reversal':reversal,
        'start':first['time'].isoformat(),'end':last['time'].isoformat(),
        'first_open':first['open'],'first_high':first['high'],'first_low':first['low'],
        'last_close':last['close'],'last_high':last['high'],'last_low':last['low']}


def confirmed_protection(cfg, auto, position, price, asof):
    high=max(position['high'],price);prior=position['stop']
    evidence={'policy':'CONFIRMED_POST_ENTRY_HIGHER_LOW','checked_at':asof.isoformat(),
        'initial_invalidation':position['initial_stop'],'prior_stop':prior,'high':high,'price':price}
    if high-position['avg']<cfg.protect_at_r*position['initial_r']:
        return high,prior,{**evidence,'reason':'PROTECTION_NOT_ARMED'}
    try:opened=datetime.fromisoformat(position['opened'])
    except (KeyError,TypeError,ValueError):return high,prior,{**evidence,'reason':'ENTRY_TIME_UNAVAILABLE'}
    # Exclude the partially observed entry minute, even when the detector has earlier ticks.
    first_full=opened.replace(second=0,microsecond=0)
    if opened>first_full:first_full+=timedelta(minutes=1)
    bars=complete_sequence(position.get('protection_bars'),asof,3,first_full,require_recent=True)
    if not bars:return high,prior,{**evidence,'reason':'CONFIRMED_PULLBACK_NOT_READY'}
    left,center,right=bars
    previous_low=position.get('confirmed_protection_low',position['initial_stop'])
    pivot=(center['low']<=left['low'] and center['low']<right['low'] and center['low']>previous_low)
    if not pivot:return high,prior,{**evidence,'reason':'NO_CONFIRMED_HIGHER_LOW'}
    candidate=floor_tick(center['low']*(1-cfg.structural_buffer))
    cost_floor=position['avg']*(1+auto.commission_buy)/((1-auto.commission_sell-auto.sell_tax)*(1-cfg.exit_slippage_fraction))
    evidence.update(pivot_low=center['low'],pivot_time=center['time'].isoformat(),
        confirmation_time=(right['time']+timedelta(minutes=1)).isoformat(),candidate_stop=candidate,cost_floor=cost_floor)
    if not prior<candidate<price or candidate<=cost_floor:
        return high,prior,{**evidence,'reason':'PIVOT_STOP_NOT_PROFITABLE_OR_NOT_HIGHER'}
    return high,candidate,{**evidence,'reason':'CONFIRMED_HIGHER_LOW_RAISED_STOP'}
