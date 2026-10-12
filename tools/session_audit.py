"""저장된 JSONL만 읽는 당일 후보/탈락 감사. 네트워크/계좌 쓰기 없음."""
import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.util import shared_binary_reader

CASES = ('금호전기', '우리로', '와이어블', '빛샘전자', '대한광통신', '덕우전자',
         '이루온', '한국파마', '윈팩', '다산네트웍스', '에이스테크', '솔루엠')

def records(path):
    # Lossless rotation must not turn an all-day audit into a last-file-only audit.
    files=sorted((path.parent/'archive').glob(path.name+'.*'))+[path]
    for file in files:
        yield from file_records(file)

def file_records(path):
    if not path.exists(): return
    # 실행 중인 로그도 최초 파일 길이까지만 읽는다. writer/파일 잠금을 사용하지 않는다.
    size = path.stat().st_size
    with shared_binary_reader(path) as stream:
        while stream.tell() < size:
            line = stream.readline(min(size-stream.tell(), 16*1024*1024))
            if not line.endswith(b'\n'): break
            try: yield json.loads(line)
            except (ValueError, UnicodeError): continue

def audit(root, day):
    found={}; stages=Counter(); reasons=Counter(); gates=Counter(); cases={};unique=defaultdict(set)
    cycle_gaps=[]; previous=None; last=None; changes=[]
    for payload in records(root/'runtime/radar_history.jsonl'):
        stamp=payload.get('time','')
        if stamp[:10]!=day: continue
        dt=datetime.fromisoformat(stamp); last=stamp; stages['cycles']+=1
        if previous: cycle_gaps.append((dt-previous).total_seconds())
        previous=dt
        for row in payload.get('rows',[]):
            sym=row['code']; first=found.setdefault(sym,{'symbol':sym,'name':row.get('name'),
                'first_raw_at':row.get('first_raw_at',stamp),'first_raw_price':row.get('first_raw_price',row.get('reference_price')),
                'first_sources':list(row.get('source_details',{}))})
            stages['raw_observations']+=1; reasons[row.get('reason','NONE')]+=1
            unique['raw'].add(sym)
            aff=row.get('affordability',{})
            stages['affordable_observations' if aff.get('entry_eligible') is True else
                   'unaffordable_observations' if aff.get('reason_code')=='ONE_SHARE_UNAFFORDABLE' else 'capital_unconfirmed_observations']+=1
            unique['affordable' if aff.get('entry_eligible') is True else 'unaffordable' if aff.get('reason_code')=='ONE_SHARE_UNAFFORDABLE' else 'capital_unconfirmed'].add(sym)
            if row.get('status') in {'ELIGIBLE','WATCH','RESERVE'}:
                stages['compression_pass_observations']+=1;unique['compression_pass'].add(sym)
            else:stages['compression_not_pass_observations']+=1
            if row.get('reason')=='소스 교집합 부족' and aff.get('entry_eligible') is True:stages['affordable_source_rejected_observations']+=1
            if row.get('status')=='WATCH':
                stages['watch_observations']+=1;unique['watch'].add(sym);first.setdefault('first_watch_at',stamp)
                first.setdefault('first_watch_price',row.get('reference_price'))
            if row.get('name') in CASES:
                case=cases.setdefault(sym,{**first,'timeline':[],'evaluations':[], 'gate_counts':Counter()})
                case.update({k:v for k,v in first.items() if k.startswith('first_')})
                detail={k:row.get(k) for k in ('status','reason','reference_price','source_count','resistance','overhead_share_estimate','discovery_score','source_deltas')}
                detail.update(timestamp=stamp,sources=list(row.get('source_details',{})),affordability=aff)
                case['timeline'].append(detail)
        for kind in ('added','removed'):
            for sym in payload.get('changes',{}).get(kind,[]): changes.append({'timestamp':stamp,'symbol':sym,'change':kind})
    external=[]; event_counts=Counter(); setup_patterns=Counter(); risk_symbols=set(); order_events=[];risk_records=[];environments={}
    for folder in sorted((root/'runtime').glob('automation-*')):
        if not folder.is_dir():continue
        path=folder/'events.jsonl'
        for event in records(path):
            stamp=event.get('timestamp') or event.get('time','')
            if stamp[:10]!=day: continue
            if stamp[11:19]<'09:00:00': continue
            kind=event.get('event') or event.get('kind'); event_counts[kind]+=1
            env=environments.setdefault(path.parent.name,Counter());env[kind]+=1
            sym=event.get('symbol'); gate_map=event.get('gates',{})
            if kind=='ENTRY_EVAL':
                for key,value in gate_map.items(): gates[f'{key}:{value.get("result")}']+=1
                trials=event.get('setup_trials') or []
                for trial in trials:
                    pattern=trial.get('setup','UNKNOWN');setup_patterns[pattern]+=1
                    if trial.get('evaluated'):stages['setup_'+pattern+'_evaluated']+=1
                    if trial.get('entry_ready'):stages['setup_'+pattern+'_entry_ready']+=1
                if trials: stages['setup_trial_evaluations']+=1;unique['setup_trials'].add(sym)
                if event.get('signal_detected') or event.get('entry_price') and gate_map.get('risk_quantity'):
                    stages['signal_evaluations']+=1
                    unique['signal'].add(sym)
                if 'risk_quantity' in gate_map:
                    stages['risk_evaluations']+=1; risk_symbols.add(sym)
                    unique['risk'].add(sym)
                    risk_records.append({k:event.get(k) for k in ('timestamp','symbol','name','setup','entry_price','invalidation','gates','primary_reason_code')})
                    if gate_map['risk_quantity'].get('values',{}).get('risk_quantity')==0: stages['risk_zero_evaluations']+=1;unique['risk_zero'].add(sym)
                if gate_map and all(v.get('result')=='PASS' for v in gate_map.values() if v.get('required',True)) and 'risk_quantity' in gate_map:
                    stages['final_order_eligible_evaluations']+=1;unique['final_order_eligible'].add(sym)
                if sym in cases and ('warmup' in gate_map or trials or 'risk_quantity' in gate_map):
                    item={k:event.get(k) for k in ('timestamp','current_price','primary_reason_code','gates','setup_trials','signal_detected')}
                    cases[sym]['evaluations'].append(item)
                    for key,value in gate_map.items(): cases[sym]['gate_counts'][f'{key}:{value.get("result")}']+=1
            if kind in {'ORDER_ACCEPTED','ORDER_REJECTED','ORDER_UNKNOWN','FILL'}:
                order_events.append({k:event.get(k) for k in ('timestamp','event','symbol','side','qty','reason','execution')})
                if kind=='ORDER_ACCEPTED' and event.get('execution')=='REAL':stages['real_orders_accepted']+=1;unique['real_order'].add(sym)
            if kind=='EXIT_EVAL' and sym=='052260': external.append({'timestamp':stamp,'reason':event.get('primary_reason_code')})
    external.sort(key=lambda row:row['timestamp'])
    external_gaps=[(datetime.fromisoformat(b['timestamp'])-datetime.fromisoformat(a['timestamp'])).total_seconds() for a,b in zip(external,external[1:])]
    shadow_counts=Counter()
    for event in records(root/'runtime/shadow_events.jsonl'):
        if event.get('timestamp','')[:10]!=day:continue
        kind=event.get('event');shadow_counts[kind]+=1
        sym=event.get('symbol')
        if sym in cases and kind=='SHADOW_EVAL':cases[sym].setdefault('shadow_evaluations',[]).append(event)
    processing=[]
    for payload in records(root/'runtime/radar_history.jsonl'):
        if payload.get('time','')[:10]==day and payload.get('performance',{}).get('cycle_ms') is not None:processing.append(payload['performance'])
    for key in ('final_order_eligible_evaluations','real_orders_accepted'):stages.setdefault(key,0)
    return {'day':day,'last_radar_at':last,'counts':dict(stages),'unique_raw':len(found),'unique_stage_symbols':{k:len(v) for k,v in unique.items()},
        'compression_reasons':dict(reasons),'entry_gate_counts':dict(gates),'setup_trial_patterns':dict(setup_patterns),
        'compression_reason_percent_of_raw':{k:round(v*100/stages['raw_observations'],3) for k,v in reasons.items()} if stages['raw_observations'] else {},
        'risk_unique_symbols':len(risk_symbols),'risk_records':risk_records,'event_counts':dict(event_counts),'order_event_evidence':order_events,
        'events_by_environment':{k:dict(v) for k,v in environments.items()},'shadow_counts':dict(shadow_counts),
        'measured_latency':{'cycles':len(processing),'mean_cycle_ms':sum(x['cycle_ms'] for x in processing)/len(processing) if processing else None,
            'max_cycle_ms':max((x['cycle_ms'] for x in processing),default=None)},
        'case_details':cases,'watch_changes':changes,
        'cycle_spacing_seconds':{'count':len(cycle_gaps),'mean':sum(cycle_gaps)/len(cycle_gaps) if cycle_gaps else None,'max':max(cycle_gaps,default=None),
            'limitation':'후보 결과 완료 간격. 처리시간과 다르며 중단·재시작·주기 대기를 포함한다.'},
        'external_hold_log':{'count':len(external),'reasons':dict(Counter(x['reason'] for x in external)),
            'mean_gap_seconds':sum(external_gaps)/len(external_gaps) if external_gaps else None,
            'min_gap_seconds':min(external_gaps,default=None)},
        'limitations':['로그 누락을 통과 증거로 해석하지 않는다','저장 시점의 값만 사용. 사후 주가로 수익기회를 추정하지 않는다',
            '종목·주기 관측 수와 고유종목 수를 구분한다','기존 로그에는 단계별 실측 latency가 없다',
            'HTS 0168 원본 목록/시각이 없으므로 선행 포착률은 계산할 수 없다']}

def markdown(result):
    lines=['# 저장 로그 기반 당일 감사',f"날짜: {result['day']} / 마지막 후보 결과: {result['last_radar_at']}",
        '종목·주기 관측 수와 고유종목 수, 신호 로그 횟수는 서로 다른 단위입니다. gate 탈락은 중복 가능합니다.',
        '', '| 단계 | 관측/판단 수 |','|---|---:|']
    lines += [f'| {key} | {value} |' for key,value in result['counts'].items()]
    lines += ['', '| 종목 | 최초 raw 시각 / 가격 | 최초 WATCH 시각 / 가격 |','|---|---|---|']
    lines += [f"| {case.get('name')} {sym} | {case.get('first_raw_at')} / {case.get('first_raw_price')} | {case.get('first_watch_at','없음')} / {case.get('first_watch_price','-')} |" for sym,case in result['case_details'].items()]
    lines += ['', '| 압축 탈락/통과 사유 | 횟수 | 전체 raw 관측 비중 % |','|---|---:|---:|']
    lines += [f"| {key} | {value} | {result['compression_reason_percent_of_raw'].get(key)} |" for key,value in sorted(result['compression_reasons'].items(),key=lambda x:-x[1])]
    lines += ['','원본 숫자/Setup trial/SHADOW 전환/가상 수량은 같은 이름의 JSON에 보존됩니다.','']+result['limitations']
    return '\n'.join(lines)+'\n'

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--day',required=True); parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); result=audit(args.root,args.day)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    args.output.with_suffix('.md').write_text(markdown(result),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('counts','unique_raw','cycle_spacing_seconds','external_hold_log')},ensure_ascii=False))

if __name__=='__main__': main()
