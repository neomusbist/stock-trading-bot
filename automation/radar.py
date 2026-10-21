from core.event_log import append_jsonl
"""랭킹은 발견 레이더다. 동일 소스의 연속 스냅샷을 비교하고 일봉 구조를 붙인다."""
import json
import queue
import threading
import time
from datetime import date,datetime as date_time
from dataclasses import replace
from pathlib import Path
from candidate_builder import RANKS, bars_from_csv, fetch_bars, structure, history_refresh_required
from core.settings import now
from core.util import code, number, atomic_json, write_csv, exception_details
from core.broker import Rejected
from automation.policy import BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,legacy_test_release_enabled
from automation.observation_settings import ObservationSettings,load_observation

FIELDS=('flu_rt','cntr_str','now_trde_qty','pred_trde_qty','exp_cntr_qty','trde_prica','bigd_rank','now_rank','base_comp_chgr')

def observation_structure(bars,price,cfg,day):
    strict=structure(bars,price,replace(cfg,max_recent_range=.99),day)
    strict['resistance_room_pass']=not (strict.get('resistance') and price and strict['resistance']/price-1<cfg.min_resistance_room)
    if strict.get('reason')=='상단 저항 여유 부족':
        strict.update(status='ELIGIBLE',reason='A/C 돌파 관찰; B는 상단 저항 여유 검사 유지',compression_deferred='RESISTANCE_ROOM_TO_SETUP',setup_allowlist=['A','C'])
    return strict

def ordinary(symbol,name):
    upper=name.upper()
    return symbol.endswith('0') and '스팩' not in name and not name.endswith('우') and 'ETN' not in upper and not any(
        upper.startswith(x) for x in ('KODEX','TIGER','ACE','RISE','SOL','PLUS','KOSEF','HANARO','ARIRANG','WOORI','TIMEFOLIO','KBSTAR'))

def collect(rest):
    result={}
    rejected_rows=[]
    rest.radar_rejected_rows=rejected_rows
    settings=getattr(rest,'radar_observation_settings',None)
    if not isinstance(settings,ObservationSettings):settings=ObservationSettings()
    # LIVE_EARLY_DISCOVERY: ka10023 (volume surge) is an OPTIONAL extra source of the normal LIVE pipeline.
    # Its rows pass through the identical ordinary()/status/affordability/WATCH/subscription/signal path.
    # Any failure (contract, auth, rate, backoff) is recorded and skipped; the 5 verified RANKS are never blocked.
    sources=list(RANKS)
    early={'enabled':False,'ok':None,'rows':0,'symbols':[],'error':None,'skipped':None}
    if getattr(settings,'live_early_discovery_enabled',False):
        from automation.early_discovery import get_volume_surge_rank_spec, INTRADAY_VOLUME_SURGE_RANK
        from automation.session import after_supported
        early['enabled']=True
        fails=getattr(rest,'_early_fail',(0,0.))
        if fails[0]>=3 and time.monotonic()<fails[1]:
            early['skipped']='BACKOFF_AFTER_REPEATED_FAILURE'
        else:
            is_after = after_supported(now())
            spec = get_volume_surge_rank_spec(session='AFTER' if is_after else 'REGULAR', venue='SOR' if is_after else 'KRX')
            sources.append(spec)
    rest.early_discovery_status=early
    for api,key,limit,payload in sources:
        optional=api=='ka10023'
        path='/api/dostk/stkinfo' if api=='ka00198' else '/api/dostk/rkinfo'
        try:
            if settings.rank_limit_multiplier>1:
                # Wider discovery uses only the already returned first page; no pagination burst.
                data=rest.query(api,payload,path=path)
                if not isinstance(data.get(key),list):raise ValueError(f'{api}: {key} 필드 없음/형식 오류')
                rows=data[key][:min(100,limit*settings.rank_limit_multiplier)]
            else:
                rows,_=rest.pages(api,payload,key,path=path,limit=limit)
        except Exception as error:
            if not optional:raise
            n=getattr(rest,'_early_fail',(0,0.))[0]+1
            rest._early_fail=(n,time.monotonic()+300.)
            early.update(ok=False,error=f'{type(error).__name__}: {error}'[:300])
            continue
        if optional:
            rest._early_fail=(0,0.)
            early.update(ok=True,rows=len(rows))
        for rank,r in enumerate(rows,1):
            try:
                if not isinstance(r,dict):raise ValueError('랭킹 행 형식 오류')
                sym=code(r['stk_cd']); name=r['stk_nm']
                if not isinstance(name,str) or not name.strip():raise ValueError('종목명 결측')
            except (KeyError,TypeError,ValueError) as error:
                # Reject this identity; it must never become a subscription or order.
                # A malformed row is not evidence that the other verified rows failed.
                rejected_rows.append({'api':api,'row_number':rank,'reason':str(error)})
                continue
            if not ordinary(sym,name): continue
            item=result.setdefault(sym,{'code':sym,'name':name,'source_details':{},'reference_price':None})
            if optional and len(early['symbols'])<15:early['symbols'].append({'code':sym,'name':name,'rank':rank})
            detail={'observed_rank':rank,'raw':dict(r),'values':{}}
            for field in FIELDS:
                try: detail['values'][field]=number(r[field],signed=True)
                except (KeyError,ValueError): detail['values'][field]=None
            item['source_details'][api]=detail
            detail['received_at']=now().isoformat()
            item.setdefault('first_raw_at',detail['received_at'])
            # 장중 실제가격 소스가 예상체결가보다 우선한다.
            for field in ('cur_prc','past_curr_prc','exp_cntr_pric'):
                try:
                    px=number(r[field])
                    if px>0 and (field!='exp_cntr_pric' or item['reference_price'] is None): item['reference_price']=px
                    break
                except (KeyError,ValueError): pass
            # 최초 발견 가격은 이후 소스의 더 늦은 현재가로 덮어쓰지 않는다.
            item.setdefault('first_raw_price',item['reference_price'])
    rows=list(result.values())
    if len(rows)>settings.max_raw_symbols:
        rows.sort(key=lambda x:(-len(x['source_details']),min(d['observed_rank'] for d in x['source_details'].values()),x['code']))
    return rows[:settings.max_raw_symbols]

class DynamicRadar:
    def __init__(self,cfg,auto,policy,fallback_source1=False):
        self.cfg=cfg; self.a=auto; self.policy=policy; self.previous={}; self.missing={}; self.weak={}; self.watch={}; self.day=None
        self.available_capital=None
        self.capital_context={}
        self.momentum={}
        self.affordability_ms=0.
        self.fallback_source1=fallback_source1 or getattr(auto,'watch_fallback_source1',False)

    def affordable(self,x):
        price=x.get('reference_price'); budget=self.capital_context.get('effective_auto_cap',self.available_capital)
        x['affordability']={'budget':budget,'one_share_cost':price*(1+self.a.commission_buy) if price else None,
                            'entry_eligible':False if self.a.capital_limit==0 else None,
                            'reason_code':'OBSERVATION_ONLY_ZERO_CAP' if self.a.capital_limit==0 else None}
        if self.a.capital_limit==0: return True
        if self.capital_context and not self.capital_context.get('account_fresh'):
            x['affordability'].update(entry_eligible=None,reason_code='ACCOUNT_CAPITAL_UNAVAILABLE')
            return False
        if budget is None: return True
        ok=price is not None and price>0 and price*(1+self.a.commission_buy)<=budget
        x['affordability'].update(entry_eligible=ok,reason_code='ONE_SHARE_AFFORDABLE' if ok else 'ONE_SHARE_UNAFFORDABLE')
        return ok

    def update(self,items,evaluate,stamp,progress=None):
        items=[x for x in items if not x.get('SHADOW_ONLY') and not x.get('shadow_research_only')]  # research rows never enter LIVE WATCH
        self.affordability_ms=0.
        if self.day!=stamp.date():
            self.previous={}; self.missing={}; self.weak={}; self.watch={}; self.day=stamp.date()
        rows=[]; selected=[]; present=set()
        for original in items:
            x=dict(original); sym=x['code']; present.add(sym); old=self.previous.get(sym)
            sources=x['source_details']; x['sources']='|'.join(sources); x['source_count']=len(sources); deltas={}
            weakened=False; recovered=False
            for api,d in sources.items():
                before=old['source_details'].get(api) if old else None
                delta={'rank_improvement':before['observed_rank']-d['observed_rank'] if before else None,'values':{}}
                for field,value in d['values'].items():
                    prev=before['values'].get(field) if before else None
                    diff=value-prev if value is not None and prev is not None else None
                    delta['values'][field]=diff
                    if field=='flu_rt' and diff is not None:
                        weakened=weakened or diff<=-self.a.weakening_drop_points
                        recovered=recovered or diff>0
                deltas[api]=delta
            if weakened: self.weak[sym]=True
            elif recovered: self.weak[sym]=False
            x['source_deltas']=deltas; x['observed_at']=stamp.isoformat()
            x['delta_period_seconds']=(stamp-old['stamp']).total_seconds() if old else None
            rank_improvement=sum(d['rank_improvement'] or 0 for d in deltas.values())
            x['discovery_score']=len(sources)*10+sum(1/d['observed_rank'] for d in sources.values())+max(0,min(10,rank_improvement))
            afford_started=time.perf_counter();affordable=self.affordable(x);self.affordability_ms+=(time.perf_counter()-afford_started)*1000
            if self.policy.locked(sym): x.update(status='REJECT',reason='HARD_LOCK')
            elif sym in BREAKEVEN_EXITS or (sym==TEST_RELEASE_SYMBOL and legacy_test_release_enabled()): x.update(status='REJECT',reason='EXISTING_POLICY_NO_BUY')
            elif not affordable: x.update(status='UNKNOWN' if x['affordability']['reason_code']=='ACCOUNT_CAPITAL_UNAVAILABLE' else 'REJECT',reason=x['affordability']['reason_code'])
            # 관찰 경쟁은 source1부터. 설정의 source 하한은 실제 주문 guard에서 유지한다.
            elif len(sources)<1 or (not self.fallback_source1 and len(sources)<max(2,self.a.min_source_count)): x.update(status='REJECT',reason='소스 교집합 부족')
            elif self.weak.get(sym): x.update(status='REJECT',reason='동일 소스 등락률 감속; 회복 스냅샷 대기')
            else:
                try: x.update(evaluate(x))
                except (OSError,ValueError,KeyError,RuntimeError) as e: x.update(status='UNKNOWN',reason=type(e).__name__)
            self.previous[sym]={'source_details':sources,'stamp':stamp}; self.missing[sym]=0; rows.append(x)
            # Publish only after the very same compression checks have finished.
            # This never makes RAW/unknown status or missing daily history eligible.
            if progress is not None and x['status']=='ELIGIBLE':
                progress([dict(r) for r in rows if r['status']=='ELIGIBLE'])
        def priority(row):
            observation=self.momentum.get(row['code'],{})
            try:fresh=0<=(stamp-date_time.fromisoformat(observation['timestamp'])).total_seconds()<=15
            except (KeyError,ValueError,TypeError):fresh=False
            gates=observation.get('gates',{}) if fresh else {}
            if self.a.setup_aware_watch_priority:
                from automation.focus_selection import readiness_tier,candidate_priority_tuple,TIER_NAMES,retained_price_block
                tier=readiness_tier(observation,row,stamp.isoformat())
                row['watch_priority_basis']=TIER_NAMES[tier]
                return candidate_priority_tuple(row,tier,row['code'] in self.watch,
                    price_blocked=retained_price_block(observation,stamp.isoformat()))
            # 신선한 가속/유동성 PASS를 우선 관찰한다. 오래된 랭킹만으로 자리를 차지하지 않는다.
            strong=all(gates.get(k,{}).get('result')=='PASS' for k in ('acceleration','liquidity'))
            weak=bool(gates) and any(gates.get(k,{}).get('result')=='FAIL' for k in ('acceleration','liquidity'))
            row['watch_priority_basis']='FRESH_MOMENTUM' if strong else 'FRESH_WEAK' if weak else 'RANK_ONLY'
            effective_score=row['discovery_score']+(2.0 if row['code'] in self.watch else 0.0)
            return (0 if strong else 2 if weak else 1,-effective_score,row.get('overhead_share_estimate',0),row['code'])
        eligible=sorted((x for x in rows if x['status']=='ELIGIBLE'),key=priority)
        selected=eligible[:self.cfg.max_watchlist]
        for x in eligible:
            if x in selected:
                x['status']='WATCH'
                if x.get('source_count')==1:
                    x['fallback_source1']=True
                    x['watch_type']='FALLBACK_SOURCE1'
            else:
                x['status']='RESERVE'
        lookup={x['code']:x for x in selected}
        for sym,old in self.watch.items():
            if sym in present: continue
            if not self.affordable(old): continue
            self.missing[sym]=self.missing.get(sym,0)+1
            if self.missing[sym]<self.a.rank_decay_cycles and len(lookup)<self.cfg.max_watchlist:
                lookup[sym]={**old,'reason':'랭킹 누락 유예','source_deltas':{},'observed_at':stamp.isoformat()}
        changes={'added':sorted(set(lookup)-set(self.watch)),'removed':sorted(set(self.watch)-set(lookup))}
        self.watch=lookup
        return rows,list(lookup.values()),changes

class RadarWorker:
    def __init__(self,cfg,auto,policy,rest,root,fallback_source1=True):
        self.cfg=cfg; self.a=auto; self.rest=rest; self.root=Path(root); self.radar=DynamicRadar(cfg,auto,policy,fallback_source1=fallback_source1)
        self.observation_settings=load_observation(root)
        rest.radar_observation_settings=self.observation_settings
        self.q=queue.Queue(maxsize=2); self.stop=threading.Event(); self.request=threading.Event(); self.cache={}; self.fetched=0
        self.extra_symbols=set()
        self.status_priority_symbols=frozenset()  # Actual main-thread WATCH, including fast promotions.
        self.available_capital=0.; self.status_fetched=0; self.status_cache={}
        self.capital_context={}; self.refresh_account=None
        self.status_waiting={};self.status_sequence=0
        self.thread=threading.Thread(target=self.run,name='rank-radar',daemon=True)
        self.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.};self.cycle_samples=[]
        self.observation_sink=None;self.observation_allowed=True
        from automation.market_context import DiscoveryContext
        self.discovery_context=DiscoveryContext(limit=self.observation_settings.max_raw_symbols*2,
                                                path=self.root/'runtime/discovery_context.json')

    def status_needs_query(self,symbol,at=None):
        status=self.status_cache.get(symbol)
        try:age=((at or now())-date_time.fromisoformat(status['checked_at'])).total_seconds()
        except (KeyError,TypeError,ValueError):return True
        ttl=self.a.scan_seconds*(2 if symbol in self.status_priority_symbols else 3)
        return not -1<=age<=ttl

    def scheduled_items(self,items):
        """Incumbent deadlines first; bounded FIFO admission for unqueried RAW.

        API-denied rows keep their position across cycles, rather than losing
        to the same high-source rows every time a status TTL expires.
        Affordability is still checked by DynamicRadar before any API call.
        """
        present={r['code'] for r in items}
        self.status_waiting={s:n for s,n in self.status_waiting.items() if s in present}
        ranked=sorted(enumerate(items),key=lambda pair:(-len(pair[1]['source_details']),pair[0]))
        for _,r in ranked:
            if r['code'] not in self.status_waiting:
                self.status_waiting[r['code']]=self.status_sequence;self.status_sequence+=1
        active=self.status_priority_symbols
        ordered=sorted(items,key=lambda r:(r['code'] not in active,r['code'] not in self.radar.watch,
                                         self.status_waiting[r['code']]))
        if not self.a.structural_breakout_enabled:return ordered
        moving=[]
        for row in ordered:
            sym=row['code'];status=self.status_cache.get(sym)
            # A valid fresh cache never uses one of the reserved admission slots.
            if status and status.get('checked_at'):
                try:
                    if 0<=(now()-date_time.fromisoformat(status['checked_at'])).total_seconds()<=self.a.scan_seconds*3:continue
                except (ValueError,TypeError):pass
            gain=row.get('discovery_context',{}).get('last_cycle_return')
            gain=gain if type(gain) in (float,int) and gain>0 else 0
            try:
                first=row.get('first_raw_at') or row.get('discovery_context',{}).get('first_raw_at')
                recent=0<=(now()-date_time.fromisoformat(first)).total_seconds()<=4*self.a.scan_seconds
            except (TypeError,ValueError):recent=False
            rising_rank=any(type(d.get('values',{}).get('flu_rt')) in (float,int) and d['values']['flu_rt']>0 for d in row['source_details'].values())
            multi_source=len(row.get('source_details',{}))>=2
            mom=getattr(self.radar,'momentum',{}).get(sym,{})
            shadow_ready=bool(mom.get('fast_signal_ready') or mom.get('tier') in (0,1,2))
            tier_bonus=1.0 if mom.get('fast_signal_ready') else 0.5 if mom.get('tier') in (0,1) else 0.2 if mom.get('tier')==2 else 0.0
            if sym not in active and sym not in self.radar.watch and (gain>0 or (recent and rising_rank) or multi_source or shadow_ready):
                moving.append((gain+tier_bonus,len(row.get('source_details',{})),-self.status_waiting[sym],sym))
        clock=now()
        needs={r['code'] for r in ordered if self.status_needs_query(r['code'],clock)}
        active_needing=len(needs & active)
        slots=max(0,self.a.max_scan_charts-active_needing)
        waiting=[r['code'] for r in ordered if r['code'] in needs and r['code'] not in active
                 and not self.radar.policy.locked(r['code'])
                 and r['code'] not in BREAKEVEN_EXITS and r['code']!=TEST_RELEASE_SYMBOL
                 and not self.radar.weak.get(r['code']) and self.radar.affordable(dict(r))]
        # The existing four admission slots are a FIFO floor, not four extra
        # requests. Previously all spare slots could go to repeatedly rising
        # rows, so an older affordable row never received its first status scan.
        aged=set(waiting[:min(4,slots)])
        hot=set([sym for *_,sym in sorted(moving,reverse=True)
                 if sym in needs and sym not in aged and sym in waiting][:max(0,slots-len(aged))])
        # Cache hits do not consume scan slots or wait behind slow status HTTP.
        # Only the actual current WATCH gets deadline priority over aging RAW.
        return sorted(ordered,key=lambda r:(r['code'] not in active,
            r['code'] in needs,r['code'] not in hot,r['code'] not in aged,self.status_waiting[r['code']]))

    def get_cached_metadata(self,symbol):
        day=now().date()
        bars=self.cache.get((day,symbol))
        status=self.status_cache.get(symbol)
        return {'bars':bars,'market_status':status}

    def evaluate(self,item):
        day=now().date(); sym=item['code']; key=(day,sym)
        cached_status=None if self.status_needs_query(sym) else self.status_cache[sym]
        if cached_status is not None:status=cached_status
        if cached_status is None:
            if self.status_fetched>=self.a.max_scan_charts: return {'status':'UNKNOWN','reason':'STATUS_SCAN_LIMIT'}
            self.status_fetched+=1
            status_started=time.perf_counter();status=self.rest.stock_status(sym);self.timings['status_ms']+=(time.perf_counter()-status_started)*1000
            self.status_cache[sym]=status
            self.status_waiting.pop(sym,None)
        if status['pass'] is not True:
            return {'status':'REJECT' if status['pass'] is False else 'UNKNOWN','reason':status['reason_code'],'market_status':status}
        history_started=time.perf_counter()
        if key not in self.cache:
            path=self.root/'data/raw'/f'{sym}_daily.csv'
            bars=bars_from_csv(path,day) if path.exists() else []
            fresh=bars and (day-date.fromisoformat(bars[-1]['datetime'])).days<=self.cfg.max_history_age_days and not history_refresh_required(bars,day)
            if not fresh or len(bars)<self.cfg.min_history_bars:
                if self.fetched>=self.a.max_scan_charts: return {'status':'UNKNOWN','reason':'이번 주기 차트 조회 한도; 다음 주기 재시도'}
                bars=fetch_bars(self.rest,sym,self.cfg,self.root/'data/raw',day); self.fetched+=1
            self.cache[key]=bars
        self.timings['history_ms']+=(time.perf_counter()-history_started)*1000
        # 상승폭 자체로 강자를 배제하지 않는다. 가격구조/호가/손절거리는 신호 단계에서 검사한다.
        effective=replace(self.cfg,max_recent_range=0.99)
        structure_started=time.perf_counter();result=observation_structure(self.cache[key],item['reference_price'],effective,day);self.timings['structure_ms']+=(time.perf_counter()-structure_started)*1000
        result['reference_basis']='랭킹 현재가; 일봉은 전일까지'
        result['market_status']=status
        return result

    def emit(self,result):
        try:self.q.put_nowait(result)
        except queue.Full:
            # The main thread can drain after the producer detects a full queue.
            # A discovery notification must not turn that harmless race into a
            # RADAR_UNAVAILABLE error or block the receiver/position manager.
            try:self.q.get_nowait()
            except queue.Empty:pass
            try:self.q.put_nowait(result)
            except queue.Full:pass

    def run(self):
        while not self.stop.is_set():
            cycle_started_mono=time.monotonic()
            stage='RANK_COLLECT'
            try:
                cycle_started=time.perf_counter()
                self.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.}
                prior_profile=dict(getattr(self.rest,'profile',{}))
                if self.refresh_account: self.refresh_account()
                self.fetched=0; self.status_fetched=0; fetch_started=time.perf_counter();items=collect(self.rest)
                try:
                    status=dict(getattr(self.rest,'early_discovery_status',{}) or {})
                    if status.get('enabled'):append_jsonl(self.root/'runtime/early_discovery_live.jsonl',{'time':now().isoformat(),**status})
                except Exception:pass
                stage='DISCOVERY_CONTEXT'
                items=self.discovery_context.update(items,now())
                rank_ms=(time.perf_counter()-fetch_started)*1000
                self.radar.available_capital=self.available_capital
                self.radar.capital_context=dict(self.capital_context)
                context=self.radar.capital_context
                if context and time.monotonic()-context.get('last_account',0)>context.get('reconcile_seconds',5)*3:
                    context['account_fresh']=False
                raw_rows=[]
                for item in items:
                    row={**item,'source_count':len(item['source_details']),'status':'RAW','reason':'진입 허가 전 raw 발견','observed_at':now().isoformat(),
                        'discovery_score':len(item['source_details'])*10+sum(1/x['observed_rank'] for x in item['source_details'].values())}
                    self.radar.affordable(row);raw_rows.append(row)
                raw_stamp=now().isoformat()
                self.emit({'phase':'RAW','time':raw_stamp,'rows':raw_rows,'capital_context':dict(context),'error':None})
                if self.observation_sink and self.observation_allowed:self.observation_sink(raw_rows,raw_stamp,dict(context))
                stage='CANDIDATE_COMPRESSION'
                for sym in sorted(self.extra_symbols): self.evaluate({'code':sym,'reference_price':None})
                # Refresh incumbent WATCH status within the unchanged API budget first.
                # A stale/unknown status still fails; query scheduling must not create a gap.
                # New candidates retain their existing source-count ordering afterwards.
                active_watch=self.status_priority_symbols  # Immutable snapshot; no cross-thread detector access.
                items=self.scheduled_items(items)
                compress_started=time.perf_counter()
                opening=now().weekday()<5 and '09:00:00'<=now().strftime('%H:%M:%S')<'09:10:00'
                last_progress=[None]
                def qualified_progress(verified):
                    mono=time.monotonic()
                    # Cached batches can contain hundreds of eligible rows. The
                    # final packet includes all of them; do not flood the main
                    # quote/position loop with a packet for every cheap cache hit.
                    if last_progress[0] is not None and mono-last_progress[0]<.25:return
                    last_progress[0]=mono
                    self.emit({'phase':'QUALIFIED_PROGRESS','time':now().isoformat(),
                        'source_observed_at':raw_stamp,'rows':verified,'raw_rows':raw_rows,
                        'capital_context':dict(context),'error':None,
                        'basis':'EXISTING_COMPRESSION_CHECKS_COMPLETE'})
                rows,watch,changes=self.radar.update(items,self.evaluate,now(),
                    progress=qualified_progress if opening else None)
                cycle_ms=(time.perf_counter()-cycle_started)*1000;self.cycle_samples=(self.cycle_samples+[cycle_ms])[-100:]
                payload={'time':now().isoformat(),'source_observed_at':raw_stamp,'rows':rows,'watch':watch,'changes':changes,'capital_context':dict(context),'error':None,
                    'performance':{'rank_ms':rank_ms,'compression_ms':(time.perf_counter()-compress_started)*1000,'cycle_ms':cycle_ms,
                        'cycle_mean_ms':sum(self.cycle_samples)/len(self.cycle_samples),'cycle_max_ms':max(self.cycle_samples),
                        'affordability_ms':self.radar.affordability_ms,**self.timings,
                        'api_wait_ms':getattr(self.rest,'profile',{}).get('api_wait_ms',0)-prior_profile.get('api_wait_ms',0),
                        'http_ms':getattr(self.rest,'profile',{}).get('http_ms',0)-prior_profile.get('http_ms',0),
                        'status_calls':self.status_fetched,'daily_fetches':self.fetched,'scan_wait_seconds':self.a.scan_seconds},
                    'raw_definition':{'sources':[r[0] for r in RANKS],'basis':'5개 ranking 합집합; 일반주 명칭/종목코드 검사 후 고유종목','hts_0168_equivalence':False,
                        'rank_limit_multiplier':self.observation_settings.rank_limit_multiplier,'max_raw_symbols':self.observation_settings.max_raw_symbols,
                        'expanded_first_page_only':self.observation_settings.rank_limit_multiplier>1}}
                rejected=getattr(self.rest,'radar_rejected_rows',[])
                payload['rejected_rank_rows']=rejected if isinstance(rejected,list) else []
                stage='PUBLICATION'
                atomic_json(self.root/'runtime/radar_latest.json',payload)
                write_csv(self.root/'candidate_audit.csv',rows)
                write_csv(self.root/'symbols_today.csv',watch)
                atomic_json(self.root/'watchlist.json',{'asof':now().date().isoformat(),'offline':False,'config_hash':self.cfg.fingerprint(),'items':watch})
                append_jsonl(self.root/'runtime/radar_history.jsonl',payload)
                self.emit(payload)
            except Exception as e:
                error={'time':now().isoformat(),**exception_details(e),'stage':stage}
                if isinstance(e,Rejected):error['error']=str(e)
                if isinstance(e,Rejected):
                    error.update(api=e.api,raw_response=e.response,
                        return_code=e.response.get('return_code') if e.response else None,
                        return_msg=e.response.get('return_msg') if e.response else None)
                self.emit(error)
            elapsed=time.monotonic()-cycle_started_mono
            wait_time=max(0.,self.a.scan_seconds-elapsed)
            self.request.wait(wait_time); self.request.clear()
            if self.stop.is_set(): break
    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.request.set(); self.thread.join(timeout=1)

