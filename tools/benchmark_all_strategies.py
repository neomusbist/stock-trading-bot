"""Safe replacement entry point for the scratch simulator's misleading STRICT label.

Examples: --cache cache.json --claims old-results.json --output audit.json
Full engine replay: --events frozen.jsonl --config frozen-config.json
 --automation frozen-auto.json --initial-cash 70420 --output result.json
Never connects to a broker, reads LIVE implicitly, or writes an input file.
"""
import argparse
from collections import Counter
from dataclasses import fields, replace
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation.policy import AutomationConfig
from core.settings import Config
from research.causal_data import Frame, aware, json_values
from research.strict_benchmark import (FullPathReplay, STRATEGIES, audit_cache,
    missing_execution_evidence, normalize_cache_row, valid_tick_price)


def audit_claims(path):
    raw=Path(path).read_bytes(); results=json.loads(raw.decode('utf-8-sig')); rows=[]
    for run in results:
        if run.get('mode')!='STRICT':continue
        for trade in run.get('trades',[]):
            issues=['CUSTOM_ACCOUNT_AND_EXIT_NOT_AUTONOMOUS_ENGINE',
                    'WATCH_STATUS_AND_SOURCE_COUNT_SYNTHESIZED',
                    'HISTORICAL_RECEIPT_AND_DEPTH_NOT_PROVEN']
            if not valid_tick_price(trade.get('entry_price')):issues.append('ENTRY_PRICE_OFF_TICK')
            if not valid_tick_price(trade.get('exit_price')):issues.append('EXIT_PRICE_OFF_TICK')
            if trade.get('exit_reason')=='DATA_END_FLATTEN':issues.append('INVENTED_END_OF_DATA_LIQUIDATION')
            rows.append(dict(date=run['date'],strategy=run['strategy'],
                legacy_trade=trade,invalidity_reasons=issues,actual_fill=False,
                actual_orderable=None,corrected_net_pnl=None))
    return dict(input_sha256=hashlib.sha256(raw).hexdigest(),claims=rows,
                accepted_as_strict_performance=False)


def run_events(path,cfg,auto,cash,strategies):
    """Canonical recorded timeline; never silently manufacture missing evidence."""
    accounts={}; counts=Counter(); first=None;last=None;last_trade={}
    try:
        with Path(path).open(encoding='utf-8-sig') as stream:
            for line_no,line in enumerate(stream,1):
                if not line.strip():continue
                r=json.loads(line);stamp=aware(r.get('available_at') or r.get('processed_at') or r.get('known_at'))
                if stamp is None:
                    counts['STRATEGY_AVAILABILITY_UNKNOWN']+=1;continue
                if last and stamp<last:raise ValueError('NONCAUSAL_INPUT_ORDER')
                first=first or stamp;last=stamp
                if not accounts:
                    accounts={s:FullPathReplay(stamp,s,cfg,auto,cash) for s in strategies}
                event=r.get('event')
                if event=='WATCH_METADATA':
                    watch=r.get('watch') or {}
                    if type(watch.get('source_count')) is not int:
                        counts['WATCH_SOURCE_COUNT_UNKNOWN']+=1;continue
                    for account in accounts.values():account.qualify(r['symbol'],watch,stamp)
                elif event=='DAILY_REFERENCE':
                    for account in accounts.values():account.reference(r['symbol'],r['bars'],stamp)
                elif event in {'TRADE','TICK','QUOTE_RETRY'}:
                    normalized=normalize_cache_row(r)
                    absent=missing_execution_evidence(normalized)
                    if absent:
                        counts.update('MISSING:'+k for k in absent)
                        # A genuine trade with an unusable book must still warm
                        # the real detector and protect held positions. Only
                        # entry is blocked; do not erase its price/volume path.
                        trade_required={'symbol','time','price','volume','cumulative',
                                        'regular','received_at','processed_at'}
                        if trade_required.intersection(absent):
                            if normalized.get('symbol'):
                                for account in accounts.values():
                                    account.detector.mark_coverage_gap(normalized['symbol'],stamp,'INCOMPLETE_TRADE_EVIDENCE')
                                counts['COVERAGE_INVALIDATED_MISSING_TRADE']+=1
                            continue
                    if event=='QUOTE_RETRY':normalized['event']='TRADE'
                    frame=Frame.from_record(normalized,f'{Path(path).name}:{line_no}')
                    frame=replace(frame,basis=r.get('input_evidence_basis','UNVERIFIED_TIMELINE_EVIDENCE'))
                    identity=(frame.tick.time,frame.tick.price,frame.tick.volume,
                              frame.tick.cumulative,frame.received_at)
                    if event=='QUOTE_RETRY' and last_trade.get(frame.tick.symbol)!=identity:
                        raise ValueError('QUOTE_RETRY_WITHOUT_SAME_OBSERVED_TRADE')
                    if event!='QUOTE_RETRY':last_trade[frame.tick.symbol]=identity
                    for account in accounts.values():account.step(frame,quote_retry=event=='QUOTE_RETRY')
                    counts[event]+=1
                elif event=='COVERAGE_GAP':
                    for account in accounts.values():account.detector.mark_coverage_gap(r['symbol'],stamp,r.get('reason','RECORDED_GAP'))
                    counts[event]+=1
                else:counts['UNSUPPORTED_EVENT:'+str(event)]+=1
        return dict(mode='FULL_PRODUCTION_PATH_PAPER',first=first.isoformat() if first else None,
                    last=last.isoformat() if last else None,counts=dict(counts),
                    strategies={s:json_values(a.result()) for s,a in accounts.items()},
                    market_performance='UNKNOWN_IF_ANY_SCOPE_OR_INPUT_EVIDENCE_MISSING',
                    actual_order_permission=False,actual_fills=False)
    finally:
        for account in accounts.values():account.close()


def read_settings(path,kind):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'));names={f.name for f in fields(kind)}
    if set(data)-names:raise ValueError('UNKNOWN_SETTING_FIELDS')
    if names-set(data):raise ValueError('INCOMPLETE_EXPLICIT_SETTINGS')
    return kind(**data).validate()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',action='append',default=[])
    parser.add_argument('--claims');parser.add_argument('--events')
    parser.add_argument('--config');parser.add_argument('--automation')
    parser.add_argument('--initial-cash',type=float)
    parser.add_argument('--strategies',nargs='+',choices=STRATEGIES,default=list(STRATEGIES))
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv);out=Path(args.output).resolve()
    inputs=args.cache+[x for x in (args.claims,args.events,args.config,args.automation) if x]
    if out in {Path(x).resolve() for x in inputs}:raise ValueError('OUTPUT_MUST_NOT_OVERWRITE_INPUT')
    result={'schema':'CAUSAL_BENCHMARK_V1','actual_order_permission':False,
            'cache_audits':[audit_cache(p) for p in args.cache]}
    if args.claims:result['legacy_claim_audit']=audit_claims(args.claims)
    if args.events:
        if not all((args.config,args.automation,args.initial_cash is not None)):
            raise ValueError('EXPLICIT_CONFIG_AUTOMATION_CASH_REQUIRED')
        result['replay']=run_events(args.events,read_settings(args.config,Config),
                                   read_settings(args.automation,AutomationConfig),args.initial_cash,args.strategies)
    result['input_fingerprints']={str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in inputs}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(out),'actual_order_permission':False}))
    return result


if __name__=='__main__':main()
