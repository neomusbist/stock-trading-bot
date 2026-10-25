"""Read-only Windows capacity evidence. No broker, subscription or LIVE writes.

Run beside an existing operator process. Samples OS and existing heartbeat files;
does not start trading or claim that afternoon/idle data validates the opening.
CPU percentages are normalized to the whole machine, not one logical core.
"""
import argparse
import ctypes
from ctypes import wintypes as wt
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import time


def distribution(values):
    rows=sorted(v for v in values if type(v) in (int,float) and math.isfinite(v))
    return dict(n=len(rows),median=statistics.median(rows),p95=rows[math.ceil(.95*len(rows))-1],max=rows[-1]) if rows else dict(n=0,median=None,p95=None,max=None)


def counter_rate(previous,current,elapsed):
    if type(previous) not in (int,float) or type(current) not in (int,float) or elapsed<=0 or current<previous:return None
    return (current-previous)/elapsed


def heartbeat_summary(root):
    from core.util import shared_binary_reader
    def read(rel):
        try:
            # Sampling must not pin a Windows status file against atomic
            # replacement or log rotation in the operating process.
            with shared_binary_reader(root/rel) as stream:return json.load(stream)
        except (OSError,ValueError):return {}
    hb=read('runtime/automation-production/heartbeat.json')
    runner=read('runtime/operator_runner.json')
    readiness=hb.get('readiness') or {}
    observation=hb.get('watch_observation') or {}
    delivery=hb.get('entry_delivery') or {}
    counts=hb.get('counts') or {}
    shadow=read('runtime/shadow_snapshot.json')
    candidate=read('runtime/candidate_summary.json')
    try:age=(datetime.now().astimezone()-datetime.fromisoformat(hb['time'])).total_seconds()
    except (TypeError,ValueError,KeyError):age=None
    # Retain only operational fields. Never retain session tokens/accounts.
    return dict(time=hb.get('time'),runner_status=runner.get('status'),pid=runner.get('pid'),
        code_commit=runner.get('code_commit'),mode=hb.get('mode'),
        connected=hb.get('connected'),account_ok=hb.get('account_ok'),
        queue_depth=hb.get('feed_queue_depth'),loop_lag_seconds=hb.get('event_loop_lag_seconds'),
        readiness=readiness.get('state'),order_ready=readiness.get('order_ready'),
        heartbeat_age_seconds=age,watch_count=len(observation),entry_delivery=delivery,
        shadow_snapshot_at=shadow.get('timestamp'),shadow_requested_count=len(shadow.get('symbols') or []),
        shadow_worker=shadow.get('performance'),candidate_shadow_subscription_count=candidate.get('shadow'),
        counts=counts,reconcile_checkpoint=hb.get('reconcile_checkpoint'),
        capture=hb.get('trade_review_capture'),diagnostic_writer=hb.get('diagnostic_event_writer'))


class MemoryStatus(ctypes.Structure):
    _fields_=[('length',wt.DWORD),('load',wt.DWORD)]+[(n,ctypes.c_ulonglong) for n in
        ('total_physical','available_physical','total_pagefile','available_pagefile','total_virtual','available_virtual','available_extended')]


class ProcessEntry(ctypes.Structure):
    _fields_=[('size',wt.DWORD),('usage',wt.DWORD),('pid',wt.DWORD),('default_heap',ctypes.c_size_t),
        ('module',wt.DWORD),('threads',wt.DWORD),('parent_pid',wt.DWORD),('priority',wt.LONG),('flags',wt.DWORD),('name',wt.WCHAR*260)]


class ProcessMemory(ctypes.Structure):
    _fields_=[('cb',wt.DWORD),('page_faults',wt.DWORD)]+[(n,ctypes.c_size_t) for n in
        ('peak_working_set','working_set','peak_paged_pool','paged_pool','peak_nonpaged_pool','nonpaged_pool','pagefile','peak_pagefile','private_bytes')]


class IoCounters(ctypes.Structure):
    _fields_=[(n,ctypes.c_ulonglong) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class WindowsProbe:
    def __init__(self):
        if os.name!='nt':raise RuntimeError('WINDOWS_REQUIRED')
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.OpenProcess.argtypes=(wt.DWORD,wt.BOOL,wt.DWORD);self.k.OpenProcess.restype=wt.HANDLE
        self.k.CloseHandle.argtypes=(wt.HANDLE,)
        self.k.CreateToolhelp32Snapshot.argtypes=(wt.DWORD,wt.DWORD);self.k.CreateToolhelp32Snapshot.restype=wt.HANDLE
        self.k.Process32FirstW.argtypes=(wt.HANDLE,ctypes.POINTER(ProcessEntry))
        self.k.Process32NextW.argtypes=(wt.HANDLE,ctypes.POINTER(ProcessEntry))
        self.k.GetProcessTimes.argtypes=(wt.HANDLE,ctypes.POINTER(wt.FILETIME),ctypes.POINTER(wt.FILETIME),ctypes.POINTER(wt.FILETIME),ctypes.POINTER(wt.FILETIME))
        self.k.K32GetProcessMemoryInfo.argtypes=(wt.HANDLE,ctypes.POINTER(ProcessMemory),wt.DWORD)
        self.k.GetProcessIoCounters.argtypes=(wt.HANDLE,ctypes.POINTER(IoCounters))
        self.k.GetSystemTimes.argtypes=(ctypes.POINTER(wt.FILETIME),ctypes.POINTER(wt.FILETIME),ctypes.POINTER(wt.FILETIME))
        self.k.GlobalMemoryStatusEx.argtypes=(ctypes.POINTER(MemoryStatus),)
        self.logical_processors=os.cpu_count() or 1
        self.previous=None;self.previous_time=None;self.previous_processes={}
        self.k.GetCurrentProcess.restype=wt.HANDLE
        self.k.SetPriorityClass.argtypes=(wt.HANDLE,wt.DWORD)
        self.k.SetPriorityClass(self.k.GetCurrentProcess(),0x4000)

    @staticmethod
    def seconds(t):return ((t.dwHighDateTime<<32)|t.dwLowDateTime)/10000000.

    def processes(self):
        handle=self.k.CreateToolhelp32Snapshot(2,0)
        if handle in (None,ctypes.c_void_p(-1).value):raise ctypes.WinError(ctypes.get_last_error())
        entry=ProcessEntry();entry.size=ctypes.sizeof(entry);rows={}
        try:
            ok=self.k.Process32FirstW(handle,ctypes.byref(entry))
            while ok:
                proc=self.k.OpenProcess(0x1000|0x10,False,entry.pid)
                if not proc:proc=self.k.OpenProcess(0x1000,False,entry.pid)
                if proc:
                    try:
                        created,exited,kernel,user=(wt.FILETIME() for _ in range(4))
                        if self.k.GetProcessTimes(proc,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel),ctypes.byref(user)):
                            row=dict(pid=entry.pid,parent_pid=entry.parent_pid,name=entry.name,
                                creation_ticks=(created.dwHighDateTime<<32)|created.dwLowDateTime,
                                cpu_seconds=self.seconds(kernel)+self.seconds(user))
                            mem=ProcessMemory();mem.cb=ctypes.sizeof(mem)
                            if self.k.K32GetProcessMemoryInfo(proc,ctypes.byref(mem),mem.cb):row.update(working_set_bytes=mem.working_set,private_bytes=mem.private_bytes)
                            io=IoCounters()
                            if self.k.GetProcessIoCounters(proc,ctypes.byref(io)):row.update(io_read_bytes=io.read_bytes,io_write_bytes=io.write_bytes)
                            rows[entry.pid]=row
                    finally:self.k.CloseHandle(proc)
                ok=self.k.Process32NextW(handle,ctypes.byref(entry))
        finally:self.k.CloseHandle(handle)
        return rows

    def sample(self):
        at=time.monotonic();idle,kernel,user=(wt.FILETIME() for _ in range(3))
        if not self.k.GetSystemTimes(ctypes.byref(idle),ctypes.byref(kernel),ctypes.byref(user)):raise ctypes.WinError(ctypes.get_last_error())
        times=tuple(self.seconds(t) for t in (idle,kernel,user))
        cpu=None
        if self.previous:
            di,dk,du=(a-b for a,b in zip(times,self.previous));total=dk+du
            if total>0:cpu=max(0.,min(100.,100*(total-di)/total))
        memory=MemoryStatus();memory.length=ctypes.sizeof(memory)
        if not self.k.GlobalMemoryStatusEx(ctypes.byref(memory)):raise ctypes.WinError(ctypes.get_last_error())
        processes=self.processes();elapsed=at-self.previous_time if self.previous_time is not None else 0
        for pid,row in processes.items():
            previous=self.previous_processes.get(pid,{})
            same=previous.get('creation_ticks')==row['creation_ticks']
            rate=counter_rate(previous.get('cpu_seconds'),row['cpu_seconds'],elapsed) if same else None
            row['cpu_machine_percent']=100*rate/self.logical_processors if rate is not None else None
            for key in ('io_read_bytes','io_write_bytes'):
                row[key+'_per_second']=counter_rate(previous.get(key),row.get(key),elapsed) if same else None
        self.previous=times;self.previous_time=at;self.previous_processes=processes
        return dict(cpu_percent=cpu,logical_processors=self.logical_processors,
                    ram_total_bytes=memory.total_physical,ram_available_bytes=memory.available_physical,
                    memory_load_percent=memory.load,processes=processes)


def collect(root,out,seconds,interval):
    root=root.resolve();out=out.resolve()
    if out==root or root in out.parents:raise ValueError('OUTPUT_MUST_BE_OUTSIDE_LIVE')
    out.mkdir(parents=True,exist_ok=True)
    dest=out/'resource_samples.jsonl'
    if dest.exists():raise FileExistsError(dest)
    probe=WindowsProbe();records=[];started=time.monotonic();previous=None
    with dest.open('w',encoding='utf-8') as stream:
        while True:
            mono=time.monotonic();os_row=probe.sample();hb=heartbeat_summary(root)
            processes=os_row.pop('processes');pid=hb.get('pid')
            # A stopped runner's stale PID is not operating evidence.
            active=hb.get('runner_status')=='RUNNING' and pid in processes
            active_pids={pid} if active else set()
            if active:
                for _ in range(3):active_pids.update(p for p,r in processes.items() if r['parent_pid'] in active_pids)
            top=sorted(processes.values(),key=lambda r:r.get('cpu_machine_percent') or 0,reverse=True)[:12]
            row=dict(at=datetime.now().astimezone().isoformat(),monotonic=mono,os=os_row,
                current_processes_top_cpu=top,runner_processes=[processes[p] for p in sorted(active_pids)],
                sampler_process=processes.get(os.getpid()),heartbeat=hb,runner_active=active,
                drive_free_bytes=shutil.disk_usage(root).free,
                rate_basis='PROCESSED_TRADES_FROM_HEARTBEAT_NOT_RAW_0B_OR_0D_RECEIVE_RATE',processed_trade_rate=None)
            fresh=hb['heartbeat_age_seconds'] is not None and -1<=hb['heartbeat_age_seconds']<=10
            row['heartbeat_fresh']=fresh
            if previous and fresh and previous['heartbeat_fresh'] and active and previous['runner_active'] and previous['heartbeat']['pid']==pid and hb['time']!=previous['heartbeat']['time']:
                try:dt=(datetime.fromisoformat(hb['time'])-datetime.fromisoformat(previous['heartbeat']['time'])).total_seconds()
                except (TypeError,ValueError):dt=0
                row['processed_trade_rate']=counter_rate(previous['heartbeat']['counts'].get('ticks'),hb['counts'].get('ticks'),dt)
            records.append(row);stream.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n');stream.flush();previous=row
            remaining=seconds-(time.monotonic()-started)
            if remaining<=0:break
            time.sleep(min(interval,remaining))
    active_rows=[r for r in records if r['runner_active'] and r['heartbeat_fresh']]
    summary=dict(started=records[0]['at'],finished=records[-1]['at'],duration_seconds=time.monotonic()-started,
        sampled_rows=len(records),active_runner_rows=len(active_rows),cpu_percent=distribution([r['os']['cpu_percent'] for r in records]),
        available_ram_gib=distribution([r['os']['ram_available_bytes']/1024**3 for r in records]),
        queue_depth=distribution([r['heartbeat']['queue_depth'] for r in active_rows]),
        loop_lag_seconds=distribution([r['heartbeat']['loop_lag_seconds'] for r in active_rows]),
        processed_trade_rate=distribution([r['processed_trade_rate'] for r in active_rows]),
        scope='CURRENT_OBSERVED_WINDOW_ONLY_NOT_OPENING_CAPACITY_CERTIFICATION',
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        output_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),live_mutations=False,
        safe_watch_max=None,safe_shadow_max=None,missed_entry_opportunities=None)
    (out/'resource_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--seconds',type=float,default=60);p.add_argument('--interval',type=float,default=2)
    a=p.parse_args()
    if not math.isfinite(a.seconds) or not 1<=a.seconds<=900 or not math.isfinite(a.interval) or not .5<=a.interval<=30:p.error('seconds1..900 interval0.5..30')
    print(json.dumps(collect(a.root,a.out,a.seconds,a.interval),ensure_ascii=False,indent=2))

if __name__=='__main__':
    # Direct execution puts tools/, not the project root, on sys.path.
    # Keep the shared reader available for the documented standalone CLI.
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    main()
