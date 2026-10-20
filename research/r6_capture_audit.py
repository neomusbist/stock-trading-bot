"""Offline bounded event audit; no broker, settings writes or order authority.

Input is a privately extracted, half-open time window. Repeated evaluations are
not independent opportunities. An episode is a consecutive formed hypothesis
for one symbol/setup, ending when an actually evaluated frame has no shape.
Unknown/missing frames cannot establish market-wide recall or profitability.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


def records(path, maximum=128*1024*1024):
    if Path(path).stat().st_size>maximum:raise ValueError('BOUNDED_INPUT_EXCEEDED')
    with Path(path).open(encoding='utf-8-sig') as f:
        for line in f:
            if len(line)>2*1024*1024:raise ValueError('BOUNDED_RECORD_EXCEEDED')
            if line.strip():yield json.loads(line)


def trial_id(row, trial):
    return (row.get('symbol'),trial.get('setup'))


def gate_pass(trial, *, omit=(), turnover=None):
    # Removing a shape prerequisite is not a signal. UNKNOWN never becomes PASS.
    if not trial.get('candidate_generated'):return False
    gates=trial.get('gates') or {}
    if not gates:return False
    for name, value in gates.items():
        if name in omit:continue
        if name=='liquidity' and turnover is not None:
            amount=value.get('values',{}).get('window_turnover')
            if type(amount) not in (float,int) or amount<turnover:return False
        elif value.get('result')!='PASS':return False
    return True


def audit(rows):
    counts=Counter();raw={};evaluated=set();cycles=[];fast=Counter();status=Counter()
    setups={s:{'evaluations':0,'formed_frames':0,'formed_symbols':set(),'episodes':set(),
               'entry_ready_frames':0,'entry_ready_episodes':set(),'failed_gate_frames':Counter(),
               'single_gate_ablation_frames':Counter(),'single_gate_ablation_episodes':defaultdict(set),
               'turnover_experiments':Counter(),'turnover_episode_experiments':defaultdict(set),
               'simultaneous_failures':Counter(),'formed_geometry_digests':set()} for s in 'ABC'}
    current={};sequence=Counter();names={};signal_frames=0;rejoin=[];releases=[]
    for row in rows:
        event=row.get('event');counts[event]+=1
        if event=='RAW_CANDIDATES':
            for item in row.get('candidates',[]):raw[item['code']]=item;names[item['code']]=item.get('name')
        if event=='CANDIDATE_CYCLE_SUMMARY':cycles.append(row)
        if event=='OBSERVATION_RELEASED':releases.append(row)
        if event=='OBSERVATION_REJOINED':rejoin.append(row)
        if event!='ENTRY_EVAL':continue
        evaluated.add(row.get('symbol'));names[row.get('symbol')]=row.get('name')
        signal_frames+=bool(row.get('signal_detected'))
        fe=row.get('details',{}).get('fast_evaluation') or {}
        fast[fe.get('reason','UNKNOWN_NOT_RECORDED')]+=1
        status[fe.get('status','UNKNOWN_NOT_RECORDED')]+=1
        for trial in row.get('setup_trials') or []:
            s=trial.get('setup')
            if s not in setups:continue
            out=setups[s];key=trial_id(row,trial);out['evaluations']+=1
            if not trial.get('candidate_generated'):
                current.pop(key,None);continue
            if key not in current:sequence[key]+=1;current[key]=(key,sequence[key])
            episode=str(current[key]);out['episodes'].add(episode)
            out['formed_frames']+=1;out['formed_symbols'].add(row['symbol'])
            geometry=trial.get('setup_geometry')
            out['formed_geometry_digests'].add(hashlib.sha256(json.dumps(geometry,sort_keys=True).encode()).hexdigest())
            failures=tuple(sorted(k for k,v in (trial.get('gates') or {}).items() if v.get('result')!='PASS'))
            out['simultaneous_failures']['|'.join(failures) or 'NONE']+=1
            out['failed_gate_frames'].update(failures)
            if gate_pass(trial):
                out['entry_ready_frames']+=1;out['entry_ready_episodes'].add(episode)
            for gate in failures:
                if gate_pass(trial,omit={gate}):
                    out['single_gate_ablation_frames'][gate]+=1
                    out['single_gate_ablation_episodes'][gate].add(episode)
            # Explicit research grids, never written to live configuration.
            for threshold in (5_000_000,10_000_000,20_000_000,30_000_000):
                if gate_pass(trial,turnover=threshold):
                    out['turnover_experiments'][str(threshold)]+=1
                    out['turnover_episode_experiments'][str(threshold)].add(episode)
    classified=defaultdict(set);cycle_status=Counter();ever_cycle=set();source_one_rejected=set();source_one_watch=set()
    for cycle in cycles:
        for item in cycle.get('candidates',[]):
            symbol=item.get('symbol');ever_cycle.add(symbol)
            classified[item.get('primary_reason_code')].add(symbol)
            cycle_status[item.get('discovery_status')]+=1
            if item.get('source_count')==1:
                if item.get('primary_reason_code')=='소스 교집합 부족':source_one_rejected.add(symbol)
                if item.get('discovery_status')=='WATCH':source_one_watch.add(symbol)
    scan=classified.get('STATUS_SCAN_LIMIT',set())
    # This is only "no positive status evidence in retained cycle records";
    # do not call it a proof that no HTTP/status scan ever occurred.
    positive_status=set()
    for cycle in cycles:
        for item in cycle.get('candidates',[]):
            if item.get('primary_reason_code') not in {'STATUS_SCAN_LIMIT','ONE_SHARE_UNAFFORDABLE','HARD_LOCK'}:
                positive_status.add(item.get('symbol'))
    output={'scope':'2026-10-20T11:00:00 <= event.time < 11:30:00 KST',
            'event_counts':dict(counts),'raw_unique':len(raw),'entry_eval_unique':len(evaluated),
            'signal_detected_frames':signal_frames,'fast_status_frames':dict(status),'fast_reason_frames':dict(fast),
            'single_source_rejected_by_source_rule_unique':len(source_one_rejected),
            'single_source_watch_unique':len(source_one_watch),
            'scan_limit_ever_unique':len(scan),'scan_limit_without_other_status_evidence_unique':len(scan-positive_status),
            'scan_limit_counts_per_cycle':[len([i for i in c.get('candidates',[]) if i.get('primary_reason_code')=='STATUS_SCAN_LIMIT']) for c in cycles],
            'raw_without_entry_evaluation_unique':len(set(raw)-evaluated),
            'cycle_unique':len(ever_cycle),'cycle_carryover_outside_raw_window':sorted(ever_cycle-set(raw)),
            'reason_sets_overlap_not_partition':{str(k):len(v) for k,v in classified.items()},
            'subscription_releases':len(releases),'subscription_rejoins':len(rejoin),
            'positive_subscription_gaps':sum((r.get('subscription_gap_seconds') or 0)>0 for r in rejoin),
            'rejoined_with_retained_bars_after_gap':sum((r.get('subscription_gap_seconds') or 0)>0 and (r.get('stored_completed_bars') or 0)>0 for r in rejoin),
            'setups':{},'actual_fills':None,'net_pnl':None,'MFE':None,'MAE':None,
            'unknowns':['Market-wide missed opportunities and expectancy cannot be derived from emitted evaluations.',
                        'Cycle reason counts overlap and include carried evaluations; scan budget is internal.',
                        'Ablation counts are same-frame gate intersections, not new executable signals or fills.']}
    for s,detail in setups.items():
        output['setups'][s]={k:(len(v) if isinstance(v,set) else
            {name:len(ids) for name,ids in v.items()} if k.endswith('_episodes') or k=='turnover_episode_experiments'
            else dict(v) if isinstance(v,Counter) else v) for k,v in detail.items()}
    return output


def main():
    p=argparse.ArgumentParser();p.add_argument('--events',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=audit(records(args.events));result['input_sha256']=hashlib.sha256(args.events.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'setups','reason_sets_overlap_not_partition'}},ensure_ascii=False))


if __name__=='__main__':main()
