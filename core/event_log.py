"""Lossless size rotation and compact human console. No compression on hot path."""
import json,os,threading,time,uuid
from datetime import datetime
from pathlib import Path
_LOCKS={};_GUARD=threading.Lock()
MAX_LOG_BYTES=64*1024*1024

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
            try:os.replace(path,target);return target
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
