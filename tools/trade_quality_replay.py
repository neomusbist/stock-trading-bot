"""Replay explicitly supplied, frozen audit JSON. No runtime/API/broker access.

Usage: python -B tools/trade_quality_replay.py --evidence-dir FROZEN_DIR --output-dir PRIVATE_DIR
Required inputs: after-quality-audit.json, latest-health.json, production_audit.json,
context-snapshot-replay-with-latest.json. Outputs contain trade evidence, so keep
them outside source control. The script never reads config, policy or runtime.
"""
import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from automation.price_context import PriceContext
from automation.trade_quality import box_direction, complete_sequence
from automation.entry_timing import known_stamp


def read(folder,name):return json.loads((folder/name).read_text(encoding='utf-8-sig'))


def stamp(value):
    try:
        result=datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError,ValueError):return None


def elapsed(later,earlier):
    a,b=stamp(later),stamp(earlier)
    return round((a-b).total_seconds(),6) if a and b and a>=b else None


def discovery_times(earliest,signal,asof):
    """Only already known same-day discovery timestamps belong in the replay."""
    symbol=signal['symbol'];discovery=signal.get('discovery_context') or {}
    epoch=known_stamp(discovery.get('first_raw_at'),asof)
    raw=known_stamp(earliest.get(symbol+':raw',{}).get('time'),asof) or epoch
    watch=known_stamp(earliest.get(symbol+':WATCH_TRANSITION',{}).get('time'),asof)
    return raw,watch,epoch


def first_entry_result(events,symbol,signal_at):
    evaluations=sorted((ev for ev in events if ev.get('symbol')==symbol and ev.get('event')=='ENTRY_EVAL'
        and ev.get('signal_at')==signal_at and ev.get('decision') in {'NO_BUY','BUY'}),key=lambda e:e['time'])
    if not evaluations:return {'status':'UNKNOWN','reason':'FIRST_ENTRY_EVALUATION_NOT_RETAINED'}
    first=evaluations[0]
    return {'at':first['time'],'decision':first['decision'],'reason':first.get('primary_reason_code')}


def original_direction(signal,count=5):
    bars=complete_sequence(signal.get('decision_bar_snapshot'),stamp(signal['time']),count)
    if not bars:return None
    a,b=bars[0],bars[-1]
    return not (b['close']<a['open'] and b['high']<a['high'] and b['low']<a['low'])


def contextual_peak(signal,known,saved):
    """Recover predecision references; never bootstrap from a later decision."""
    asof=stamp(signal['time']);symbol=signal['symbol'];context=PriceContext()
    prior=[s for s in known if s.get('symbol')==symbol and stamp(s.get('time'))
        and stamp(s['time'])<=asof and stamp(s['time']).date()==asof.date()]
    recovery=context.bootstrap(prior,asof)
    reference=deepcopy((saved or {}).get('nearest_confirmed_peak'))
    supplement=False
    if reference and stamp(reference.get('confirmed_at')) and stamp(reference['confirmed_at'])<=asof:
        row=context.rows.setdefault(symbol,{'bars':deque(maxlen=30),
            'peaks':deque(maxlen=128),
            'first_observed_at':reference['time'],'last_complete_at':reference['confirmed_at']})
        context.day=asof.date()
        existing=next((p for p in row['peaks'] if p['time']==reference['time']),None)
        if existing is None:
            # An earlier frozen audit captured a reference outside the retained
            # decision-bar slice. Preserve its timestamp and explicit provenance.
            supplement=True;reference['provenance']='FROZEN_POINT_IN_TIME_REFERENCE'
            confirmed=stamp(reference['confirmed_at'])
            clears=[]
            for s in prior:
                for raw in s.get('decision_bar_snapshot',[]):
                    valid=complete_sequence([raw],stamp(s['time']),1)
                    if valid and valid[0]['time']>=confirmed and valid[0]['close']>reference['price']:
                        clears.append(valid[0])
            if clears:
                bar=min(clears,key=lambda b:b['time'])
                reference.update(cleared_at=(bar['time']+timedelta(minutes=1)).isoformat(),cleared_by_close=bar['close'])
            row['peaks'].append(reference)
    result=context.snapshot(symbol,SimpleNamespace(time=asof),signal['limit'])
    return result['nearest_confirmed_peak'],{
        'scope':'AVAILABLE_PREDECISION_SNAPSHOTS_AND_FROZEN_REFERENCE_ONLY',
        'recovered_complete_references':recovery['recovered_complete_references'],
        'supplemented_reference':supplement,'cleared_peak_count':result['cleared_peak_count'],
        'unknown_earlier_or_post_entry_history':True}


def replay(folder):
    audit=read(folder,'after-quality-audit.json');health=read(folder,'latest-health.json')
    production=read(folder,'production_audit.json');reference=read(folder,'context-snapshot-replay-with-latest.json')
    trades=sorted(audit['trades']+[health['last_trade']],key=lambda t:t['opened'])
    known=[t['signal'] for t in trades]+production.get('signals',[])
    saved={(r['symbol'],r['asof']):r for r in reference['rows']}
    events=production.get('timeline',[])+production.get('signals',[])+audit.get('signals',[])+audit.get('trade_events',[])
    rows=[]
    for trade in trades:
        signal=trade['signal'];symbol=signal['symbol'];asof=stamp(signal['time']);key=(symbol,signal['time'])
        ref=saved.get(key,{});peak,coverage=contextual_peak(signal,known,ref)
        entry,stop,target=signal['limit'],signal['stop'],signal['target']
        new_target=min(target,peak['price']) if peak else target
        rr=(new_target-entry)/(entry-stop)
        old_direction=original_direction(signal) if signal['pattern']=='C' else True
        new_direction,evidence=box_direction(signal.get('decision_bar_snapshot'),asof,5) if signal['pattern']=='C' else (True,{'reason':'A_SCOPE_UNCHANGED'})
        earliest=production.get('earliest',{})
        raw,watch,epoch_raw=discovery_times(earliest,signal,asof)
        pattern_events=[]
        for ev in events:
            event_time=ev.get('signal_at') or ev.get('time')
            dt=stamp(event_time)
            if ev.get('symbol')==symbol and dt and dt.date()==asof.date() and dt<=asof:
                if ev.get('event')=='ENTRY_CANDIDATE' or ev.get('signal_detected') is True:pattern_events.append(event_time)
        prior_valid=min(pattern_events) if pattern_events else signal['time']
        warmup=signal.get('evaluation_gates',{}).get('warmup',{}).get('values',{})
        observation_at=known_stamp(warmup.get('observation_start'),asof)
        observation=stamp(observation_at)
        epoch_signals=[t for t in pattern_events if observation and stamp(t)>=observation]
        epoch_first=min(epoch_signals) if epoch_signals else signal['time']
        first_result=first_entry_result(events,symbol,epoch_first)
        rows.append({
            'symbol':symbol,'setup':signal['pattern'],'first_candidate_in_retained_evidence':raw,
            'first_watch_in_retained_evidence':watch,'current_discovery_epoch_started':epoch_raw,
            'current_observation_started':observation_at,
            'first_saved_detector_signal_for_symbol':prior_valid,
            'first_saved_detector_signal_in_current_observation':epoch_first,
            'first_signal_entry_result_in_current_observation':first_result,
            'first_saved_detector_signal_entry_result':first_entry_result(events,symbol,prior_valid),
            'first_fully_executable_signal':'UNKNOWN: retained detector signals do not prove all account/order guards passed',
            'executed_signal_at':signal['time'],'order_sent_at':signal.get('order_sent_at'),
            'filled_at':trade['opened'],'entry_limit':entry,'actual_average_entry':trade['entry'],
            'signal_to_order_seconds':elapsed(signal.get('order_sent_at'),signal['time']),
            'signal_to_fill_seconds':elapsed(trade['opened'],signal['time']),
            'resistance':peak,'initial_invalidation':stop,'old_projection_target':target,
            'revised_target':new_target,'revised_rr':rr,
            'historical_live_entry':'ALLOWED_AND_FILLED',
            'base_7693c56_direction_pass':old_direction,
            'base_7693c56_resistance_pass':ref.get('rr_pass'),
            'base_7693c56_combined_pass':old_direction is True and ref.get('rr_pass') is True,
            'revised_direction_pass':new_direction,'direction_evidence':evidence,
            'revised_resistance_rr_pass':rr>=1.5,
            'revised_entry_price_gates_pass':new_direction is True and rr>=1.5,
            'revised_order_permission':'NOT_REPLAYED: account/cash/authorization/order guards still required',
            'actual_exit_at':trade['closed'],'actual_exit_price':trade['exit'],'actual_exit_reason':trade['reason'],
            'observed_cost_estimate_net':trade['pnl_estimate'],
            'observed_MFE':trade['MFE'],'observed_MAE':trade['MAE'],
            'observed_MFE_reached_1R':trade['MFE']>=trade['entry']-trade['initial_stop'],
            'revised_exit_price_and_net':'UNKNOWN: complete post-entry bars and receiver/REST receipt stream not retained',
            'coverage':coverage,'chronology_scope':'EARLIEST_IN_RETAINED_EVIDENCE; preopen WATCH does not prove continuous intraday observation',
        })
    blocked=[r for r in rows if not r['revised_entry_price_gates_pass']]
    return {'base_commit':'7693c56','scope':'OFFLINE_FROZEN_EVIDENCE_ONLY','future_prices_used_for_entry':False,
        'actual_orders':0,'network_calls':0,'rows':rows,'count':len(rows),
        'entry_gates_blocked':len(blocked),'blocked_old_losses':sum(r['observed_cost_estimate_net']<0 for r in blocked),
        'blocked_old_winners':sum(r['observed_cost_estimate_net']>0 for r in blocked),
        'parent_blocked':sum(not r['base_7693c56_combined_pass'] for r in rows),
        'newly_allowed_vs_parent':sum(r['revised_entry_price_gates_pass'] and not r['base_7693c56_combined_pass'] for r in rows),
        'signal_to_order_seconds_max':max(r['signal_to_order_seconds'] for r in rows if r['signal_to_order_seconds'] is not None),
        'observed_outcome_use':'AFTER_DECISION_COMPARISON_ONLY; not used to select entries or tune thresholds',
        'profitability':'NOT_ESTABLISHED; eight selected trades cannot establish expected value',
        'discovery_coverage':{'morning_raw_unique':production.get('raw_unique'),'afternoon_raw_unique':len(audit.get('raw',[])),
            'afternoon_watch_unique':len(audit.get('watch',[])),'whole_market_coverage':'UNKNOWN'},
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();result=replay(args.evidence_dir)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'trade-quality-replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
