"""Offline economic-horizon comparison. No runner import or order permission.

A daily resistance ceiling and a micro-reaction projection are hypotheses,
not probability estimates. Neither proves that the price can be reached before
the existing FAST exit. Risk, entry price and quantity remain unchanged.
"""
import math
from datetime import datetime
from core.util import floor_tick


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _stamp(value):
    try:
        value=datetime.fromisoformat(value) if isinstance(value,str) else value
        return value if isinstance(value,datetime) and value.tzinfo is not None else None
    except ValueError:return None


def compare_horizon(geometry,asof,minimum_rr=1.5):
    """Compare the SAME signal at known daily ceiling vs causal reaction range.

    Only the target assumption varies. A lack of history is UNKNOWN. No price
    forecast, later candle, invented fill or outcome-derived threshold is used.
    """
    base={'actual_order_permission':False,'orders_enabled':False,'research_only':True,
          'profitability_proven':False,'net_pnl':None,'MFE':None,'MAE':None,
          'fill_possible':None,'expectancy':None}
    try:
        at=_stamp(asof);known=_stamp(geometry.get('target_known_at'))
        structure=geometry['structural_evidence'];costs=geometry['costs']
        structure_at=_stamp(structure.get('asof'))
        entry=geometry['entry'];emergency=geometry['emergency'];ceiling=geometry['target']
        support=structure['support'];origin=structure['origin'];breakout=structure['breakout']
        values=(entry,emergency,ceiling,support,origin,breakout,minimum_rr)
        if not all(_number(v) and v>0 for v in values):raise ValueError
        if at is None or known is None or structure_at is None:raise ValueError
        if any(t.date()!=at.date() or t>at for t in (known,structure_at)):
            return {**base,'status':'UNKNOWN','reason':'EVIDENCE_NOT_KNOWN_AT_DECISION'}
        for key in ('origin_at','support_at','breakout_at','confirmation_at'):
            known_structure=_stamp(structure.get(key))
            if known_structure is None or known_structure.date()!=at.date() or known_structure>at:
                return {**base,'status':'UNKNOWN','reason':'STRUCTURE_TIME_UNPROVEN'}
        if not 0<emergency<entry<ceiling or not origin<support<breakout<entry:raise ValueError
        for key in ('buy','sell','tax','entry_slippage','exit_slippage'):
            if not _number(costs[key]) or not 0<=costs[key]<1:raise ValueError
        if costs['sell']+costs['tax']>=1:raise ValueError
        outlay=entry*(1+costs['entry_slippage'])*(1+costs['buy'])
        sell=(1-costs['exit_slippage'])*(1-costs['sell']-costs['tax'])
        risk=outlay-emergency*sell
        if risk<=0:raise ValueError
        local_target=floor_tick(min(ceiling,entry+(breakout-origin)))
        def policy(target):
            reward=target*sell-outlay
            return {'target':target,'net_reward_per_share':reward,'net_risk_per_share':risk,
                    'net_rr':reward/risk,'meets_existing_rr':reward/risk>=minimum_rr}
        return {**base,'status':'COMPARABLE_TARGET_HYPOTHESES_ONLY','asof':at.isoformat(),
                'entry':entry,'emergency':emergency,'required_net_rr':minimum_rr,
                'break_even_exit_bid':outlay/sell,
                'minimum_exit_bid_for_existing_rr':(outlay+minimum_rr*risk)/sell,
                'daily_ceiling':policy(ceiling),'reaction_projection':policy(local_target),
                'projection_basis':'ENTRY_PLUS_PREDECISION_BREAKOUT_MINUS_ORIGIN',
                'projection_is_resistance':False,'target_reachability':'UNKNOWN',
                'exit_policy_unchanged':True,'quantity_unchanged':True,
                'limitation':'Observed reaction size is not a validated holding-horizon target.'}
    except (KeyError,TypeError,ValueError,OverflowError,ZeroDivisionError):
        return {**base,'status':'UNKNOWN','reason':'INCOMPLETE_OR_INVALID_SIGNAL_EVIDENCE'}
