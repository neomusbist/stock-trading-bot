"""Replay the bounded WATCH recorder without ticks that were never saved.

Run with explicit input files and an aware cutoff. No broker is constructed;
price-ready research episodes remain distinct from orders or completed trades.
"""
from collections import defaultdict,deque
from datetime import datetime,timedelta
import argparse,json
from pathlib import Path

from automation.capital import Costs
from automation.fast_momentum import FastMomentumStrategy
from automation.opportunity_audit import at,audit_records
from automation.price_context import PriceContext
from automation.reclaim_research import reclaim_trial
from core.strategy import Tick


def captured_at(row):
    if row.get('event')=='COMPLETED_BAR':
        return at({'asof':row.get('processed_at')})
    return at(row) or at({'asof':row.get('received_at') or row.get('at')})


def replay_capture(records,cfg,auto,cutoff):
    if not isinstance(cutoff,datetime) or cutoff.tzinfo is None:raise ValueError('AWARE_CUTOFF_REQUIRED')
    selected=[];future=0;unknown=0
    for row in records:
        stamp=captured_at(row)
        if stamp is None:unknown+=1;continue
        if stamp>cutoff:future+=1;continue
        if stamp.date()==cutoff.date():selected.append((stamp,row))
    selected.sort(key=lambda pair:pair[0])
    bars=defaultdict(lambda:deque(maxlen=120));context=PriceContext(bar_limit=120)
    costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
    quotes=FastMomentumStrategy(cfg,auto,costs);episodes={};rejects=defaultdict(int);samples=[];bar_count=0
    capture_losses={};resets=0
    for stamp,row in selected:
        symbol=row.get('symbol');kind=row.get('event')
        session=row.get('capture_session');losses=row.get('capture_losses')
        lost=isinstance(losses,int) and losses>capture_losses.get(session,0)
        if isinstance(losses,int):capture_losses[session]=losses
        if (kind=='CAPTURE_STARTED' or lost or
                (kind=='STREAM_STATE' and row.get('kind') in {'DISCONNECTED','ERROR','QUEUE_GAP'})):
            bars.clear();context=PriceContext(bar_limit=120);resets+=1
            if kind in {'CAPTURE_STARTED','STREAM_STATE'}:continue
        if kind=='SUBSCRIPTION_SCOPE':
            for removed in row.get('removed',[]):bars.pop(removed,None)
            continue
        if not symbol:continue
        if kind=='COMPLETED_BAR':
            if row.get('source')!='ACTUAL_RECEIVED_DETECTOR_HISTORY' or row.get('reference_bars_used') is not False:continue
            bar=dict(row.get('bar') or {});start=at({'time':bar.get('time')})
            if start is None or start+timedelta(minutes=1)>stamp:continue
            bar['time']=start
            if not bars[symbol] or bars[symbol][-1]['time']!=start:bars[symbol].append(bar);bar_count+=1
            context.record(symbol,bar,stamp,cfg.stale_seconds)
            continue
        if kind!='OPPORTUNITY_SAMPLE':continue
        samples.append(row)
        exchange=at({'asof':row.get('exchange_at')});book=at({'asof':row.get('book_time')})
        if exchange is None or any(type(row.get(k)) not in (int,float) or row[k]<=0 for k in ('price','bid','ask')):
            rejects['RECLAIM_EXECUTABLE_PRICE_OR_TIME_UNKNOWN']+=1;continue
        tick=Tick(symbol,exchange,row.get('price'),
            row.get('volume'),row.get('cumulative'),row.get('ask'),row.get('bid'),row.get('regular') is True,
            ask_qty=row.get('ask_qty'),bid_qty=row.get('bid_qty'),depth_available=row.get('depth_available'),
            book_time=book,
            received_at=row.get('received_at'))
        watch={**row.get('watch',{}),'market_status':row.get('market_status',{})}
        price_context=context.snapshot(symbol,tick,tick.ask)
        signal,trial=reclaim_trial(tick,list(bars[symbol]),watch,cfg,auto,costs,stamp,
            row.get('gates',{}),quote_reason=quotes._quote(tick,stamp),context=price_context)
        rejects[trial['primary_reason_code']]+=1
        if signal is not None:
            key=(symbol,tuple(signal['structural_evidence']['times']))
            episodes.setdefault(key,signal)
    return {'cutoff':cutoff.isoformat(),'baseline_observed':audit_records(samples,cutoff),
        'completed_bars_observed':bar_count,'sampled_evaluations':len(samples),
        'reclaim_price_ready_episodes':len(episodes),'reclaim_episodes':list(episodes.values()),
        'reclaim_primary_rejection_samples':dict(rejects),'future_records_excluded':future,
        'unknown_time_records':unknown,'actual_trades':None,'net_pnl':None,'MFE':None,'MAE':None,
        'evidence_reset_events':resets,
        'orders_enabled':False,'profitability_proven':False,
        'coverage':'SAMPLED_WATCH_ONLY; MISSING_TICKS_QUANTITY_AND_FILLS_UNKNOWN',
        'counter_trace':'ORIGINAL_MONOTONIC_AND_CLOCK_CORRECTION_TRACE_NOT_REPLAYED'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,action='append',required=True)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--automation',type=Path,required=True)
    p.add_argument('--cutoff',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--max-input-bytes',type=int,default=32*1024*1024)
    args=p.parse_args(argv)
    if args.max_input_bytes<=0 or sum(path.stat().st_size for path in args.input)>args.max_input_bytes:
        p.error('EXPLICIT_INPUT_BYTE_LIMIT_EXCEEDED')
    from core.settings import load_config
    from automation.policy import load_automation
    records=[]
    for path in args.input:
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                if line.strip():records.append(json.loads(line))
    result=replay_capture(records,load_config(args.config),load_automation(args.automation),datetime.fromisoformat(args.cutoff))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'reclaim_episodes','baseline_observed'}},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
