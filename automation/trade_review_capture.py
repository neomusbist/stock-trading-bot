"""Bounded receiver evidence for actual trades. No broker or strategy decisions.

WATCH data stays in a short memory ring. Only a real entry candidate promotes
that ring to disk; holdings receive priority capture independent of SHADOW.
Existing order/fill/fee logs are linked, not copied into a second ledger.
"""
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
import uuid


def iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


class TradeReviewCapture:
    def __init__(self, folder, *, ring_seconds=45., ring_rows=256,
                 ring_bytes=1024*1024, queue_rows=4096, queue_bytes=4*1024*1024,
                 file_bytes=4*1024*1024, day_bytes=16*1024*1024,
                 total_bytes=64*1024*1024, clock=None, start=True):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda:datetime.now().astimezone())
        self.ring_seconds=ring_seconds;self.ring_rows=ring_rows;self.ring_limit=ring_bytes
        self.queue_rows=queue_rows;self.queue_limit=queue_bytes
        self.file_limit=file_bytes;self.day_limit=day_bytes;self.total_limit=total_bytes
        self.lock=threading.RLock();self.io_lock=threading.Lock();self.stop=threading.Event()
        self.session=uuid.uuid4().hex;self.sequence=0
        self.watch=frozenset();self.held=frozenset();self.promoted={}
        self.rings={};self.ring_size=0;self.pending=deque();self.pending_size=0
        self.losses=0;self.errors=0;self.ring_evictions=0;self.written=0
        self.storage_blocked=False;self.last_error='';self.last_lines={};self.last_bar={}
        self.last_status_write=0.
        self.day='';self.day_written=0;self.total_written=0;self.path=None;self.path_size=0
        own=sorted(self.folder.glob('trade-review-????????-????.jsonl'))
        self.total_written=sum(p.stat().st_size for p in own)
        self._record({'event':'CAPTURE_STARTED','at':iso(self.clock()),
                      'coverage':'STARTS_NOW_NO_RESTORED_TICKS_OR_BARS',
                      'orders_enabled':False}, force=True)
        self.thread=threading.Thread(target=self._run,name='trade-review-writer',daemon=True) if start else None
        if self.thread:self.thread.start()

    def _encode(self,row):
        self.sequence+=1
        return (json.dumps({**row,'capture_session':self.session,'capture_sequence':self.sequence,
            'capture_losses':self.losses},ensure_ascii=False,default=iso,allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')

    def _queue(self,data):
        if (len(data)>32768 or len(self.pending)>=self.queue_rows
                or self.pending_size+len(data)>self.queue_limit):
            self.losses+=1;return False
        self.pending.append(data);self.pending_size+=len(data);return True

    def _record(self,row,symbol=None,force=False):
        try:
            with self.lock:
                now=time.monotonic()
                active=symbol in self.held or self.promoted.get(symbol,0)>now
                if not force and not active and symbol not in self.watch:return
                data=self._encode(row)
                if active or force:
                    self._queue(data);return
                ring=self.rings.setdefault(symbol,deque())
                ring.append((now,data));self.ring_size+=len(data)
                while ring and (len(ring)>self.ring_rows or now-ring[0][0]>self.ring_seconds):
                    self.ring_size-=len(ring.popleft()[1]);self.ring_evictions+=1
                while self.ring_size>self.ring_limit:
                    oldest=min((r[0][0],s) for s,r in self.rings.items() if r)[1]
                    self.ring_size-=len(self.rings[oldest].popleft()[1]);self.ring_evictions+=1
        except Exception as error:
            self.errors+=1;self.last_error=type(error).__name__

    def set_scope(self,watch,positions,at):
        try:
            with self.lock:
                self.watch=frozenset(sorted(set(watch))[:12])
                for symbol in self.held-set(positions):
                    # B/C alternatives may still be holding after the real A
                    # exit. Keep a bounded 120-second tail, never call it a fill.
                    if symbol in self.promoted or len(self.promoted)<8:
                        self.promoted[symbol]=time.monotonic()+120.
                    else:self.losses+=1
                self.held=frozenset(positions)
                current=time.monotonic()
                self.promoted={s:until for s,until in self.promoted.items() if until>current or s in self.held}
                for s in set(self.rings)-self.watch-self.held:
                    self.ring_size-=sum(len(b) for _,b in self.rings.pop(s))
                for mapping in (self.last_lines,self.last_bar):
                    for s in set(mapping)-self.watch-self.held-set(self.promoted):mapping.pop(s,None)
            for symbol,p in positions.items():self.position(symbol,p,at)
        except Exception as error:
            self.errors+=1;self.last_error=type(error).__name__

    def promote(self,symbol,at):
        with self.lock:
            # Short-lived rejected candidates cannot accumulate unbounded tape.
            if symbol not in self.promoted and len(self.promoted)>=8:
                self.losses+=1;return
            self.promoted[symbol]=time.monotonic()+30.
            ring=self.rings.pop(symbol,deque())
            self._record({'event':'PREENTRY_BUFFER','symbol':symbol,'at':iso(at),
                'rows':len(ring),'requested_seconds':self.ring_seconds,
                'coverage':'BOUNDED_PARTIAL_RING_NOT_A_COMPLETENESS_PROOF',
                'ring_evictions_total':self.ring_evictions},symbol,force=True)
            for _,data in ring:
                self.ring_size-=len(data);self._queue(data)

    def tick(self,t,received):
        row={'event':'TRADE','symbol':t.symbol,'exchange_at':iso(t.time),'received_at':iso(received),
             'received_monotonic':getattr(t,'received_monotonic',None),
             **{k:getattr(t,k,None) for k in ('price','volume','cumulative','bid','ask','bid_qty','ask_qty',
                   'regular','depth_available','depth_source','venue')},'book_time':iso(getattr(t,'book_time',None))}
        self._record(row,t.symbol)

    def book(self,symbol,book,received):
        self._record({'event':'BOOK','symbol':symbol,'exchange_at':book.get('time'),
            'received_at':iso(received),'received_monotonic':book.get('received_monotonic'),
            **{k:book.get(k) for k in ('best_bid','best_ask','bid_qty','ask_qty','depth_available','source')}},symbol)

    def stream(self,kind,at):
        self._record({'event':'STREAM_STATE','kind':kind,'received_at':iso(at),
            'symbols':sorted(self.watch|self.held),'coverage':'GAP_OR_ACK_REQUIRES_FRESH_DATA'},force=True)

    def subscription(self,added,removed,at):
        self._record({'event':'SUBSCRIPTION_SCOPE','at':iso(at),'added':sorted(added),
            'removed':sorted(removed),'requires_fresh_tick':True},force=True)

    def completed_bar(self,symbol,bar,received,processed):
        key=iso(bar.get('time'))
        if self.last_bar.get(symbol)==key:return
        self.last_bar[symbol]=key
        self._record({'event':'COMPLETED_BAR','symbol':symbol,'bar':dict(bar),
            'confirmed_at':iso(received),'processed_at':iso(processed),
            'source':'ACTUAL_RECEIVED_DETECTOR_HISTORY','reference_bars_used':False},symbol)

    def position(self,symbol,p,at):
        sig=p.get('signal') or {}
        identity=(p.get('opened'),p.get('avg'))
        fast=p.get('fast_exit_evidence') or {}
        key=(identity,p.get('qty'),p.get('stop'),p.get('emergency'),p.get('protection_stop'),
             p.get('exit_requested'),p.get('exit_reason'),fast.get('decision'),fast.get('reason'))
        if self.last_lines.get(symbol)==key:return
        self.last_lines[symbol]=key
        self._record({'event':'POSITION_LINES','symbol':symbol,'at':iso(at),
            **{k:p.get(k) for k in ('opened','avg','qty','pattern','initial_stop','stop','emergency',
               'protection_stop','high','low','exit_requested','exit_reason')},
            'signal_at':sig.get('signal_detected_at',sig.get('time')),
            'invalidation_policy':sig.get('invalidation_policy'),
            'fast_exit_evidence':fast,'invalidation_evidence':p.get('invalidation_evidence'),
            'confirmed_protection':p.get('protection_evidence'),
            'target':sig.get('target'),'orders_enabled':False},symbol,force=True)

    def event_ref(self,kind,data,at,positions):
        symbol=data.get('symbol')
        if kind=='ENTRY_CANDIDATE' and symbol:
            self.promote(symbol,at)
            self._record({'event':'ENTRY_BASIS','symbol':symbol,'at':iso(at),
                **{k:data.get(k) for k in ('pattern','limit','stop','emergency','target','qty',
                   'fast_entry_evidence','entry_execution_evidence','invalidation_policy',
                   'fast_context_observation','fast_context_snapshot','fast_reference_bar_snapshot',
                   'fast_reference_received_at')},
                'existing_event':'ENTRY_CANDIDATE'},symbol,force=True)
        if kind in {'ENTRY_CANDIDATE','ORDER_ACCEPTED','BUY_ORDER','SELL_ORDER','FILL',
                     'ENTRY_EXECUTION_RECHECK','EXIT_REQUESTED','BROKER_PNL_RECONCILED'}:
            self._record({'event':'EVENT_REF','existing_event':kind,'at':iso(at),'symbol':symbol,
                'local':data.get('local'),'side':data.get('side'),
                'source':'automation-production/events.jsonl_AND_state.json',
                'actual_fees_and_pnl':'USE_EXISTING_BROKER_RECONCILED_LEDGER'},symbol,force=True)
        self.set_scope(self.watch,positions,at)

    def stats(self):
        with self.lock:
            return {'session':self.session,'written_rows':self.written,'queue_rows':len(self.pending),
                'queue_bytes':self.pending_size,'ring_bytes':self.ring_size,
                'ring_rows':sum(len(r) for r in self.rings.values()),'capture_losses':self.losses,
                'write_errors':self.errors,'last_error':self.last_error,'storage_blocked':self.storage_blocked,
                'coverage':'UNKNOWN_IF_LOSS_OR_GAP_OR_PARTIAL_BUFFER','orders_enabled':False,
                'total_bytes':self.total_written,'day_bytes':self.day_written}

    def flush(self):
        with self.io_lock:
            day=self.clock().strftime('%Y%m%d')
            if self.day!=day:
                self.day=day;self.path=None
                self.day_written=sum(p.stat().st_size for p in self.folder.glob('trade-review-'+day+'-????.jsonl'))
            with self.lock:
                batch=[];size=0
                while self.pending and size+len(self.pending[0])<=self.file_limit:
                    data=self.pending.popleft();batch.append(data);size+=len(data);self.pending_size-=len(data)
            if not batch:return True
            if self.total_written+size>self.total_limit or self.day_written+size>self.day_limit:
                self.storage_blocked=True;self.losses+=len(batch);return False
            try:
                if self.path is None or self.path_size+size>self.file_limit:
                    existing=list(self.folder.glob('trade-review-'+day+'-????.jsonl'))
                    part=max([int(p.stem.rsplit('-',1)[1]) for p in existing]+[0])+1
                    if part>9999:raise OSError('CAPTURE_FILE_LIMIT')
                    self.path=self.folder/f'trade-review-{day}-{part:04d}.jsonl';self.path_size=0
                offset=self.path.stat().st_size if self.path.exists() else 0
                try:
                    with self.path.open('ab') as out:out.write(b''.join(batch))
                except OSError:
                    # Never append a retry after an incomplete JSON line.
                    try:
                        with self.path.open('r+b') as out:out.truncate(offset)
                    except OSError:
                        self.path=None;self.losses+=len(batch)
                    raise
                self.written+=len(batch);self.path_size+=size;self.total_written+=size;self.day_written+=size
                return True
            except Exception as error:
                self.errors+=1;self.last_error=type(error).__name__
                # Do not block or grow indefinitely during file locks/disk errors.
                with self.lock:
                    for data in reversed(batch):
                        if len(self.pending)<self.queue_rows and self.pending_size+len(data)<=self.queue_limit:
                            self.pending.appendleft(data);self.pending_size+=len(data)
                        else:self.losses+=1
                return False

    def _run(self):
        while not self.stop.wait(.5):
            try:ok=self.flush()
            except Exception as error:
                self.errors+=1;self.last_error=type(error).__name__;ok=False
            self.write_status()
            if not ok:self.stop.wait(1.)

    def write_status(self,force=False):
        if not force and time.monotonic()-self.last_status_write<5.:return
        try:
            # This tiny overwrite remains possible when the tape budget is
            # exhausted; an absent/incomplete tape must never imply coverage.
            path=self.folder/'capture-status.json';temp=self.folder/'capture-status.tmp'
            temp.write_text(json.dumps({'event':'CAPTURE_STATUS','at':iso(self.clock()),
                **self.stats()},ensure_ascii=False,allow_nan=False),encoding='utf-8')
            os.replace(temp,path);self.last_status_write=time.monotonic()
        except OSError as error:
            self.errors+=1;self.last_error=type(error).__name__

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2.)
        # Shutdown may leave a pending bounded buffer; never delay protection.
        if self.thread and self.thread.is_alive():return False
        try:return self.flush()
        finally:self.write_status(force=True)
