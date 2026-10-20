"""Bounded, offline replay of recorded evidence; never connects to a broker.

Cache audit alone intentionally returns UNKNOWN performance. Paired 0B/0D
snapshots can support a *paper L1 feasibility scenario*, not a historical fill.
Capture bars enter the simulation only when their receipt/processing proves
they were known. No reconstruction from the final PriceContext close is used.
"""
import argparse
from collections import Counter,defaultdict,deque
from dataclasses import asdict,fields
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from core.settings import Config
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.local_breakout import LocalBreakoutStrategy
from research.causal_data import (aware,finite,file_sha,json_values,audit_cache,
                                  read_frames,captured_bars,frozen_lines)
from research.causal_account import PaperAccount
from research.retest_strategy import ConfirmedRetestStrategy,paper_engine_signal
from research.causal_strategy_adapters import build_all_strategy_adapters


def read_config(path,kind):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    allowed={f.name for f in fields(kind)}
    return kind(**{k:v for k,v in data.items() if k in allowed}).validate()


def capture_metadata(paths,day,limits=None):
    events=[];stats=Counter();trials=defaultdict(Counter);fast=Counter();ready=[]
    seen_samples=set();episode_state={};episodes=[]
    sessions={};raw_first={};watch_first={};peak_meta={}
    for path in paths:
        for line in frozen_lines(path,(limits or {}).get(str(path))):
            try:r=json.loads(line)
            except ValueError:stats['MALFORMED_CAPTURE_JSON']+=1;continue
            stamp=aware(r.get('asof') or r.get('at') or r.get('processed_at') or r.get('received_at')
                        or r.get('timestamp') or r.get('time'))
            if not stamp or stamp.date().isoformat()!=day:continue
            if not '09:00:00'<=stamp.strftime('%H:%M:%S')<'15:30:00':continue
            ev=r.get('event');stats[ev]+=1
            session=r.get('capture_session','UNKNOWN')
            sessions[session]=max(sessions.get(session,0),r.get('capture_losses',0))
            gap_events={'OBSERVATION_RELEASED','OBSERVATION_DATA_GAP','SHADOW_DATA_RESET',
                        'SHADOW_STREAM_RESET','SHADOW_QUEUE_GAP','SHADOW_SEQUENCE_GAP'}
            if ev in gap_events or (ev=='OBSERVATION_REJOINED' and finite(r.get('subscription_gap_seconds'))):
                events.append((stamp,str(r.get('symbol') or ''),{
                    'event':'COVERAGE_GAP','reason':ev,'symbols':r.get('symbols'),
                    'capture_session':session}))
                continue
            if ev!='OPPORTUNITY_SAMPLE':continue
            key=(session,r.get('capture_sequence'))
            if key in seen_samples:continue
            seen_samples.add(key);sym=r['symbol'];watch=r.get('watch') or {}
            for source,mapping in (('first_raw_at',raw_first),('first_watch_at',watch_first)):
                dt=aware(watch.get(source))
                if dt and dt<=stamp:mapping[sym]=min(mapping.get(sym,dt),dt)
            events.append((stamp,sym,r))
            for trial in r.get('trials',[]):
                name=trial.get('setup','UNKNOWN');trials[name]['sampled_evaluations']+=1
                trials[name]['formed']+=bool(trial.get('formed'));trials[name]['ready']+=bool(trial.get('ready'))
                for reason in trial.get('failures',[]):trials[name]['reason:'+str(reason)]+=1
                formed=bool(trial.get('formed'));state=episode_state.get((sym,name),False)
                if formed and not state:
                    episodes.append({'symbol':sym,'pattern':name,'first_sampled_at':stamp.isoformat(),
                        'formed':True,'ready':bool(trial.get('ready')),'failures':trial.get('failures',[]),
                        'entry':trial.get('entry'),'stop':trial.get('stop'),'target':trial.get('target'),
                        'coverage':'SAMPLED_EPISODE_BOUNDARY_NOT_A_COMPLETE_SIGNAL_CENSUS'})
                episode_state[sym,name]=formed
                if trial.get('ready'):ready.append({'symbol':sym,'pattern':name,'at':stamp.isoformat()})
            f=r.get('fast') or {};fast[str(f.get('status'))+':'+str(f.get('reason'))]+=1
    return sorted(events,key=lambda x:(x[0],x[1])),{
        'events':dict(stats),'captured_losses_by_session':sessions,
        'ABC_sampled_gates':{k:dict(v) for k,v in trials.items()},'FAST_sampled_status':dict(fast),
        'sampled_formed_episodes':episodes,'sampled_ready':ready,
        'symbols_with_raw_time':len(raw_first),'symbols_with_watch_time':len(watch_first),
        'discovery_to_watch_seconds':{s:(watch_first[s]-raw_first[s]).total_seconds()
            for s in raw_first.keys()&watch_first.keys()},
        'coverage':'SAMPLED_SELECTED_WATCH_NOT_FULL_UNIVERSE_OR_ALL_SIGNALS'}


class HypothesisReplay:
    def __init__(self,cfg,auto,*,clock_basis='processed',latency_ms=0,adverse_ticks=0,
                 initial_cash,approved_cap=None,with_accounts=False):
        self.cfg=cfg;self.auto=auto;self.clock_basis=clock_basis
        self.costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,
                         cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        self.strategies=build_all_strategy_adapters(cfg,auto,self.costs)
        self.accounts={};self.account_params=dict(cfg=cfg,auto=auto,initial_cash=initial_cash,
            approved_cap=approved_cap,latency_ms=latency_ms,adverse_ticks=adverse_ticks)
        self.with_accounts=with_accounts
        self.history=defaultdict(lambda:deque(maxlen=30));self.peaks=defaultdict(lambda:deque(maxlen=128))
        self.watch={};self.evaluated={};self.episodes={k:{} for k in self.strategies}
        self.reasons={k:Counter() for k in self.strategies};self.quote_reasons=Counter();self.delays=Counter()
        self.seen=Counter();self.invalidations=Counter();self.frame_count=0;self.first=None;self.last=None
        self.coverage_after={};self.global_coverage_after=None

    def coverage_gap(self,stamp,symbol,meta):
        targets={symbol} if symbol else set(meta.get('symbols') or ())
        if not targets:
            self.global_coverage_after=max(self.global_coverage_after or stamp,stamp)
            targets=set(self.history)|set(self.watch)|set(self.seen)
        for sym in targets:
            if stamp<=self.coverage_after.get(sym,stamp-timedelta(microseconds=1)):continue
            self.coverage_after[sym]=stamp
            self.history.pop(sym,None)
            for adapter in self.strategies.values():
                adapter.mark_coverage_gap(sym,stamp,meta['reason'])
        self.invalidations[meta['reason']]+=1

    def bar(self,known,sym,b):
        gap=self.coverage_after.get(sym) or self.global_coverage_after
        if gap and b['time']<=gap.replace(second=0,microsecond=0):return False
        h=self.history[sym]
        if h and b['time']<=h[-1]['time']:return False
        h.append(b)
        for peak in self.peaks[sym]:
            if not peak.get('cleared_at') and b['close']>peak['price']:peak['cleared_at']=known
        if len(h)>=3:
            left,middle,right=list(h)[-3:]
            if ((middle['time']-left['time']).total_seconds()==60
                and (right['time']-middle['time']).total_seconds()==60
                and middle['high']>max(left['high'],right['high'])):
                self.peaks[sym].append({'price':middle['high'],'time':middle['time'].isoformat(),
                    'confirmed_at':(right['time']+timedelta(minutes=1)).isoformat(),
                    'available_at':known.isoformat(),'observation_source':'ACTUAL_RECEIVED'})
        for adapter in self.strategies.values():
            adapter.ingest_bar(known,sym,b)
        return True

    def frame(self,f,new_bars=()):
        t=f.tick;at=f.available_at;self.frame_count+=1
        if self.global_coverage_after and t.symbol not in self.coverage_after:
            self.coverage_gap(self.global_coverage_after,t.symbol,{'reason':'GLOBAL_OBSERVATION_GAP'})
        self.first=self.first or at;self.last=at;self.seen[t.symbol]+=1
        delay=(at-f.received_at).total_seconds()
        self.delays['<=1s' if delay<=1 else '<=5s' if delay<=5 else '>5s']+=1
        quote_reason=f.quote_reason(at,self.cfg.stale_seconds)
        self.quote_reasons[quote_reason or 'MEASURED_FRESH_PAIRED_QUOTE']+=1
        for adapter in self.strategies.values():
            adapter.ingest_tick(t,at)
        for key in self.strategies:
            if self.with_accounts and key not in self.accounts:self.accounts[key]=PaperAccount(at,**self.account_params)
            account=self.accounts.get(key)
            if account and (not quote_reason or account.e.s['positions'] or account.e.active_orders()):
                meta=self.watch.get(t.symbol) or {};account.step(f,bar_events=new_bars,market_status=meta.get('market_status'))
        # Match the bounded research evaluation cadence. Do not pretend this
        # evaluates every transient tick or duplicates each event as a trade.
        prior=self.evaluated.get(t.symbol)
        if prior and (at-prior).total_seconds()<1.:return
        self.evaluated[t.symbol]=at
        if quote_reason:return
        h=list(self.history[t.symbol]);meta=self.watch.get(t.symbol)
        if not meta:
            for reason in self.reasons.values():reason['POINT_IN_TIME_WATCH_METADATA_UNKNOWN']+=1
            return
        view=dict(meta.get('watch') or {})
        peaks=[p for p in self.peaks[t.symbol] if not p.get('cleared_at') and p['price']>t.ask]
        if peaks:view['nearest_confirmed_peak']=min(peaks,key=lambda p:p['price'])
        for key,strategy in self.strategies.items():
            account=self.accounts.get(key)
            capital=account.e.watch_budget() if account else self.account_params['initial_cash']
            risk=(account.e.risk_equity() if account else capital)*self.cfg.risk_fraction
            signal,detail=strategy.evaluate(t,h,view,capital,risk,at)
            if signal is None:self.reasons[key][detail.get('reason','UNKNOWN')]+=1;continue
            # Independent structural opportunity, not every repeated tick.
            extra=signal.get('extra') or {} if isinstance(signal,dict) else getattr(signal,'extra',{})
            pattern=signal.get('pattern') if isinstance(signal,dict) else getattr(signal,'pattern')
            stop=signal.get('stop') if isinstance(signal,dict) else getattr(signal,'stop')
            lvl=extra.get('local_high',extra.get('breakout_level',signal.get('limit') if isinstance(signal,dict) else getattr(signal,'limit',0)))
            identity=(t.symbol,pattern,stop,lvl)
            encoded='|'.join(map(str,identity));episode=hashlib.sha256(encoded.encode()).hexdigest()[:24]
            if episode in self.episodes[key]:continue
            sig_dict=signal if isinstance(signal,dict) else asdict(signal)
            record={'episode_id':episode,'at':at.isoformat(),'source_id':f.source_id,
                'symbol':t.symbol,'signal':json_values(sig_dict),
                'quote_basis':'MEASURED_PAIRED_L1_NOT_AN_ACTUAL_FILL',
                'clock_basis':self.clock_basis,'engine_order':None}
            if account:
                if isinstance(signal,dict):
                    adapted=signal
                else:
                    source={'source_count':view.get('source_count'),'fallback_source1':view.get('fallback_source1',False),
                            'timestamp':meta['asof']}
                    adapted=paper_engine_signal(signal,self.cfg,self.auto,self.costs,source_evidence=source)
                order=account.enter(adapted)
                record['engine_order']=order['local'] if order else None
            self.episodes[key][episode]=record

    def result(self):
        return {'clock_basis':self.clock_basis,'first':self.first,'last':self.last,
            'frames':self.frame_count,'observed_symbols':len(self.seen),
            'coverage_invalidations':dict(self.invalidations),
            'receipt_to_processing_histogram':dict(self.delays),'quote_status':dict(self.quote_reasons),
            'policies':{key:{'independent_structural_candidates':len(self.episodes[key]),
                'episodes':list(self.episodes[key].values()),'rejections':dict(self.reasons[key]),
                'account':self.accounts[key].result() if key in self.accounts else None} for key in self.strategies},
            'coverage':'RECORDED_SHADOW_SUBSET_AND_KNOWN_CAPTURE_BARS_NOT_THE_WHOLE_MARKET',
            'net_positive_expectancy_proven':False,'actual_order_permission':False,
            'fill_assumptions':['L1 displayed liquidity is only an upper bound for a marketable paper fill',
                'queue priority, hidden orders, cancellation races and exact exchange latency UNKNOWN',
                'adverse ticks/latency are explicit sensitivities; BUY limit never increased',
                'no last-bar forced liquidation; open positions and pending orders remain open']}

    def close(self):
        for account in self.accounts.values():account.close()


def run_replay(tick_paths,capture_paths,day,cfg,auto,**kwargs):
    # Freeze byte extents before reading a running append-only journal. A hash
    # of its later contents must not be presented as the input actually read.
    limits={str(p):Path(p).stat().st_size for p in [*tick_paths,*capture_paths]}
    bars,bar_stats=captured_bars(capture_paths,day,limits)
    metadata,meta_stats=capture_metadata(capture_paths,day,limits)
    replay=HypothesisReplay(cfg,auto,**kwargs);stats=Counter();bi=mi=0
    try:
        for f in read_frames(tick_paths,day,clock_basis=kwargs.get('clock_basis','processed'),stats=stats,limits=limits):
            released=[]
            # Merge releases by actual availability. A gap at the same time is
            # applied before bars, so delayed capture records cannot bridge it.
            while True:
                next_bar=bars[bi][0] if bi<len(bars) else None
                next_meta=metadata[mi][0] if mi<len(metadata) else None
                if next_meta is not None and next_meta<=f.available_at and (next_bar is None or next_meta<=next_bar):
                    stamp,sym,meta=metadata[mi];mi+=1
                    if meta.get('event')=='COVERAGE_GAP':
                        replay.coverage_gap(stamp,sym,meta)
                    else:replay.watch[sym]=meta
                elif next_bar is not None and next_bar<=f.available_at:
                    known,sym,b=bars[bi];bi+=1
                    if replay.bar(known,sym,b):released.append((sym,b))
                else:break
            replay.frame(f,released)
        result=replay.result()
    finally:replay.close()
    return json_values({'day':day,'ticks':[{ 'path':str(p),'frozen_bytes':limits[str(p)],'sha256_prefix':file_sha(p,limits[str(p)])} for p in tick_paths],
        'capture':[{'path':str(p),'frozen_bytes':limits[str(p)],'sha256_prefix':file_sha(p,limits[str(p)])} for p in capture_paths],
        'input_growth_handling':'ONLY_COMPLETE_LINES_WITHIN_FROZEN_BYTE_EXTENTS',
        'frame_reader':dict(stats),'bar_reader':bar_stats,'verified_bars':len(bars),
        'released_bars':bi,'recorded_existing_R6':meta_stats,'replay':result,
        'scenario_config':asdict(cfg),'scenario_automation':asdict(auto)})


def main(argv=None,*,account_default=False):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache',type=Path,default=Path(__file__).resolve().parents[1]/'audit_data/price_context_cache_20261020.json')
    p.add_argument('--ticks',type=Path,nargs='+');p.add_argument('--capture',type=Path,nargs='+',default=[])
    p.add_argument('--day',default='2026-10-20');p.add_argument('--config',type=Path);p.add_argument('--automation',type=Path)
    p.add_argument('--paper-account',action='store_true',default=account_default)
    p.add_argument('--initial-cash',type=float);p.add_argument('--approved-cap',type=int)
    p.add_argument('--latency-ms',type=float,default=0);p.add_argument('--adverse-ticks',type=int,default=0)
    p.add_argument('--clock-basis',choices=['processed','receiver_counterfactual'],default='processed')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    if a.ticks:
        if not a.config or not a.automation:p.error('tick replay requires explicit --config and --automation scenario inputs')
        if a.initial_cash is None:p.error('tick replay requires explicit --initial-cash; a library default is not an account snapshot')
        cfg=read_config(a.config,Config);auto=read_config(a.automation,AutomationConfig)
        result=run_replay(a.ticks,a.capture,a.day,cfg,auto,clock_basis=a.clock_basis,
            with_accounts=a.paper_account,initial_cash=a.initial_cash,approved_cap=a.approved_cap,
            latency_ms=a.latency_ms,adverse_ticks=a.adverse_ticks)
    else:
        result={'status':'UNKNOWN_EXECUTION_PERFORMANCE','cache':audit_cache(a.cache),
            'invalidated_legacy_claims':{'independent_trades':89,'net_pnl':-71184.2,
                'account_trades':5,'account_pnl':[-2948.5,-3020.1]},
            'reason':'Legacy START-labelled OHLC and invented bid/ask/depth cannot establish available signals or fills',
            'corrected_actual_trades':None,'corrected_net_pnl':None,
            'required':'timestamped actual trade and book evidence plus proven bar availability; use --ticks/--capture',
            'actual_order_permission':False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'report':str(a.output),'sha256':file_sha(a.output),
        'status':result.get('status','PAPER_EVIDENCE_SCENARIO_COMPLETE_NOT_PROFITABILITY_PROOF')},ensure_ascii=False))
    return result


if __name__=='__main__':main()
