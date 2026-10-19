"""Offline local higher-low reclaim hypothesis. Never an order policy.

Four genuinely completed bars prove a rise, controlled pullback and reclaim.
The target is a previously known ceiling, not a multiple solved from a stop.
"""
import math
import statistics
from datetime import datetime

from automation.decisions import gate
from automation.trade_quality import complete_sequence
from core.util import floor_tick,tick_size


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


def known(stamp,asof):
    try:
        stamp=datetime.fromisoformat(stamp) if isinstance(stamp,str) else stamp
        return isinstance(stamp,datetime) and stamp.tzinfo is not None and stamp.date()==asof.date() and stamp<=asof
    except (TypeError,ValueError):return False


def reclaim_trial(t,bars,watch,cfg,auto,costs,asof,base_gates,*,quote_reason,context=None):
    """A price/flow research candidate, with account/fill eligibility UNKNOWN."""
    context=context or {}
    gates={key:base_gates.get(key,gate(None,reason='MISSING_OBSERVATION'))
           for key in ('warmup','spread','structure')}
    turnover=base_gates.get('liquidity',{}).get('values',{}).get('window_turnover')
    gates['liquidity']=gate(turnover>=cfg.min_bc_window_turnover if number(turnover) else None,
                           window_turnover=turnover,min_turnover=cfg.min_bc_window_turnover)
    status=watch.get('market_status',{})
    checked=status.get('checked_at')
    checked_stamp=datetime.fromisoformat(checked) if isinstance(checked,str) and known(checked,asof) else checked
    current_status=(status.get('pass') is True and known(checked,asof)
                    and (asof-checked_stamp).total_seconds()<=auto.scan_seconds*3)
    gates['market_status']=gate(current_status,status=status.get('pass'),checked_at=checked)
    gates['quote']=gate(quote_reason is None,reason=quote_reason)
    seq=complete_sequence(bars,asof,4,require_recent=True)
    formed=False;geometry=None;reasons=[];proof={'basis':'FOUR_OBSERVED_COMPLETE_BARS','asof':asof.isoformat()}
    if not seq:reasons.append('RECLAIM_COMPLETE_BAR_EVIDENCE_UNKNOWN')
    else:
        origin,impulse,pullback,reclaim=seq
        volumes=[b.get('volume') for b in seq]
        volume_known=all(number(v) and v>0 for v in volumes)
        rise=impulse['close']/origin['open']-1 if origin['open']>0 else None
        formed=bool(volume_known and rise is not None and rise>=auto.pullback_min_rise
            and impulse['high']>origin['high'] and impulse['close']>origin['close']
            and origin['low']<pullback['low']<impulse['close']
            and pullback['high']<=impulse['high'] and pullback['close']<impulse['close']
            and pullback['volume']<=impulse['volume']
            and reclaim['low']>=pullback['low'] and reclaim['close']>pullback['high']
            and reclaim['close']>reclaim['open'] and reclaim['volume']>=pullback['volume'])
        proof.update(times=[b['time'].isoformat() for b in seq],rise=rise,
            pullback_low=pullback['low'],reclaim_close=reclaim['close'],formed=formed)
        if not formed:reasons.append('RECLAIM_RISE_PULLBACK_RECOVERY_NOT_CONFIRMED')
        else:
            breakout=max(impulse['high'],reclaim['high']);entry=t.ask;support=pullback['low']
            gates['breakout']=gate(t.price>breakout and entry>breakout,price=t.price,entry=entry,breakout=breakout)
            gates['chase']=gate(entry/breakout-1<=cfg.max_chase,chase=entry/breakout-1,max_chase=cfg.max_chase)
            if not all(number(v) and v>0 for v in (entry,t.bid,t.ask)) or t.bid>t.ask:
                reasons.append('RECLAIM_EXECUTABLE_PRICE_UNKNOWN')
            else:
                spread=t.ask-t.bid
                buffer=max(tick_size(support),support*cfg.structural_buffer,spread)
                stop=floor_tick(support-buffer)
                noise=max(statistics.median(b['high']-b['low'] for b in seq),spread,tick_size(entry))
                emergency=floor_tick(stop-noise)
                ceilings=[]
                resistance=watch.get('resistance')
                resistance_at=watch.get('resistance_asof') or watch.get('observed_at')
                if number(resistance) and resistance>entry and known(resistance_at,asof):
                    ceilings.append((resistance,'KNOWN_DAILY_RESISTANCE',resistance_at))
                peak=context.get('nearest_confirmed_peak')
                if isinstance(peak,dict) and number(peak.get('price')) and known(peak.get('confirmed_at'),asof):
                    cleared=peak.get('cleared_at')
                    if not known(cleared,asof):
                        # A known unretired ceiling at/below the ask is not
                        # removed just because a current print crossed it.
                        ceilings.append((peak['price'],'KNOWN_INTRADAY_CEILING',peak['confirmed_at']))
                target,basis,known_at=min(ceilings,key=lambda x:x[0]) if ceilings else (None,'UNKNOWN',None)
                safe=0<emergency<stop<entry
                gates['invalidation']=gate(safe,entry=entry,stop=stop,emergency=emergency,support=support,buffer=buffer,noise=noise)
                if target is not None:target=floor_tick(target)
                buy=entry*(1+costs.entry_slippage)*(1+costs.buy)
                sell=(1-costs.exit_slippage)*(1-costs.sell-costs.tax)
                risk=buy-emergency*sell if safe else None
                reward=target*sell-buy if target is not None else None
                rr=reward/risk if reward is not None and risk and risk>0 else None
                gates['net_reward_risk']=gate(rr>=cfg.min_reward_risk if rr is not None else None,
                    target=target,net_rr=rr,required=cfg.min_reward_risk,reward_net_per_share=reward,risk_net_per_share=risk)
                geometry=dict(pattern='RECLAIM_RESEARCH',symbol=t.symbol,time=t.time.isoformat(),
                    signal_detected_at=asof.isoformat(),limit=entry,stop=stop,emergency=emergency,
                    breakout=breakout,support=support,target=target,target_basis=basis,target_known_at=known_at,
                    net_reward_risk=rr,reward_net_per_share=reward,risk_net_per_share=risk,
                    structural_evidence=proof,planned_qty=None,
                    invalidation_policy={'policy_name':'OBSERVED_LOCAL_RECLAIM_RESEARCH','enabled':True,
                        'structural_stop':stop,'emergency_stop':emergency,'noise_band':noise},
                    sizing='UNKNOWN_NEEDS_POINT_IN_TIME_ACCOUNT_AND_FULL_DEPTH',
                    earlier_intraday_history='UNKNOWN_OUTSIDE_OBSERVATION')
    reasons.extend('RECLAIM_'+key.upper()+'_FAIL' for key,value in gates.items() if value['result']!='PASS')
    ready=bool(formed and geometry and not reasons)
    trial={'setup':'RECLAIM_RESEARCH','candidate_generated':formed,'entry_ready':ready,
           'failed_reasons':reasons,'primary_reason_code':reasons[0] if reasons else 'RECLAIM_PRICE_EVIDENCE_PASS',
           'gates':gates,'setup_geometry':geometry,'structural_evidence':proof,'acceleration_required':False}
    signal=None
    if ready:
        signal={**geometry,'evaluation_gates':gates,'experimental_policy':'OBSERVED_LOCAL_RECLAIM_RESEARCH',
            'SHADOW_ONLY':True,'diagnostic_only':True,'actual_order_permission':False,'orders_enabled':False,
            'profitability_proven':False,'actual_fill':None,'actual_net_pnl':None}
    return signal,trial
