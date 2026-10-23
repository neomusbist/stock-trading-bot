"""Bounded, offline reuse of A/B detectors on a verified session freeze.

No broker transport, operating-root writes, new strategy or LIVE approval.
SQLite sorts actual availability on disk; four variants run sequentially.
"""
import argparse
from contextlib import closing
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import time
from unittest.mock import patch

from core.market_evidence import aware
from research.causal_data import file_sha, frozen_lines, Frame, json_values

VARIANTS = {'baseline': {}, 'a_retest': {'a_breakout_retest_entry_enabled': True},
            'b_reclaim': {'b_support_reclaim_entry_enabled': True},
            'ab_rework': {'a_breakout_retest_entry_enabled': True, 'b_support_reclaim_entry_enabled': True}}

def verify_freeze(folder):
    root=Path(folder).resolve();manifest=json.loads((root/'MANIFEST.json').read_bytes())
    records=manifest['files'];seen=set()
    for record in records:
        rel=record['relative'];path=(root/rel).resolve()
        if root not in path.parents or rel in seen:raise ValueError('UNSAFE_OR_DUPLICATE_MANIFEST_PATH')
        seen.add(rel)
        if path.stat().st_size!=record['byte_end'] or file_sha(path)!=record['sha256']:
            raise ValueError('FROZEN_INPUT_CHANGED: '+rel)
        end=record.get('complete_jsonl_byte_end')
        if end is not None:
            if not 0<=end<=record['byte_end']:raise ValueError('INVALID_COMPLETE_LINE_EXTENT')
            if end:
                with path.open('rb') as stream:
                    stream.seek(end-1)
                    if stream.read(1)!=b'\n':raise ValueError('INVALID_COMPLETE_LINE_BOUNDARY')
    return manifest

def known_at(row):
    values=[aware(row.get(k)) for k in ('processed_at','asof','known_at','received_at','received','at','confirmed_at')]
    if row.get('event') not in {'TRADE','TICK','BOOK','COMPLETED_BAR'}:
        values.append(aware(row.get('time')))  # Journal processing time, never a trade exchange time.
    return max((v for v in values if v is not None),default=None)

def index_freeze(folder,db,day,session,max_records=3000000):
    root=Path(folder);manifest=verify_freeze(root);counts=Counter();ordinal=0
    db.execute('CREATE TABLE events (known TEXT, ordinal INTEGER, source TEXT, payload TEXT)')
    low,high=('09:00:00','15:30:00') if session=='REGULAR' else ('15:40:00','20:00:00')
    accepted={'TRADE','BOOK','WATCH_METADATA','OPPORTUNITY_SAMPLE','SUBSCRIPTION_SCOPE',
              'STREAM_STATE','COMPLETED_BAR','REFERENCE_BARS_PART','CAPTURE_STARTED','CAPTURE_STATUS','RAW_CANDIDATES','ENTRY_QUOTE_RETRY'}
    for record in manifest['files']:
        if record.get('complete_jsonl_byte_end') is None:continue
        path=root/record['relative'];offset=0
        if record['byte_end']!=record['complete_jsonl_byte_end']:counts['INCOMPLETE_FILE_TAILS']+=1
        for line in frozen_lines(path,record['complete_jsonl_byte_end']):
            start=offset;offset+=len(line)
            if len(line)>262144:counts['OVERSIZE_RECORDS_UNKNOWN']+=1;continue
            try:row=json.loads(line)
            except ValueError:counts['MALFORMED_RECORDS_UNKNOWN']+=1;continue
            event=row.get('event')
            # Radar journals contain actual recognition snapshots, not ticks.
            if path.name.startswith('radar_history'):
                row={'event':'RAW_SNAPSHOT','at':row.get('time'),
                     'rows':[{'code':x.get('code'),'observed_at':x.get('observed_at'),
                              'first_raw_at':x.get('first_raw_at')} for x in row.get('rows',[])]}
                event=row['event']
            if event not in accepted|{'RAW_SNAPSHOT'}:continue
            at=known_at(row)
            if at is None:counts['AVAILABILITY_UNKNOWN']+=1;continue
            if at.date().isoformat()!=day or not low<=at.strftime('%H:%M:%S')<high:continue
            ordinal+=1
            if ordinal>max_records:raise ValueError('OFFLINE_RECORD_BUDGET_EXCEEDED_NO_COMPLETE_RESULT')
            if ordinal%10000==0 and shutil.disk_usage(root).free<32*1024**3:
                raise ValueError('OFFLINE_32GiB_RESERVE_REQUIRED')
            db.execute('INSERT INTO events VALUES (?,?,?,?)',(at.isoformat(),ordinal,
                record['relative']+':'+str(start),json.dumps(row,ensure_ascii=False)))
            counts[event]+=1
    db.execute('CREATE INDEX ordered_events ON events(known,ordinal)')
    db.execute("CREATE INDEX book_receipts ON events(json_extract(payload,'$.symbol'),json_extract(payload,'$.received_at')) WHERE json_extract(payload,'$.event')='BOOK'")
    db.commit()
    return manifest,dict(counts)

def paper_from_preopen(folder,session_manifest,cfg,auto):
    """Actual preopen state seeds a hypothetical engine, never LIVE authority."""
    if folder is None:return None,'UNKNOWN_NO_PREOPEN_STATE'
    root=Path(folder);m=verify_freeze(root)
    if m.get('snapshot_kind')!='PREOPEN_STATE_NOT_AN_ACCOUNT_OR_ORDER_APPROVAL':
        raise ValueError('PREOPEN_MANIFEST_REQUIRED_NO_AFTER_MARKET_BACKFILL')
    if m['day']!=session_manifest['day']:raise ValueError('PREOPEN_DAY_MISMATCH')
    stamp=aware(m.get('requested_preopen_at'))
    if not stamp or not '08:00:00'<=stamp.strftime('%H:%M:%S')<'09:00:00':raise ValueError('PREOPEN_CLOCK_REQUIRED')
    source={r['relative']:r['sha256'] for r in session_manifest['files']}
    for rel in ('config.json','automation.json'):
        if source.get(rel)!=file_sha(root/rel):raise ValueError('CONFIG_CHANGED_WITHIN_SESSION')
    ledger=json.loads((root/'runtime/automation-production/state.json').read_bytes())
    active=[o for o in ledger.get('orders',{}).values() if o.get('status') not in {'FILLED','CANCELED','REJECTED'}]
    if ledger.get('positions') or active or ledger.get('halt'):
        return None,'UNKNOWN_NONFLAT_PREOPEN_REQUIRES_ACCOUNT_EVENT_RECONSTRUCTION'
    if ledger.get('day')!=m['day']:return None,'UNKNOWN_PREOPEN_LEDGER_NOT_ROLLED_TO_TARGET_DAY'
    capital=ledger.get('automation',{}).get('capital_snapshot',{})
    cash=capital.get('actual_orderable_cash')
    if type(cash) not in (int,float) or cash<0:return None,'UNKNOWN_ORDERABLE_CASH'
    from research.causal_account import PaperAccount
    manual=ledger.get('automation',{}).get('existing',{})
    if not isinstance(manual,dict) or any(not isinstance(h,dict)
            or type(h.get('qty')) is not int or type(h.get('available')) is not int
            or not 0<=h['available']<=h['qty'] or type(h.get('avg')) not in (int,float)
            or h['qty']>0 and h['avg']<=0 for h in manual.values()):
        return None,'UNKNOWN_MANUAL_HOLDING_FIELDS'
    account=PaperAccount(stamp.replace(hour=9,minute=0,second=0),
        initial_cash=min(cash,ledger['cash'],auto.capital_limit),cfg=cfg,auto=auto,manual=manual)
    # The position cap/daily loss/entry counters are retained; no fresh-day reset.
    for key in ('daily_loss','entries','consecutive_losses'):
        if key in ledger:account.e.s[key]=ledger[key]
    return account,'PREOPEN_SEEDED_PAPER_SCENARIO_NOT_RECORDED_BROKER_OR_APPROVAL'

def replay_variant(db,cfg,auto,variant,output,manifest,preopen=None,evaluation_mode='samples'):
    from automation.patterns import PatternDetector
    from automation.capital import Costs
    from core.realtime import current_book_tick
    auto=replace(auto,**VARIANTS[variant])
    if auto.fast_momentum_enabled or auto.pullback_expansion_target_enabled:
        raise ValueError('FAST_AND_B_EXPANSION_MUST_REMAIN_OFF')
    detector=PatternDetector(cfg,auto,Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,
        cfg.entry_slippage_fraction,cfg.exit_slippage_fraction));detector.continuous_observation=True
    account,basis=paper_from_preopen(preopen,manifest,cfg,auto)
    counts=Counter();gaps=Counter();watch={};raw_first={};watch_first={};last_frames={};books={};parts={}
    last_loss={};last_sequence={};sessions=set();fingerprints=set();producer_bars={};state_keys={};signal_keys=set();started=time.perf_counter();cpu=time.process_time()
    def reset(at,reason):
        gaps[reason]+=1
        for sym in list(detector.last):detector.mark_coverage_gap(sym,at,reason)
    try:
        with (output/(variant+'.jsonl')).open('w',encoding='utf-8') as decisions:
            for stamp,source,payload in db.execute('SELECT known,source,payload FROM events ORDER BY known,ordinal'):
                row=json.loads(payload);at=aware(stamp);event=row['event'];symbol=row.get('symbol')
                session=row.get('capture_session');seq=row.get('capture_sequence');loss=row.get('capture_losses',0)
                if session:
                    if session not in sessions:
                        if sessions:reset(at,'RECORDER_RESTART_UNKNOWN_GAP')
                        sessions.add(session)
                    # Subset capture lines can skip IDs by design. Report sequence
                    # gaps, but only explicit loss counters invalidate continuity.
                    if type(seq) is int and session in last_sequence and seq>last_sequence[session]+1:
                        gaps['CAPTURE_SEQUENCE_DISCONTINUITY_REQUIRES_SCOPE_AUDIT']+=1
                    if type(seq) is int:last_sequence[session]=seq
                    if loss>last_loss.get(session,0):reset(at,'RECORDED_CAPTURE_LOSS')
                    last_loss[session]=max(loss,last_loss.get(session,0))
                if event=='RAW_SNAPSHOT':
                    counts['radar_snapshots_not_first_RAW_proof']+=1
                if event=='RAW_CANDIDATES':
                    for x in row.get('candidates',[]):
                        raw_first.setdefault(x['code'],{'recognized_at':stamp,'source_observed_at':row.get('observed_at'),
                            'source':source,'cached_first_raw_at_not_receipt_proof':x.get('first_raw_at')})
                if event=='STREAM_STATE':
                    kind=str(row.get('kind'))
                    if kind in {'DISCONNECTED','RECONNECTING','TRANSPORT_FAILURE','ERROR'}:reset(at,'STREAM_'+kind)
                    else:counts['stream_control_'+kind]+=1  # REG/LOGIN ACK is not a disconnect.
                if event=='SUBSCRIPTION_SCOPE':
                    for sym in row.get('removed',[]):detector.mark_coverage_gap(sym,at,'ACTUAL_UNSUBSCRIBE');counts['unsubscribe']+=1
                if event=='WATCH_METADATA':
                    if row.get('active'):
                        watch[symbol]=dict(row['watch']);watch_first.setdefault(symbol,stamp)
                    else:watch.pop(symbol,None)
                    counts['watch_role_changes']+=1  # Rank removal alone is not an unsubscribe.
                if event in {'WATCH_METADATA','SUBSCRIPTION_SCOPE','STREAM_STATE'}:
                    affected=([symbol] if symbol else row.get('removed',[]) or list(detector.last))
                    for sym in affected:
                        decisions.write(json.dumps(json_values({'event':'CONTINUITY_STATE','source_id':source,
                            'at':stamp,'symbol':sym,'cause':event,'watch_active':sym in watch,
                            'stored_bars':len(detector.history.get(sym,())),
                            'usable_bars':len(detector.usable_bars(sym)),
                            'observation_start':detector.observation_start.get(sym),
                            'coverage_gap':detector.coverage_gaps.get(sym)}),ensure_ascii=False)+'\n')
                if event=='REFERENCE_BARS_PART':
                    key=(symbol,row['reference_sha256'],stamp);p=parts.setdefault(key,{})
                    p[row['part']]=row['rows']
                    if len(p)==row['parts']:
                        bars=[b for i in range(row['parts']) for b in p[i]]
                        raw=[[b['time']]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in bars]
                        if hashlib.sha256(json.dumps(raw,separators=(',',':')).encode()).hexdigest()!=row['reference_sha256']:
                            raise ValueError('REFERENCE_HASH_MISMATCH')
                        detector.price_context.merge_history(symbol,[{**b,'time':aware(b['time'])} for b in bars],at,row.get('venue','KRX'))
                        counts['REST_reference_snapshots']+=1;parts.pop(key)
                if event=='COMPLETED_BAR':
                    b={**row['bar'],'time':aware(row['bar']['time'])}
                    if row.get('source')!='ACTUAL_RECEIVED_DETECTOR_HISTORY' or not b['time'] or b['time']+timedelta(minutes=1)>at:
                        gaps['UNVERIFIED_OR_FUTURE_BAR']+=1;continue
                    history={x['time']:x for x in detector.history.get(symbol,())};history[b['time']]=b
                    detector.history[symbol]=deque(sorted(history.values(),key=lambda x:x['time'])[-30:],maxlen=30)
                    producer_bars.setdefault(symbol,{})[b['time']]=b
                    producer_bars[symbol]={k:v for k,v in producer_bars[symbol].items() if k in {x['time'] for x in detector.history[symbol]}}
                    counts['received_completed_bars']+=1
                if event=='BOOK':
                    books[symbol]={'time':row.get('exchange_at'),'received_at':row.get('received_at'),
                        'book_receipt_id':row.get('book_receipt_id'),
                        **{k:row.get(k) for k in ('best_bid','best_ask','bid_qty','ask_qty','depth_available')}}
                    if not row.get('processed_at'):gaps['BOOK_STRATEGY_AVAILABILITY_UNKNOWN']+=1
                eligible=(event=='OPPORTUNITY_SAMPLE' if evaluation_mode=='samples' else
                          event=='TRADE' and row.get('capture_point')=='STRATEGY_INGESTION')
                if event=='ENTRY_QUOTE_RETRY' and evaluation_mode=='received':
                    prior=last_frames.get(symbol)
                    if not prior or prior.tick.received_at!=row.get('trade_received_at'):
                        gaps['RETRY_TRADE_PAIR_UNPROVEN']+=1;continue
                    found=db.execute("SELECT payload FROM events WHERE json_extract(payload,'$.event')='BOOK' AND json_extract(payload,'$.symbol')=? AND json_extract(payload,'$.received_at')=? AND known<=? ORDER BY known DESC LIMIT 1",
                        (symbol,row.get('book_received_at'),stamp)).fetchone()
                    if not found:gaps['RETRY_BOOK_PAIR_UNPROVEN']+=1;continue
                    actual=json.loads(found[0]);books[symbol]={'time':actual.get('exchange_at'),
                        'received_at':actual['received_at'],'book_receipt_id':actual.get('book_receipt_id'),
                        **{k:actual.get(k) for k in ('best_bid','best_ask','bid_qty','ask_qty','depth_available')}}
                    eligible=True;counts['actual_quote_retry_processing_joined']+=1
                    row={**row,'receipt_evidence':{'trade_received_at':prior.tick.received_at,
                        'book_receipt_id':actual.get('book_receipt_id'),
                        'basis':'RECORDED_RETRY_PROCESSING_TIME_AND_MATCHED_RECEIVER_BOOK'}}
                if event=='TRADE' and row.get('capture_point')=='STRATEGY_INGESTION' or event=='OPPORTUNITY_SAMPLE':
                    try:frame=Frame.from_record({**row,'event':'TRADE'},source)
                    except (ValueError,KeyError,TypeError):gaps['FRAME_AVAILABILITY_UNKNOWN']+=1;continue
                    # Synthetic fixtures retain their label all the way to PAPER fills.
                    if row.get('input_evidence_basis')=='SYNTHETIC_TEST_QUOTE':frame=replace(frame,basis='SYNTHETIC_TEST_QUOTE')
                    if event=='TRADE':
                        if detector.ingest(frame.tick,frame.received_at):counts['accepted_ingestions']+=1
                        else:
                            counts['duplicate_or_invalid_ingestions']+=1;eligible=False
                            old=detector.last.get(symbol);gap=detector.coverage_gaps.get(symbol)
                            reason=('AT_OR_BEFORE_REAL_GAP' if gap and frame.received_at<=gap['at'] else
                                    'DUPLICATE_OR_REVERSED' if old and (frame.tick.time<old.time or frame.tick.cumulative<=old.cumulative)
                                    else 'INVALID_SESSION_OR_STALE_TRADE')
                            counts['ingest_rejected_'+reason]+=1
                        history={b['time']:b for b in detector.history.get(symbol,())}
                        history.update({k:v for k,v in producer_bars.get(symbol,{}).items()
                                        if k<frame.tick.time.replace(second=0,microsecond=0)})
                        if history:detector.history[symbol]=deque(sorted(history.values(),key=lambda x:x['time'])[-30:],maxlen=30)
                    last_frames[symbol]=frame
                    if account and (account.e.s['positions'] or account.e.active_orders()):account.step(frame)
                if not eligible:continue
                if event=='OPPORTUNITY_SAMPLE':
                    current=dict(row.get('watch') or {});current['market_status']=row.get('market_status',{})
                    # Producer-known warmup only; never infer from REST or future snapshots.
                    start=aware(row.get('gates',{}).get('warmup',{}).get('values',{}).get('observation_start'))
                    if start and start<=at:detector.observation_start[symbol]=start
                else:current=watch.get(symbol)
                if not current:counts['qualification_unknown_no_evaluation']+=1;continue
                frame=last_frames[symbol];tick=current_book_tick(frame.tick,books.get(symbol),at,cfg.stale_seconds)
                frame=replace(frame,tick=tick);current={**current,'code':symbol}
                signal=detector.signal(tick,current);ev=detector.last_eval.get(symbol,{})
                counts['evaluations']+=1
                stored=len(detector.history.get(symbol,()));usable=len(detector.usable_bars(symbol))
                observation=detector.observation_start.get(symbol);gap=detector.coverage_gaps.get(symbol)
                warmup=ev.get('gates',{}).get('warmup',{})
                state_key=(stored,usable,observation,str(gap),warmup.get('result'))
                if state_keys.get(symbol)!=state_key:
                    state_keys[symbol]=state_key
                    decisions.write(json.dumps(json_values({'event':'EVALUATION_STATE','source_id':source,
                        'at':stamp,'symbol':symbol,'stored_bars':stored,'usable_bars':usable,
                        'observation_start':observation,'coverage_gap':gap,'warmup':warmup,
                        'raw_first_observed':raw_first.get(symbol,'UNKNOWN'),
                        'watch_first_observed':watch_first.get(symbol,'UNKNOWN'),
                        'receipt_evidence':row.get('receipt_evidence','UNKNOWN')}),ensure_ascii=False)+'\n')
                trials=[x for x in ev.get('trials',[]) if x.get('setup') in ('A','B')]
                for trial in trials:
                    counts[trial['setup']+'_evaluated']+=1
                    if trial.get('entry_ready'):counts[trial['setup']+'_ready_evaluation_events']+=1
                    if not trial.get('candidate_generated'):continue
                    key=hashlib.sha256(json.dumps([symbol,trial['setup'],trial.get('setup_geometry'),
                        trial.get('gates',{}).get('risk_reward',{}).get('values',{}).get('target')],sort_keys=True,default=str).encode()).hexdigest()
                    if key in fingerprints:continue
                    if len(fingerprints)>=200000:raise ValueError('STRUCTURE_BUDGET_EXCEEDED_NO_COMPLETE_RESULT')
                    fingerprints.add(key);counts[trial['setup']+'_structure_fingerprints']+=1
                    decisions.write(json.dumps(json_values({'source_id':source,'at':stamp,'symbol':symbol,
                        'setup':trial['setup'],'raw_first_observed':raw_first.get(symbol,'UNKNOWN'),
                        'watch_first_observed':watch_first.get(symbol,'UNKNOWN'),
                        'receipt_evidence':row.get('receipt_evidence','UNKNOWN'),
                        'known_completed_bars':len(detector.history.get(symbol,())),
                        'usable_completed_bars':len(detector.usable_bars(symbol)),
                        'observation_start':detector.observation_start.get(symbol),
                        'coverage_gap':detector.coverage_gaps.get(symbol),'trial':trial,
                        'quote':{'ask':tick.ask,'book_time':tick.book_time,'book_received_at':tick.book_received_at},
                        'exact_live_first_onset':False,'evaluation_basis':evaluation_mode,
                        'actual_order_permission':False}),ensure_ascii=False)+'\n')
                if signal is None:continue
                if signal.get('pattern') not in {'A','B'}:counts['non_AB_selected_not_ordered']+=1;continue
                counts['selected_signal_events']+=1
                signal_key=tuple(signal.get(k) for k in ('symbol','pattern','breakout','support','stop','target','entry_basis'))
                if signal_key in signal_keys:counts['repeated_signal_events']+=1
                else:signal_keys.add(signal_key);counts['unique_signal_fingerprints']+=1
                if account is None:counts['quantity_and_PAPER_unknown_without_preopen']+=1;continue
                signal['source_evidence']={'timestamp':stamp,'source_count':current.get('source_count'),
                    'fallback_source1':current.get('fallback_source1',False)}
                account.step(frame,market_status=current.get('market_status'))
                order=account.enter(signal)
                counts['paper_intents']+=bool(order)
            result={'variant':variant,'options':VARIANTS[variant],'evaluation_basis':evaluation_mode,
                'counts':dict(counts),'coverage_warnings':dict(gaps),'incomplete_reference_groups':len(parts),
                'paper_basis':basis,'paper_account':account.result() if account else None,
                'actual_live_CPU_queue':'UNKNOWN_OFFLINE_ONLY','wall_seconds':time.perf_counter()-started,
                'baseline':'CURRENT_RESEARCH_ABRR_CODE_NOT_AN_INSTALLED_LIVE_FINANCIAL_BASELINE',
                'cpu_seconds':time.process_time()-cpu,'actual_order_permission':False,
                'independent_market_opportunities':None,'target_and_risk_policy_changed':False}
    finally:
        if account:account.close()
    (output/(variant+'.json')).write_text(json.dumps(json_values(result),ensure_ascii=False,indent=2),encoding='utf-8')
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--freeze',type=Path,required=True);p.add_argument('--preopen',type=Path)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--session',choices=('REGULAR','AFTER'),default='REGULAR')
    p.add_argument('--evaluations',choices=('samples','received'),default='samples')
    p.add_argument('--max-records',type=int,default=3000000)
    args=p.parse_args(argv);root=args.freeze.resolve();out=args.output.resolve()
    if out==root or root in out.parents or out.exists():raise ValueError('NEW_EXTERNAL_OUTPUT_REQUIRED')
    manifest=verify_freeze(root)
    if manifest.get('snapshot_kind')!='SESSION_EVIDENCE':raise ValueError('SESSION_FREEZE_REQUIRED')
    if not 1<=args.max_records<=3000000:raise ValueError('BOUNDED_RECORD_LIMIT_REQUIRED')
    from core.settings import load_config
    from automation.policy import load_automation
    cfg=load_config(root/'config.json');auto=load_automation(root/'automation.json')
    out.mkdir(parents=True);network=[]
    def denied(*a,**kw):network.append(1);raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
    with patch.object(socket,'socket',denied),patch.object(socket,'create_connection',denied):
        with closing(sqlite3.connect(out/'events.sqlite')) as db:
            manifest,inventory=index_freeze(root,db,manifest['day'],args.session,args.max_records)
            results=[replay_variant(db,cfg,auto,v,out,manifest,args.preopen,args.evaluations) for v in VARIANTS]
    summary={'day':manifest['day'],'session':args.session,'inventory':inventory,'results':results,
        'manifest_sha256':file_sha(root/'MANIFEST.json'),'source_sha256':file_sha(Path(__file__)),
        'network_attempts':len(network),'actual_orders':0,'LIVE_modified':False,
        'interpretation':'COUNTERFACTUAL_ON_AVAILABLE_RECEIVED_EVIDENCE_NOT_FULL_MARKET_OR_PROFITABILITY'}
    (out/'SUMMARY.json').write_text(json.dumps(json_values(summary),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'day':manifest['day'],'variants':{r['variant']:r['counts'] for r in results},
                      'network_attempts':len(network),'actual_orders':0}))
    return 0

if __name__=='__main__':main()
