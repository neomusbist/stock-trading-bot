"""One inactive hypothesis: observed box break, supported retest, renewed trade.

C is only the legacy engine's risk/exit carrier. research_strategy=C2 and the
engine's sealed PAPER guard distinguish it from the frozen C baseline. No
projected target: the nearest pre-break confirmed overhead reference is fixed.
"""
from copy import deepcopy
from collections import deque
from datetime import timedelta
import hashlib,json,math
from automation.patterns import PatternDetector,observation_locked
from automation.capital import entry_economics,viable_entry_zone
from automation.allocation import efficiency
from automation.decisions import gate,tick_source_identity
from automation.session import supported
from core.util import floor_tick,tick_size
from core.market_time import book_freshness
from core.market_evidence import aware


class C2Detector(PatternDetector):
    isolated_setup='C'
    def __init__(self,cfg,auto,costs):
        super().__init__(cfg,auto,costs);self.continuous_observation=True
        self.boxes={};self.event_limit=4096;self.events=deque(maxlen=self.event_limit)
        self.event_losses=0;self.last_rows={};self.episode_summary={}

    def forget(self,symbol):
        self.boxes.pop(symbol,None)
        return super().forget(symbol)

    def reset(self):
        self.boxes.clear();self.last_rows.clear()
        return super().reset()

    def record(self,t,state,reason,box=None,**values):
        row=dict(strategy='C2',symbol=t.symbol,exchange_at=t.time.isoformat(),
            received_at=t.received_at,processed_at=getattr(self,'evaluation_at',t.time).isoformat(),receipt=tick_source_identity(t),state=state,reason=reason,
            episode_id=box.get('id') if box else None,actual_order_permission=False,**values)
        signature=json.dumps([state,reason,row['episode_id'],values.get('entry'),values.get('net_rr')],sort_keys=True)
        previous=self.last_rows.get(t.symbol)
        if previous==signature:return row
        self.last_rows[t.symbol]=signature
        if len(self.events)>=self.event_limit:self.event_losses+=1
        self.events.append(row);return row

    def _arm(self,t,bars,watch):
        asof=getattr(self,'evaluation_at',t.time)
        if len(bars)<self.a.box_min_bars:return None,'C2_USABLE_BOX_BARS_UNKNOWN'
        base=bars[-self.a.box_min_bars:];top=max(b['high'] for b in base);low=min(b['low'] for b in base)
        if not 0<(top-low)/low<=self.a.box_max_range:return None,'C2_BOX_WIDTH_REJECTED'
        if base[-1]['close']<base[0]['open']:return None,'C2_BOX_DIRECTION_REJECTED'
        row=self.price_context.rows.get(t.symbol,{})
        if row.get('context_venue') not in (None,'KRX'):return None,'C2_REFERENCE_VENUE_UNKNOWN'
        received=aware(row.get('history_received_at'))
        if received and received>asof:return None,'C2_REFERENCE_NOT_YET_KNOWN'
        peaks=[p for p in row.get('peaks',[]) if p['price']>top
               and aware(p.get('confirmed_at')) and aware(p['confirmed_at'])<=asof
               and (not p.get('cleared_at') or aware(p['cleared_at'])>asof)]
        if not peaks:return None,'C2_PREBREAK_OVERHEAD_UNKNOWN'
        peak=min(peaks,key=lambda p:p['price'])
        center=aware(peak['time']);wanted={center-timedelta(minutes=1),center,center+timedelta(minutes=1)}
        proof=[b for b in row.get('bars',[]) if b['time'] in wanted]
        if len(proof)!=3 or any(b.get('observation_source') not in {'ACTUAL_RECEIVED','REST_REFERENCE'} for b in proof):
            return None,'C2_OVERHEAD_SOURCE_PROOF_UNKNOWN'
        identity=[t.symbol,base[0]['time'].isoformat(),base[-1]['time'].isoformat(),top,low,peak['time']]
        ceiling=watch.get('resistance');target=peak['price']
        if type(ceiling) in (int,float) and ceiling>top:target=min(target,ceiling)
        box=dict(id=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],state='ARMED',
            top=top,low=low,base_start=base[0]['time'],base_end=base[-1]['time'],
            known_at=asof,target=target,target_evidence={**deepcopy(peak),'known_by':asof.isoformat(),'known_watch_ceiling':ceiling,
                'source_bars':[{**b,'time':b['time'].isoformat()} for b in proof]},
            noise=max(b['high']-b['low'] for b in base),last_bar=base[-1]['time'],bars=deepcopy(base),post_break_bars=[])
        if len(self.episode_summary)>=256:return None,'C2_EPISODE_BUDGET_UNKNOWN'
        self.episode_summary[box['id']]=dict(symbol=t.symbol,box_known_at=asof.isoformat(),
            top=top,low=low,target=target,target_evidence=deepcopy(box['target_evidence']),
            retest_confirmed=False,price_zone_exists=False,trigger_zone_intersection=False,detector_ready=False)
        return box,None

    @observation_locked
    def signal(self,t,watch):
        gates,bars,fast_count,base_count=self._base_evaluation(t,watch)
        reasons=[];b=self.boxes.get(t.symbol)
        if not supported(t.time):reasons.append('C2_REGULAR_SESSION_REQUIRED')
        if b and b['known_at'].date()!=t.time.date():self.boxes.pop(t.symbol);b=None
        if self.coverage_gaps.get(t.symbol,{}).get('pending'):
            self.boxes.pop(t.symbol,None);b=None;reasons.append('C2_COVERAGE_GAP_UNKNOWN')
        real_watch=getattr(self,'watch_basis',None)==(t.symbol,t.time,t.cumulative,'WATCH')
        if watch.get('status')!='WATCH' and not (watch.get('status')=='ELIGIBLE' and real_watch):
            reasons.append('C2_ACTUAL_WATCH_REQUIRED')
        if any(gates[k]['result']!='PASS' for k in ('warmup','spread','structure')):reasons.append('C2_NATIVE_DATA_GATE_NOT_READY')
        if fast_count<2 or not base_count:reasons.append('C2_NATIVE_TICK_WINDOW_UNKNOWN')
        if b is None and not reasons:
            b,why=self._arm(t,bars,watch)
            if b:
                if len(self.boxes)>=self.cfg.max_watchlist:
                    self.boxes.pop(min(self.boxes,key=lambda s:self.boxes[s]['known_at']));self.event_losses+=1
                self.boxes[t.symbol]=b
            else:reasons.append(why)
        if b and b['state'] in {'INVALID','CONSUMED'}:
            reasons.append('C2_EPISODE_'+b['state'])
            # A new non-overlapping box must be entirely later than this setup.
            if len(bars)>=self.a.box_min_bars and bars[-self.a.box_min_bars]['time']>b.get('finished_at',b['base_end']):
                self.boxes.pop(t.symbol,None)
        if b and not reasons:
            ceiling=watch.get('resistance')
            if type(ceiling) in (int,float) and b['top']<ceiling<b['target']:
                b['target']=ceiling;b['target_evidence']['lower_ceiling_known_at']=getattr(self,'evaluation_at',t.time).isoformat()
            if t.price>=b['target']:
                b.update(state='INVALID',finished_at=t.time);reasons.append('C2_KNOWN_REWARD_ALREADY_EXHAUSTED')
            elif b['state']=='ARMED':
                if t.price>b['top']:
                    b.update(state='WAIT_RETEST',break_at=t.time,break_price=t.price,break_receipt=tick_source_identity(t),
                        break_minute=t.time.replace(second=0,microsecond=0))
                    soft=floor_tick(b['low']-tick_size(b['low']))
                    b['breakout_economics']=entry_economics(t.ask,soft,floor_tick(soft-b['noise']),b['target'],self.costs,self.cfg.min_reward_risk)
                    self.record(t,'WAIT_RETEST','C2_BREAK_OBSERVED_NO_BUY',b,breakout_economics=b['breakout_economics'])
                reasons.append('C2_WAIT_POST_BREAK_RETEST')
            elif b['state']=='WAIT_RETEST':
                for bar in bars:
                    if bar['time']<=max(b['last_bar'],b['break_minute']):continue
                    b['last_bar']=bar['time'];b['noise']=max(b['noise'],bar['high']-bar['low'])
                    b['post_break_bars'].append(deepcopy(bar))
                    if bar['close']<b['top']:
                        b.update(state='INVALID',finished_at=bar['time']+timedelta(minutes=1));reasons.append('C2_BREAK_FAILED_COMPLETED_CLOSE');break
                    step=tick_size(b['top'])
                    if b['top']-step<=bar['low']<=b['top']+step and bar['close']>b['top']:
                        structural=floor_tick(bar['low']-tick_size(bar['low']))
                        noise=max(b['noise'],t.ask-t.bid,tick_size(t.price))
                        b.update(state='RETEST_CONFIRMED',retest=deepcopy(bar),confirmed_at=getattr(self,'evaluation_at',t.time),
                            structural=structural,noise=noise,emergency=floor_tick(structural-noise),
                            trigger=bar['high'])
                        self.record(t,b['state'],'C2_RETEST_COMPLETE_NO_RETROACTIVE_ENTRY',b)
                        self.episode_summary[b['id']]['retest_confirmed']=True
                        break
                if b['state']=='WAIT_RETEST':reasons.append('C2_WAIT_POST_BREAK_RETEST')
        economics=None;zone=None;target=None
        if b and b['state']=='RETEST_CONFIRMED' and not reasons:
            target=b['target']
            current_ceiling=watch.get('resistance')
            if type(current_ceiling) in (int,float) and current_ceiling>b['top']:target=min(target,current_ceiling)
            economics=entry_economics(t.ask,b['structural'],b['emergency'],target,self.costs,self.cfg.min_reward_risk)
            zone=viable_entry_zone(economics,b['trigger'],self.cfg.max_chase)
            info=self.episode_summary[b['id']]
            info['price_zone_exists']|=zone.get('price_zone_exists') is True
            info['trigger_zone_intersection']|=zone.get('trigger_compatible_zone') is True
            if economics.get('rr_pass') and 'first_economic_at' not in info:
                info.update(first_economic_at=getattr(self,'evaluation_at',t.time).isoformat(),entry=t.ask,
                    structural=b['structural'],emergency=b['emergency'],effective_target=target,
                    net_rr=economics['net_rr'],maximum_entry=economics['max_entry_price'])
            if not (t.price>b['trigger'] and t.ask>b['trigger']):reasons.append('C2_RENEWED_TRADE_TRIGGER_NOT_MET')
            if t.ask/b['trigger']-1>self.cfg.max_chase:reasons.append('C2_CHASE_REJECTED')
            if economics.get('status')!='KNOWN':reasons.append('C2_ECONOMICS_UNKNOWN')
            elif not economics['rr_pass']:reasons.append('C2_NET_RR_REJECTED')
            if not book_freshness(t,getattr(self,'evaluation_at',t.time),self.cfg.stale_seconds).get('fresh'):reasons.append('C2_BOOK_UNKNOWN_OR_STALE')
            if not t.depth_available or t.ask_qty<1 or t.bid_qty<1:reasons.append('C2_EXECUTABLE_DEPTH_UNKNOWN')
            amount=gates['liquidity']['values']['window_turnover']
            if amount<self.cfg.min_bc_window_turnover:reasons.append('C2_NATIVE_LIQUIDITY_REJECTED')
        score=None
        if economics and not reasons:
            score=efficiency(t.ask,b['emergency'],target,.5,self.a.horizon_c_minutes,self.costs,self.a.min_efficiency_score,economics=economics)
            if score['decision']!='TRADE':reasons.append('C2_EXISTING_EFFICIENCY_REJECTED')
        state=b['state'] if b else 'SEARCH'
        trial=dict(setup='C',research_strategy='C2',candidate_generated=bool(b),entry_ready=bool(economics and not reasons),
            setup_geometry=None if not b else dict(pattern='C',research_strategy='C2',breakout=b.get('trigger',b['top']),
                support=b.get('structural',b['low']),projection=b['target'],episode_id=b['id']),
            failed_reasons=list(dict.fromkeys(reasons)),gates=deepcopy(gates),virtual_entry=t.ask,
            virtual_invalidation=b.get('emergency') if b else None)
        if economics:
            trial['gates']['risk_reward']=gate(economics['rr_pass'],economics=economics,target=target,
                calculated_rr=economics.get('net_rr'),net_rr=economics.get('net_rr'),entry_zone=zone)
        self.last_eval[t.symbol]=dict(time=t.time,cumulative=t.cumulative,gates=gates,trials=[trial])
        self._eval_keys[t.symbol]=self._evaluation_key(t,watch)
        self.record(t,state,reasons[0] if reasons else 'C2_PRICE_TRIGGER_READY',b,
            entry=t.ask,target=target,structural=b.get('structural') if b else None,
            emergency=b.get('emergency') if b else None,net_rr=economics.get('net_rr') if economics else None,
            maximum_entry=economics.get('max_entry_price') if economics else None,reasons=trial['failed_reasons'])
        if not trial['entry_ready']:return None
        self.episode_summary[b['id']]['detector_ready']=True
        policy=dict(enabled=True,policy_name='C2_CONFIRMED_BOX_RETEST',structural_stop=b['structural'],
            emergency_stop=b['emergency'],noise_band=b['noise'],confirmation_bar=b['retest']['time'].isoformat())
        gates={**gates,'pattern':gate(True,setup='C2'),'breakout':gate(True,breakout=b['trigger']),
            'risk_reward':trial['gates']['risk_reward'],'liquidity':gate(True,window_turnover=amount,min_turnover=self.cfg.min_bc_window_turnover),
            'acceleration':{**gates['acceleration'],'required':False,'scope':'OBSERVATION_ONLY'}}
        def serialized_bar(bar):
            return {**bar,'time':bar['time'].isoformat()}
        proof=dict(base=[serialized_bar(x) for x in b['bars']],known_at=b['known_at'].isoformat(),
            break_at=b['break_at'].isoformat(),break_price=b['break_price'],break_receipt=deepcopy(b['break_receipt']),
            post_break_bars=[serialized_bar(x) for x in b['post_break_bars']],
            confirmed_at=b['confirmed_at'].isoformat(),target_evidence=deepcopy(b['target_evidence']))
        return dict(symbol=t.symbol,time=t.time.isoformat(),pattern='C',research_strategy='C2',paper_only=True,c2_structure_proof=proof,
            limit=t.ask,stop=b['emergency'],emergency=b['emergency'],support=b['structural'],target=target,
            breakout=b['trigger'],breakout_level=b['trigger'],projection=b['target'],
            target_basis='PREBREAK_CONFIRMED_OVERHEAD_NOT_BOX_MULTIPLE',target_evidence=deepcopy(b['target_evidence']),
            c2_episode_id=b['id'],c2_box_top=b['top'],c2_retest_confirmed_at=b['confirmed_at'].isoformat(),
            invalidation_policy=policy,entry_economics=economics,entry_zone=zone,efficiency=score,
            evaluation_gates=gates,setup_trials=[trial],source_count=watch.get('source_count'),
            fallback_source1=bool(watch.get('fallback_source1')),receipt_evidence=tick_source_identity(t),
            session='REGULAR',venue='KRX',planned_qty=None)

    def consumed(self,symbol,at):
        if symbol in self.boxes:self.boxes[symbol].update(state='CONSUMED',finished_at=at)
