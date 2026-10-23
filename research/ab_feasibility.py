"""Offline A/B evidence classification, using the production cash-flow model.

No detector, rank, target, stop, order authority or LIVE configuration is changed.
Classes describe a recorded structure, never all earlier market opportunities.
"""
from dataclasses import replace
from datetime import datetime
import math

from automation.capital import Costs,entry_economics,viable_entry_zone

IMPOSSIBLE='1_STRUCTURALLY_INCOMPATIBLE'
LATE='2_OBSERVED_VIABLE_PRICE_BEFORE_CONFIRMATION'
MISSING='3_EVIDENCE_INSUFFICIENT'
ORDERABLE='4_ACTUAL_RECEIVED_ORDERABLE'

def first_record(row):
    """Adapt the existing frozen first-structure audit, without filling gaps."""
    return dict(source_id=row['source_id'],symbol=row['symbol'],setup=row['setup'],at=row['asof'],
        entry=row.get('quote',{}).get('ask'),trigger=row.get('normalized_price_zone',{}).get('trigger_price'),
        target=row.get('normalized_target'),structural_stop=row.get('structural_invalidation'),
        emergency_stop=row.get('emergency_stop'),proof_status=row.get('proof_status'),
        earlier_received_path='UNKNOWN',concurrent_failures=row.get('concurrent_non_RR_failures',[]))

def assess(record,costs,minimum_rr=1.5,max_chase=.008):
    """Numeric sensitivity alone can NEVER promote a record to orderable."""
    r=record
    e=entry_economics(r.get('entry'),r.get('structural_stop'),r.get('emergency_stop'),r.get('target'),costs,minimum_rr)
    zone=viable_entry_zone(e,r.get('trigger'),max_chase)
    confirmed=r.get('proof_status')=='PASS' or r.get('received_role_geometry_confirmed') is True
    numeric=(e['status']=='KNOWN' and zone.get('minimum_trigger_entry') is not None)
    blocked=numeric and (r['target']<=r['trigger'] or e['max_entry_price']<zone['minimum_trigger_entry'])
    reason='NO_COMPLETE_RECEIVED_PROOF'
    category=MISSING
    # A real earlier eligible quote in the cost-only zone can establish a
    # confirmation delay. A hypothetical maximum entry is insufficient.
    p=r.get('prior_viable_observation') or {}
    try:
        prior_at=datetime.fromisoformat(p['at']);current_at=datetime.fromisoformat(r['at'])
        pe=entry_economics(p.get('ask'),r['structural_stop'],r['emergency_stop'],r['target'],costs,minimum_rr)
        prior_known=(p.get('actual_received') is True and p.get('qualification_known') is True
            and p.get('quote_fresh') is True and p.get('structure_key')==r.get('structure_key')
            and r.get('structure_key') is not None and p.get('structure_confirmed') is True
            and pe['status']=='KNOWN' and pe['rr_pass'] and prior_at.tzinfo is not None
            and current_at.tzinfo is not None and prior_at<current_at and p.get('trigger_pending') is True)
    except (KeyError,TypeError,ValueError):prior_known=False
    if confirmed and prior_known and e['status']=='KNOWN' and not e['rr_pass']:
        category=LATE;reason='ACTUAL_EARLIER_ELIGIBLE_COST_ZONE_OBSERVED'
    elif confirmed and blocked:
        category=IMPOSSIBLE;reason='TARGET_AT_OR_BELOW_TRIGGER' if r['target']<=r['trigger'] else 'COST_MAX_BELOW_FIRST_EXECUTABLE_TRIGGER'
    elif confirmed and numeric and zone.get('current_order_price_in_zone') is True:
        x=r.get('execution_evidence') or {}
        if (all(x.get(k) is True for k in ('actual_received','qualification_known','quote_fresh','depth_confirmed','engine_gates_pass'))
                and type(x.get('quantity')) is int and x['quantity']>0):
            category=ORDERABLE;reason='ALL_RECORDED_RECEIPT_AND_ORDER_GATES_CONFIRMED'
        else:reason='PRICE_FEASIBLE_EXECUTION_EVIDENCE_MISSING'
    elif not confirmed:reason='INDEPENDENT_STRUCTURE_NOT_CONFIRMED'
    return {'category':category,'reason':reason,'economics':e,'zone':zone,
        'numeric_obstruction_even_if_proof_missing':bool(blocked),'structure_confirmed':confirmed,
        'earlier_opportunity_status':'OBSERVED' if prior_known else 'UNKNOWN',
        'actual_order_permission':False}

def cost_scenarios(base):
    """Uncalibrated bounds, not recommended transaction costs or LIVE settings."""
    return {'LIVE_CONFIG':base,
        'HALF_SLIPPAGE_UNCALIBRATED':replace(base,entry_slippage=base.entry_slippage/2,exit_slippage=base.exit_slippage/2),
        'FEE_TAX_ONLY_LOWER_BOUND':replace(base,entry_slippage=0.,exit_slippage=0.),
        'HIGHER_SLIPPAGE_STRESS':replace(base,entry_slippage=base.entry_slippage*1.5,exit_slippage=base.exit_slippage*1.5),
        'ZERO_COST_MATHEMATICAL_BOUND':Costs(0.,0.,0.,0.,0.)}

def distribution(values):
    values=sorted(x for x in values if type(x) in (int,float) and math.isfinite(x))
    def q(f):
        i=(len(values)-1)*f;lo=int(i);hi=min(lo+1,len(values)-1)
        return values[lo]+(values[hi]-values[lo])*(i-lo)
    return {'n':len(values),'min':values[0] if values else None,'p25':q(.25) if values else None,
        'median':q(.5) if values else None,'p75':q(.75) if values else None,
        'p90':q(.9) if values else None,'max':values[-1] if values else None}

def pretrigger_headroom(quote,reference,asof,maximum_quote_age_seconds=5.):
    """Known resistance room only. Unformed structure never creates a stop/RR."""
    result={'status':'UNKNOWN','reason':'PRETRIGGER_REFERENCE_AVAILABILITY_UNPROVEN',
        'structural_stop':None,'target':None,'maximum_entry':None,'actual_order_permission':False}
    try:
        at=datetime.fromisoformat(asof);known=datetime.fromisoformat(reference['available_at'])
        observed=datetime.fromisoformat(quote['received_at']);confirmed=datetime.fromisoformat(reference['confirmed_at'])
        ask=quote['ask'];ceiling=reference['price']
        if (not all(x.tzinfo for x in (at,known,observed,confirmed)) or max(known,observed,confirmed)>at
                or not 0<=(at-observed).total_seconds()<=maximum_quote_age_seconds
                or quote.get('actual_received') is not True
                or any(type(x) not in (int,float) or not math.isfinite(x) or x<=0 for x in (ask,ceiling))):return result
        return {**result,'status':'KNOWN_ROOM_ONLY','reason':'NOT_A_CONFIRMED_STOP_TARGET_OR_ENTRY_SIGNAL',
            'known_resistance':ceiling,'observed_ask':ask,'room_fraction':(ceiling-ask)/ask}
    except (KeyError,TypeError,ValueError):return result
