"""Bounded receiver evidence for actual trades. No broker or strategy decisions.

WATCH trade/book frames are retained continuously within explicit disk budgets.
The legacy ring-only mode remains available for bounded offline fixtures.
Holdings receive reserved queue capacity independent of WATCH/SHADOW traffic.
Existing order/fill/fee logs are linked, not copied into a second ledger.
"""
from collections import deque
from datetime import datetime, timedelta
import json
import os
import shutil
import hashlib
from pathlib import Path
import threading
import time
import uuid
from automation.decisions import tick_source_identity


def iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


def bar_knowledge_time(bar, received, processed):
    """Conservative research cutoff; never infer availability from bar label.

    Historical confirmed_at meant the trigger tick's receipt. A queued tick can
    be processed much later, or have an exchange second just ahead of the PC.
    Keep that legacy field, but expose the actual receipt, processing and close
    separately. This function has no connection to detector/order decisions.
    """
    def aware(value):
        try:
            value=datetime.fromisoformat(value) if isinstance(value,str) else value
            return value if isinstance(value,datetime) and value.tzinfo is not None else None
        except (TypeError,ValueError):return None
    start=aware(bar.get('time')); receipt=aware(received); processing=aware(processed)
    end=start+timedelta(minutes=1) if start and not (start.second or start.microsecond) else None
    known=max(end,receipt,processing) if end and receipt and processing else None
    return {'bar_closed_at':iso(end),'trigger_received_at':iso(receipt),
            'known_at':iso(known),'knowledge_time_status':'KNOWN' if known else 'UNKNOWN',
            'knowledge_time_basis':'MAX_BAR_CLOSE_TRIGGER_RECEIPT_PROCESSING'}


def sampled_reward_ratio(values):
    """Retain the producer's basis; do not turn an unknown ratio into zero."""
    for key in ('calculated_rr','net_rr','rr'):
        if values.get(key) is not None:return values[key],key
    return None,'UNKNOWN'


class BoundedCoreDecisionLog:
    """Separate bounded-capacity diagnostic path for core entry/rejection decisions.

    Preserves compact core decision records even when the main 32 GiB high-volume
    tape is paused, without disabling the 32 GiB limit on TradeReviewCapture.
    If free disk space drops below its own safety floor (default 5.0 GiB) or its
    bounded byte cap (default 4 MiB) is reached, it safely stops writing and
    counts omitted records without raising any exception or triggering HALT.
    """
    def __init__(self, folder, *, clock=None, max_file_bytes=4*1024*1024,
                 min_free_bytes=5*1024**3, dedup_seconds=15.):
        self.folder=Path(folder)
        self.clock=clock or (lambda:datetime.now().astimezone())
        self.max_file_bytes=max_file_bytes
        self.min_free_bytes=min_free_bytes
        self.dedup_seconds=dedup_seconds
        self.path=self.folder/'core-decisions-bounded.jsonl'
        self.lock=threading.Lock()
        self.written=0
        self.omitted=0
        self.errors=0
        self.last_error=''
        self.blocked=False
        self.block_reason=None
        self.last_sig={}
        try:
            self.bytes_written=self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            self.bytes_written=0

    def record(self, row, symbol=None, force=False):
        try:
            with self.lock:
                now_dt=self.clock()
                now_ts=now_dt.timestamp() if hasattr(now_dt,'timestamp') else time.time()
                sig_key=(symbol,row.get('setup'),row.get('decision'),row.get('primary_reason_code'),
                         row.get('entry_price'),row.get('structural_stop'),row.get('emergency_stop'),
                         row.get('target'),row.get('quantity'),row.get('observation_gap'))
                prev=self.last_sig.get(symbol) if symbol else None
                if not force and row.get('decision')!='BUY' and prev and prev[0]==sig_key and (now_ts-prev[1])<self.dedup_seconds:
                    return True
                payload=(json.dumps(row,ensure_ascii=False,default=iso,allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')
                size=len(payload)
                if self.max_file_bytes and self.bytes_written+size>self.max_file_bytes:
                    self.blocked=True;self.block_reason='DIAGNOSTIC_BYTE_CAP_REACHED';self.omitted+=1;return False
                if self.min_free_bytes:
                    free_bytes=shutil.disk_usage(self.folder).free
                    if free_bytes-size<self.min_free_bytes:
                        self.blocked=True;self.block_reason='DIAGNOSTIC_FREE_SPACE_BELOW_SAFETY_LIMIT';self.omitted+=1;return False
                for attempt in range(3):
                    try:
                        with self.path.open('ab') as out:
                            out.write(payload)
                        break
                    except PermissionError as perm_err:
                        if attempt<2 and getattr(perm_err,'winerror',None) in (5,32):
                            time.sleep(0.003*(attempt+1));continue
                        raise
                self.written+=1
                self.bytes_written+=size
                self.blocked=False
                self.block_reason=None
                if symbol:
                    self.last_sig[symbol]=(sig_key,now_ts)
                return True
        except Exception as error:
            self.errors+=1
            self.omitted+=1
            self.last_error=type(error).__name__
            self.blocked=True
            self.block_reason=f'DIAGNOSTIC_IO_ERROR:{self.last_error}'
            return False

    def stats(self):
        with self.lock:
            return {'path':str(self.path.name),'written_rows':self.written,'omitted_rows':self.omitted,
                    'write_errors':self.errors,'last_error':self.last_error,'blocked':self.blocked,
                    'block_reason':self.block_reason,'bytes_written':self.bytes_written,
                    'max_file_bytes':self.max_file_bytes,'min_free_bytes':self.min_free_bytes}


class TradeReviewCapture:
    def __init__(self, folder, *, ring_seconds=45., ring_rows=256,
                 ring_bytes=1024*1024, queue_rows=4096, queue_bytes=4*1024*1024,
                 file_bytes=32*1024*1024, day_bytes=4*1024**3,
                 total_bytes=12*1024**3, min_free_bytes=32*1024**3,
                 diagnostic_max_bytes=4*1024*1024, diagnostic_min_free_bytes=5*1024**3,
                 continuous_watch=True, clock=None, start=True):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda:datetime.now().astimezone())
        self.core_decisions=BoundedCoreDecisionLog(self.folder,clock=self.clock,
            max_file_bytes=diagnostic_max_bytes,min_free_bytes=diagnostic_min_free_bytes)
        self.ring_seconds=ring_seconds;self.ring_rows=ring_rows;self.ring_limit=ring_bytes
        self.queue_rows=queue_rows;self.queue_limit=queue_bytes
        self.file_limit=file_bytes;self.day_limit=day_bytes;self.total_limit=total_bytes
        self.min_free_bytes=min_free_bytes;self.continuous_watch=continuous_watch;self.storage_block_reason=None
        self.lock=threading.RLock();self.io_lock=threading.Lock();self.stop=threading.Event()
        self.session=uuid.uuid4().hex;self.sequence=0
        self.watch=frozenset();self.held=frozenset();self.promoted={}
        self.rings={};self.ring_size=0;self.pending=deque();self.pending_size=0
        self.losses=0;self.errors=0;self.ring_evictions=0;self.written=0
        self.loss_intervals=deque(maxlen=32)
        self.storage_blocked=False;self.last_error='';self.last_lines={};self.last_bar={};self.last_evaluation_at={}
        self.rejected_buffer_at={};self.rejected_buffers=0;self.rejected_buffer_bytes=0;self.rejected_buffer_skips=0
        self.last_status_write=0.
        from automation.entry_evidence import EntryDecisionEvidence
        self.entry_evidence=EntryDecisionEvidence()
        self.entry_order_episode={}
        self.day='';self.day_written=0;self.total_written=0;self.path=None;self.path_size=0
        own=sorted(self.folder.glob('trade-review-????????-????.jsonl'))
        self.total_written=sum(p.stat().st_size for p in own)
        self._record({'event':'CAPTURE_STARTED','at':iso(self.clock()),
                      'coverage':'STARTS_NOW_NO_RESTORED_TICKS_OR_BARS',
                      'detail_sampling_seconds':{'opening_before_0930':15,'otherwise':60},
                      'samples_establish_exact_first_onset':False,
                      'continuous_scope':'WATCH_AND_HELD_ONLY_WITHIN_EXISTING_BUDGETS',
                      'orders_enabled':False}, force=True)
        self.thread=threading.Thread(target=self._run,name='trade-review-writer',daemon=True) if start else None
        if self.thread:self.thread.start()

    def _encode(self,row):
        self.sequence+=1
        return (json.dumps({**row,'capture_enqueued_at':iso(self.clock()),'capture_session':self.session,'capture_sequence':self.sequence,
            'capture_losses':self.losses},ensure_ascii=False,default=iso,allow_nan=False,separators=(',',':'))+'\n').encode('utf-8')

    def _loss(self, reason, count=1):
        """Bounded diagnostic intervals; a capture loss does not prove feed loss."""
        with self.lock:
            at=iso(self.clock());self.losses+=count
            if self.loss_intervals and self.loss_intervals[-1]['reason']==reason and self.loss_intervals[-1]['open']:
                item=self.loss_intervals[-1];item['last_detected_at']=at;item['rows']+=count
            else:
                self.loss_intervals.append({'reason':reason,'first_detected_at':at,'last_detected_at':at,
                                            'rows':count,'open':True,'feed_loss':'UNKNOWN'})

    def _queue(self,data):
        if (len(data)>32768 or len(self.pending)>=self.queue_rows
                or self.pending_size+len(data)>self.queue_limit):
            self._loss('CAPTURE_QUEUE_LIMIT');return False
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
                if self.continuous_watch and symbol in self.watch:
                    # Maintain in-memory ring for WATCH symbols so recent price geometry is always preserved
                    ring=self.rings.setdefault(symbol,deque())
                    ring.append((now,data));self.ring_size+=len(data)
                    while ring and (len(ring)>self.ring_rows or now-ring[0][0]>self.ring_seconds):
                        self.ring_size-=len(ring.popleft()[1]);self.ring_evictions+=1
                    while self.ring_size>self.ring_limit:
                        oldest=min((r[0][0],s) for s,r in self.rings.items() if r)[1]
                        self.ring_size-=len(self.rings[oldest].popleft()[1]);self.ring_evictions+=1
                    # Research traffic cannot fill the queue space reserved
                    # for held positions and order-related diagnostic records.
                    reserve_rows=min(256,max(1,self.queue_rows//4))
                    reserve_bytes=min(512*1024,max(1,self.queue_limit//4))
                    if (len(self.pending)>=self.queue_rows-reserve_rows
                            or self.pending_size+len(data)>self.queue_limit-reserve_bytes):
                        self._loss('CAPTURE_RESERVED_CAPACITY');return
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
            metadata={}
            if isinstance(watch,dict):
                for symbol in sorted(watch)[:12]:
                    row=watch[symbol]
                    if not isinstance(row,dict):continue
                    status=row.get('market_status') or {}
                    metadata[symbol]={k:row.get(k) for k in ('status','source_count','fallback_source1','resistance',
                        'resistance_room_pass','first_raw_at','first_watch_at','observed_at','venue')}
                    metadata[symbol]['market_status']={k:status.get(k) for k in ('pass','reason_code','checked_at','vi_active','single_price_auction')}
            with self.lock:
                previous=getattr(self,'watch_metadata',{})
                if not isinstance(watch,dict):
                    # Order-event position refreshes pass the existing symbol
                    # set. They do not revoke already known WATCH qualification.
                    metadata={s:row for s,row in previous.items() if s in watch}
                changes=[(s,metadata.get(s)) for s in previous.keys()|metadata.keys() if previous.get(s)!=metadata.get(s)]
                self.watch_metadata=metadata
                self.watch=frozenset(sorted(set(watch))[:12])
                for symbol in self.held-set(positions):
                    # B/C alternatives may still be holding after the real A
                    # exit. Keep a bounded 120-second tail, never call it a fill.
                    if symbol in self.promoted or len(self.promoted)<8:
                        self.promoted[symbol]=time.monotonic()+120.
                    else:self._loss('CAPTURE_HELD_TAIL_CAPACITY')
                self.held=frozenset(positions)
                current=time.monotonic()
                self.promoted={s:until for s,until in self.promoted.items() if until>current or s in self.held}
                for s in set(self.rings)-self.watch-self.held:
                    self.ring_size-=sum(len(b) for _,b in self.rings.pop(s))
                for mapping in (self.last_lines,self.last_bar,self.last_evaluation_at):
                    for s in set(mapping)-self.watch-self.held-set(self.promoted):mapping.pop(s,None)
            for symbol,p in positions.items():self.position(symbol,p,at)
            # Reuse the bounded asynchronous recorder. Only committed role or
            # qualification changes produce rows; no per-tick metadata log.
            for symbol,value in changes:
                self._record({'event':'WATCH_METADATA','symbol':symbol,'known_at':iso(at),'active':value is not None,
                    'watch':value,'actual_order_permission':False},symbol,force=True)
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

    def decision_tape(self,symbol,at,snapshot):
        """Persist bounded FAST evidence even when promotion beat WATCH scope.

        This is a separate provenance-labelled snapshot, not fabricated TRADE
        frames or a claim that the receiver ring was complete. All existing
        queue, per-line and disk caps still apply; recorder failure cannot trade.
        """
        if not isinstance(snapshot,dict):return
        rows=snapshot.get('rows',[])
        if not isinstance(rows,list):return
        parts=[rows[i:i+8] for i in range(0,min(len(rows),256),8)]
        snapshot_id=uuid.uuid4().hex
        with self.lock:
            self._record({'event':'FAST_DECISION_TAPE','symbol':symbol,'at':iso(at),
                **{k:snapshot.get(k) for k in ('asof','eligible_rows','truncated','max_rows',
                   'reset_reason','source','coverage','direction_basis')},
                'snapshot_id':snapshot_id,'retained_rows':min(len(rows),256),'parts':len(parts),
                'separate_book_history':'UNKNOWN_USE_ORIGINAL_PAIRED_TICK_FIELDS',
                'orders_enabled':False,'actual_order_permission':False},symbol,force=True)
            for index,part in enumerate(parts):
                self._record({'event':'FAST_DECISION_TAPE_PART','symbol':symbol,'at':iso(at),
                    'snapshot_id':snapshot_id,'asof':snapshot.get('asof'),
                    'part':index,'parts':len(parts),'rows':part,
                    'orders_enabled':False,'actual_order_permission':False},symbol,force=True)

    def tick(self,t,received,processed=None):
        row={'event':'TRADE','symbol':t.symbol,'exchange_at':iso(t.time),'received_at':iso(received),
             'receipt_evidence':tick_source_identity(t),
             'processed_at':iso(processed),'capture_point':'STRATEGY_INGESTION' if processed is not None else 'RECEIVER',
             'received_monotonic':getattr(t,'received_monotonic',None),
             **{k:getattr(t,k,None) for k in ('price','volume','cumulative','bid','ask','bid_qty','ask_qty',
                   'regular','depth_available','depth_source','venue','book_received_at',
                   'book_received_monotonic')},'book_time':iso(getattr(t,'book_time',None))}
        self._record(row,t.symbol)

    def book(self,symbol,book,received):
        self._record({'event':'BOOK','symbol':symbol,'exchange_at':book.get('time'),
            'book_receipt_id':f"0D:{symbol}:{iso(book.get('received_at') or received)}:{book.get('received_monotonic')}",
            'received_at':iso(received),'received_monotonic':book.get('received_monotonic'),
            **{k:book.get(k) for k in ('best_bid','best_ask','bid_qty','ask_qty','depth_available','source')}},symbol)

    def reference_snapshot(self,symbol,bars,received,processed,venue):
        """Retain the actual accepted REST reference prefix and knowledge time.

        Reference bars never become received trade/warmup bars. Future or
        incomplete bars cannot enter this prefix. Parts remain under the
        existing per-row, queue and disk limits.
        """
        rows=[]
        for raw in list(bars)[-120:]:
            stamp=raw.get('time')
            stamp=datetime.fromisoformat(stamp) if isinstance(stamp,str) else stamp
            if not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp+timedelta(minutes=1)>processed:continue
            rows.append({**raw,'time':stamp})
        digest=hashlib.sha256(json.dumps([[iso(b['time'])]+[b[k] for k in ('open','high','low','close')]+[b.get('volume')] for b in rows],separators=(',',':')).encode()).hexdigest()
        parts=[rows[i:i+8] for i in range(0,len(rows),8)]
        for index,part in enumerate(parts):
            self._record({'event':'REFERENCE_BARS_PART','symbol':symbol,'received_at':iso(received),
                'processed_at':iso(processed),'known_at':iso(max(received,processed)),
                'source':'REST_REFERENCE_MERGED_ACCEPTED','venue':venue,'bar_label':'MINUTE_START',
                'reference_sha256':digest,'part':index,'parts':len(parts),'rows':part,
                'restored_signal_bars':0,'orders_enabled':False},symbol,force=True)

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
            **bar_knowledge_time(bar,received,processed),
            'source':'ACTUAL_RECEIVED_DETECTOR_HISTORY','reference_bars_used':False},symbol,
            force=symbol in self.watch or symbol in self.held)

    def evaluation(self,t,watch,evaluation,fast,asof):
        """Small sampled no-entry evidence; no new detector or order evaluation.

        At most 12 WATCH symbols, one sample/15s in the opening half-hour,
        otherwise one/minute. Existing queue/day/total limits still apply.
        This is explicitly incomplete evidence, never a census of all ticks.
        """
        symbol=t.symbol
        if symbol not in self.watch and symbol not in self.held:return
        self.rejected_candidate_buffer(symbol,evaluation,asof)
        interval=15. if '09:00:00'<=asof.strftime('%H:%M:%S')<'09:30:00' else 60.
        with self.lock:
            previous=self.last_evaluation_at.get(symbol)
            if previous is not None and (asof-previous).total_seconds()<interval:return
            self.last_evaluation_at[symbol]=asof
        gates=evaluation.get('gates',{}) if isinstance(evaluation,dict) else {}
        trials=[]
        def price_stage(trial):
            gates=trial.get('gates',{});values=gates.get('risk_reward',{}).get('values',{})
            proof=gates.get('breakout_structure' if trial.get('setup')=='A' else 'pullback_structure',{})
            structure=proof.get('values',{});geometry=trial.get('setup_geometry') or {}
            return {'setup':trial.get('setup'),'entry_basis':geometry.get('entry_basis'),
                'structure_result':proof.get('result'),'confirmation_closed_at':geometry.get('confirmation_closed_at'),
                'structure_observed_by':iso(asof),'observation_basis':'SAMPLED_PROCESSING_TIME_NOT_EXACT_FIRST_ONSET',
                'structural_stop':structure.get('structural_stop',structure.get('buffered_stop')),
                'emergency_stop':structure.get('emergency_stop'),
                'reference_sha256':structure.get('reference_sha256',structure.get('basis_sha256')),
                'maximum_entry':values.get('economics',{}).get('max_entry_price'),
                'entry_zone':values.get('entry_zone'),'target_inputs':values.get('target_inputs'),
                'trigger':gates.get('breakout'),'reclaim_tick':gates.get('reclaim_tick'),'price_ready':trial.get('entry_ready'),
                'failed_reasons':trial.get('failed_reasons'),
                'quantity_status':'NOT_REACHED_AT_DIAGNOSTIC_SAMPLE','actual_order_permission':False}
        for trial in (evaluation or {}).get('trials',[]):
            price=trial.get('gates',{}).get('invalidation',{}).get('values',{})
            reward=trial.get('gates',{}).get('risk_reward',{}).get('values',{})
            rr,rr_basis=sampled_reward_ratio(reward)
            trials.append({'setup':trial.get('setup'),'formed':trial.get('candidate_generated'),
                'ready':trial.get('entry_ready'),'failures':trial.get('failed_reasons'),
                'entry':price.get('entry'),'stop':price.get('stop'),'target':reward.get('target'),
                'rr':rr,'rr_basis':rr_basis,'entry_stage':price_stage(trial),
                'entry_alternatives':[price_stage(x) for x in trial.get('entry_alternatives',[])[:1]]})
        fast=fast if isinstance(fast,dict) else {}
        self._record({'event':'OPPORTUNITY_SAMPLE','symbol':symbol,'asof':iso(asof),
            'detail_sampling_seconds':interval,'exact_first_onset':False,
            'receipt_evidence':tick_source_identity(t),
            'exchange_at':iso(t.time),'received_at':getattr(t,'received_at',None),
            'received_monotonic':getattr(t,'received_monotonic',None),
            **{k:getattr(t,k,None) for k in ('price','volume','cumulative','bid','ask','bid_qty','ask_qty','depth_available','depth_source','regular','book_received_at','book_received_monotonic')},
            'processed_at':iso(asof),
            'book_time':iso(getattr(t,'book_time',None)),
            'watch':{k:watch.get(k) for k in ('status','source_count','fallback_source1','resistance',
                'resistance_asof','observed_at','first_raw_at','first_watch_at','venue','resistance_room_pass')},
            'market_status':watch.get('market_status',{}),
            'gates':{k:gates[k] for k in ('warmup','spread','liquidity','acceleration','structure') if k in gates},
            'trials':trials,'fast':{k:fast.get(k) for k in ('time','status','reason','regime','quote_evidence')},
            'sampling_interval_seconds':interval,'coverage':'SAMPLED_NOT_ALL_OPPORTUNITIES',
            'orders_enabled':False,'actual_order_permission':False},symbol,force=True)
        try:
            for tr in trials:
                if not tr.get('formed'):continue
                st=tr.get('entry_stage') or {}
                failures=tr.get('failures') or []
                obs_gap=bool(any('UNKNOWN' in str(r) or 'UNAVAILABLE' in str(r) or 'GAP' in str(r) for r in failures)
                             or gates.get('warmup',{}).get('result')!='PASS')
                rej_reason = failures[0] if failures else None
                self.core_decisions.record({
                    'event':'CORE_DECISION_DIAGNOSTIC','source':'OPPORTUNITY_SAMPLE',
                    'symbol':symbol,'timestamp':iso(asof),
                    'evaluation_time':iso(asof),
                    'trade_time':iso(getattr(t,'time',None)),
                    'trade_price':getattr(t,'price',None),
                    'trade_volume':getattr(t,'volume',None),
                    'best_bid':getattr(t,'bid',None),
                    'best_ask':getattr(t,'ask',None),
                    'p_max':st.get('maximum_entry'),
                    'candidate_rejection_reason':rej_reason,
                    'setup':'A2' if st.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST' else tr.get('setup'),
                    'entry_basis':st.get('entry_basis'),
                    'decision':'READY' if tr.get('ready') else 'NO_BUY',
                    'primary_reason_code':failures[0] if failures else ('READY' if tr.get('ready') else 'REJECT'),
                    'entry_price':tr.get('entry'),'structural_stop':st.get('structural_stop',tr.get('stop')),
                    'emergency_stop':st.get('emergency_stop',tr.get('stop')),'target':tr.get('target'),
                    'gross_rr':None,'net_rr':tr.get('rr'),
                    'capital_quantity':None,'risk_quantity':None,'quantity':None,
                    'observation_gap':obs_gap,'failed_reasons':failures[:4]
                },symbol=symbol)
        except Exception:
            pass

    def bind_entry_context(self,t,watch,detector,at):
        """Memory only. The caller's actual input, not a diagnostic re-evaluation."""
        try:
            with self.lock:self.entry_evidence.bind(t,watch,detector,at)
        except Exception as error:
            self.errors+=1;self.last_error=type(error).__name__

    def decision_evidence(self,engine,symbol,decision,reason,gates,signal,extra):
        try:
            with self.lock:row=self.entry_evidence.row(engine,symbol,decision,reason,gates,signal,extra)
            # Do not consume the queue capacity reserved for held positions.
            if row is not None:self._record(row,symbol)
            sig=signal or {};gates=gates or {};extra=extra or {}
            rq=gates.get('risk_quantity',{}).get('values',{})
            econ=sig.get('entry_economics') or rq.get('entry_economics') or gates.get('risk_reward',{}).get('values',{}).get('economics') or {}
            pol=sig.get('invalidation_policy') or {}
            rr_vals=gates.get('risk_reward',{}).get('values',{})
            setup_name='A2' if sig.get('setup_variant')=='A2' or sig.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST' else sig.get('pattern')
            entry_px=sig.get('limit',econ.get('entry_price',rq.get('entry')))
            s_stop=econ.get('structural_stop',pol.get('structural_stop',rq.get('structural_stop',sig.get('stop'))))
            e_stop=econ.get('emergency_stop',pol.get('emergency_stop',rq.get('emergency_stop',sig.get('stop'))))
            target_px=sig.get('target',econ.get('target',rr_vals.get('target')))
            gross_rr=econ.get('gross_rr',rr_vals.get('gross_rr'))
            net_rr=econ.get('net_rr',rr_vals.get('net_rr',rr_vals.get('calculated_rr')))
            cap_qty=rq.get('capital_quantity')
            risk_qty=rq.get('risk_quantity')
            final_qty=rq.get('quantity',extra.get('quantity'))
            zero_reason=rq.get('reason_code') if final_qty==0 else None
            obs_gap=bool((row and row.get('coverage_gap'))
                         or any(v.get('result')=='UNKNOWN' for v in gates.values() if isinstance(v,dict))
                         or (isinstance(reason,str) and ('UNKNOWN' in reason or 'UNAVAILABLE' in reason or 'STALE' in reason)))
            tick = getattr(engine, 'entry_tick', None) or extra.get('tick')
            band = econ.get('entry_price_band') or {}
            p_max = econ.get('p_max') or band.get('p_max') or sig.get('max_chase_price')
            trade_px = getattr(tick, 'price', None) if tick else None
            trade_vol = getattr(tick, 'volume', None) if tick else None
            trade_ts = iso(getattr(tick, 'time', None)) if tick and hasattr(tick, 'time') else None
            best_bid = getattr(tick, 'bid', None) if tick else None
            best_ask = getattr(tick, 'ask', None) if tick else None

            self.core_decisions.record({
                'event':'CORE_DECISION_DIAGNOSTIC','source':'ENTRY_DECISION',
                'symbol':symbol,'timestamp':iso(engine.clock()),
                'evaluation_time':iso(engine.clock()),
                'trade_time':trade_ts,
                'trade_price':trade_px,
                'trade_volume':trade_vol,
                'best_bid':best_bid,
                'best_ask':best_ask,
                'p_max':p_max,
                'candidate_rejection_reason':reason if decision != 'BUY' else None,
                'setup':setup_name,'entry_basis':sig.get('entry_basis'),
                'decision':decision,'primary_reason_code':reason,
                'entry_price':entry_px,'structural_stop':s_stop,'emergency_stop':e_stop,
                'target':target_px,'gross_rr':gross_rr,'net_rr':net_rr,
                'capital_quantity':cap_qty,'risk_quantity':risk_qty,'quantity':final_qty,
                'zero_quantity_reason':zero_reason,'zero_quantity_detail':rq.get('zero_quantity_detail'),
                'observation_gap':obs_gap
            },symbol=symbol,force=(decision=='BUY'))
        except Exception as error:
            self.errors+=1;self.last_error=type(error).__name__

    def account_protection_evidence(self,engine,kind,at):
        """Observe a blocked protection path; never change account/order authority."""
        if kind not in {'ACCOUNT_UNAVAILABLE','ACCOUNT_LATENCY','EXIT_REQUEST'}:return
        try:
            import time
            positions=engine.s.get('positions',{})
            age=max(0,time.monotonic()-engine.last_account)
            blocked=not engine.account_ok or age>engine.c.reconcile_seconds*3
            key=(bool(positions),blocked,tuple(sorted((s,bool(p.get('exit_requested'))) for s,p in positions.items())))
            prior=getattr(self,'_account_protection_last',None)
            if prior and prior[0]==key and (at-prior[1]).total_seconds()<30:return
            self._account_protection_last=(key,at)
            for symbol,p in positions.items():
                proof=engine.protection_certificate(symbol,p['qty']) if blocked and hasattr(engine,'protection_certificate') else {}
                self._record(dict(event='PROTECTION_ACCOUNT_EVIDENCE',symbol=symbol,asof=iso(at),
                    existing_event=kind,account_age_seconds=age,original_freshness_limit_seconds=engine.c.reconcile_seconds*3,
                    account_ok=engine.account_ok,balance_refresh_uncertain=getattr(engine,'balance_refresh_uncertain',False),
                    ledger_quantity=p.get('qty'),exit_requested=bool(p.get('exit_requested')),
                    exit_reason=p.get('exit_reason'),active_order_count=len(engine.active_orders(symbol)),
                    protection_status='PROTECTION_ONLY_VERIFIED' if proof.get('allowed') else 'BLOCKED_ACCOUNT_UNCERTAIN' if blocked else 'LAST_VERIFIED_ACCOUNT_STILL_WITHIN_ORIGINAL_DEADLINE',
                    protection_authority=proof or None,
                    recovery='FRESH_COMPLETE_RECONCILIATION_REQUIRED_NO_QUANTITY_ASSUMPTION' if blocked and not proof.get('allowed') else None,
                    actual_order_permission=False,not_an_order_ledger=True),symbol)
        except Exception as error:
            self.errors+=1;self.last_error=type(error).__name__

    def rejected_candidate_buffer(self,symbol,evaluation,asof):
        """Retain a bounded real prefix when only economic gates rejected it.

        This does not promote a symbol or authorize an order. It deliberately
        keeps the existing partial ring, rather than inventing missing ticks.
        Reserve queue capacity for holdings; at most 16 prefixes/512KiB per
        process and one per symbol/minute, under the unchanged disk budgets.
        """
        trials=(evaluation or {}).get('trials',[])
        eligible=[]
        for trial in trials:
            failures={k for k,v in trial.get('gates',{}).items() if v.get('result')!='PASS'}
            if (trial.get('candidate_generated') and not trial.get('entry_ready') and failures
                    and failures<={'risk_reward','economic','continuation_cost_reward_risk'}):
                eligible.append({'setup':trial.get('setup'),'failed_gates':sorted(failures)})
        if not eligible:return
        with self.lock:
            if symbol in self.held or self.promoted.get(symbol,0)>time.monotonic():return
            previous=self.rejected_buffer_at.get(symbol)
            if previous is not None and (asof-previous).total_seconds()<60:return
            rows=[data for _,data in self.rings.get(symbol,())]
            if not rows:return
            marker=self._encode({'event':'REJECTED_CANDIDATE_BUFFER','symbol':symbol,'at':iso(asof),
                'trials':eligible,'rows':len(rows),'coverage':'BOUNDED_PARTIAL_RECEIVED_PREFIX_NOT_COMPLETE_TAPE',
                'orders_enabled':False,'actual_order_permission':False})
            size=sum(map(len,rows))+len(marker)
            if (self.rejected_buffers>=16 or self.rejected_buffer_bytes+size>512*1024
                    or len(self.pending)+len(rows)+1>max(0,self.queue_rows-32)
                    or self.pending_size+size>max(0,self.queue_limit-128*1024)):
                self.rejected_buffer_skips+=1;return
            for data in [marker,*rows]:self._queue(data)
            self.rejected_buffer_at[symbol]=asof
            self.rejected_buffers+=1;self.rejected_buffer_bytes+=size

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

    def order_evidence(self,kind,order,data,at):
        if kind not in {'ENTRY_EXECUTION_RECHECK','ORDER_ACCEPTED','ORDER_REJECTED','ORDER_UNKNOWN','FILL'} or not order:return
        signal=order.get('signal') or {};symbol=order.get('symbol')
        setup='E2' if signal.get('pattern')=='E' else signal.get('pattern')
        episode=self.entry_evidence.episodes.get((symbol,setup)) or {}
        if order.get('side')=='BUY' and order.get('status')=='INTENT':
            episode=self.entry_evidence.prepare_episode(symbol,setup,signal,at)
            self.entry_order_episode[symbol]=episode
        elif symbol in self.entry_order_episode:episode=self.entry_order_episode[symbol]
        self._record(dict(event='ORDER_PATH_EVIDENCE',existing_event=kind,at=iso(at),symbol=symbol,
            episode_id=episode.get('id'),local=order.get('local'),broker_order_id=order.get('id'),
            side=order.get('side'),status=order.get('status'),quantity=order.get('qty'),
            filled=order.get('filled'),limit=order.get('limit'),entry_receipt=signal.get('receipt_evidence'),
            phase=data.get('phase'),reason=data.get('reason'),fill_price=data.get('price'),
            observed_intent_persisted=kind=='ENTRY_EXECUTION_RECHECK' and order.get('status')=='INTENT',
            not_an_order_ledger=True,actual_order_permission=False),symbol)

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
                'capture_loss_intervals':list(self.loss_intervals),
                'write_errors':self.errors,'last_error':self.last_error,'storage_blocked':self.storage_blocked,
                'rejected_buffers':self.rejected_buffers,'rejected_buffer_bytes':self.rejected_buffer_bytes,
                'rejected_buffer_skips':self.rejected_buffer_skips,
                'entry_evidence':dict(self.entry_evidence.counts),
                'core_decision_diagnostics':self.core_decisions.stats(),
                'continuous_watch':self.continuous_watch,'storage_block_reason':self.storage_block_reason,
                'limits':{'file_bytes':self.file_limit,'day_bytes':self.day_limit,'total_bytes':self.total_limit,'min_free_bytes':self.min_free_bytes},
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
                self.storage_blocked=True;self.storage_block_reason='CAPTURE_BYTE_BUDGET';self._loss(self.storage_block_reason,len(batch));return False
            if self.min_free_bytes and shutil.disk_usage(self.folder).free-size<self.min_free_bytes:
                self.storage_blocked=True;self.storage_block_reason='CAPTURE_FREE_SPACE_RESERVE';self._loss(self.storage_block_reason,len(batch));return False
            try:
                if self.path is None or self.path_size+size>self.file_limit:
                    existing=list(self.folder.glob('trade-review-'+day+'-????.jsonl'))
                    part=max([int(p.stem.rsplit('-',1)[1]) for p in existing]+[0])+1
                    if part>9999:raise OSError('CAPTURE_FILE_LIMIT')
                    self.path=self.folder/f'trade-review-{day}-{part:04d}.jsonl';self.path_size=0
                offset=self.path.stat().st_size if self.path.exists() else 0
                try:
                    payload=b''.join(batch)
                    for attempt in range(4):
                        try:
                            with self.path.open('ab') as out:out.write(payload)
                            break
                        except PermissionError as perm_err:
                            if attempt<3 and getattr(perm_err,'winerror',None) in (5,32):
                                time.sleep(0.005*(attempt+1));continue
                            raise
                except OSError:
                    # Never append a retry after an incomplete JSON line.
                    try:
                        with self.path.open('r+b') as out:out.truncate(offset)
                    except OSError:
                        self.path=None;self.losses+=len(batch)
                    raise
                self.written+=len(batch);self.path_size+=size;self.total_written+=size;self.day_written+=size
                with self.lock:
                    self.storage_blocked=False;self.storage_block_reason=None
                    for interval in self.loss_intervals:interval['open']=False
                return True
            except Exception as error:
                self.errors+=1;self.last_error=type(error).__name__
                # Do not block or grow indefinitely during file locks/disk errors.
                with self.lock:
                    for data in reversed(batch):
                        if len(self.pending)<self.queue_rows and self.pending_size+len(data)<=self.queue_limit:
                            self.pending.appendleft(data);self.pending_size+=len(data)
                        else:self._loss('CAPTURE_RETRY_QUEUE_LIMIT')
                return False
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
            path=self.folder/'capture-status.json'
            from core.util import atomic_json
            atomic_json(path,{'event':'CAPTURE_STATUS','at':iso(self.clock()),
                **self.stats()},fsync=False)
            self.last_status_write=time.monotonic()
        except OSError as error:
            self.errors+=1;self.last_error=type(error).__name__

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=2.)
        # Shutdown may leave a pending bounded buffer; never delay protection.
        if self.thread and self.thread.is_alive():return False
        try:return self.flush()
        finally:self.write_status(force=True)
