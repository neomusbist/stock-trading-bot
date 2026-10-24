"""Every accepted trade/known quote replay for the inactive initial hypothesis.

Uses the existing freeze/index/PatternDetector ingestion and PAPER account.
OPPORTUNITY_SAMPLE supplies qualification/quote evidence, never invented trades.
"""
import argparse
from collections import Counter,deque
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import hashlib,json,socket,sqlite3,time
from pathlib import Path
from unittest.mock import patch
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.policy import load_automation
from core.market_evidence import aware,bar_evidence
from core.realtime import current_book_tick
from core.settings import load_config
from research.causal_data import Frame,file_sha,json_values
from research.initial_structure import InitialBaseResearch
from research.session_validation import index_freeze,verify_freeze


def replay(db,cfg,auto,output,*,scenario_cash=None,paper=False,hypothesis='A'):
    if auto.fast_momentum_enabled or auto.pullback_expansion_target_enabled:raise ValueError('KEEP_FAST_AND_EXPANSION_OFF')
    costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
    d=PatternDetector(cfg,auto,costs);d.continuous_observation=True
    if hypothesis=='A':s=InitialBaseResearch(cfg,auto)
    else:
        from research.support_recovery import SupportRecoveryResearch
        s=SupportRecoveryResearch(cfg,auto,noise_policy='received_prefix' if hypothesis=='B_PREFIX' else 'three_bar_range')
    counts=Counter();reasons=Counter();formation_reasons=Counter();plans={};unpriced={};unresolved={};signals={};frames={};books={}
    bars={};rest={};parts={};revision=Counter();reference_cache={};last_loss={};account=None
    cash=(min(scenario_cash,auto.capital_limit) if scenario_cash is not None else None)
    budget=(cash*cfg.max_position_fraction if cash is not None else None)
    risk=(min(cash*cfg.risk_fraction,cash*cfg.max_daily_loss_fraction) if cash is not None else None)
    started=time.perf_counter();cpu=time.process_time()
    def invalidate(symbol,at,reason):
        d.mark_coverage_gap(symbol,at,reason);s.reset(symbol);counts['coverage_resets']+=1
        for record in signals.values():
            if record['symbol']==symbol:record['path_coverage']='GAP_UNKNOWN'
    def references(symbol):
        if reference_cache.get(symbol,(None,))[0]!=revision[symbol]:
            merged={**rest.get(symbol,{}),**bars.get(symbol,{})}
            reference_cache[symbol]=(revision[symbol],sorted(merged.values(),key=lambda b:b['time'])[-120:])
        return reference_cache[symbol][1]
    def observe_path(frame):
        for record in signals.values():
            if record['symbol']!=frame.tick.symbol:continue
            delta=(frame.available_at-aware(record['first_L1_at'])).total_seconds()
            if not 0<delta<=185:continue
            price=frame.tick.price;entry=record['signal']['limit']
            record['observed_MFE']=max(record['observed_MFE'],price/entry-1)
            record['observed_MAE']=min(record['observed_MAE'],price/entry-1)
            record['post_trade_rows']+=1
            stop=record['signal']['invalidation_policy']['structural_stop']
            if price<stop:record.setdefault('first_structure_failure_at',frame.available_at.isoformat())
            if price<=record['signal']['stop']:record.setdefault('first_emergency_touch_at',frame.available_at.isoformat())
            if price>=record['signal']['target']:record.setdefault('first_target_touch_at',frame.available_at.isoformat())
            for horizon in (30,60,180):
                key=str(horizon)
                if delta<horizon or key in record['markouts']:continue
                q=record['signal']['planned_qty']
                reason=frame.quote_reason(frame.available_at,cfg.stale_seconds)
                valid=delta-horizon<=cfg.stale_seconds and reason is None and q and frame.tick.bid_qty>=q
                record['markouts'][key]={'at':frame.available_at.isoformat(),'delay_seconds':delta-horizon,
                    'basis':'RECEIVED_L1_HYPOTHETICAL_LIQUIDATION_NOT_EXIT_STRATEGY' if valid else 'UNKNOWN',
                    'bid':frame.tick.bid if valid else None,
                    'net_with_both_slippage_reserves':q*(frame.tick.bid*(1-auto.commission_sell-auto.sell_tax-cfg.exit_slippage_fraction)
                        -entry*(1+auto.commission_buy+cfg.entry_slippage_fraction)) if valid else None,
                    'quantity_basis':'EXPLICIT_CASH_SCENARIO_NOT_HISTORICAL_ACCOUNT','actual_fill':False}
    def evaluate(frame,source,receipt,*,qualifier=None,basis='ACTUAL_STRATEGY_INGESTION'):
        nonlocal account
        sym=frame.tick.symbol;at=frame.available_at
        actual=d.usable_bars(sym)
        sig,detail=s.evaluate(frame.tick,actual,references(sym),at,cash_budget=budget,risk_budget=risk,
            receipt=receipt,input_revision=(revision[sym],tuple(b['time'] for b in actual[-3:])))
        counts['evaluation_events']+=1;reasons[detail['reason']]+=1
        key=detail.get('structure_id')
        if detail.get('noise_evidence') and detail.get('emergency_stop') is None:
            missing_key=(sym,detail['touch_at'],detail['micro_left_at'],detail['reason'])
            unresolved.setdefault(missing_key,{'symbol':sym,'first_observed_at':at.isoformat(),
                'source_id':source,'receipt':receipt,'evidence':detail,
                'quote_at_candidate':{'ask':frame.tick.ask,'bid':frame.tick.bid,'ask_qty':frame.tick.ask_qty,
                    'bid_qty':frame.tick.bid_qty,'book_received_at':frame.tick.book_received_at}})
        if not key and detail.get('reason')=='NO_PREKNOWN_OVERHEAD_ABOVE_BASE':
            unknown_key=(sym,tuple(b['time'].isoformat() for b in actual[-3:]),revision[sym])
            unpriced.setdefault(unknown_key,{'symbol':sym,'first_observed_at':at.isoformat(),
                'reason':detail['reason'],'level':detail.get('level'),'base_times':list(unknown_key[1]),
                'target':None,'price_zone':'UNKNOWN_NO_INDEPENDENT_TARGET'})
        if key and key not in plans:
            plans[key]={'symbol':sym,'source_id':source,'first_observed_at':at.isoformat(),'plan':detail}
            formation_reasons[detail['reason']]+=1
        if not sig:return
        if key not in signals:
            signals[key]={'symbol':sym,'source_id':source,'first_L1_at':at.isoformat(),'availability_basis':basis,
                'first_structure_at':sig['research_structure']['first_structure_at'],
                'trigger_at':sig['trigger_evidence']['trigger_at'],'signal':sig,
                'quote':{'bid':frame.tick.bid,'ask':frame.tick.ask,'bid_qty':frame.tick.bid_qty,'ask_qty':frame.tick.ask_qty,
                    'book_at':frame.tick.book_time,'book_received_at':frame.tick.book_received_at},
                'qualification':'UNKNOWN_NOT_A_WATCH_CENSUS','paper_order':None,
                'observed_MFE':0.,'observed_MAE':0.,'post_trade_rows':0,'markouts':{},
                'path_coverage':'OBSERVED_SUBSCRIBED_SUBSET_ONLY_NOT_COMPLETENESS_PROOF'}
        record=signals[key]
        if not qualifier:return
        record['qualification']='RECORDED_WATCH_SAMPLE_AT_THIS_EXACT_TRADE'
        record.setdefault('first_qualified_L1_at',at.isoformat())
        if not paper or record.get('paper_attempted'):return
        record['paper_attempted']=True
        if account is None:
            from research.causal_account import PaperAccount
            account=PaperAccount(at,initial_cash=cash,cfg=cfg,auto=auto)
        watch=qualifier.get('watch') or {}
        if watch.get('status')!='WATCH':record['paper_rejection']='WATCH_NOT_CONFIRMED';return
        sig['source_evidence']={'timestamp':qualifier['asof'],'source_count':watch.get('source_count'),
            'fallback_source1':watch.get('fallback_source1',False)}
        account.step(frame,market_status=qualifier.get('market_status'))
        order=account.enter(sig)
        record['paper_order']=order['local'] if order else None
        record['paper_decision']=account.entry_decisions[-1] if account.entry_decisions else None
    try:
        for stamp,source,payload in db.execute('SELECT known,source,payload FROM events ORDER BY known,ordinal'):
            r=json.loads(payload);at=aware(stamp);ev=r['event'];sym=r.get('symbol')
            session=r.get('capture_session');loss=r.get('capture_losses',0)
            if session and loss>last_loss.get(session,0):
                for code in list(d.last):invalidate(code,at,'ACTUAL_CAPTURE_LOSS')
            if session:last_loss[session]=max(loss,last_loss.get(session,0))
            if ev=='SUBSCRIPTION_SCOPE':
                for code in r.get('removed',[]):invalidate(code,at,'ACTUAL_UNSUBSCRIBE')
            if ev=='STREAM_STATE' and r.get('kind') in {'ERROR','DISCONNECTED','RECONNECTING'}:
                for code in list(d.last):invalidate(code,at,'ACTUAL_STREAM_GAP')
            if ev=='REFERENCE_BARS_PART':
                key=(sym,r['reference_sha256'],stamp);group=parts.setdefault(key,{})
                group[r['part']]=r['rows']
                if len(group)==r['parts']:
                    rows=[b for i in range(r['parts']) for b in group[i]]
                    raw=[[b['time']]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in rows]
                    if hashlib.sha256(json.dumps(raw,separators=(',',':')).encode()).hexdigest()!=r['reference_sha256']:
                        raise ValueError('REFERENCE_HASH_MISMATCH')
                    rest[sym]={aware(b['time']):{**b,'time':aware(b['time']),'observation_source':'REST_REFERENCE',
                        'available_at':stamp} for b in rows}
                    revision[sym]+=1;parts.pop(key);counts['known_REST_reference_snapshots']+=1
            if ev=='COMPLETED_BAR':
                b={**r['bar'],'time':aware(r['bar']['time']),'available_at':stamp,
                   'observation_source':'ACTUAL_RECEIVED' if r.get('source')=='ACTUAL_RECEIVED_DETECTOR_HISTORY' else 'UNKNOWN'}
                if not bar_evidence(b)['signal_source_verified']:counts['unusable_recorded_bars']+=1;continue
                bars.setdefault(sym,{})[b['time']]=b
                history={x['time']:x for x in d.history.get(sym,())};history[b['time']]=b
                d.history[sym]=deque(sorted(history.values(),key=lambda x:x['time'])[-30:],maxlen=30)
                revision[sym]+=1;counts['recorded_completed_bars']+=1
            if ev=='TRADE' and r.get('capture_point')=='STRATEGY_INGESTION':
                try:f=Frame.from_record(r,source)
                except (KeyError,ValueError,TypeError):counts['invalid_frame']+=1;continue
                if r.get('input_evidence_basis')=='SYNTHETIC_TEST_QUOTE':f=replace(f,basis='SYNTHETIC_TEST_QUOTE')
                if not d.ingest(f.tick,f.received_at):counts['rejected_ingestions']+=1;continue
                history={x['time']:x for x in d.history.get(sym,())}
                bars.setdefault(sym,{})
                for minute,b in history.items():
                    if minute not in bars.setdefault(sym,{}):
                        b={**b,'available_at':stamp,'observation_source':'ACTUAL_RECEIVED'}
                        bars[sym][minute]=b;revision[sym]+=1
                history.update({k:v for k,v in bars[sym].items() if k<f.tick.time.replace(second=0,microsecond=0)})
                d.history[sym]=deque(sorted(history.values(),key=lambda b:b['time'])[-30:],maxlen=30)
                # Bound completed references; past bars never become a synthetic print.
                bars[sym]=dict(sorted(bars[sym].items())[-120:])
                frames[sym]=f;counts['accepted_trades']+=1
                if account and (account.e.s['positions'] or account.e.active_orders()):account.step(f)
                observe_path(f)
                q=current_book_tick(f.tick,books.get(sym),at,cfg.stale_seconds)
                evaluate(replace(f,tick=q),source,r.get('receipt_evidence'))
            if ev=='BOOK':
                b={'time':r.get('exchange_at'),'received_at':r.get('received_at'),
                   **{k:r.get(k) for k in ('best_bid','best_ask','bid_qty','ask_qty','depth_available')}}
                books[sym]=b
                if sym not in frames:continue
                prior=frames[sym];q=current_book_tick(prior.tick,b,at,cfg.stale_seconds)
                if q==prior.tick:continue
                f=replace(prior,tick=q,available_at=at)
                evaluate(f,source,{'book_receipt_id':r.get('book_receipt_id'),
                    'trade_source_id':prior.source_id},basis='BOOK_RECEIVER_KNOWLEDGE_NOT_ORIGINAL_STRATEGY_CALL')
            if ev=='OPPORTUNITY_SAMPLE':
                counts['baseline_sampled_decisions']+=1
                for trial in r.get('trials',[]):
                    if trial.get('ready'):counts['baseline_sampled_'+trial.get('setup','UNKNOWN')+'_ready']+=1
                old=frames.get(sym)
                if not old or (old.tick.cumulative,old.tick.price,old.tick.received_at)!=(r.get('cumulative'),r.get('price'),r.get('received_at')):
                    counts['sample_exact_trade_not_retained']+=1;continue
                # An actual sample quote for an existing trade, never a new trade.
                q=replace(old.tick,**{k:r[k] for k in ('bid','ask','bid_qty','ask_qty','depth_available','depth_source') if k in r},
                    book_time=aware(r.get('book_time')),book_received_at=r.get('book_received_at'))
                evaluate(replace(old,tick=q,available_at=at),source,r.get('receipt_evidence'),qualifier=r,
                    basis='RECORDED_EXACT_TRADE_QUALIFICATION_SAMPLE')
        result={'hypothesis':s.name,'counts':dict(counts),'repeated_evaluation_reasons':dict(reasons),
            'unique_economic_plans':len(plans),'first_plan_outcomes':dict(formation_reasons),
            'unique_L1_signal_structures':len(signals),'qualified_exact_sample_signals':sum('first_qualified_L1_at' in r for r in signals.values()),
            'signals':list(signals.values()),'plans':list(plans.values()),'paper_account':account.result() if account else None,
            'unresolved_noise_candidates':list(unresolved.values()),
            'unpriced_supported_structures':list(unpriced.values()),
            'budget_scenario':{'cash':cash,'entry_cash_budget':budget,'per_trade_risk':risk,'actual_historical_account':False},
            'independent_market_trades':None,'actual_order_permission':False,'actual_orders':0,
            'live_candidate':False,'profitability_proven':False,'cpu_seconds':time.process_time()-cpu,
            'wall_seconds':time.perf_counter()-started,'live_CPU_queue_impact':'UNKNOWN_OFFLINE_ONLY',
            'MFE_MAE_basis':'OBSERVED_FUTURE_PRINT_SUBSET_NOT_EXECUTED_TRADES',
            'incomplete_reference_parts':len(parts)}
    finally:
        if account:account.close()
    (output/'RESULT.json').write_text(json.dumps(json_values(result),ensure_ascii=False,indent=2),encoding='utf-8')
    return result

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--freeze',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--scenario-cash',type=float)
    p.add_argument('--paper',action='store_true');p.add_argument('--hypothesis',choices=('A','B','B_PREFIX'),default='A');a=p.parse_args(argv)
    root=a.freeze.resolve();out=a.output.resolve();m=verify_freeze(root)
    if out.exists() or out==root or root in out.parents:raise ValueError('NEW_EXTERNAL_OUTPUT_REQUIRED')
    if a.scenario_cash is not None and (not 0<=a.scenario_cash<float('inf')):raise ValueError('INVALID_SCENARIO_CASH')
    if a.paper and a.scenario_cash is None:raise ValueError('EXPLICIT_PAPER_CASH_REQUIRED')
    cfg=load_config(root/'config.json');auto=load_automation(root/'automation.json');out.mkdir(parents=True)
    attempts=[]
    def denied(*args,**kw):attempts.append(1);raise RuntimeError('OFFLINE_NETWORK_FORBIDDEN')
    with patch.object(socket,'socket',denied),patch.object(socket,'create_connection',denied):
        with closing(sqlite3.connect(out/'events.sqlite')) as db:
            _,inventory=index_freeze(root,db,m['day'],'REGULAR')
            result=replay(db,cfg,auto,out,scenario_cash=a.scenario_cash,paper=a.paper,hypothesis=a.hypothesis)
    summary={k:v for k,v in result.items() if k not in {'signals','plans','paper_account','unpriced_supported_structures','unresolved_noise_candidates'}}
    summary['unresolved_noise_candidates']=len(result['unresolved_noise_candidates'])
    summary['supported_structures_with_unknown_target']=len(result['unpriced_supported_structures'])
    summary.update(input_manifest_sha256=file_sha(root/'MANIFEST.json'),inventory=inventory,network_attempts=len(attempts),
        source_sha256={f:file_sha(Path(__file__).parent/f) for f in ('initial_structure.py','initial_structure_replay.py','support_recovery.py') if (Path(__file__).parent/f).exists()})
    (out/'SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False));return 0

if __name__=='__main__':main()
