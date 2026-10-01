"""운영 프로세스의 존재/생성시각과 OS 잠금을 확인한다. 주문·네트워크 코드는 없다."""
import os
from core.util import ProcessLock

def process_identity(pid):
    if type(pid) is not int or pid<=0: return None
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; api.OpenProcess.restype=wintypes.HANDLE
        api.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
        api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        api.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=api.OpenProcess(0x1000|0x100000,False,pid)
        if not handle: return None
        try:
            if api.WaitForSingleObject(handle,0)!=0x102: return None
            creation,exit_time,kernel,user=(wintypes.FILETIME() for _ in range(4))
            if not api.GetProcessTimes(handle,*[ctypes.byref(x) for x in (creation,exit_time,kernel,user)]): return None
            return str((creation.dwHighDateTime<<32)|creation.dwLowDateTime)
        finally: api.CloseHandle(handle)
    try:
        text=open(f'/proc/{pid}/stat').read(); fields=text[text.rfind(')')+2:].split()
        return None if fields[0]=='Z' else fields[19]
    except (OSError,IndexError): return None

def busy(root):
    # Status is a reader: do not create/append lock files or modify runtime.
    # Start still uses the existing exclusive locks to arbitrate races.
    for name in ('supervisor.lock','process.lock','operator_runner.lock'):
        try:
            with (root/'runtime'/name).open('rb') as f:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(),msvcrt.LK_NBRLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except FileNotFoundError: continue
        except OSError: return True
    return False
