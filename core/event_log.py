"""Lossless size rotation and compact human console. No compression on hot path."""
import json,os,threading,time,uuid
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
        if not path.exists() or path.stat().st_size<max_bytes:return None
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
    with log_lock(path):
        path.parent.mkdir(parents=True,exist_ok=True)
        rotate_if_large(path,max_bytes)
        with path.open('a',encoding='utf-8') as stream:stream.write(line)

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
