"""저우선순위 관찰 작업자. 주문 객체/실제 engine을 소유하지 않는다."""
import json
import queue
import threading
import time
from copy import deepcopy
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from candidate_builder import bars_from_csv, fetch_bars, structure
from core.util import atomic_json, exception_details
from core.event_log import append_jsonl
from automation.policy import HARD_LOCKS, BREAKEVEN_EXITS, TEST_RELEASE_SYMBOL
from automation.shadow_observation import ShadowObserver
from core.settings import now
from automation.observation_settings import load_observation
from automation.radar import observation_structure
from automation.focus_selection import FocusNotification,calculate_tier,unviable_price_geometry

class TickLogger:
    """Buffered JSONL logger for raw SHADOW ticks.

    Guarantees (see tests/test_r6_independent_audit.py):
    - rotated files are numbered .1, .2, ... and are preserved by default;
      when max_rotated_files is explicitly specified, older rotations exceeding the limit are purged;
    - a failed write keeps the rows in memory (bounded by max_buffer_rows) instead of silently
      discarding them; every failure/drop is counted in stats();
    - each row carries the tick time (exchange) and the local receive time separately, plus the
      best-level quote quantities that the receiver actually provides (no deeper book exists).
    """
    def __init__(self, log_path: Path, max_bytes: int = 64 * 1024 * 1024, max_buffer_rows: int = 200_000, max_rotated_files: Optional[int] = None):
        self.path = Path(log_path)
        self.max_bytes = max_bytes
        self.max_buffer_rows = max_buffer_rows
        self.max_rotated_files = max_rotated_files
        self.buffer = []
        self.last_flush = time.monotonic()
        self.retry_after = 0.0
        self.rows_logged = 0
        self.rows_written = 0
        self.rows_dropped = 0
        self.write_errors = 0
        self.rotations = 0
        self.last_error = ''
        self.lock = threading.RLock()
        self.next_sequence = 0
        self.rollback_errors = 0

    @staticmethod
    def _iso(value):
        return value.isoformat() if hasattr(value, 'isoformat') else (None if value is None else str(value))

    def _append(self, row):
        with self.lock:
            self.next_sequence += 1
            row.setdefault('record_sequence', self.next_sequence)
            self.buffer.append(row)
            self.rows_logged += 1
            overflow = len(self.buffer) - self.max_buffer_rows
            if overflow > 0:
                del self.buffer[:overflow]
                self.rows_dropped += overflow
            # Capture never performs file I/O. The worker's periodic flush and
            # explicit close own writes; a burst cannot stall its receiver.
            return True

    def log(self, tick, received, processed=None, sequence=None):
        depth = getattr(tick, 'depth_available', None) is True
        return self._append({
            'event': 'TRADE', 'transport_sequence': sequence,
            'symbol': getattr(tick, 'symbol', ''),
            'time': self._iso(tick.time),
            'received': self._iso(received),
            'exchange_at': self._iso(tick.time),
            'receiver_at': self._iso(received),
            'processed_at': self._iso(processed),
            'price': getattr(tick, 'price', 0.0),
            'volume': getattr(tick, 'volume', 0),
            'cumulative': getattr(tick, 'cumulative', None),
            'bid': getattr(tick, 'bid', 0.0),
            'ask': getattr(tick, 'ask', 0.0),
            'bid_qty': getattr(tick, 'bid_qty', None) if depth else None,
            'ask_qty': getattr(tick, 'ask_qty', None) if depth else None,
            'depth_available': getattr(tick, 'depth_available', None),
            'depth_source': getattr(tick, 'depth_source', None),
            'book_time': self._iso(getattr(tick, 'book_time', None)),
            'book_received_at': self._iso(getattr(tick, 'book_received_at', None)),
            'book_received_monotonic': getattr(tick, 'book_received_monotonic', None),
            'venue': getattr(tick, 'venue', None),
            'venue_basis': getattr(tick, 'venue_basis', 'RECEIVER_PARSED_NOT_INDEPENDENTLY_VERIFIED'),
            'subscription_item': getattr(tick, 'subscription_item', None),
            'raw_market_fields': getattr(tick, 'raw_market_fields', None),
            'regular': getattr(tick, 'regular', None),
            'vi_active': getattr(tick, 'vi_active', None),
            'depth_status': 'AVAILABLE' if depth else 'UNKNOWN_NOT_PROVIDED_OR_UNUSABLE',
        })

    def log_book(self, symbol, book, received, processed=None, sequence=None):
        depth = book.get('depth_available') is True
        raw = book.get('raw_fields', {})
        return self._append({'event': 'BOOK', 'symbol': symbol, 'transport_sequence': sequence,
            'time': book.get('time'), 'received': self._iso(received),
            'exchange_at': book.get('time') if raw.get('21') not in (None,'') else None, 'receiver_at': self._iso(received),
            'quote_time_basis': 'EXCHANGE_FIELD_21' if raw.get('21') not in (None,'') else 'UNKNOWN_NO_EXCHANGE_TIME',
            'subscription_item': book.get('subscription_item'),
            'processed_at': self._iso(processed), 'bid': book.get('best_bid'), 'ask': book.get('best_ask'),
            'bid_qty': book.get('bid_qty') if depth else None,
            'ask_qty': book.get('ask_qty') if depth else None,
            'total_bid_qty': book.get('total_bid_qty') if raw.get('125') not in (None, '') else None,
            'total_ask_qty': book.get('total_ask_qty') if raw.get('121') not in (None, '') else None,
            'depth_available': depth, 'depth_source': book.get('source'), 'raw_fields': raw,
            'depth_status': 'AVAILABLE' if depth else 'UNKNOWN_NOT_PROVIDED_OR_UNUSABLE'})

    def flush(self, force: bool = False) -> bool:
        with self.lock:
            return self._flush(force)

    def _flush(self, force: bool = False) -> bool:
        if not self.buffer:
            return True
        if not force and time.monotonic() < self.retry_after:
            return False
        try:
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                index = 1
                while self.path.with_name(f'{self.path.name}.{index}').exists():
                    index += 1
                self.path.rename(self.path.with_name(f'{self.path.name}.{index}'))
                self.rotations += 1
                if self.max_rotated_files is not None and self.max_rotated_files > 0:
                    for i in range(1, index):
                        if index - i >= self.max_rotated_files:
                            old_f = self.path.with_name(f'{self.path.name}.{i}')
                            try:
                                if old_f.exists(): old_f.unlink()
                            except OSError: pass
            payload = '\n'.join(json.dumps(row, ensure_ascii=False) for row in self.buffer) + '\n'
            with open(self.path, 'a+', encoding='utf-8') as f:
                original = f.tell()
                try:
                    f.write(payload)
                    f.flush()
                except Exception:
                    try:
                        f.seek(original); f.truncate(); f.flush()
                    except Exception:
                        self.rollback_errors += 1
                    raise
        except Exception as error:
            self.write_errors += 1
            self.last_error = f'{type(error).__name__}: {error}'[:200]
            self.retry_after = time.monotonic() + 1.0
            return False
        self.rows_written += len(self.buffer)
        self.buffer.clear()
        self.last_flush = time.monotonic()
        self.retry_after = 0.0
        return True

    def stats(self) -> dict:
        with self.lock:
            return {'rows_logged': self.rows_logged, 'rows_written': self.rows_written, 'rows_buffered': len(self.buffer),
                'rows_dropped': self.rows_dropped, 'write_errors': self.write_errors, 'rotations': self.rotations,
                'rollback_errors': self.rollback_errors, 'last_error': self.last_error}

class ObservationWorker:
    def __init__(self,cfg,auto,costs,root,rest=None,limit=32,on_fast_signal=None,on_processed_tick=None,on_stream_reset=None):
        self.cfg=cfg; self.auto=auto; self.root=Path(root); self.rest=rest;self.settings=load_observation(root);self.clock=now
        if limit==32:limit=self.settings.max_symbols
        self.folder=self.root/'runtime'; self.folder.mkdir(parents=True,exist_ok=True)
        self.observer=ShadowObserver(cfg,auto,costs,self.event,limit=limit)
        self.observer.detector.continuous_observation=True
        self.observer.eval_seconds=self.settings.evaluation_seconds
        self.tick_logger=TickLogger(self.folder/'shadow_ticks.jsonl')
        self.updates=queue.Queue(maxsize=1); self.prepared=queue.Queue(maxsize=1); self.ticks=queue.Queue(maxsize=4096)
        self.focus_notifications=queue.Queue(maxsize=32)
        self.stop=threading.Event(); self.lock=threading.Lock(); self.event_lock=threading.Lock(); self.snapshot={}
        self.delivery_lock=threading.Lock()
        self.runtime_snapshot={'symbols':(), 'evaluations':{}}
        self.metadata={}; self.daily_cache=OrderedDict(); self.dropped=0; self.apis=0; self.tick_ms=0.; self.prepare_ms=0.
        self.input_sequence=0; self.processed_sequence=0; self.queue_gap_ranges=[]
        self.coverage_barrier=0
        self.on_fast_signal=on_fast_signal; self.on_processed_tick=on_processed_tick
        self.on_stream_reset=on_stream_reset
        self.thread=threading.Thread(target=self.run,name='shadow-diagnostic',daemon=True)
        self.metadata_thread=threading.Thread(target=self.prepare_loop,name='shadow-metadata',daemon=True)
    def event(self,kind,**data):
        record={'event':kind,**data}
        with self.event_lock:
            append_jsonl(self.folder/'shadow_events.jsonl',record,default=str)
        if kind=='SHADOW_FAST_SIGNAL' and self.on_fast_signal:
            self.on_fast_signal(record)
        if kind=='SHADOW_STREAM_RESET' and self.on_stream_reset:self.on_stream_reset(record)
        if kind=='SHADOW_EVAL':
            momentum={'timestamp':data['timestamp'],'gates':{key:{'result':data.get('gates',{}).get(key,{}).get('result')}
                for key in ('acceleration','liquidity')}}
            fast_ready=bool(data.get('fast_signal_ready'))
            momentum['tier']=0 if fast_ready else calculate_tier(data.get('gates',{}),data.get('setup_trials',[]))
            momentum['price_blocked']=False if fast_ready else unviable_price_geometry(data)
            momentum['fast_signal_ready']=fast_ready
            with self.lock:
                self.snapshot.setdefault('evaluations',{})[data['symbol']]=record
                self.runtime_snapshot['evaluations'][data['symbol']]=momentum
        elif kind=='OBSERVATION_READINESS_CHANGED':
            sym=data.get('symbol','')
            if sym:
                with self.lock:
                    prior_eval=dict(self.runtime_snapshot['evaluations'].get(sym,{}))
                    prior_eval['timestamp']=data.get('timestamp',self.clock().isoformat())
                    prior_eval['tier']=data.get('tier',3)
                    if data.get('fast_signal_ready'):
                        prior_eval['price_blocked']=False
                        prior_eval['fast_signal_ready']=True
                    self.runtime_snapshot['evaluations'][sym]=prior_eval
            notif=FocusNotification(
                symbol=sym,
                event_seq=data.get('event_seq',0),
                observation_seq=data.get('observation_seq',0),
                transport_epoch=str(data.get('transport_epoch','')),
                detected_at=data.get('timestamp',self.clock().isoformat()),
                tier=data.get('tier',3),
                readiness_kind=data.get('readiness_kind','TIER_CHANGED')
            )
            self.latest_put(self.focus_notifications,notif)
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
        return self._deliver('TRADE',tick,received)
    def deliver_book(self,symbol,book,received):
        return self._deliver('BOOK',(symbol,dict(book)),received)
    def deliver_reset(self,reason,received,symbol=None):
        return self._deliver('RESET',(reason,symbol),received)
    def _deliver(self,kind,item,received):
        with self.delivery_lock:
            self.input_sequence+=1; sequence=self.input_sequence
            try:
                if self.stop.is_set():raise queue.Full
                self.ticks.put_nowait((kind,item,received,sequence)); return True
            except queue.Full:
                self.dropped+=1
                if self.queue_gap_ranges and self.queue_gap_ranges[-1][1]==sequence-1:self.queue_gap_ranges[-1][1]=sequence
                else:self.queue_gap_ranges.append([sequence,sequence])
                self.queue_gap_ranges=self.queue_gap_ranges[-128:]
                return False  # Diagnostic queue only. Never changes LIVE order state.
    def delivery_state(self):
        with self.delivery_lock:
            return {'input_sequence':self.input_sequence,'dropped':self.dropped,
                    'missing_sequence_ranges':[r[:] for r in self.queue_gap_ranges]}
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
    def daily_bars(self,symbol,path,day):
        """Metadata-thread-only cache: day + file identity; never account/status/permission."""
        try:info=path.stat()
        except FileNotFoundError:
            self.daily_cache.pop(symbol,None);return []
        key=(day,info.st_mtime_ns,info.st_size,info.st_ino,info.st_dev)
        cached=self.daily_cache.get(symbol)
        if cached and cached[0]==key:
            self.daily_cache.move_to_end(symbol);return cached[1]
        bars=bars_from_csv(path,day)
        after=path.stat()
        if (after.st_mtime_ns,after.st_size,after.st_ino,after.st_dev)==key[1:]:
            self.daily_cache[symbol]=(key,bars)
            self.daily_cache.move_to_end(symbol)
            # Bounded I/O optimization only. Eviction does not release observation/history.
            while len(self.daily_cache)>2*self.observer.limit:self.daily_cache.popitem(last=False)
        else:self.daily_cache.pop(symbol,None)
        return bars
    def prepare(self,rows,stamp,capital,allow_metadata=True,publish=True,progress=None):
        started=time.perf_counter(); clock=datetime.fromisoformat(stamp) if isinstance(stamp,str) else stamp
        eligible=[r for r in rows if r.get('affordability',{}).get('entry_eligible') is True
            and r['code'] not in HARD_LOCKS and r['code'] not in BREAKEVEN_EXITS and r['code']!=TEST_RELEASE_SYMBOL]
        # 미관찰 종목도 순서대로 확인하고 WATCH 관찰은 유지한다.
        eligible.sort(key=lambda r:(r.get('status')!='WATCH',r['code'] in self.metadata,
            -r.get('discovery_score',0),r['code']))
        def admit(row,saved):
            # Admission collects observations only. Missing daily structure stays
            # UNKNOWN; this does not make a raw candidate eligible for an order.
            row['shadow_view']={**row.get('shadow_view',{'status':'UNKNOWN'}),'market_status':saved['status']}
        # Publish already verified candidates before unrelated REST/chart calls.
        for row in eligible:
            saved=self.metadata.get(row['code'])
            if row.get('market_status'):
                saved={**(saved or {}),'checked':clock,'status':row['market_status']};self.metadata[row['code']]=saved
            if saved and 0<=(clock-saved['checked']).total_seconds()<=self.settings.metadata_ttl_seconds:
                admit(row,saved)
        if progress:progress(deepcopy(rows),clock,dict(capital))
        attempts=0;chart_attempted=False
        for row in eligible:
            sym=row['code']; saved=self.metadata.get(sym)
            if row.get('market_status'):
                saved={**(saved or {}),'checked':clock,'status':row['market_status']}; self.metadata[sym]=saved
            if saved is None or (clock-saved['checked']).total_seconds()>self.settings.metadata_ttl_seconds:
                if not self.rest or not allow_metadata or attempts>=self.settings.metadata_budget:continue
                # 같은 API 키의 호출 대기를 제한한다. 봇 포지션/미체결 중에는 새 진단 조회를 시작하지 않는다.
                attempts+=1; self.apis+=1
                try:
                    status=self.rest.stock_status(sym)
                    saved={**(saved or {}),'checked':clock,'status':status}; self.metadata[sym]=saved
                except Exception as error:
                    self.event('SHADOW_METADATA_ERROR',timestamp=clock.isoformat(),symbol=sym,SHADOW_ONLY=True,**exception_details(error));continue
            if saved['status'].get('pass') is not True:continue
            admit(row,saved)
            if progress:progress(deepcopy(rows),clock,dict(capital))
            path=self.root/'data/raw'/f'{sym}_daily.csv'
            try:
                bars=self.daily_bars(sym,path,clock.date())
                # 독립 일봉 캐시. 갱신당 최대 1종목. LIVE 캐시에 되돌려 쓰지 않는다.
                if len(bars)<self.cfg.min_history_bars and self.rest and allow_metadata and not chart_attempted and saved.get('bars') is None and (clock-saved.get('last_chart_attempt',clock.replace(year=clock.year-1))).total_seconds()>=30:
                    chart_attempted=True;self.apis+=1;saved['last_chart_attempt']=clock
                    bars=fetch_bars(self.rest,sym,self.cfg,self.root/'data/observation',clock.date());saved['bars']=bars
                if not bars:bars=saved.get('bars') or []
                struct=observation_structure(bars,row['reference_price'],self.cfg,clock.date())
                row['shadow_view']={**struct,'market_status':saved['status'],
                    'resistance_asof':self.clock().isoformat() if struct.get('resistance') else None}
            except Exception as error:
                row['shadow_view']={'status':'UNKNOWN','market_status':saved['status'],'reason':'SHADOW_HISTORY_UNKNOWN'}
                self.event('SHADOW_METADATA_ERROR',timestamp=clock.isoformat(),symbol=sym,SHADOW_ONLY=True,**exception_details(error))
        self.prepare_ms=(time.perf_counter()-started)*1000
        if publish:self.publish(rows,clock,capital)
        return rows,clock,capital
    def publish(self,rows,source_stamp,capital):
        self.observer.capital=dict(capital)
        ready=self.clock()
        # Incremental metadata publication must not evict an already observed
        # candidate just because its replacement status query has not finished.
        merged=[]
        for row in rows:
            prior=self.observer.rows.get(row['code'])
            status=row.get('shadow_view',row).get('market_status',{})
            saved=self.metadata.get(row['code'],{})
            checked=saved.get('checked')
            fresh=checked is not None and 0<=(ready-checked).total_seconds()<=self.settings.metadata_ttl_seconds
            if prior and not status and fresh and saved.get('status',{}).get('pass') is True:
                row={**row,'shadow_view':{**prior.get('shadow_view',{}),'market_status':saved['status']}}
            merged.append(row)
        self.observer.update(merged,ready)
        with self.lock:
            selected=set(self.observer.rows)
            # Subscription retention consumes this ranked order. A set loses
            # it and can evict stronger warm observations for arbitrary symbols.
            self.runtime_snapshot={'symbols':tuple(self.observer.rows),'evaluations':{s:v for s,v in self.runtime_snapshot['evaluations'].items() if s in selected}}
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
                ready=self.prepare(*item,publish=False,
                    progress=lambda rows,stamp,capital:self.latest_put(self.prepared,(rows,stamp,capital)))
                if not self.stop.is_set():self.latest_put(self.prepared,ready)
            except Exception as error:self.record_error(error)
    def run(self):
        last_save=0.; dropped=0
        while not self.stop.is_set() or not self.ticks.empty():
            try:
                try:self.publish(*self.prepared.get_nowait())
                except queue.Empty:pass
                delivery=self.delivery_state()
                if dropped!=delivery['dropped']:
                    dropped=delivery['dropped'];self.observer.reset_stream('QUEUE_GAP',stamp=self.clock())
                    self.coverage_barrier=max((r[1] for r in delivery['missing_sequence_ranges']),default=delivery['input_sequence'])
                    with self.lock:
                        self.snapshot['evaluations']={};self.runtime_snapshot['evaluations']={}
                    self.event('SHADOW_QUEUE_GAP',timestamp=self.clock().isoformat(),SHADOW_ONLY=True,dropped=dropped,missing_sequence_ranges=delivery['missing_sequence_ranges'],data_action='RESET',actual_order_permission=False)
                try:kind,t,received,sequence=self.ticks.get(timeout=.05)
                except queue.Empty:t=None
                if t is not None and sequence>self.processed_sequence+1:
                    self.observer.reset_stream('TRANSPORT_SEQUENCE_GAP',stamp=self.clock())
                    self.event('SHADOW_SEQUENCE_GAP',timestamp=self.clock().isoformat(),missing_sequence_range=[self.processed_sequence+1,sequence-1],actual_order_permission=False)
                if t is not None and kind=='RESET':
                    self.observer.reset_stream(t[0],symbol=t[1],stamp=self.clock());self.processed_sequence=sequence
                elif t is not None and kind=='BOOK':
                    self.tick_logger.log_book(t[0],t[1],received,processed=self.clock(),sequence=sequence)
                    self.processed_sequence=sequence
                elif t is not None:
                    processed=self.clock()
                    self.tick_logger.log(t, received,processed=processed,sequence=sequence)
                    self.observer.capital=dict(getattr(self,'current_capital',self.observer.capital))
                    started=time.perf_counter()
                    # 큐 대기도 수신 지연에 포함한다. 오래된 틱으로 가상 신호를 만들지 않는다.
                    self.observer.tick(t,self.clock(),transport_received=received,coverage_complete=sequence>self.coverage_barrier);self.tick_ms=(time.perf_counter()-started)*1000
                    if self.on_processed_tick:self.on_processed_tick(t,received,processed)
                    self.processed_sequence=sequence
                if time.monotonic()-last_save>=2:
                    if not self.tick_logger.flush():
                        self.record_error(RuntimeError('SHADOW_TAPE_WRITE_FAILED: '+self.tick_logger.last_error))
                    snapshot=self.state();snapshot['performance']={'prepare_ms':self.prepare_ms,'last_eval_ms':self.tick_ms,'metadata_calls':self.apis,'queue_depth':self.ticks.qsize(),**self.delivery_state(),'processed_sequence':self.processed_sequence,'tick_logger':self.tick_logger.stats()}
                    atomic_json(self.folder/'shadow_snapshot.json',snapshot);last_save=time.monotonic()
            except Exception as error:self.record_error(error)
    def start(self):self.metadata_thread.start();self.thread.start()
    def close(self):
        self.stop.set()
        for thread in (self.thread,self.metadata_thread):
            if thread.ident is not None:thread.join(timeout=5)
        if self.thread.is_alive():
            self.record_error(RuntimeError('SHADOW_CLOSE_WORKER_NOT_DRAINED'))
            raise RuntimeError('SHADOW_CLOSE_WORKER_NOT_DRAINED')
        if not self.tick_logger.flush(force=True):
            self.record_error(RuntimeError('SHADOW_CLOSE_FLUSH_FAILED: '+self.tick_logger.last_error))
            raise RuntimeError('SHADOW_CLOSE_FLUSH_FAILED: '+self.tick_logger.last_error)
        return {'drained':self.ticks.empty(),**self.delivery_state(),
                'processed_sequence':self.processed_sequence,'tick_logger':self.tick_logger.stats()}
