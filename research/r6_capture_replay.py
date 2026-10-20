"""Offline before/after component replay. Inputs never grant order permission.

Run this very file with --source-root pointing at each frozen source tree.
Actual tape components and synthetic causal counterexamples are reported apart.
It intentionally refuses to invent missing WATCH ticks, books or fill prices.
"""
import argparse
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--tape',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-actual',type=Path,help='Reuse the identical archived component result for the same source; scenarios still rerun')
    args=parser.parse_args();sys.path.insert(0,str(args.source_root))
    from automation.capital import Costs
    from automation.fast_momentum import FastMomentumStrategy,MicroTape
    from automation.patterns import PatternDetector
    from automation.policy import AutomationConfig
    from automation.radar import RadarWorker
    from automation.runner import SubscriptionState
    from core.settings import Config,KST
    from core.strategy import Tick
    cfg=Config();auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,
        max_scan_charts=20,structural_breakout_enabled=True)
    costs=Costs(.00015,.00015,.002,.002,.003)
    at=datetime(2026,10,20,11,tzinfo=KST);sym='999990'
    synthetic={}
    d=PatternDetector(cfg,auto,costs);d.continuous_observation=True
    cumulative=0
    for second in range(0,186,5):
        cumulative+=100;t=Tick(sym,at+timedelta(seconds=second),1000,100,cumulative,1001,1000)
        d.ingest(t,t.time)
    e=SimpleNamespace(event=lambda *a,**k:None,connected=True,account_ok=True)
    feed=SimpleNamespace(update_symbols=lambda *a:None,transport_failed=False)
    state=SubscriptionState([sym]);gap=at+timedelta(seconds=186)
    state.refresh(e,d,feed,[],gap,grace_seconds=360)
    state.refresh(e,d,feed,[sym],gap+timedelta(seconds=2),grace_seconds=360)
    cumulative+=100;t=Tick(sym,gap+timedelta(seconds=2),1000,100,cumulative,1001,1000)
    d.ingest(t,t.time)
    synthetic['subscription_gap']={'stored_completed_bars':len(d.history[sym]),
        'usable_bars_after_known_gap':len(d.usable_bars(sym)),
        'rolling_window_ticks':len(d.buffers.get(sym,())),
        'warmup_gate':d.base_gates(t,{'status':'WATCH'})['warmup']['result']}
    d=PatternDetector(cfg,auto,costs)
    bars=[{'time':at+timedelta(minutes=i),'open':1000,'high':1010,'low':995,'close':1001,'volume':100} for i in range(3)]
    d.price_context.merge_history(sym,bars,at+timedelta(minutes=3),'KRX')
    synthetic['rest_reference']={'reference_bars':len(d.price_context.reference_bars(sym,at+timedelta(minutes=3),'KRX')),
        'loaded_as_signal_bars':d.preload_from_context(sym,at+timedelta(minutes=3))}
    import queue
    from automation.runner import prewarm_handoff
    d=PatternDetector(cfg,auto,costs);d.continuous_observation=True
    t=Tick(sym,at,1000,100,100,1001,1000);d.ingest(t,at)
    queued=Tick(sym,at+timedelta(seconds=1),1001,100,200,1001,1000)
    feed=SimpleNamespace(observation_symbols=frozenset({sym}),observation_drops=0,
        observation_q=queue.Queue(),q=queue.Queue(),overflow=SimpleNamespace(is_set=lambda:False),
        transport_failed=False,subscription_lock=None,configure_observation=lambda *a:None)
    feed.observation_q.put((queued.time,queued))
    engine=SimpleNamespace(s={'positions':{},'automation':{'existing':{}}},active_orders=lambda:[],event=lambda *a,**k:None)
    prewarm_handoff(engine,d,feed,set(),{sym},set(),0)
    synthetic['actual_caller_promotion_scope']={'queued_cumulative':200,
        'detector_cumulative_after_promotion':d.last[sym].cumulative,
        'lost_queued_trade':d.last[sym].cumulative!=200}
    with tempfile.TemporaryDirectory() as tmp:
        worker=RadarWorker(cfg,auto,SimpleNamespace(locked=lambda s:False),SimpleNamespace(),Path(tmp))
        worker.radar.available_capital=71000
        def row(s,hot=False):
            return {'code':s,'source_details':{'a':{'observed_rank':1,'values':{'flu_rt':1}}},
                'reference_price':1000,'discovery_context':{'last_cycle_return':.01 if hot else 0}}
        old=[row(f'{i:06d}') for i in range(31,39)]
        with patch('automation.radar.now',return_value=at):worker.scheduled_items(old)
        active=[row(f'{i:06d}') for i in range(101,113)]
        hot=[row(f'{i:06d}',True) for i in range(201,241)]
        worker.status_priority_symbols=frozenset(r['code'] for r in active)
        with patch('automation.radar.now',return_value=at):selected=worker.scheduled_items(active+hot+old)[:20]
        synthetic['scan_budget']={'status_budget':20,'actual_watch_first':len([r for r in selected[:12] if r['code'] in worker.status_priority_symbols]),
            'older_waiting_served':len({r['code'] for r in selected}&{r['code'] for r in old}),
            'selected_codes':[r['code'] for r in selected]}
    kw={'ask_qty':10000,'bid_qty':10000,'depth_available':True,'book_time':at,
        'received_at':at.isoformat(),'received_monotonic':103.}
    if 'book_received_at' in Tick.__dataclass_fields__:
        kw.update(book_received_at=at.isoformat(),book_received_monotonic=90.)
    t=Tick(sym,at,1000,100,100,1001,1000,**kw)
    fast=FastMomentumStrategy(cfg,auto,costs)
    with patch('core.market_time.time.monotonic',return_value=103.):reason=fast._quote(t,at)
    synthetic['book_clock_correction']={'old_book_elapsed_seconds':13,'new_trade_elapsed_seconds':0,'quote_reject_reason':reason}

    if args.tape.stat().st_size>128*1024*1024:raise ValueError('BOUNDED_TAPE_EXCEEDED')
    tape_by_symbol={}
    actual={'basis':'SAVED_SHADOW_TRADE_COMPONENTS_NOT_FULL_ORDER_REPLAY','rows':0,'symbols':set(),
            'accepted':0,'duplicates_or_invalid':Counter(),'quote_results':Counter(),'depth_sources':Counter(),
            'volume_larger_than_cumulative_delta':0,'positive_unobserved_cumulative_delta':0,
            'full_order_signals':None,'PRE_HTTP':None,'fills':None,'net_pnl':None,'MFE':None,'MAE':None}
    started=time.perf_counter()
    if args.reuse_actual:
        prior=json.loads(args.reuse_actual.read_text(encoding='utf-8'))
        if prior['source_root']!=str(args.source_root):raise ValueError('REPLAY_SOURCE_ROOT_MISMATCH')
        actual=prior['actual_tape_components']
        if actual['input_sha256']!=hashlib.sha256(args.tape.read_bytes()).hexdigest():raise ValueError('REPLAY_INPUT_CHANGED')
    for line in (() if args.reuse_actual else args.tape.open(encoding='utf-8-sig')):
        r=json.loads(line)
        if r.get('event')!='TRADE':continue
        received=datetime.fromisoformat(r['received']);stamp=datetime.fromisoformat(r['time'])
        kw={k:r[k] for k in ('ask_qty','bid_qty','depth_available','depth_source','regular','venue','vi_active') if k in r}
        kw['book_time']=datetime.fromisoformat(r['book_time']) if r.get('book_time') else None
        kw['received_at']=received.isoformat()
        # Old tapes did not retain receipt counters for books; do not fabricate
        # those clocks or compare archived counters to this replay process.
        t=Tick(r['symbol'],stamp,r['price'],r['volume'],r['cumulative'],r['ask'],r['bid'],**kw)
        tape=tape_by_symbol.setdefault(t.symbol,MicroTape())
        previous=tape.last_cumulative
        accepted=tape.ingest(t,received)
        actual['rows']+=1;actual['symbols'].add(t.symbol);actual['depth_sources'][r.get('depth_source')]+=1
        actual['accepted']+=accepted
        if not accepted:actual['duplicates_or_invalid'][tape.last_rejection]+=1
        if accepted and previous is not None:
            delta=t.cumulative-previous
            actual['volume_larger_than_cumulative_delta']+=t.volume>delta
            actual['positive_unobserved_cumulative_delta']+=delta>t.volume
        processed=datetime.fromisoformat(r.get('processed_at') or r['received'])
        reason=fast._quote(t,processed)
        actual['quote_results'][reason or 'EXECUTABLE_AT_RECORDED_PROCESSING_TIME']+=1
    if not args.reuse_actual:
        actual['symbols']=len(actual['symbols']);actual['seconds']=round(time.perf_counter()-started,3)
    actual['input_sha256']=hashlib.sha256(args.tape.read_bytes()).hexdigest()
    actual['not_proven']=['Tape is a subscribed SHADOW subset, not all discovered or WATCH trades.',
        'BOOK event stream, original subscription epochs and book receipt counters are missing.',
        'No synthetic target, account, fill or future execution quote was supplied to close these gaps.']
    result={'source_root':str(args.source_root),'causal_synthetic':synthetic,'actual_tape_components':actual,
            'real_orders':0,'live_modified':False}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'synthetic':synthetic,'actual_rows':actual['rows'],'accepted':actual['accepted'],
                      'quote_results':actual['quote_results'],'seconds':actual['seconds']},ensure_ascii=False))


if __name__=='__main__':main()
