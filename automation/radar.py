"""랭킹은 발견 레이더다. 동일 소스의 연속 스냅샷을 비교하고 일봉 구조를 붙인다."""
import json
import queue
import threading
import time
from datetime import date,datetime as date_time
from dataclasses import replace
from pathlib import Path
from candidate_builder import RANKS, bars_from_csv, fetch_bars, structure
from core.settings import now
from core.util import code, number, atomic_json, write_csv
from core.broker import Rejected
from automation.policy import BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,legacy_test_release_enabled

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
    for api,key,limit,payload in RANKS:
        rows,_=rest.pages(api,payload,key,path='/api/dostk/stkinfo' if api=='ka00198' else '/api/dostk/rkinfo',limit=limit)
        for rank,r in enumerate(rows,1):
            sym=code(r['stk_cd']); name=r['stk_nm']
            if not ordinary(sym,name): continue
            item=result.setdefault(sym,{'code':sym,'name':name,'source_details':{},'reference_price':None})
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
    return list(result.values())

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

    def update(self,items,evaluate,stamp):
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
            x['discovery_score']=len(sources)*10+sum(1/d['observed_rank'] for d in sources.values())+max(-10,min(10,rank_improvement))
            afford_started=time.perf_counter();affordable=self.affordable(x);self.affordability_ms+=(time.perf_counter()-afford_started)*1000
            if self.policy.locked(sym): x.update(status='REJECT',reason='HARD_LOCK')
            elif sym in BREAKEVEN_EXITS or (sym==TEST_RELEASE_SYMBOL and legacy_test_release_enabled()): x.update(status='REJECT',reason='EXISTING_POLICY_NO_BUY')
            elif not affordable: x.update(status='UNKNOWN' if x['affordability']['reason_code']=='ACCOUNT_CAPITAL_UNAVAILABLE' else 'REJECT',reason=x['affordability']['reason_code'])
            elif len(sources)<max(2,self.a.min_source_count): x.update(status='REJECT',reason='소스 교집합 부족')
            elif self.weak.get(sym): x.update(status='REJECT',reason='동일 소스 등락률 감속; 회복 스냅샷 대기')
            else:
                try: x.update(evaluate(x))
                except (OSError,ValueError,KeyError,RuntimeError) as e: x.update(status='UNKNOWN',reason=type(e).__name__)
            self.previous[sym]={'source_details':sources,'stamp':stamp}; self.missing[sym]=0; rows.append(x)
        def priority(row):
            observation=self.momentum.get(row['code'],{})
            try:fresh=0<=(stamp-date_time.fromisoformat(observation['timestamp'])).total_seconds()<=15
            except (KeyError,ValueError,TypeError):fresh=False
            gates=observation.get('gates',{}) if fresh else {}
            # 신선한 가속/유동성 PASS를 우선 관찰한다. 오래된 랭킹만으로 자리를 차지하지 않는다.
            strong=all(gates.get(k,{}).get('result')=='PASS' for k in ('acceleration','liquidity'))
            weak=bool(gates) and any(gates.get(k,{}).get('result')=='FAIL' for k in ('acceleration','liquidity'))
            row['watch_priority_basis']='FRESH_MOMENTUM' if strong else 'FRESH_WEAK' if weak else 'RANK_ONLY'
            return (0 if strong else 2 if weak else 1,-row['discovery_score'],row.get('overhead_share_estimate',0),row['code'])
        eligible=sorted((x for x in rows if x['status']=='ELIGIBLE'),key=priority)
        selected=eligible[:self.cfg.max_watchlist]
        for x in eligible: x['status']='WATCH' if x in selected else 'RESERVE'
        lookup={x['code']:x for x in selected}
        for sym,old in self.watch.items():
            if sym in present: continue
            if not self.affordable(old): continue
            self.missing[sym]=self.missing.get(sym,0)+1
            if self.missing[sym]<self.a.rank_decay_cycles and len(lookup)<self.cfg.max_watchlist:
                lookup[sym]={**old,'reason':'랭킹 누락 유예','source_deltas':{},'observed_at':stamp.isoformat()}
        if self.fallback_source1 and len(lookup)<self.cfg.max_watchlist:
            fallbacks=[x for x in rows
                       if x.get('source_count')==1
                       and x.get('affordability',{}).get('entry_eligible') is True
                       and not self.policy.locked(x['code'])
                       and x['code'] not in BREAKEVEN_EXITS
                       and (x['code']!=TEST_RELEASE_SYMBOL or not legacy_test_release_enabled())
                       and not self.weak.get(x['code'])
                       and x['code'] not in lookup]
            fallbacks.sort(key=priority)
            for fb in fallbacks:
                if len(lookup)>=self.cfg.max_watchlist: break
                try: ev=evaluate(fb); fb.update(ev)
                except (OSError,ValueError,KeyError,RuntimeError) as e: fb.update(status='UNKNOWN',reason=type(e).__name__)
                if fb.get('status')=='ELIGIBLE':
                    fb['status']='WATCH'
                    fb['watch_type']='FALLBACK_SOURCE1'
                    fb['fallback_source1']=True
                    fb['reason']='WATCH_FALLBACK_SOURCE1'
                    lookup[fb['code']]=fb
        changes={'added':sorted(set(lookup)-set(self.watch)),'removed':sorted(set(self.watch)-set(lookup))}
        self.watch=lookup
        return rows,list(lookup.values()),changes

class RadarWorker:
    def __init__(self,cfg,auto,policy,rest,root,fallback_source1=True):
        self.cfg=cfg; self.a=auto; self.rest=rest; self.root=Path(root); self.radar=DynamicRadar(cfg,auto,policy,fallback_source1=fallback_source1)
        self.q=queue.Queue(maxsize=2); self.stop=threading.Event(); self.request=threading.Event(); self.cache={}; self.fetched=0
        self.extra_symbols=set()
        self.available_capital=0.; self.status_fetched=0; self.status_cache={}
        self.capital_context={}; self.refresh_account=None
        self.thread=threading.Thread(target=self.run,name='rank-radar',daemon=True)
        self.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.};self.cycle_samples=[]
        self.observation_sink=None;self.observation_allowed=True

    def get_cached_metadata(self,symbol):
        day=now().date()
        bars=self.cache.get((day,symbol))
        status=self.status_cache.get(symbol)
        return {'bars':bars,'market_status':status}

    def evaluate(self,item):
        day=now().date(); sym=item['code']; key=(day,sym)
        cached_status=self.status_cache.get(sym)
        if cached_status and cached_status.get('checked_at'):
            try:age=(now()-date_time.fromisoformat(cached_status['checked_at'])).total_seconds()
            except (ValueError,TypeError):age=float('inf')
            if -1<=age<=self.a.scan_seconds*3:
                status=cached_status
            else:cached_status=None
        else:cached_status=None
        if cached_status is None:
            if self.status_fetched>=self.a.max_scan_charts: return {'status':'UNKNOWN','reason':'STATUS_SCAN_LIMIT'}
            self.status_fetched+=1
            status_started=time.perf_counter();status=self.rest.stock_status(sym);self.timings['status_ms']+=(time.perf_counter()-status_started)*1000
            self.status_cache[sym]=status
        if status['pass'] is not True:
            return {'status':'REJECT' if status['pass'] is False else 'UNKNOWN','reason':status['reason_code'],'market_status':status}
        history_started=time.perf_counter()
        if key not in self.cache:
            path=self.root/'data/raw'/f'{sym}_daily.csv'
            bars=bars_from_csv(path,day) if path.exists() else []
            fresh=bars and (day-date.fromisoformat(bars[-1]['datetime'])).days<=self.cfg.max_history_age_days
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
        if self.q.full(): self.q.get_nowait()
        self.q.put_nowait(result)

    def run(self):
        while not self.stop.is_set():
            cycle_started_mono=time.monotonic()
            try:
                cycle_started=time.perf_counter()
                self.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.}
                prior_profile=dict(getattr(self.rest,'profile',{}))
                if self.refresh_account: self.refresh_account()
                self.fetched=0; self.status_fetched=0; fetch_started=time.perf_counter();items=collect(self.rest)
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
                for sym in sorted(self.extra_symbols): self.evaluate({'code':sym,'reference_price':None})
                # 일봉 조회 우선순위는 소스 교집합. 소스 원본은 모두 로그에 남긴다.
                items.sort(key=lambda x:-len(x['source_details']))
                compress_started=time.perf_counter()
                rows,watch,changes=self.radar.update(items,self.evaluate,now())
                cycle_ms=(time.perf_counter()-cycle_started)*1000;self.cycle_samples=(self.cycle_samples+[cycle_ms])[-100:]
                payload={'time':now().isoformat(),'source_observed_at':raw_stamp,'rows':rows,'watch':watch,'changes':changes,'capital_context':dict(context),'error':None,
                    'performance':{'rank_ms':rank_ms,'compression_ms':(time.perf_counter()-compress_started)*1000,'cycle_ms':cycle_ms,
                        'cycle_mean_ms':sum(self.cycle_samples)/len(self.cycle_samples),'cycle_max_ms':max(self.cycle_samples),
                        'affordability_ms':self.radar.affordability_ms,**self.timings,
                        'api_wait_ms':getattr(self.rest,'profile',{}).get('api_wait_ms',0)-prior_profile.get('api_wait_ms',0),
                        'http_ms':getattr(self.rest,'profile',{}).get('http_ms',0)-prior_profile.get('http_ms',0),
                        'status_calls':self.status_fetched,'daily_fetches':self.fetched,'scan_wait_seconds':self.a.scan_seconds},
                    'raw_definition':{'sources':[r[0] for r in RANKS],'basis':'5개 ranking 합집합; 일반주 명칭/종목코드 검사 후 고유종목','hts_0168_equivalence':False}}
                atomic_json(self.root/'runtime/radar_latest.json',payload)
                write_csv(self.root/'candidate_audit.csv',rows)
                write_csv(self.root/'symbols_today.csv',watch)
                atomic_json(self.root/'watchlist.json',{'asof':now().date().isoformat(),'offline':False,'config_hash':self.cfg.fingerprint(),'items':watch})
                with (self.root/'runtime/radar_history.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(payload,ensure_ascii=False)+'\n')
                self.emit(payload)
            except Exception as e:
                error={'time':now().isoformat(),'error':str(e) if isinstance(e,Rejected) else type(e).__name__}
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

