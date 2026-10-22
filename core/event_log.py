"""Lossless size rotation and compact human console. No compression on hot path."""
import json,os,queue,threading,time,uuid
from datetime import datetime
from pathlib import Path
_LOCKS={};_GUARD=threading.Lock()
MAX_LOG_BYTES=64*1024*1024
MAX_ARCHIVE_TOTAL_BYTES=200*1024*1024
MAX_ARCHIVE_AGE_SECONDS=3*86400

# Explicit Tier A / Protected patterns (never delete under any circumstance)
PROTECTED_PATTERNS = (
    'state.json',
    'events.jsonl',
    'trade-review',
    'heartbeat.json',
    'session_settings.json',
    'operator_account.json',
    'policy.json',
    'RELEASE_IDENTITY',
    'DEPLOYMENT',
    'TRADE_PRIVATE',
    'BROKER_PRIVATE',
    'CAPTURE_PRIVATE',
    'EVENT_WINDOW_PRIVATE',
    '.bundle',
    'lunit',
    'bexel',
    'incident',
    'fixture',
)

# Explicit Tier C / Allowlist of deletable diagnostic prefixes (only these may be pruned)
DELETABLE_ARCHIVE_PREFIXES = (
    'operator_console.txt',
    'runner_stdout',
    'debug',
    'temp_',
    'operator_error_history.jsonl',
)

def is_protected_file(path):
    name_lower = Path(path).name.lower()
    return any(p.lower() in name_lower for p in PROTECTED_PATTERNS)

def is_deletable_diagnostic_archive(path):
    p = Path(path)
    if is_protected_file(p):
        return False
    name_lower = p.name.lower()
    return any(name_lower.startswith(prefix.lower()) for prefix in DELETABLE_ARCHIVE_PREFIXES)

def prune_archive(archive, max_total_bytes=MAX_ARCHIVE_TOTAL_BYTES, max_age_seconds=MAX_ARCHIVE_AGE_SECONDS):
    """
    Safely prunes ONLY whitelisted diagnostic log archives (Tier C).
    Never touches Tier A ledgers (state.json, events.jsonl, trade-review, etc.).
    Rejects unknown or protected files.
    """
    try:
        archive = Path(archive)
        if not archive.exists(): return
        now_ts = time.time()
        files = [f for f in archive.glob('*') if f.is_file() and is_deletable_diagnostic_archive(f)]
        for f in files:
            try:
                if now_ts - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
            except OSError: pass
        remaining = [f for f in archive.glob('*') if f.is_file() and is_deletable_diagnostic_archive(f)]
        total_size = sum(f.stat().st_size for f in remaining)
        if total_size > max_total_bytes:
            remaining.sort(key=lambda x: x.stat().st_mtime)
            for f in remaining:
                if total_size <= max_total_bytes: break
                try:
                    sz = f.stat().st_size
                    f.unlink()
                    total_size -= sz
                except OSError: pass
    except Exception: pass

def log_lock(path):
    key=os.path.normcase(str(Path(path).absolute()))
    with _GUARD:return _LOCKS.setdefault(key,threading.RLock())

def rotate_if_large(path,max_bytes=MAX_LOG_BYTES):
    """Lossless rename; stdout logs call only while the runner is stopped."""
    path=Path(path)
    with log_lock(path):
        try:size=path.stat().st_size
        except FileNotFoundError:return None
        if size<max_bytes:return None
        archive=path.parent/'archive';archive.mkdir(exist_ok=True)
        target=archive/(path.name+'.'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.'+str(os.getpid())+'.'+uuid.uuid4().hex[:8])
        for attempt in range(6):
            try:
                os.replace(path,target)
                prune_archive(archive)
                return target
            except PermissionError:
                if attempt==5:raise
                time.sleep(.01*(2**attempt))

def append_jsonl(path,row,default=None,max_bytes=MAX_LOG_BYTES):
    path=Path(path)
    line=json.dumps(row,ensure_ascii=False,allow_nan=False,**({'default':default} if default else {}))+'\n'
    _append_line(path,line,max_bytes)

def _append_line(path,line,max_bytes):
    """Same synchronous, lossless append for an already-frozen JSON line."""
    path=Path(path)
    with log_lock(path):
        rotate_if_large(path,max_bytes)
        try:stream=path.open('a',encoding='utf-8')
        except FileNotFoundError:
            # The ordinary event path already exists. Create directories only
            # for first use, retaining synchronous append and real-size rotation.
            path.parent.mkdir(parents=True,exist_ok=True)
            stream=path.open('a',encoding='utf-8')
        with stream:stream.write(line)


class AsyncDiagnosticEventWriter:
    """Explicit runner-only diagnostic offload; never an order/ledger writer.

    Only a small allowlist without any signal/BUY/SELL evidence is eligible.
    Critical events and full/oversized diagnostic queues synchronously append.
    Physical diagnostic order may follow a later critical event; original event
    times and session-scoped ingress sequence preserve that provenance.
    """
    DIAGNOSTIC_KINDS=frozenset({'ENTRY_EVAL','NO_TRADE','ORDER_READINESS_CHANGED','READINESS_CHANGED',
        'OBSERVATION_REJOINED','WATCH_TRANSITION','ENTRY_QUOTE_RETRY','ACCOUNT_LATENCY',
        'FOCUS_SELECTION_BLOCKED','FOCUS_SELECTION_APPLIED','INTRADAY_REFERENCE_READY',
        'INTRADAY_REFERENCE_PENDING','SUBSCRIPTION_REFRESH','RAW_CANDIDATES',
        'CANDIDATE_LATENCY','RADAR_UPDATED','CANDIDATE_CYCLE_SUMMARY'})

    def __init__(self,path,capacity=64,max_line_bytes=256*1024,max_bytes=MAX_LOG_BYTES):
        if type(capacity) is not int or not 1<=capacity<=64:raise ValueError('diagnostic capacity must be 1..64')
        if type(max_line_bytes) is not int or max_line_bytes<=0:raise ValueError('positive diagnostic line byte bound required')
        self.path=Path(path);self.max_line_bytes=max_line_bytes;self.max_bytes=max_bytes
        self.pending=queue.Queue(maxsize=capacity);self._lock=threading.Lock();self._stop=threading.Event()
        self._closed=False;self._error=None;self.failed_line=None;self._sequence=0
        self._session=uuid.uuid4().hex;self._queued=0;self._written=0;self._fallback=0;self._synchronous=0
        self.thread=threading.Thread(target=self._run,name='diagnostic-event-writer',daemon=True)
        self.thread.start()

    @classmethod
    def diagnostic(cls,row):
        if row.get('event') not in cls.DIAGNOSTIC_KINDS:return False
        if row.get('decision') not in (None,'NO_BUY'):return False
        if row.get('side') in {'BUY','SELL'}:return False
        if any(row.get(key) for key in ('signal_detected','signal_at','signal','setup','pattern','order_created')):return False
        if row.get('event')=='ENTRY_EVAL' and row.get('decision')!='NO_BUY':return False
        return True

    def check_error(self):
        with self._lock:error=self._error
        if error is not None:raise error

    def append(self,row):
        # Serialize once on the caller before return: later mutable engine state
        # cannot alter the recorded diagnostic, and invalid JSON fails here.
        eligible=self.diagnostic(row)
        with self._lock:
            # A latched diagnostic error must still let the main handler record
            # its synchronous halt/error/position protection events. The runner
            # checks check_error() before new order processing on every cycle.
            if eligible and self._error is not None:raise self._error
            if eligible and self._closed:raise OSError('diagnostic event writer is closed')
            self._sequence+=1
            tagged={**row,'event_ingress_sequence':self._sequence,
                    'diagnostic_writer_session':self._session}
            if eligible:
                tagged.update(diagnostic_async=True,diagnostic_delivery='ASYNC')
                line=json.dumps(tagged,ensure_ascii=False,allow_nan=False)+'\n'
                if len(line.encode('utf-8'))<=self.max_line_bytes:
                    try:
                        self.pending.put_nowait(line);self._queued+=1;return 'ASYNC'
                    except queue.Full:pass
                tagged.update(diagnostic_async=False,diagnostic_delivery='SYNC_FALLBACK')
                self._fallback+=1;delivery='SYNC_FALLBACK'
            else:
                tagged.update(diagnostic_async=False,diagnostic_delivery='SYNC_CRITICAL')
                self._synchronous+=1;delivery='SYNC_CRITICAL'
        # No queue.join/flush here. A critical event only shares the existing
        # per-file append lock with at most the currently-writing diagnostic.
        append_jsonl(self.path,tagged,max_bytes=self.max_bytes)
        return delivery

    def _run(self):
        while not self._stop.is_set() or not self.pending.empty():
            try:line=self.pending.get(timeout=.05)
            except queue.Empty:continue
            try:
                _append_line(self.path,line,self.max_bytes)
                with self._lock:self._written+=1
            except Exception as error:
                with self._lock:
                    self.failed_line=line
                    self._error=error if isinstance(error,OSError) else OSError('diagnostic event write failed: '+str(error))
                return
            finally:self.pending.task_done()

    def stats(self):
        with self._lock:
            return {'queue_depth':self.pending.qsize(),'capacity':self.pending.maxsize,
                    'max_line_bytes':self.max_line_bytes,'queued':self._queued,'written':self._written,
                    'synchronous':self._synchronous,'fallback':self._fallback,
                    'error':str(self._error) if self._error is not None else None,
                    'failed_line_retained':self.failed_line is not None,'closed':self._closed}

    def close(self,timeout=5):
        with self._lock:self._closed=True;self._stop.set()
        self.thread.join(timeout)
        if self.thread.is_alive():raise TimeoutError('diagnostic event drain did not complete')
        self.check_error()


class HumanConsole:
    diagnostic={'ENTRY_EVAL','SHADOW_EVAL','POSITION_EVAL','EXIT_EVAL','RAW_CANDIDATES','BOOK_DEPTH_OBSERVED','CANDIDATE_CYCLE_SUMMARY','RADAR_UPDATED','CANDIDATE_LATENCY','ACCOUNT_LATENCY','OBSERVATION_RELEASED','OBSERVATION_REJOINED','SHADOW_CAPACITY','NO_TRADE'}
    def __init__(self):self.last={}
    def emit(self,row,stamp):
        kind=row['event']
        if kind not in self.diagnostic or row.get('signal_detected') or row.get('decision') in {'BUY','SELL'}:
            print(json.dumps(row,ensure_ascii=False));return
        signature=(row.get('state'),row.get('decision'),row.get('primary_reason_code') or row.get('reason'),row.get('protection'),row.get('quantity'),
            tuple((k,v.get('result')) for k,v in (row.get('gates') or {}).items()),
            tuple((t.get('setup'),tuple(t.get('failed_reasons') or [])) for t in row.get('setup_trials') or []),row.get('data_action'))
        key=(kind,row.get('symbol'));prior=self.last.get(key)
        if prior and prior[0]==signature and 0<=stamp-prior[1]<10:return
        self.last[key]=(signature,stamp)
        concise={k:row[k] for k in ('time','event','symbol','name','decision','primary_reason_code','reason','current_price','entry_price','invalidation','protection','quantity','data_action') if row.get(k) is not None}
        concise['full_record']='events.jsonl (rotated records: archive/)'
        print(json.dumps(concise,ensure_ascii=False))
