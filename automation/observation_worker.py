"""저우선순위 관찰 작업자. 주문 객체/실제 engine을 소유하지 않는다."""
import json
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from candidate_builder import bars_from_csv, fetch_bars, structure
from core.util import atomic_json, exception_details
from automation.policy import HARD_LOCKS, BREAKEVEN_EXITS, TEST_RELEASE_SYMBOL
from automation.shadow_observation import ShadowObserver
from core.settings import now
from automation.observation_settings import load_observation

class ObservationWorker:
    def __init__(self,cfg,auto,costs,root,rest=None,limit=32):
        self.cfg=cfg; self.auto=auto; self.root=Path(root); self.rest=rest;self.settings=load_observation(root);self.clock=now
        if limit==32:limit=self.settings.max_symbols
        self.folder=self.root/'runtime'; self.folder.mkdir(parents=True,exist_ok=True)
        self.observer=ShadowObserver(cfg,auto,costs,self.event,limit=limit)
        self.observer.detector.continuous_observation=True
        self.observer.eval_seconds=self.settings.evaluation_seconds
        self.updates=queue.Queue(maxsize=1); self.prepared=queue.Queue(maxsize=1); self.ticks=queue.Queue(maxsize=4096)
        self.stop=threading.Event(); self.lock=threading.Lock(); self.event_lock=threading.Lock(); self.snapshot={}
        self.runtime_snapshot={'symbols':(), 'evaluations':{}}
        self.metadata={}; self.dropped=0; self.apis=0; self.tick_ms=0.; self.prepare_ms=0.
        self.thread=threading.Thread(target=self.run,name='shadow-diagnostic',daemon=True)
        self.metadata_thread=threading.Thread(target=self.prepare_loop,name='shadow-metadata',daemon=True)
    def event(self,kind,**data):
        record={'event':kind,**data}
        line=json.dumps(record,ensure_ascii=False,default=str)+'\n'
        with self.event_lock:
            with (self.folder/'shadow_events.jsonl').open('a',encoding='utf-8') as stream:stream.write(line)
        if kind=='SHADOW_EVAL':
            momentum={'timestamp':data['timestamp'],'gates':{key:{'result':data.get('gates',{}).get(key,{}).get('result')}
                for key in ('acceleration','liquidity')}}
            with self.lock:
                self.snapshot.setdefault('evaluations',{})[data['symbol']]=record
                self.runtime_snapshot['evaluations'][data['symbol']]=momentum
    @staticmethod
    def latest_put(target,item):
        try:target.put_nowait(item)
        except queue.Full:
            try:target.get_nowait()
            except queue.Empty:pass
            try:target.put_nowait(item)
            except queue.Full:pass
    def submit(self,rows,stamp,capital,allow_metadata=True):
        item=([dict(row) for row in rows],stamp,dict(capital),allow_metadata)
        self.latest_put(self.updates,item)
    def deliver(self,tick,received):
        try:self.ticks.put_nowait((tick,received))
        except queue.Full:self.dropped+=1  # 진단 큐만 누락 처리. 실제 주문 큐를 HALT시키지 않는다.
    def state(self):
        # 상세 기록 복제는 잠금 밖에서 수행하여 실제 보호 루프의 상태 참조를 기다리게 하지 않는다.
        with self.lock:
            snapshot={**self.snapshot,'evaluations':dict(self.snapshot.get('evaluations',{}))}
        return json.loads(json.dumps(snapshot,default=str))
    def runtime_view(self):
        """실제 관리 루프에는 구독목록과 우선순위용 PASS/FAIL만 전달한다."""
        with self.lock:return {'symbols':self.runtime_snapshot['symbols'],'evaluations':dict(self.runtime_snapshot['evaluations'])}
    def set_capital(self,capital):
        with self.lock:self.snapshot['capital']=dict(capital)
        self.current_capital=dict(capital)
    def prepare(self,rows,stamp,capital,allow_metadata=True,publish=True):
        started=time.perf_counter(); clock=datetime.fromisoformat(stamp) if isinstance(stamp,str) else stamp
        eligible=[r for r in rows if r.get('affordability',{}).get('entry_eligible') is True
            and r['code'] not in HARD_LOCKS and r['code'] not in BREAKEVEN_EXITS and r['code']!=TEST_RELEASE_SYMBOL]
        # 미관찰 종목도 순서대로 확인하고 WATCH 관찰은 유지한다.
        eligible.sort(key=lambda r:(r.get('status')!='WATCH',r['code'] in self.metadata,
            -r.get('discovery_score',0),r['code']))
        attempts=0;chart_attempted=False
        for row in eligible:
            sym=row['code']; saved=self.metadata.get(sym)
            if row.get('market_status'):
                saved={'checked':clock,'status':row['market_status'],'bars':None}; self.metadata[sym]=saved
            if saved is None or (clock-saved['checked']).total_seconds()>self.settings.metadata_ttl_seconds:
                if not self.rest or not allow_metadata or attempts>=self.settings.metadata_budget:continue
                # 같은 API 키의 호출 대기를 제한한다. 봇 포지션/미체결 중에는 새 진단 조회를 시작하지 않는다.
                attempts+=1; self.apis+=1
                try:
                    status=self.rest.stock_status(sym)
                    saved={'checked':clock,'status':status,'bars':None}; self.metadata[sym]=saved
                except Exception as error:
                    self.event('SHADOW_METADATA_ERROR',timestamp=clock.isoformat(),symbol=sym,SHADOW_ONLY=True,**exception_details(error));continue
            if saved['status'].get('pass') is not True:continue
            path=self.root/'data/raw'/f'{sym}_daily.csv'
            try:
                bars=bars_from_csv(path,clock.date()) if path.exists() else []
                # 독립 일봉 캐시. 갱신당 최대 1종목. LIVE 캐시에 되돌려 쓰지 않는다.
                if len(bars)<self.cfg.min_history_bars and self.rest and allow_metadata and not chart_attempted and saved.get('bars') is None and (clock-saved.get('last_chart_attempt',clock.replace(year=clock.year-1))).total_seconds()>=30:
                    chart_attempted=True;self.apis+=1;saved['last_chart_attempt']=clock
                    bars=fetch_bars(self.rest,sym,self.cfg,self.root/'data/observation',clock.date());saved['bars']=bars
                if not bars:bars=saved.get('bars') or []
                row['shadow_view']={**structure(bars,row['reference_price'],replace(self.cfg,max_recent_range=.99,min_resistance_room=0),clock.date()),'market_status':saved['status']}
            except Exception as error:
                row['shadow_view']={'status':'UNKNOWN','market_status':saved['status'],'reason':'SHADOW_HISTORY_UNKNOWN'}
                self.event('SHADOW_METADATA_ERROR',timestamp=clock.isoformat(),symbol=sym,SHADOW_ONLY=True,**exception_details(error))
        self.prepare_ms=(time.perf_counter()-started)*1000
        if publish:self.publish(rows,clock,capital)
        return rows,clock,capital
    def publish(self,rows,source_stamp,capital):
        self.observer.capital=dict(capital)
        ready=self.clock();self.observer.update(rows,ready)
        with self.lock:
            selected=set(self.observer.rows)
            self.runtime_snapshot={'symbols':tuple(selected),'evaluations':{s:v for s,v in self.runtime_snapshot['evaluations'].items() if s in selected}}
            self.snapshot['evaluations']={s:v for s,v in self.snapshot.get('evaluations',{}).items() if s in selected}
            self.snapshot.update(timestamp=ready.isoformat(),source_observed_at=source_stamp.isoformat(),symbols=list(self.observer.rows),rows=list(self.observer.rows.values()),
                capital=dict(capital),limit=self.observer.limit,actual_order_permission=False,SHADOW_ONLY=True)
    def record_error(self,error):
        details=exception_details(error)
        with self.lock:self.snapshot['last_error']={'timestamp':self.clock().isoformat(),**details}
        try:self.event('SHADOW_WORKER_ERROR',timestamp=self.clock().isoformat(),SHADOW_ONLY=True,actual_order_permission=False,**details)
        except Exception as log_error:
            import sys
            print(json.dumps({'event':'SHADOW_LOG_ERROR','original':details,'logging_error':exception_details(log_error)},ensure_ascii=False),file=sys.stderr,flush=True)
    def prepare_loop(self):
        while not self.stop.is_set():
            try:
                try:item=self.updates.get(timeout=.1)
                except queue.Empty:continue
                ready=self.prepare(*item,publish=False)
                if not self.stop.is_set():self.latest_put(self.prepared,ready)
            except Exception as error:self.record_error(error)
    def run(self):
        last_save=0.; dropped=0
        while not self.stop.is_set():
            try:
                try:self.publish(*self.prepared.get_nowait())
                except queue.Empty:pass
                if dropped!=self.dropped:
                    dropped=self.dropped;self.observer.detector.reset()
                    self.observer.latest.clear();self.observer.last_log.clear()
                    with self.lock:
                        self.snapshot['evaluations']={};self.runtime_snapshot['evaluations']={}
                    self.event('SHADOW_QUEUE_GAP',timestamp=datetime.now().astimezone().isoformat(),SHADOW_ONLY=True,dropped=dropped,data_action='RESET',actual_order_permission=False)
                try:t,received=self.ticks.get(timeout=.05)
                except queue.Empty:t=None
                if t is not None:
                    self.observer.capital=dict(getattr(self,'current_capital',self.observer.capital))
                    started=time.perf_counter()
                    # 큐 대기도 수신 지연에 포함한다. 오래된 틱으로 가상 신호를 만들지 않는다.
                    self.observer.tick(t,self.clock());self.tick_ms=(time.perf_counter()-started)*1000
                if time.monotonic()-last_save>=2:
                    snapshot=self.state();snapshot['performance']={'prepare_ms':self.prepare_ms,'last_eval_ms':self.tick_ms,'metadata_calls':self.apis,'queue_depth':self.ticks.qsize(),'dropped':self.dropped}
                    atomic_json(self.folder/'shadow_snapshot.json',snapshot);last_save=time.monotonic()
            except Exception as error:self.record_error(error)
    def start(self):self.metadata_thread.start();self.thread.start()
    def close(self):
        self.stop.set()
        for thread in (self.thread,self.metadata_thread):
            if thread.ident is not None:thread.join(timeout=1)
