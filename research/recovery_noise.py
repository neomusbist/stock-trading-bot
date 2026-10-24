"""Offline stop-noise audit; never submits or qualifies a production order.

Discover the unchanged recovery cohort with the existing replay. Then separate
receiver silence, consumer delay, cumulative-volume gaps and independent BOOK
knowledge. Candidate noise is frozen BEFORE looking at the subsequent path.
"""
import argparse
from bisect import bisect_left,bisect_right
from contextlib import closing
from datetime import timedelta
import hashlib,json,math,socket,sqlite3,time
from pathlib import Path
from unittest.mock import patch
from automation.capital import Costs,entry_economics,entry_quantity,loss_reserve,viable_entry_zone
from automation.policy import load_automation
from core.market_evidence import aware,bar_evidence
from core.market_time import market_freshness
from core.settings import load_config
from core.realtime import current_book_tick
from core.util import floor_tick,tick_size
from research.causal_data import Frame,file_sha,json_values
from research.initial_structure_replay import replay
from research.session_validation import index_freeze,verify_freeze
from research.evidence_collector import audit_freeze


def finite(x):return type(x) in (int,float) and math.isfinite(x)


def receiver_comparison(records,symbol,start,end):
    """Expose receiver-only packets; do not pretend they were consumed."""
    key=lambda r:(r.get('received_at') or r.get('received'),r.get('cumulative'),r.get('price'),r.get('volume'))
    consumed=set();received={}
    for k,s,r in records:
        if r.get('event')!='TRADE' or r.get('symbol')!=symbol or aware(k)>end:continue
        arrival=aware(r.get('received_at') or r.get('received'))
        if not arrival or not start<=arrival<=end:continue
        if r.get('capture_point')=='STRATEGY_INGESTION':consumed.add(key(r))
        if r.get('capture_point')=='RECEIVER':
            received[key(r)]={'source_id':s,'received_at':arrival,'receiver_known_at':aware(k),
                'price':r.get('price'),'volume':r.get('volume'),'cumulative':r.get('cumulative')}
    missing=[row for k,row in received.items() if k not in consumed]
    return {'receiver_records':len(received),'receiver_only_not_consumed_by_cutoff':missing,
        'scope':'RECORDED_RECEIVER_SUBSET' if received else 'UNKNOWN_NO_RECEIVER_CAPTURE_FOR_SYMBOL_WINDOW',
        'absence_of_receiver_record_is_not_absence_of_market_trade':True}


def trades(records,symbol,cutoff=None):
    """Actual consumer-known prints, including history stale at consumption.

    Stale history can describe a pretrigger path. It never becomes a fresh
    entry/exit tick, and receiver-only evidence cannot backdate consumption.
    """
    out=[];seen=set()
    for known,source,r in records:
        if r.get('event')!='TRADE' or r.get('capture_point')!='STRATEGY_INGESTION' or r.get('symbol')!=symbol:continue
        at=aware(known);received=aware(r.get('received_at') or r.get('received'));exchange=aware(r.get('exchange_at') or r.get('time'))
        if not at or not received or not exchange or cutoff and at>cutoff:continue
        key=(received,r.get('cumulative'),r.get('price'),r.get('volume'))
        if key in seen:continue
        seen.add(key)
        out.append({**r,'known':at,'received':received,'exchange':exchange,'source_id':source})
    return sorted(out,key=lambda r:(r['received'],r['cumulative'] if finite(r.get('cumulative')) else -1))


def book_intervals(records,symbol,start,end,ttl):
    """Local knowledge validity, never price/queue persistence beyond TTL.

    New invalid BOOKs invalidate old ones too. No future quote is carried back.
    These are documented quote intervals, not an exchange losslessness proof.
    """
    rows=[]
    for known,source,r in records:
        if r.get('event')!='BOOK' or r.get('symbol')!=symbol:continue
        at=aware(known);received=aware(r.get('received_at'));exchange=aware(r.get('exchange_at'))
        if not at or not received or not exchange or at>end:continue
        rows.append((at,received,exchange,source,r))
    rows.sort(key=lambda x:x[0]);intervals=[];valid=[]
    price_changes=sorted((aware(k),r.get('bid'),r.get('ask')) for k,s,r in records
        if r.get('symbol')==symbol and r.get('event')=='TRADE' and aware(k)<=end
        and finite(r.get('bid')) and finite(r.get('ask')) and 0<r['bid']<=r['ask'])
    for i,(at,received,exchange,source,r) in enumerate(rows):
        b,a=r.get('best_bid'),r.get('best_ask')
        fresh=market_freshness(exchange,at,ttl,received_at=received)['fresh']
        if not (finite(b) and finite(a) and 0<b<=a and fresh and r.get('depth_available') is True):continue
        until=min(received+timedelta(seconds=ttl),exchange+timedelta(seconds=ttl),end)
        if i+1<len(rows):until=min(until,rows[i+1][0])
        mismatch=next((k for k,bid,ask in price_changes if at<=k<=until and (bid,ask)!=(b,a)),None)
        if mismatch is not None:until=mismatch
        lo=max(start,at)
        if until<lo:continue
        row={'start':lo,'end':until,'known_at':at,'received_at':received,'exchange_at':exchange,
            'bid':b,'ask':a,'bid_qty':r.get('bid_qty'),'ask_qty':r.get('ask_qty'),'source_id':source}
        intervals.append(row);valid.append(r)
    merged=[]
    for row in intervals:
        lo,hi=row['start'],row['end']
        if merged and lo<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],hi)
        else:merged.append([lo,hi])
    covered=sum((hi-lo).total_seconds() for lo,hi in merged)
    return {'raw_BOOK_rows':sum(start<=x[0]<=end for x in rows),'valid_intervals':intervals,
        'covered_seconds':covered,'window_seconds':(end-start).total_seconds(),
        'coverage_scope':'RECEIVER_KNOWLEDGE_UNDER_RECORDED_UPDATES_NOT_PRICE_GUARANTEE',
        'capture_scope_complete':'UNKNOWN_REQUIRES_CONTINUOUS_WATCH_RECORDING_SCOPE',
        'max_observed_spread':max((r['best_ask']-r['best_bid'] for r in valid),default=None)}


def controls(records,symbol,start,end):
    gaps=[];added=None;removed=None;sessions=set();losses={}
    for known,source,r in records:
        at=aware(known)
        if not at or at>end:continue
        if r['event']=='SUBSCRIPTION_SCOPE':
            if symbol in r.get('added',[]):added=at
            if symbol in r.get('removed',[]):
                removed=at
                if at>=start:gaps.append({'at':at,'reason':'ACTUAL_UNSUBSCRIBE','source_id':source})
        if at<start:continue
        if r['event']=='STREAM_STATE' and r.get('kind') in {'ERROR','DISCONNECTED','RECONNECTING','TRANSPORT_FAILURE'}:
            gaps.append({'at':at,'reason':'STREAM_'+r['kind'],'source_id':source})
        session=r.get('capture_session')
        if session:
            sessions.add(session);losses[session]=max(losses.get(session,0),r.get('capture_losses',0))
    if any(losses.values()):gaps.append({'reason':'RECORDED_CAPTURE_LOSS_CANNOT_CERTIFY_WINDOW'})
    return {'explicit_disruptions':gaps,'last_added_at':added,'last_removed_at':removed,
        'subscription_at_start':'RECORDED_ACTIVE' if added and added<=start and (not removed or removed<added) else 'UNKNOWN',
        'capture_sessions_in_window':sorted(sessions),'transport_losslessness':'UNKNOWN_LOCAL_IDS_ARE_NOT_EXCHANGE_IDS'}


def continuity_pairs(rows,records,symbol,ttl,cutoff=None):
    cutoff=cutoff or max((r['known'] for r in rows),default=None)
    book_times=sorted(aware(r.get('received_at')) for k,s,r in records if r.get('symbol')==symbol
        and r['event']=='BOOK' and aware(r.get('received_at')) and cutoff and aware(k)<=cutoff)
    pairs=[]
    for left,right in zip(rows,rows[1:]):
        receipt_gap=(right['received']-left['received']).total_seconds()
        process_gap=(right['known']-left['known']).total_seconds()
        delta=right['cumulative']-left['cumulative'] if finite(right.get('cumulative')) and finite(left.get('cumulative')) else None
        volume=abs(right['volume']) if finite(right.get('volume')) else None
        balanced=(finite(delta) and finite(volume) and delta==volume and delta>0
            and left.get('venue') in {'KRX','NXT','SOR'} and left.get('venue')==right.get('venue'))
        book_count=bisect_left(book_times,right['received'])-bisect_right(book_times,left['received'])
        classification=('PROCESSING_DELAY_NOT_RECEIVER_SILENCE' if process_gap>ttl and receipt_gap<=ttl else
            'NO_UNACCOUNTED_TRADE_VOLUME_WITH_BOOK_UPDATES' if balanced and receipt_gap>ttl and book_count else
            'NO_UNACCOUNTED_TRADE_VOLUME_AT_RECORDED_BOUNDARIES' if balanced else 'UNKNOWN_UNACCOUNTED_CUMULATIVE_VOLUME')
        pairs.append({'left_received':left['received'],'right_received':right['received'],
            'left_known':left['known'],'right_known':right['known'],'receive_gap_seconds':receipt_gap,
            'process_gap_seconds':process_gap,'cumulative_delta':delta,'next_print_volume':volume,
            'volume_balanced':balanced,'BOOK_rows_between_receipts':book_count,'classification':classification,
            'right_source':right['source_id']})
    return pairs


def prefix_audit(records,symbol,cutoff,support,cfg):
    start=cutoff-timedelta(seconds=60);allprints=trades(records,symbol,cutoff)
    rows=[r for r in allprints if start<=r['received']<=cutoff]
    scope=controls(records,symbol,start,cutoff);book=book_intervals(records,symbol,start,cutoff,cfg.stale_seconds)
    pairs=continuity_pairs(rows,records,symbol,cfg.stale_seconds,cutoff)
    receiver=receiver_comparison(records,symbol,start,cutoff)
    result={'start':start,'cutoff':cutoff,'actual_known_prints':len(rows),'controls':scope,'books':book,
        'pairs':pairs,'receiver_comparison':receiver,'candidate_noise':None,'actual_order_permission':False,'noise_model_validated':False}
    if len(rows)<3:return {**result,'reason':'UNKNOWN_TOO_FEW_ACTUAL_HISTORY_PRINTS'}
    span=(rows[-1]['received']-rows[0]['received']).total_seconds()
    result.update(receive_span_seconds=span,max_receive_gap_seconds=max(p['receive_gap_seconds'] for p in pairs),
        max_process_gap_seconds=max(p['process_gap_seconds'] for p in pairs),
        max_process_delay_seconds=max((r['known']-r['received']).total_seconds() for r in rows),
        process_stale_history_rows=sum(not market_freshness(r['exchange'],r['known'],cfg.stale_seconds,received_at=r['received'])['fresh'] for r in rows))
    if span<60-cfg.stale_seconds or (cutoff-rows[-1]['received']).total_seconds()>cfg.stale_seconds:
        return {**result,'reason':'UNKNOWN_PREFIX_ENDPOINT_COVERAGE'}
    if scope['explicit_disruptions']:return {**result,'reason':'UNKNOWN_EXPLICIT_RECORDING_OR_SUBSCRIPTION_GAP'}
    if receiver['receiver_only_not_consumed_by_cutoff']:
        return {**result,'reason':'UNKNOWN_RECEIVER_ONLY_PREFIX_EVENTS_NOT_STRATEGY_KNOWN'}
    if not all(p['volume_balanced'] for p in pairs):return {**result,'reason':'UNKNOWN_UNACCOUNTED_TRADE_VOLUME'}
    if any(not market_freshness(r['exchange'],r['received'],cfg.stale_seconds,received_at=r['received'])['fresh'] for r in rows):
        return {**result,'reason':'UNKNOWN_RECEIVER_CLOCK_OR_SOURCE_STALENESS'}
    if any(not finite(r.get('bid')) or not finite(r.get('ask')) or not 0<r['bid']<=r['ask'] for r in rows):
        return {**result,'reason':'UNKNOWN_RECORDED_QUOTE_PRICE_PAIR'}
    jump=max(abs(b['price']-a['price']) for a,b in zip(rows,rows[1:]))
    print_spread=max(r['ask']-r['bid'] for r in rows)
    noise=max(jump,print_spread,book['max_observed_spread'] or 0,tick_size(support))
    return {**result,'reason':'OBSERVED_HISTORY_CANDIDATE_NOT_VALIDATED_CONTINUOUS_BOOK_ENVELOPE',
        'candidate_noise':noise,'max_adjacent_price_change':jump,'max_recorded_print_quote_spread':print_spread,
        'counter_assumption':'SAME_VENUE_REPORTED_CUMULATIVE_EQUALS_SUM_OF_POSITIVE_PRINT_VOLUMES',
        'unseen_book_widening':'UNKNOWN_OUTSIDE_DOCUMENTED_BOOK_INTERVALS',
        'first_received':rows[0]['received'],'last_received':rows[-1]['received']}


def received_bars(rows,records=(),symbol=''):
    """Known-at close reconstruction only; never a synthetic trade or REST bar."""
    groups={};closed=[]
    for r in sorted(rows,key=lambda x:x['known']):
        minute=r['exchange'].replace(second=0,microsecond=0)
        for prior in sorted(k for k in groups if k<minute):
            values=groups.pop(prior);ordered=sorted(values,key=lambda x:(x['exchange'],x['cumulative']))
            anchor=next((a for a in reversed(rows) if a['received']<ordered[0]['received'] and a['known']<=r['known']),None)
            boundary=([anchor] if anchor else [])+ordered+[r]
            balanced=bool(anchor) and all(finite(b.get('volume')) and b['cumulative']-a['cumulative']==abs(b['volume'])>0
                and a.get('venue')==b.get('venue') for a,b in zip(boundary,boundary[1:]))
            disruption=controls(records,symbol,prior,r['known'])['explicit_disruptions'] if records else []
            gap=('MINUTE_START_UNOBSERVED' if ordered[0]['exchange'].second>5 else
                'CUMULATIVE_BOUNDARIES_UNPROVEN' if not balanced else 'EXPLICIT_SCOPE_OR_STREAM_GAP' if disruption else None)
            closed.append({'time':prior,'available_at':max(prior+timedelta(minutes=1),r['known']),
                'open':ordered[0]['price'],'high':max(x['price'] for x in values),'low':min(x['price'] for x in values),
                'close':ordered[-1]['price'],'first_second':ordered[0]['exchange'].second,
                'observation_source':'ACTUAL_RECEIVED','source':'RECONSTRUCTED_ACTUAL_PRINTS_NOT_PRODUCER_SIGNAL_BAR',
                'gap_reason':gap})
        groups.setdefault(minute,[]).append(r)
    return closed


def path_audit(records,symbol,at,support,structural,emergency,cfg,quantity,entry,costs,horizon=600):
    """Fixed initial lines; no hypothetical strategy execution or profit claim."""
    allrows=trades(records,symbol);end=at+timedelta(seconds=horizon)
    future=[r for r in allrows if at<r['received'] and r['known']<=end]
    candidates={b['time']:b for b in received_bars(allrows,records,symbol)}
    for k,s,r in records:
        if r['event']!='COMPLETED_BAR' or r.get('symbol')!=symbol or r.get('source')!='ACTUAL_RECEIVED_DETECTOR_HISTORY':continue
        b={**r['bar'],'available_at':k,'observation_source':'ACTUAL_RECEIVED'};ev=bar_evidence(b)
        if ev['signal_source_verified'] and ev['available_at']<=end:
            candidates[ev['start']]={**b,'time':ev['start'],'available_at':ev['available_at'],'source':'RECORDED_PRODUCER_COMPLETE_BAR'}
    bars=sorted((b for b in candidates.values() if b['time']>=at and b['available_at']<=end and not b.get('gap_reason')),key=lambda b:b['available_at'])
    confirmed=next((b for b in bars if b['close']<=structural),None)
    window=[r for r in future if not confirmed or r['known']<=confirmed['available_at']]
    fresh=lambda r:market_freshness(r['exchange'],r['known'],cfg.stale_seconds,received_at=r['received'])['fresh']
    touched=next((r for r in future if r['price']<=emergency),None)
    actionable=next((r for r in future if r['price']<=emergency and fresh(r)),None)
    recovered=next((b for b in bars if touched and b['available_at']>touched['known'] and b['close']>structural),None)
    quote_ticks=[]
    for r in future:
        if (r.get('depth_available') is True and r.get('depth_source') in {'0D','0B_FIELDS'}
            and market_freshness(r.get('book_time'),r['known'],cfg.stale_seconds,received_at=r.get('book_received_at'))['fresh']):
            quote_ticks.append({'at':r['known'],'received_at':aware(r.get('book_received_at')),
                'bid':r.get('bid'),'bid_qty':r.get('bid_qty'),'source':r['source_id']})
    for k,s,r in records:
        known=aware(k)
        if r['event']=='BOOK' and r.get('symbol')==symbol and at<known<=at+timedelta(seconds=horizon):
            if (r.get('depth_available') is True and market_freshness(r.get('exchange_at'),known,cfg.stale_seconds,received_at=r.get('received_at'))['fresh']):
                quote_ticks.append({'at':known,'received_at':aware(r.get('received_at')),'bid':r.get('best_bid'),
                    'bid_qty':r.get('bid_qty'),'source':s})
    quote_ticks.sort(key=lambda r:r['at'])
    bid_touch=next((r for r in quote_ticks if finite(r['bid']) and r['bid']<=emergency),None)
    trigger_at=min((x for x in [actionable['known'] if actionable else None,bid_touch['at'] if bid_touch else None] if x),default=None)
    protect_at=min((x for x in [trigger_at,confirmed['available_at'] if confirmed else None] if x),default=None)
    protect_reason=('CONFIRMED_COMPLETE_CLOSE' if confirmed and protect_at==confirmed['available_at'] else 'FRESH_EMERGENCY' if protect_at else None)
    liquidation=[]
    if protect_at and quantity:
        for delay in (0,.25,1.,2.):
            # First subsequently observed quote, not a claimed fill at the TTL.
            q=next((q for q in quote_ticks if protect_at+timedelta(seconds=delay)<=q['at']<=protect_at+timedelta(seconds=delay+cfg.stale_seconds)),None)
            enough=q and type(q['bid_qty']) is int and q['bid_qty']>=quantity
            liquidation.append({'assumed_latency_seconds':delay,'quote_at':q['at'] if q else None,
                'bid':q['bid'] if q else None,'available_bid_qty':q['bid_qty'] if q else None,
                'full_quantity_L1_observed':bool(enough),'actual_fill':False,
                'trigger_basis':protect_reason,'trigger_at':protect_at,
                'scenario_loss_with_reserves':quantity*loss_reserve(entry,q['bid'],costs) if enough else None,
                'planned_loss_reserve':quantity*loss_reserve(entry,emergency,costs)})
    pairs=continuity_pairs(future,records,symbol,cfg.stale_seconds)
    unbalanced=[p for p in pairs if not p['volume_balanced']]
    checkpoints={}
    for seconds in (30,60,180,horizon):
        selected=[r for r in future if r['received']<=at+timedelta(seconds=seconds)]
        tail=(at+timedelta(seconds=seconds)-selected[-1]['received']).total_seconds() if selected else None
        checkpoints[str(seconds)]={'observed_trade_rows':len(selected),'last_received':selected[-1]['received'] if selected else None,
            'tail_gap_seconds':tail,'tail_observed_within_existing_TTL':tail is not None and tail<=cfg.stale_seconds,
            'min_observed_price':min((r['price'] for r in selected),default=None),
            'max_observed_price':max((r['price'] for r in selected),default=None),'actual_holding_period':False}
    return {'basis':'COUNTERFACTUAL_FIXED_INITIAL_LINES_NOT_EXECUTED_TRADE','horizon_seconds':horizon,
        'future_trade_rows':len(future),'first_future_at':future[0]['known'] if future else None,
        'last_future_at':future[-1]['known'] if future else None,
        'first_raw_emergency_touch':touched['known'] if touched else None,
        'first_fresh_trade_touch':actionable['known'] if actionable else None,'first_fresh_bid_touch':bid_touch,
        'first_actionable_emergency_at':trigger_at,
        'first_fixed_line_protection_event':{'at':protect_at,'reason':protect_reason},
        'emergency_delay_seconds':(trigger_at-at).total_seconds() if trigger_at else None,
        'first_eligible_complete_close_failure':confirmed,
        'first_eligible_close_above_structure_after_touch':recovered,
        'touch_then_recovered_before_confirmed_failure':bool(touched and recovered and (not confirmed or recovered['available_at']<confirmed['available_at'])),
        'max_observed_excursion_below_support_before_close_failure':max((max(0,support-r['price']) for r in window),default=None),
        'max_observed_excursion_below_structural_before_close_failure':max((max(0,structural-r['price']) for r in window),default=None),
        'max_observed_entry_adverse_before_close_failure':max((max(0,entry-r['price']) for r in window),default=None),
        'future_unbalanced_volume_pairs':len(unbalanced),'unbalanced_pair_evidence':unbalanced,
        'long_receive_or_processing_intervals':[p for p in pairs if max(p['receive_gap_seconds'],p['process_gap_seconds'])>cfg.stale_seconds],
        'future_controls':controls(records,symbol,at,end),'checkpoints':checkpoints,
        'receiver_comparison':receiver_comparison(records,symbol,at,end),
        'fresh_quote_observations':len(quote_ticks),'liquidation_sensitivity':liquidation,
        'dynamic_trailing_and_other_exits':'NOT_SIMULATED','unseen_extremes':'UNKNOWN_NOT_AN_EXCHANGE_CENSUS'}


def risk_stress(economics,sizing,risk_budget,observed_jump,costs):
    q=sizing.get('quantity')
    if not q or not finite(observed_jump) or observed_jump<=0:return None
    stop=economics['emergency_stop'];price=floor_tick(stop-observed_jump)
    if price<=0:return None
    planned=q*economics['risk_per_share'];loss=q*loss_reserve(economics['entry_price'],price,costs)
    return {'basis':'ONE_EXTRA_OBSERVED_PRETRIGGER_JUMP_AFTER_STOP_HYPOTHESIS_NOT_A_FILL',
        'extra_adverse_move':observed_jump,'stressed_exit_price':price,'quantity':q,
        'planned_loss_reserve':planned,'stressed_loss_with_reserves':loss,
        'extra_loss_over_plan':loss-planned,'scenario_trade_risk_budget':risk_budget,
        'exceeds_trade_budget':risk_budget is not None and loss>risk_budget,'actual_fill':False}


def audit_case(records,case,cfg,auto,scenario_cash,*,path_seconds=1200):
    p=case['plan'];at=aware(case['first_observed_at']);sym=case['symbol']
    cutoff=aware(p['micro_defense']['rows'][0]['known_at'])
    prefix=prefix_audit(records,sym,cutoff,p['support'],cfg)
    if not any(s==case['source_id'] for _,s,_ in records):raise ValueError('COHORT_SOURCE_NOT_IN_FROZEN_INPUT')
    actual=trades(records,sym,at);recovered=p['micro_defense']['rows'][-1]
    source=next((r for r in reversed(actual) if r['cumulative']==recovered['cumulative'] and r['price']==recovered['price']),None)
    if source is None:raise ValueError('COHORT_RECOVERED_PRINT_MISSING')
    frame=Frame.from_record(source,source['source_id']);book=None
    for k,s,r in records:
        if aware(k)<=at and r['event']=='BOOK' and r.get('symbol')==sym:
            book={'time':r.get('exchange_at'),'received_at':r.get('received_at'),
                **{f:r.get(f) for f in ('best_bid','best_ask','bid_qty','ask_qty','depth_available')}}
    tick=current_book_tick(frame.tick,book,at,cfg.stale_seconds)
    entry=tick.ask;costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
    cash=min(scenario_cash,auto.capital_limit) if scenario_cash is not None else None
    risk=min(cash*cfg.risk_fraction,cash*cfg.max_daily_loss_fraction) if cash is not None else None
    models={}
    for name,noise in [('bar_range',p['noise_band']),('received_60s',prefix['candidate_noise'])]:
        if noise is None:models[name]={'status':'UNKNOWN','reason':prefix['reason']};continue
        emergency=floor_tick(p['structural_stop']-noise)
        economics=entry_economics(entry,p['structural_stop'],emergency,p['target'],costs,cfg.min_reward_risk)
        zone=viable_entry_zone(economics,p['level'],cfg.max_chase)
        size=entry_quantity(economics,cash*cfg.max_position_fraction,risk) if cash is not None else {'quantity':None}
        q=size['quantity']
        quote_reason=Frame(tick,frame.received_at,at,frame.basis,frame.source_id).quote_reason(at,cfg.stale_seconds)
        quote_known=quote_reason is None
        depth=bool(quote_known and q and type(tick.ask_qty) is int and type(tick.bid_qty) is int and min(tick.ask_qty,tick.bid_qty)>=q)
        models[name]={'noise':noise,'emergency':emergency,'economics':economics,'entry_zone':zone,'sizing_scenario':size,
            'current_received_quote_depth_known':quote_known,'quote_rejection':quote_reason,'current_depth_sufficient_for_scenario':depth,
            'price_RR_L1_scenario_pass':bool(economics['rr_pass'] and depth and zone.get('current_order_price_in_zone')
                and not tick.vi_active and tick.regular and (tick.ask-tick.bid)/tick.price<=cfg.max_spread),
            'actual_historical_account':'UNKNOWN','WATCH_qualification':'UNKNOWN_NOT_IN_THIS_COHORT_RECORD',
            'noise_validated':False,'actual_orderable':False,
            'extra_jump_risk_sensitivity':risk_stress(economics,size,risk,prefix.get('max_adjacent_price_change'),costs),
            'path':path_audit(records,sym,at,p['support'],p['structural_stop'],emergency,cfg,q,entry,costs,horizon=path_seconds)}
    return {'symbol':sym,'structure_and_trigger_at':at,'source_id':case['source_id'],
        'unchanged_geometry':{k:p[k] for k in ('level','support','structural_stop','target','target_evidence')},
        'recorded_entry_ask':entry,'recorded_bid':source.get('bid'),'recorded_ask_qty':source.get('ask_qty'),
        'recorded_bid_qty':source.get('bid_qty'),'prefix':prefix,'models':models,
        'scenario_cash':cash,'scenario_per_trade_risk':risk,'actual_order_permission':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario-cash',type=float);parser.add_argument('--cohort-freeze',type=Path)
    parser.add_argument('--path-seconds',type=int,default=1200);args=parser.parse_args(argv)
    # The optional older freeze fixes the discovery cohort while an append-only
    # extension supplies more AFTERPATH only. Neither prefix can change prices.
    root=args.freeze.resolve();out=args.output.resolve();manifest=verify_freeze(root)
    cohort_root=args.cohort_freeze.resolve() if args.cohort_freeze else root
    cohort_manifest=verify_freeze(cohort_root)
    if out.exists() or out==root or root in out.parents:raise ValueError('NEW_EXTERNAL_OUTPUT_REQUIRED')
    if out==cohort_root or cohort_root in out.parents:raise ValueError('OUTPUT_OVERLAPS_COHORT_INPUT')
    if cohort_manifest['day']!=manifest['day'] or any(file_sha(root/n)!=file_sha(cohort_root/n) for n in ('config.json','automation.json')):
        raise ValueError('COHORT_DAY_OR_COST_CONFIG_MISMATCH')
    if args.scenario_cash is not None and (not finite(args.scenario_cash) or args.scenario_cash<0):raise ValueError('INVALID_CASH_SCENARIO')
    if not 180<=args.path_seconds<=3600:raise ValueError('OFFLINE_PATH_HORIZON_MUST_BE_180_TO_3600_SECONDS')
    cfg=load_config(root/'config.json');auto=load_automation(root/'automation.json')
    if auto.fast_momentum_enabled or auto.pullback_expansion_target_enabled:raise ValueError('KEEP_FAST_B_EXPANSION_OFF')
    out.mkdir(parents=True);(out/'cohort').mkdir()
    source_files=('recovery_noise.py','support_recovery.py','initial_structure_replay.py')
    source_before={n:file_sha(Path(__file__).parent/n) for n in source_files}
    started=time.perf_counter();cpu=time.process_time()
    attempts=[]
    def denied(*a,**kw):attempts.append(1);raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
    with patch.object(socket,'socket',denied),patch.object(socket,'create_connection',denied):
        audit_freeze(root,out/'SEQUENCE_AUDIT.json')
        with closing(sqlite3.connect(out/'events.sqlite')) as db:
            _,inventory=index_freeze(root,db,manifest['day'],'REGULAR')
            if cohort_root==root:cohort=replay(db,cfg,auto,out/'cohort',hypothesis='B',scenario_cash=args.scenario_cash)
            else:
                with closing(sqlite3.connect(out/'cohort_events.sqlite')) as discovery:
                    index_freeze(cohort_root,discovery,manifest['day'],'REGULAR')
                    cohort=replay(discovery,cfg,auto,out/'cohort',hypothesis='B',scenario_cash=args.scenario_cash)
            cases=[]
            for c in cohort['plans']:
                at=aware(c['first_observed_at'])
                # Include earlier controls so unknown initial scope stays explicit.
                records=[(k,s,json.loads(p)) for k,s,p in db.execute(
                    "SELECT known,source,payload FROM events WHERE known<=? AND (json_extract(payload,'$.symbol')=? OR json_extract(payload,'$.event') IN ('STREAM_STATE','SUBSCRIPTION_SCOPE','CAPTURE_STARTED','CAPTURE_STATUS')) ORDER BY known,ordinal",
                    ((at+timedelta(seconds=args.path_seconds)).isoformat(),c['symbol']))]
                cases.append(audit_case(records,c,cfg,auto,args.scenario_cash,path_seconds=args.path_seconds))
    result={'day':manifest['day'],'input_manifest_sha256':file_sha(root/'MANIFEST.json'),
        'cohort_input_manifest_sha256':file_sha(cohort_root/'MANIFEST.json'),
        'cohort_structures':len(cases),'cases':cases,'actual_orderable_signals':0,
        'conclusion':'HOLD_NOT_A_LIVE_CANDIDATE','network_attempts':len(attempts),'actual_orders':0,
        'baseline_source_unchanged':True,'inventory':inventory,
        'cpu_seconds':time.process_time()-cpu,'wall_seconds':time.perf_counter()-started,
        'price_RR_L1_cash_scenario_cases':sum(c['models']['received_60s'].get('price_RR_L1_scenario_pass',False) for c in cases),
        'source_sha256':{name:file_sha(Path(__file__).parent/name) for name in source_files}}
    if source_before!=result['source_sha256']:raise ValueError('SOURCE_CHANGED_DURING_AUDIT_NO_COMPLETE_RESULT')
    (out/'RESULT.json').write_text(json.dumps(json_values(result),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='cases'},ensure_ascii=False));return 0


if __name__=='__main__':main()
