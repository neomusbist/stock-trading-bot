"""랭킹은 발견 레이더다. 동일 소스의 연속 스냅샷을 비교하고 일봉 구조를 붙인다."""
import json
import queue
import threading
import time
from datetime import date
from dataclasses import replace
from pathlib import Path
from candidate_builder import RANKS, bars_from_csv, fetch_bars, structure
from core.settings import now
from core.util import code, number, atomic_json, write_csv
from core.broker import Rejected
from automation.policy import BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,legacy_test_release_enabled

FIELDS=('flu_rt','cntr_str','now_trde_qty','pred_trde_qty','exp_cntr_qty','trde_prica','bigd_rank','now_rank','base_comp_chgr')

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
            # 장중 실제가격 소스가 예상체결가보다 우선한다.
            for field in ('cur_prc','past_curr_prc','exp_cntr_pric'):
                try:
                    px=number(r[field])
                    if px>0 and (field!='exp_cntr_pric' or item['reference_price'] is None): item['reference_price']=px
                    break
                except (KeyError,ValueError): pass
    return list(result.values())

class DynamicRadar:
    def __init__(self,cfg,auto,policy):
        self.cfg=cfg; self.a=auto; self.policy=policy; self.previous={}; self.missing={}; self.weak={}; self.watch={}; self.day=None
        self.available_capital=None
        self.capital_context={}

    def affordable(self,x):
        price=x.get('reference_price'); budget=self.available_capital
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
            affordable=self.affordable(x)
            if self.policy.locked(sym): x.update(status='REJECT',reason='HARD_LOCK')
            elif sym in BREAKEVEN_EXITS or (sym==TEST_RELEASE_SYMBOL and legacy_test_release_enabled()): x.update(status='REJECT',reason='EXISTING_POLICY_NO_BUY')
            elif not affordable: x.update(status='UNKNOWN' if x['affordability']['reason_code']=='ACCOUNT_CAPITAL_UNAVAILABLE' else 'REJECT',reason=x['affordability']['reason_code'])
            elif len(sources)<self.a.min_source_count: x.update(status='REJECT',reason='소스 교집합 부족')
            elif self.weak.get(sym): x.update(status='REJECT',reason='동일 소스 등락률 감속; 회복 스냅샷 대기')
            else:
                try: x.update(evaluate(x))
                except (OSError,ValueError,KeyError,RuntimeError) as e: x.update(status='UNKNOWN',reason=type(e).__name__)
            self.previous[sym]={'source_details':sources,'stamp':stamp}; self.missing[sym]=0; rows.append(x)
        eligible=sorted((x for x in rows if x['status']=='ELIGIBLE'),key=lambda x:(-x['discovery_score'],x['overhead_share_estimate'],x['code']))
        selected=eligible[:self.cfg.max_watchlist]
        for x in eligible: x['status']='WATCH' if x in selected else 'RESERVE'
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
    def __init__(self,cfg,auto,policy,rest,root):
        self.cfg=cfg; self.a=auto; self.rest=rest; self.root=Path(root); self.radar=DynamicRadar(cfg,auto,policy)
        self.q=queue.Queue(maxsize=2); self.stop=threading.Event(); self.request=threading.Event(); self.cache={}; self.fetched=0
        self.extra_symbols=set()
        self.available_capital=0.; self.status_fetched=0
        self.capital_context={}; self.refresh_account=None
        self.thread=threading.Thread(target=self.run,name='rank-radar',daemon=True)

    def evaluate(self,item):
        day=now().date(); sym=item['code']; key=(day,sym)
        if self.status_fetched>=self.a.max_scan_charts: return {'status':'UNKNOWN','reason':'STATUS_SCAN_LIMIT'}
        self.status_fetched+=1
        status=self.rest.stock_status(sym)
        if status['pass'] is not True:
            return {'status':'REJECT' if status['pass'] is False else 'UNKNOWN','reason':status['reason_code'],'market_status':status}
        if key not in self.cache:
            path=self.root/'data/raw'/f'{sym}_daily.csv'
            bars=bars_from_csv(path,day) if path.exists() else []
            fresh=bars and (day-date.fromisoformat(bars[-1]['datetime'])).days<=self.cfg.max_history_age_days
            if not fresh or len(bars)<self.cfg.min_history_bars:
                if self.fetched>=self.a.max_scan_charts: return {'status':'UNKNOWN','reason':'이번 주기 차트 조회 한도; 다음 주기 재시도'}
                bars=fetch_bars(self.rest,sym,self.cfg,self.root/'data/raw',day); self.fetched+=1
            self.cache[key]=bars
        # 상승폭 자체로 강자를 배제하지 않는다. 가격구조/호가/손절거리는 신호 단계에서 검사한다.
        effective=replace(self.cfg,max_recent_range=0.99)
        result=structure(self.cache[key],item['reference_price'],effective,day)
        result['reference_basis']='랭킹 현재가; 일봉은 전일까지'
        result['market_status']=status
        return result

    def emit(self,result):
        if self.q.full(): self.q.get_nowait()
        self.q.put_nowait(result)

    def run(self):
        while not self.stop.is_set():
            try:
                if self.refresh_account: self.refresh_account()
                self.fetched=0; self.status_fetched=0; items=collect(self.rest)
                self.radar.available_capital=self.available_capital
                self.radar.capital_context=dict(self.capital_context)
                context=self.radar.capital_context
                if context and time.monotonic()-context.get('last_account',0)>context.get('reconcile_seconds',5)*3:
                    context['account_fresh']=False
                for sym in sorted(self.extra_symbols): self.evaluate({'code':sym,'reference_price':None})
                # 일봉 조회 우선순위는 소스 교집합. 소스 원본은 모두 로그에 남긴다.
                items.sort(key=lambda x:-len(x['source_details']))
                rows,watch,changes=self.radar.update(items,self.evaluate,now())
                payload={'time':now().isoformat(),'rows':rows,'watch':watch,'changes':changes,'capital_context':dict(context),'error':None}
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
            self.request.wait(self.a.scan_seconds); self.request.clear()
            if self.stop.is_set(): break
    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.request.set(); self.thread.join(timeout=1)

