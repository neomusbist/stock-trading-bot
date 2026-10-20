"""Bounded, read-only evidence inventory; not a strategy or a fill simulator.

Reads a frozen ledger plus retained receiver records. All sell fills with the
same trade identity are one trade. Missing tape, late bars and truncated tails
stay unknown. No broker, network, runtime import, subscription or order method.
Outputs contain private trade details: keep them outside source control.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path


def timestamp(value):
    try:
        dt=datetime.fromisoformat(value) if isinstance(value,str) else value
        return dt if isinstance(dt,datetime) and dt.tzinfo is not None else None
    except (TypeError,ValueError):return None


def known_at(row):
    """Receiver order for prints, consumer order AND closed interval for bars."""
    if row.get('event')=='COMPLETED_BAR':
        start=timestamp((row.get('bar') or {}).get('time'))
        receipt=timestamp(row.get('trigger_received_at') or row.get('confirmed_at'))
        processed=timestamp(row.get('processed_at'))
        if not start or start.second or start.microsecond or not receipt or not processed:return None
        values=[start+timedelta(minutes=1),receipt,processed]
        explicit=timestamp(row.get('known_at'))
        if explicit:values.append(explicit)
        return max(values)
    for field in ('received_at','at','asof','confirmed_at'):
        value=timestamp(row.get(field))
        if value:return value
    return None


def seconds(end,start):
    end=timestamp(end);start=timestamp(start)
    return (end-start).total_seconds() if end and start else None


def grouped_trades(state,dates):
    """A partial exit is not a second opportunity. Missing IDs remain explicit."""
    groups=defaultdict(list)
    for row in state.get('trades',[]):
        if str(row.get('opened',''))[:10] not in dates:continue
        groups[(row.get('trade_id'),row.get('symbol'),row.get('opened'))].append(row)
    result=[]
    for (identity,symbol,opened),fills in groups.items():
        first=fills[0];sig=first.get('signal') or {};qty=sum(f['qty'] for f in fills)
        closed=max(f['closed'] for f in fills);plan=sig.get('exit_plan') or {}
        evidence=sig.get('fast_entry_evidence') or {}
        trigger=first.get('exit_trigger_evidence') or {}
        broker_confirmed=all(f.get('pnl_source')=='BROKER_QUERY' and f.get('net_pnl') is not None for f in fills)
        fees_known=all(f.get('fee_actual') is not None for f in fills)
        def seen_extreme(field):
            values=[f.get(field) for f in fills
                    if type(f.get(field)) in (int,float) and math.isfinite(f[field]) and f[field]>=0]
            return max(values) if values else None
        result.append({'trade_id':identity,'identity_status':'KNOWN' if identity else 'UNKNOWN_FALLBACK_SYMBOL_OPENED',
            'symbol':symbol,'name':first.get('name'),'setup':first.get('setup'),
            'opened':opened,'closed':closed,'holding_seconds_ledger':seconds(closed,opened),
            'holding_basis':'LEDGER_NOT_BROKER_CLOCK_PROOF','sell_fill_rows':len(fills),'qty':qty,
            'entry':sum(f['entry']*f['qty'] for f in fills)/qty,
            'exit':sum(f['exit']*f['qty'] for f in fills)/qty,
            'broker_net_pnl':sum(f['net_pnl'] for f in fills) if broker_confirmed else None,
            'actual_costs':sum(f['fee_actual'] for f in fills) if fees_known else None,
            'pnl_basis':'SAVED_BROKER_QUERY' if broker_confirmed else 'UNKNOWN',
            'MFE_seen_per_share':seen_extreme('MFE'),
            'MAE_seen_per_share':seen_extreme('MAE'),
            'extrema_basis':'SEEN_LEDGER_PRICES_NOT_COMPLETE_MARKET_EXTREMA',
            'signal_at':first.get('signal_at'),'signal_detected_at':sig.get('signal_detected_at'),
            'order_sent_at':first.get('order_sent_at'),
            'signal_to_submit_seconds':seconds(first.get('order_sent_at'),sig.get('signal_detected_at')),
            'submit_to_first_fill_seconds':seconds(opened,first.get('order_sent_at')),
            'opportunity_timing':sig.get('opportunity_timing'),
            'entry_limit':plan.get('entry',sig.get('limit')),
            'invalidation_policy':sig.get('invalidation_policy'),'geometry':evidence.get('geometry'),
            'metrics':evidence.get('metrics'),'target':sig.get('target'),
            'display_exit_plan_net_rr':plan.get('net_reward_risk'),
            'exit_reason':first.get('exit_reason',first.get('reason')),
            'exit_signal_at':first.get('exit_signal_at'),
            'frozen_first_exit':{k:trigger.get(k) for k in ('requested_at','reason','initial_invalidation',
                'effective_stop','last_quote','invalidation','protection','fast_exit')},
            'coverage':{}})
    return sorted(result,key=lambda r:r['opened'])


def captured_lines(path,byte_limit):
    """Freeze the read extent. Never chase a growing LIVE log or repair it."""
    with path.open('rb') as stream:
        while stream.tell()<byte_limit:
            line=stream.readline(min(1024*1024,byte_limit-stream.tell()))
            if not line:break
            # A partially written/truncated record stays unknown, not repaired.
            if not line.endswith(b'\n'):
                yield None,len(line);continue
            try:row=json.loads(line)
            except (ValueError,UnicodeError):row=None
            yield row,len(line)


def inventory(state,capture_folder,dates,*,max_bytes=64*1024*1024,tail_seconds=120):
    if not 0<max_bytes<=64*1024*1024 or not 0<=tail_seconds<=120:raise ValueError('BOUND_REQUIRED')
    trades=grouped_trades(state,dates);by_symbol=defaultdict(list)
    for trade in trades:by_symbol[trade['symbol']].append(trade)
    files=[(p,p.stat().st_size) for p in sorted(Path(capture_folder).glob('trade-review-????????-????.jsonl'))
           if p.name[13:21] in {d.replace('-','') for d in dates}]
    if sum(size for _,size in files)>max_bytes:raise ValueError('INPUT_TOO_LARGE_USE_EXPLICIT_DAY_SUBSET')
    events=Counter();byte_counts=Counter();samples=Counter();sessions={};malformed=0
    sample_symbols=set();unique_bars=set();file_report=[];bar_delays=[]
    for path,size in files:
        before=path.stat();rows=0;digest=hashlib.sha256()
        # Hash only the frozen prefix, even if the producer appends later.
        with path.open('rb') as stream:
            remaining=size
            while remaining:
                chunk=stream.read(min(65536,remaining))
                if not chunk:raise ValueError('SOURCE_TRUNCATED_DURING_AUDIT')
                digest.update(chunk);remaining-=len(chunk)
        for row,length in captured_lines(path,size):
            rows+=1
            if not isinstance(row,dict):malformed+=1;continue
            kind=row.get('event','UNKNOWN');events[kind]+=1;byte_counts[kind]+=length
            known=known_at(row);session=row.get('capture_session')
            if session:
                item=sessions.setdefault(session,{'max_losses':0,'records':0})
                item['max_losses']=max(item['max_losses'],row.get('capture_losses',0));item['records']+=1
            if kind=='COMPLETED_BAR':
                unique_bars.add((row.get('symbol'),(row.get('bar') or {}).get('time')))
                delay=seconds(row.get('processed_at'),row.get('trigger_received_at') or row.get('confirmed_at'))
                if delay is not None:bar_delays.append(delay)
            if kind=='OPPORTUNITY_SAMPLE':
                samples['rows']+=1;sample_symbols.add((row.get('symbol'),str(row.get('asof'))[:10]))
                for trial in row.get('trials',[]):
                    samples['trials']+=1;samples['formed']+=bool(trial.get('formed'))
                    samples['ready']+=bool(trial.get('ready'));samples['rr_present']+=trial.get('rr') is not None
            for trade in by_symbol.get(row.get('symbol'),[]):
                if known is None:continue
                start=timestamp(trade['opened']);end=timestamp(trade['closed'])
                phase=('holding' if start<=known<=end else 'tail120' if end<known<=end+timedelta(seconds=tail_seconds)
                       else 'pre120' if start-timedelta(seconds=120)<=known<start else None)
                if phase:
                    cov=trade['coverage'].setdefault(phase,{'records':{},'max_losses':0,'full_qty_depth_rows':0,
                        'bar_starts':[],'evidence_quality':'OBSERVED_ROWS_NOT_A_COMPLETENESS_PROOF'})
                    cov['records'][kind]=cov['records'].get(kind,0)+1
                    cov['max_losses']=max(cov['max_losses'],row.get('capture_losses',0))
                    if kind=='COMPLETED_BAR':cov['bar_starts'].append((row.get('bar') or {}).get('time'))
                    if kind in {'BOOK','TRADE'} and row.get('depth_available') is True and (row.get('bid_qty') or 0)>=trade['qty']:
                        cov['full_qty_depth_rows']+=1
        after=path.stat()
        file_report.append({'file':path.name,'prefix_bytes':size,'prefix_sha256':digest.hexdigest(),'records':rows,
            'source_changed_while_reading':after.st_size!=before.st_size or after.st_mtime_ns!=before.st_mtime_ns})
    delays=sorted(bar_delays)
    return {'actual_orders':0,'profitability_proven':False,'independent_opportunity_count':None,
        'independent_opportunities_reason':'SAMPLED_EVENTS_HAVE_NO_FROZEN_EPISODE_ID',
        'dates':sorted(dates),'trades':trades,'independent_trades':len(trades),
        'sell_fill_rows':sum(t['sell_fill_rows'] for t in trades),'files':file_report,
        'event_counts':dict(events),'event_bytes':dict(byte_counts),'malformed_or_partial_rows':malformed,
        'unique_bar_labels':len(unique_bars),'sample_stats':{**samples,'symbol_days':len(sample_symbols)},
        'capture_sessions':sessions,'processing_delay_seconds':{
            'n':len(delays),'p50':delays[len(delays)//2] if delays else None,
            'p95':delays[int((len(delays)-1)*.95)] if delays else None,'max':max(delays,default=None)},
        'limits':['NO_FUTURE_PRICE_RECONSTRUCTION','NO_GUARANTEED_EXECUTION_OR_EXPECTANCY',
                  'CAPTURE_SEQUENCE_GAPS_ARE_NOT_PROOF_OF_LOSS_WATCH_RINGS_ARE_PARTIAL']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-snapshot',type=Path,required=True)
    parser.add_argument('--capture-folder',type=Path,required=True)
    parser.add_argument('--dates',nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve();capture=args.capture_folder.resolve();snapshot=args.state_snapshot.resolve()
    # Require an external output location: never write into the LIVE evidence
    # tree, its parent runtime, or over the frozen source snapshot.
    if output==snapshot or output==capture or output.is_relative_to(capture.parent):
        parser.error('OUTPUT_MUST_BE_OUTSIDE_RUNTIME')
    if output.exists():parser.error('OUTPUT_EXISTS_CHOOSE_NEW_PATH')
    raw=snapshot.read_bytes();state=json.loads(raw.decode('utf-8-sig'))
    report=inventory(state,capture,set(args.dates))
    report['state_snapshot_sha256']=hashlib.sha256(raw).hexdigest()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'report':str(output),'trades':report['independent_trades'],
                      'actual_orders':0,'profitability_proven':False},ensure_ascii=False))


if __name__=='__main__':main()
