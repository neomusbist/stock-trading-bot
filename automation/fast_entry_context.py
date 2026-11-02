"""Causal FAST entry alternatives, observation only; never order permission.

The micro tape proves a short rebound, not a five-minute uptrend. Reuse the
existing bounded PriceContext for independent trend and overhead evidence.
Missing context is UNKNOWN. No entry/exit line or LIVE gate is changed here.
"""
from datetime import datetime
import math
from core.util import floor_tick, tick_size


def _stamp(value):
    result=value if isinstance(value,datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:raise ValueError('AWARE_TIME_REQUIRED')
    return result


def _positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def observe_fast_entry_context(signal,context,asof,minimum_rr):
    result={'SHADOW_ONLY':True,'diagnostic_only':True,'actual_order_permission':False,
        'profitability_proven':False,'live_policy_changed':False,
        'policy':'KNOWN_INTRADAY_CEILING_AND_COMPLETED_TREND',
        'status':'UNKNOWN','reason':'ENTRY_CONTEXT_INCOMPLETE',
        'original_target':signal.get('target'),'candidate_target':None,
        'candidate_net_rr':None,'actual_fill':None,'actual_net_pnl':None}
    try:
        asof=_stamp(asof);signaled=_stamp(signal['time']);observed=_stamp(context['asof'])
        if (not signaled.date()==observed.date()==asof.date() or signaled>asof or observed>asof):
            return {**result,'reason':'CONTEXT_NOT_AVAILABLE_AT_DECISION'}
        geometry=signal['fast_entry_evidence']['geometry'];costs=geometry['costs']
        struct_ev=geometry.get('structural_evidence') or {}
        entry=signal['limit'];stop=signal['stop'];emergency=signal['emergency'];original=signal['target']
        if not all(_positive(v) for v in (entry,stop,emergency,original,minimum_rr)) or not emergency<stop<entry<original:
            return {**result,'reason':'ENTRY_GEOMETRY_UNKNOWN'}
        if any(type(costs[k]) not in (int,float) or not math.isfinite(costs[k]) or not 0<=costs[k]<1
               for k in ('buy','sell','tax','entry_slippage','exit_slippage')) or costs['sell']+costs['tax']>=1:
            return {**result,'reason':'COSTS_UNKNOWN'}
        peak=context.get('nearest_confirmed_peak')
        trend=context.get('trend_5m',{})
        result.update(asof=asof.isoformat(),context_asof=observed.isoformat(),
            nearest_confirmed_peak=peak,trend_5m=trend,
            structural_stop=stop,emergency_stop=emergency,
            context_coverage=context.get('coverage','UNKNOWN'),
            missing_earlier_history=context.get('missing_earlier_history','UNKNOWN'))
        if peak is None:
            return {**result,'reason':'KNOWN_INTRADAY_CEILING_UNKNOWN',
                'trend_candidate':'REJECT' if trend.get('status')=='DOWN' else 'UNKNOWN'}
        confirmed=_stamp(peak['confirmed_at']);peak_at=_stamp(peak['time'])
        available=_stamp(peak.get('available_at',peak['confirmed_at']))
        cleared=_stamp(peak['cleared_at']) if peak.get('cleared_at') else None
        support=struct_ev.get('support');breakout=struct_ev.get('breakout')
        confirmed_breakout=(
            _positive(peak['price'])
            and (
                entry >= peak['price']
                or (
                    entry > peak['price'] + tick_size(peak['price'])
                    and _positive(breakout) and breakout > peak['price']
                )
            )
        )
        if (not _positive(peak['price']) or peak['price'] <= entry or confirmed_breakout or not peak_at<confirmed<=asof
                or not peak_at.date()==confirmed.date()==available.date()==asof.date()
                or available>asof or (cleared and cleared<=asof)):
            return {**result,'reason':'CONFIRMED_PEAK_UNAVAILABLE_OR_CLEARED'}
        target=floor_tick(min(original,peak['price']))
        buy=entry*(1+costs['entry_slippage'])*(1+costs['buy'])
        proceeds=(1-costs['exit_slippage'])*(1-costs['sell']-costs['tax'])
        reward=target*proceeds-buy;risk=buy-emergency*proceeds
        if risk<=0:return {**result,'reason':'RISK_UNKNOWN'}
        rr=reward/risk
        result.update(candidate_target=target,candidate_net_rr=rr,
            candidate_reward_after_cost=reward,candidate_risk_after_cost=risk,
            required_net_rr=minimum_rr,cost_basis='SAME_FAST_ENTRY_ASK_AND_EMERGENCY_COST_MODEL',
            target_candidate='REJECT' if rr<minimum_rr else 'OBSERVE',
            trend_candidate='OBSERVE' if trend.get('status')=='UP' else 'REJECT' if trend.get('status')=='DOWN' else 'UNKNOWN')
        if rr<minimum_rr:return {**result,'status':'REJECT_CANDIDATE','reason':'KNOWN_CEILING_NET_REWARD_RISK_LOW'}
        if trend.get('status')=='DOWN':return {**result,'status':'REJECT_CANDIDATE','reason':'COMPLETED_TREND_DOWN'}
        if trend.get('status')!='UP':return {**result,'reason':'COMPLETED_UPTREND_UNCONFIRMED'}
        return {**result,'status':'OBSERVE_CANDIDATE','reason':'CONTEXT_HYPOTHESIS_ONLY'}
    except (KeyError,TypeError,ValueError,OverflowError):return result


def attach_fast_entry_observation(signal,detector,tick,asof,minimum_rr):
    """Reuse existing references without filling signal bars or changing gates."""
    from dataclasses import replace
    from automation.fast_momentum import saved_evidence
    try:
        at=_stamp(asof)
        struct_ev=((signal.get('fast_entry_evidence') or {}).get('geometry') or {}).get('structural_evidence') or {}
        view=detector.price_context.snapshot(tick.symbol,replace(tick,time=at),signal['limit'],
            support=struct_ev.get('support'),breakout=struct_ev.get('breakout'))
        signal['fast_context_observation']=observe_fast_entry_context(signal,view,at,minimum_rr)
        signal['fast_context_snapshot']=saved_evidence(view)
        bars=detector.price_context.reference_bars(tick.symbol,at,signal.get('venue','KRX'))
        signal['fast_reference_bar_snapshot']=saved_evidence(bars[-30:])
        signal['fast_reference_received_at']=detector.price_context.rows.get(tick.symbol,{}).get('history_received_at')
    except (AttributeError,KeyError,TypeError,ValueError,OverflowError):
        signal['fast_context_observation']={'status':'UNKNOWN','reason':'REFERENCE_CONTEXT_UNAVAILABLE',
            'SHADOW_ONLY':True,'diagnostic_only':True,'actual_order_permission':False,
            'live_policy_changed':False,'profitability_proven':False}
    return signal
