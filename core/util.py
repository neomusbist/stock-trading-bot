import csv
import json
import math
import os
import re
import tempfile
import time
import traceback
import threading
from pathlib import Path
from contextlib import contextmanager

def number(value, signed=False):
    if value is None or str(value).strip() == '':
        raise ValueError('숫자 결측')
    n = float(str(value).replace(',', '').strip())
    if not math.isfinite(n):
        raise ValueError('유한 숫자 필요')
    return n if signed else abs(n)

def code(value):
    x = str(value).strip().lstrip("'")
    if len(x) == 7 and x.startswith('A'):
        x = x[1:]
    if x.endswith(('_NX', '_AL')):
        x = x[:-3]
    if len(x) == 7 and x.startswith('A'):
        x = x[1:]
    if not re.fullmatch('[0-9A-Z]{6}', x):
        raise ValueError('KRX 코드 오류')
    return x

_json_locks = {}
_json_locks_guard = threading.Lock()

def atomic_json(path, value, fsync=True):
    key=os.path.normcase(str(Path(path).absolute()))
    with _json_locks_guard: lock=_json_locks.setdefault(key,threading.RLock())
    with lock: _atomic_json(path,value,fsync=fsync)

def _atomic_json(path, value, fsync=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Windows reader handles may briefly prevent replacement. Each writer owns
    # its own temporary file; retries never expose a partially written JSON.
    fd, name = tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            # Encode before one write: a large trade ledger otherwise performs
            # hundreds of thousands of tiny writes on the quote/control thread.
            # Keep identical JSON, flush/fsync and atomic-replace semantics.
            f.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(tmp,path)
                break
            except (PermissionError, OSError) as e:
                winerror = getattr(e, 'winerror', None)
                if winerror not in (5, 32) and not isinstance(e, PermissionError):
                    raise
                if attempt == 7:
                    raise
                time.sleep(min(0.5, 0.015 * (2 ** attempt)))
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except OSError:
                # Cleanup must not hide the original write failure.
                import logging
                logging.exception('atomic_json temporary cleanup failed: %s -> %s',tmp,path)

def exception_details(error):
    return {'error':type(error).__name__,'exception_message':str(error),
            'filename':getattr(error,'filename',None),'filename2':getattr(error,'filename2',None),
            'errno':getattr(error,'errno',None),'winerror':getattr(error,'winerror',None),
            'traceback':''.join(traceback.format_exception(type(error),error,error.__traceback__))}

def read_json_retry(path,encoding='utf-8'):
    key=os.path.normcase(str(Path(path).absolute()))
    with _json_locks_guard: lock=_json_locks.setdefault(key,threading.RLock())
    with lock: return _read_json_retry(path,encoding)

_reader_api=None
_reader_api_lock=threading.Lock()

def windows_reader_api():
    # Cache only immutable function signatures, never any file content or state.
    global _reader_api
    if _reader_api is None:
        with _reader_api_lock:
            if _reader_api is None:
                import ctypes
                from ctypes import wintypes
                api=ctypes.WinDLL('kernel32',use_last_error=True)
                api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
                    ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
                api.CreateFileW.restype=wintypes.HANDLE
                api.CloseHandle.argtypes=[wintypes.HANDLE]
                _reader_api=api
    return _reader_api

def _read_json_retry(path,encoding):
    for attempt in range(6):
        try:
            if os.name!='nt': return json.loads(Path(path).read_text(encoding=encoding))
            # Share DELETE as well as READ/WRITE so a status reader never pins
            # the old filename against the writer's atomic replacement.
            import ctypes,msvcrt
            from ctypes import wintypes
            api=windows_reader_api()
            handle=api.CreateFileW(str(Path(path).absolute()),0x80000000,7,None,3,0x80,None)
            if handle==wintypes.HANDLE(-1).value:
                code=ctypes.get_last_error()
                raise OSError(0,ctypes.FormatError(code),str(path),code)
            try: fd=msvcrt.open_osfhandle(handle,os.O_RDONLY)
            except Exception:
                api.CloseHandle(handle); raise
            with os.fdopen(fd,'r',encoding=encoding) as f: return json.load(f)
        except PermissionError:
            if attempt==5: raise
            time.sleep(.01*(2**attempt))

@contextmanager
def shared_binary_reader(path):
    """A live log reader must permit atomic replacement and size rotation."""
    if os.name!='nt':
        with Path(path).open('rb') as stream:yield stream
        return
    import ctypes,msvcrt
    from ctypes import wintypes
    api=windows_reader_api()
    handle=api.CreateFileW(str(Path(path).absolute()),0x80000000,7,None,3,0x80,None)
    if handle==wintypes.HANDLE(-1).value:
        error=ctypes.get_last_error()
        raise OSError(0,ctypes.FormatError(error),str(path),error)
    try:fd=msvcrt.open_osfhandle(handle,os.O_RDONLY)
    except Exception:
        api.CloseHandle(handle);raise
    with os.fdopen(fd,'rb') as stream:yield stream

def write_csv(path, rows, fields=None):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def read_csv(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))

def tick_size(price):
    # KRX 보통주 호가단위. ETF/ETN은 후보 수집 단계에서 제외한다.
    for ceiling, step in [(2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)]:
        if price < ceiling:
            return step
    return 1000

def floor_tick(price):
    return int(price // tick_size(price) * tick_size(price))

class ProcessLock:
    """OS lock은 비정상 종료 시 해제되며 같은 실행 폴더의 이중 실행을 차단한다."""
    def __init__(self,path):
        self.path=Path(path)
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.f=self.path.open('a+b')
        self.f.seek(0)  # Windows permits locking beyond EOF; no pre-lock write.
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise RuntimeError('V1이 이미 실행 중입니다.')
        return self
    def __exit__(self,*args):
        self.f.close()
