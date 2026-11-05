"""Extract recorded trade evidence from explicitly supplied frozen files, offline.

Example: python -B tools/stream_trade_evidence.py --state SNAPSHOT.json
         --events FROZEN_EVENTS.jsonl --symbol 290690 --date 2026-11-05
This does not reconstruct missing ticks, execute orders, or estimate expectancy.
"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


def parse_time(value):
    try:
        t=datetime.fromisoformat(value)
        return t if t.tzinfo is not None else None
    except (TypeError,ValueError):return None


def analyze(state_path, event_paths, symbol, date, print_paths=()):
    state_path=Path(state_path)
    if state_path.stat().st_size>64*1024*1024:
        raise ValueError('STATE_SNAPSHOT_TOO_LARGE')
    before_state=state_path.stat();raw_state=state_path.read_bytes();after_state=state_path.stat()
    if (before_state.st_size,before_state.st_mtime_ns)!=(after_state.st_size,after_state.st_mtime_ns):raise ValueError('STATE_SOURCE_CHANGED_DURING_READ')
    state=json.loads(raw_state)
    trades=[]
    for t in state.get('trades',[]):
        if t.get('symbol')!=symbol or not str(t.get('opened','')).startswith(date):continue
        sig=t.get('signal') or {}; geo=(sig.get('fast_entry_evidence') or {}).get('geometry') or {}
        timing=sig.get('opportunity_timing') or {}
        trades.append({'trade_id':t.get('trade_id'),'symbol':symbol,'opened_recorded':t.get('opened'),
            'closed_recorded':t.get('closed'),'entry':t.get('entry'),'exit':t.get('exit'),'quantity':t.get('qty'),
            'strategy':t.get('pattern'),'exit_reason':t.get('reason'),'raw_first_at':timing.get('first_raw_at'),
            'watch_first_at':timing.get('first_watch_at'),'signal_at':sig.get('signal_detected_at') or sig.get('time'),
            'structural_stop':(sig.get('invalidation_policy') or {}).get('structural_stop'),
            'emergency_stop':sig.get('emergency'),'target':sig.get('target'),'net_rr_at_entry':sig.get('net_reward_risk'),
            'target_basis':geo.get('target_basis'),'entry_structure':geo.get('structural_evidence'),
            'exit_evidence':t.get('fast_exit_evidence'),'recorded_mfe':t.get('MFE'),'recorded_mae':t.get('MAE'),
            'gross_pnl':t.get('gross_pnl'),'net_pnl':t.get('net_pnl') if t.get('pnl_source')=='BROKER_QUERY' else None,
            'net_pnl_basis':t.get('pnl_source') or 'UNKNOWN','post_exit_path':'UNKNOWN_NO_CONTINUOUS_RECEIVED_TICKS',
            'coverage':'UNKNOWN_UNLESS_INDEPENDENTLY_PROVEN'})
    selected={'FILL','ENTRY_CANDIDATE','ENTRY_EXECUTION_RECHECK','WATCH_DYNAMIC_PROMOTION','EXIT_EVAL','SELL_ORDER','ORDER_ACCEPTED'}
    events=[];sources=[];events_omitted=0
    for path in map(Path,event_paths):
        before=path.stat();digest=hashlib.sha256();bad=0
        with path.open('rb') as f:
            for line in f:
                digest.update(line)
                if symbol.encode() not in line:continue
                try:row=json.loads(line)
                except (ValueError,UnicodeDecodeError):bad+=1;continue
                if row.get('symbol')!=symbol or not str(row.get('time','')).startswith(date) or row.get('event') not in selected:continue
                if len(events)<10000:
                    events.append({k:row.get(k) for k in ('time','event','symbol','side','qty','price','local','decision','primary_reason_code')})
                else:events_omitted+=1
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('EVENT_SOURCE_CHANGED_DURING_READ')
        sources.append({'name':path.name,'sha256':digest.hexdigest(),'bytes':before.st_size,'matching_malformed_lines':bad})
    # Only received TRADE records, with their receipt time, can contribute to
    # post-exit observations. Coverage and executable fills remain UNKNOWN.
    horizons=(60,180,300,600)
    for trade in trades:
        trade['post_exit_observed']={str(h):{'count':0,'min':None,'max':None,'last':None,
            'last_received_at':None,'first_received_at':None,'max_record_interval_seconds':None,
            'out_of_order':False,'coverage':'UNKNOWN'} for h in horizons}
    for path in map(Path,print_paths):
        before=path.stat();digest=hashlib.sha256();bad=0
        with path.open('rb') as f:
            for line in f:
                digest.update(line)
                if symbol.encode() not in line:continue
                try:row=json.loads(line)
                except (ValueError,UnicodeDecodeError):bad+=1;continue
                if row.get('event')!='TRADE' or row.get('symbol')!=symbol:continue
                received=parse_time(row.get('received_at') or row.get('receiver_at'));price=row.get('price')
                if not received or type(price) not in (int,float) or not 0<price<float('inf'):continue
                for trade in trades:
                    closed=parse_time(trade.get('closed_recorded'))
                    if closed is None:continue
                    age=(received-closed).total_seconds()
                    if age<0:continue
                    for horizon in horizons:
                        if age>horizon:continue
                        bucket=trade['post_exit_observed'][str(horizon)];bucket['count']+=1
                        bucket['min']=price if bucket['min'] is None else min(bucket['min'],price)
                        bucket['max']=price if bucket['max'] is None else max(bucket['max'],price)
                        previous=parse_time(bucket['last_received_at'])
                        if previous is not None and received<previous:bucket['out_of_order']=True
                        if bucket['first_received_at'] is None:bucket['first_received_at']=received.isoformat()
                        if previous is None or received>=previous:
                            interval=(received-(previous or closed)).total_seconds()
                            bucket['max_record_interval_seconds']=max(bucket['max_record_interval_seconds'] or 0,interval)
                            bucket['last']=price;bucket['last_received_at']=received.isoformat()
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('PRINT_SOURCE_CHANGED_DURING_READ')
        sources.append({'name':path.name,'kind':'RECEIVED_TRADE_CAPTURE','sha256':digest.hexdigest(),
                        'bytes':before.st_size,'matching_malformed_lines':bad})
    return {'symbol':symbol,'date':date,'state_sha256':hashlib.sha256(raw_state).hexdigest(),
            'sources':sources,'trades':trades,'recorded_events':events,'events_omitted':events_omitted,
            'independent_opportunities':'UNKNOWN','record_interval_interpretation':'NO_INFERENCE_OF_PACKET_LOSS_OR_NO_TRADES',
            'execution_and_expectancy':'UNKNOWN_WITHOUT_COMPLETE_RECEIPT_AND_QUOTE_COVERAGE'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',required=True);p.add_argument('--events',nargs='*',default=[])
    p.add_argument('--symbol',required=True);p.add_argument('--date',required=True)
    p.add_argument('--prints',nargs='*',default=[],help='Frozen capture JSONL with TRADE received_at/price fields')
    a=p.parse_args();print(json.dumps(analyze(a.state,a.events,a.symbol,a.date,a.prints),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
