"""FAST profit-policy comparison only; never imported by the order engine.

A is the independently supplied FAST decision. B reuses confirmed_protection
(1R + confirmed three-bar higher low). C reuses STRUCTURAL_TRAIL (confirmed
higher low + recovery above its high, without adding a 1R prerequisite).
Neither price touches nor two trades prove a completed-bar trend failure.
All state belongs to this observer, not the live position or risk manager.
"""
from copy import deepcopy
from datetime import datetime,timedelta

from automation.exit_plan import fresh,number
from automation.exit_candidates import observe_candidates
from automation.trade_quality import complete_sequence,confirmed_protection

POLICIES=('A_FAST_TWO_TRADES','B_CONFIRMED_BAR','C_STRUCTURAL_TRAIL')
REQUIRED_DATA=(
    'post-fill received 1-minute OHLCV, minute start/end and completion flag',
    'original exchange time, receipt time and processing time; gap/drop markers',
    'signal initial structure, initial emergency and actual fill time/price/quantity',
    'micro-line and confirmed-line change history with causal confirmation times',
    'sell-side bid/ask, full-quantity depth and their original quote times',
    'order send/ack/fill times, partial fills and broker commission/tax',
)


def observe_fast_profit(cfg,auto,position,asof,prior=None):
    """Point-in-time, side-effect-free observation; no broker or authority input.

    Saved live stop values are deliberately not used as candidate profit lines.
    In particular, an old micro line copied to a live stop is not bar evidence.
    Common account/data/structural risk guards remain owned by the live engine.
    """
    state=deepcopy(prior or {})
    out={'asof':asof.isoformat() if isinstance(asof,datetime) else None,
         'orders_enabled':False,'actual_order_permission':False,'observation_only':True,
         'actual_fill':None,'realized_net_pnl':None,'profitability_proven':False,
         'live_risk_authority':'UNCHANGED_ENGINE_NOT_REPLACED_BY_OBSERVER',
         'required_data':list(REQUIRED_DATA),'state':state,'policies':{}}

    def row(action,reason,line=None,**evidence):
        return {'action':action,'reason':reason,'profit_line':line,
                'orders_enabled':False,'actual_fill':None,'realized_net_pnl':None,
                'evidence':evidence}

    def unknown(reason):
        out['policies']={name:row('UNKNOWN',reason) for name in POLICIES}
        return out

    try:
        opened=datetime.fromisoformat(position['opened'])
        entry=position['avg'];initial=position['initial_stop'];qty=position['qty']
        signal=position.get('signal') or {};policy=signal.get('invalidation_policy') or {}
        emergency=policy.get('emergency_stop',position.get('emergency'))
        if (not isinstance(asof,datetime) or asof.tzinfo is None or opened.tzinfo is None
                or opened>asof or opened.date()!=asof.date()
                or not all(number(v) and v>0 for v in (entry,initial,emergency,qty))
                or not emergency<initial<entry):return unknown('INVALID_ENTRY_STRUCTURE_OR_TIME')
        if (position.get('pattern',signal.get('pattern'))!='FAST'):
            return unknown('NOT_FAST_POSITION')
        identity=[position.get('symbol',signal.get('symbol')),opened.isoformat(),entry]
        if state and state.get('identity')!=identity:
            state={};out['state']=state
        previous_at=datetime.fromisoformat(state['asof']) if state.get('asof') else None
        if previous_at and previous_at>asof:return unknown('NON_MONOTONIC_OBSERVATION')
    except (KeyError,ValueError,TypeError):return unknown('INVALID_ENTRY_STRUCTURE_OR_TIME')

    first=opened.replace(second=0,microsecond=0)
    if opened>first:first+=timedelta(minutes=1)
    raw=[]
    for bar in position.get('protection_bars') or ():
        if not isinstance(bar,dict) or bar.get('complete') is False:continue
        if bar.get('received_at') and not fresh(bar['received_at'],asof,86400):continue
        raw.append(bar)
    latest=complete_sequence(raw,asof,1,first,require_recent=True)
    bars=complete_sequence(raw,asof,3,first,require_recent=True)
    bars=bars if bars and all(number(b.get('volume')) and b['volume']>=0 for b in bars) else []
    latest=latest if latest and number(latest[0].get('volume')) and latest[0]['volume']>=0 else []
    out['completed_bar_data']='READY' if bars else 'INSUFFICIENT'
    out['latest_complete_bar']=latest[-1]['time'].isoformat() if latest else None

    quote=position.get('last_quote') or {}
    bid=quote.get('bid') if fresh(quote.get('time'),asof,cfg.stale_seconds) else None
    bid=bid if number(bid) and bid>0 else None
    out['shared_risk']={'initial_structure':initial,'initial_emergency':emergency,
        'fresh_bid':bid,'structural_loss_guard':'UNCHANGED_ENGINE',
        'account_loss_and_connection_guard':'UNCHANGED_ENGINE'}
    fast=position.get('fast_exit_evidence') or {}
    if fresh(fast.get('checked_at'),asof,cfg.stale_seconds) and fast.get('decision') in ('EXIT','HOLD'):
        out['policies'][POLICIES[0]]=row('EXIT_ALL_OBSERVATION' if fast['decision']=='EXIT' else 'HOLD',
            fast.get('reason','FAST_DECISION_UNKNOWN'),fast.get('micro_protection_stop'),
            basis='SUPPLIED_FAST_SNAPSHOT_NOT_RECOMPUTED',checked_at=fast.get('checked_at'))
    else:out['policies'][POLICIES[0]]=row('UNKNOWN','FRESH_FAST_DECISION_UNAVAILABLE')

    # An actual fresh bid at the original emergency is never delayed for a bar.
    # Other live risk guards are not reimplemented or weakened by this observer.
    if bid is not None and bid<=emergency:
        for name in POLICIES[1:]:out['policies'][name]=row('EXIT_ALL_OBSERVATION','INITIAL_EMERGENCY',bid=bid)
        return out
    if latest and latest[-1]['close']<=initial:
        for name in POLICIES[1:]:out['policies'][name]=row('EXIT_ALL_OBSERVATION','INITIAL_STRUCTURE_CLOSE_FAILED',
            closed_at=(latest[-1]['time']+timedelta(minutes=1)).isoformat())
        return out

    # Evaluate an already confirmed line before looking for any new pivot. One
    # valid new completed close suffices to break a known line; a missing third
    # historical candle must not defer that break or invent a new support.
    known_high=max([entry]+[b['high'] for b in bars or latest]
                   +([state['confirmed_high']] if number(state.get('confirmed_high')) else []))
    next_state=deepcopy(state);next_state.update(asof=asof.isoformat(),identity=identity,confirmed_high=known_high)
    for name in POLICIES[1:]:
        old=state.get(name) or {};line=old.get('line')
        valid_line=number(line) and initial<line and fresh(old.get('confirmed_at'),asof,86400)
        line=line if valid_line else None
        try:
            broken=bool(line is not None and latest and latest[-1]['close']<=line
                and datetime.fromisoformat(old['confirmed_at'])<latest[-1]['time']+timedelta(minutes=1))
        except (ValueError,TypeError):broken=False
        if broken:
            out['policies'][name]=row('EXIT_ALL_OBSERVATION','CONFIRMED_SUPPORT_CLOSE_FAILED',line,
                close=latest[-1]['close'],bar_time=latest[-1]['time'].isoformat())
            continue
        if not bars:
            out['policies'][name]=row('UNKNOWN','COMPLETE_POST_FILL_BARS_UNAVAILABLE',line)
            continue
        if name==POLICIES[1]:
            candidate=deepcopy(position)
            candidate.update(stop=line if line is not None else initial,
                high=known_high,initial_r=entry-initial,protection_bars=bars)
            candidate.pop('confirmed_protection_low',None)
            if valid_line and number(old.get('pivot_low')):candidate['confirmed_protection_low']=old['pivot_low']
            high,new,evidence=confirmed_protection(cfg,auto,candidate,bars[-1]['close'],asof)
            if new>candidate['stop']:
                line=new;next_state[name]={'line':line,'pivot_low':evidence['pivot_low'],
                    'confirmed_at':asof.isoformat()}
            out['policies'][name]=row('HOLD',evidence['reason'],line,
                basis='EXISTING_1R_CONFIRMED_HIGHER_LOW',source=evidence)
        else:
            candidate=deepcopy(position)
            candidate.update(stop=initial,protection_bars=bars,exit_requested=False,exit_reason='')
            candidate_state=observe_candidates(cfg,auto,candidate,asof,old.get('candidate_state') if valid_line else None)
            result=candidate_state['policies']['STRUCTURAL_TRAIL']
            new=candidate_state.get('pivot_stop')
            if number(new) and (line is None or new>line):line=new
            next_state[name]={'line':line,'confirmed_at':candidate_state.get('pivot_confirmed_at'),
                'candidate_state':candidate_state}
            out['policies'][name]=row('EXIT_ALL_OBSERVATION' if result['action']=='EXIT_ALL' else result['action'],
                result['reason'],line,basis='EXISTING_CONFIRMED_PIVOT_AND_RECOVERY',
                candidate_orders_enabled=result['orders_enabled'])
    out['state']=next_state
    return out
