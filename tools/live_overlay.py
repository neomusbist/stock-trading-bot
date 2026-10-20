"""Explicitly approved, five-file operational overlay; never starts trading.

Default CLI mode is read-only check. Apply and rollback require --approved,
STOP, no running target process, flat bot inventory and the normal runtime OS
locks. Ledger/settings/approval receipts are never restored or overwritten.
"""
import argparse
from contextlib import ExitStack,contextmanager
from datetime import datetime
import hashlib,json,os,platform,subprocess,tempfile,time
from pathlib import Path

ALLOWED={'automation/runner.py','automation/observation_worker.py','automation/engine.py',
         'core/engine.py','runtime/RELEASE_IDENTITY.json'}
LOCKS=('operator_launch.lock','operator_runner.lock','supervisor.lock','process.lock','control.lock')

def digest(path):
    path=Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def inside(root,rel):
    root=Path(root).resolve();path=(root/rel).resolve()
    if path==root or root not in path.parents:raise ValueError('PATH_OUTSIDE_TARGET')
    return path

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def running_processes(target):
    if os.name!='nt':return []
    env=dict(os.environ,R6_OVERLAY_TARGET=str(Path(target).resolve()),R6_OVERLAY_PID=str(os.getpid()))
    script="""$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | Where-Object {
      $_.ProcessId -ne [int]$env:R6_OVERLAY_PID -and $_.Name -match '^(python|pythonw|python3|operator_runner|supervisor).*\\.exe$' -and
      (-not $_.CommandLine -or $_.CommandLine.IndexOf($env:R6_OVERLAY_TARGET,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
       $_.CommandLine -match '(operator_gui|operator_runner|supervisor)\\.py|main\\.py.*--automation')
    } | Select-Object ProcessId,Name) | ConvertTo-Json -Compress"""
    completed=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
        capture_output=True,text=True,env=env,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:raise RuntimeError('PROCESS_INSPECTION_FAILED')
    result=json.loads(completed.stdout.strip() or '[]')
    return result if isinstance(result,list) else [result]

def standby(target):
    processes=running_processes(target)
    if processes:raise RuntimeError('TARGET_PROCESS_RUNNING: '+json.dumps(processes))
    policy=read(inside(target,'runtime/automation_policy.json'))
    if policy.get('mode')!='STOP':raise RuntimeError('STOP_REQUIRED')
    state=read(inside(target,'runtime/automation-production/state.json'))
    active=[o for o in state.get('orders',{}).values() if o.get('status') not in {'FILLED','CANCELED','REJECTED'}]
    if state.get('positions') or active or state.get('halt'):raise RuntimeError('FLAT_RESOLVED_LEDGER_REQUIRED')

def verify_identity(target):
    identity=read(inside(target,'runtime/RELEASE_IDENTITY.json'))
    declared=identity.get('files')
    if not isinstance(declared,dict) or not declared:raise RuntimeError('INVALID_RELEASE_MANIFEST')
    mismatch=[rel for rel,h in declared.items() if digest(inside(target,rel))!=h]
    if mismatch:raise RuntimeError('RELEASE_HASH_MISMATCH: '+','.join(mismatch))
    return identity

def check(bundle,target):
    bundle=Path(bundle).resolve();target=Path(target).resolve();manifest=read(bundle/'manifest.json')
    if str(target)!=str(Path(manifest['target']).resolve()):raise RuntimeError('EXACT_TARGET_REQUIRED')
    if platform.node().upper()!=manifest['machine'].upper():raise RuntimeError('WRONG_MACHINE')
    if set(manifest['files'])!=ALLOWED:raise RuntimeError('EXACT_FIVE_FILE_ALLOWLIST_REQUIRED')
    standby(target);verify_identity(target)
    for rel,record in manifest['files'].items():
        if digest(inside(target,rel))!=record['before']:raise RuntimeError('TARGET_CHANGED: '+rel)
        if digest(inside(bundle/'payload',rel))!=record['after']:raise RuntimeError('PAYLOAD_CHANGED: '+rel)
    for rel,h in manifest['protected_hashes'].items():
        if digest(inside(target,rel))!=h:raise RuntimeError('PROTECTED_STATE_CHANGED: '+rel)
    if manifest.get('validation_status')!='PASS':raise RuntimeError('VALIDATION_REQUIRED')
    return manifest

@contextmanager
def runtime_locks(target):
    # Same nonblocking OS locks used by the existing Windows launcher/runner.
    # No PID, policy or ledger content is written to these existing lock files.
    with ExitStack() as stack:
        for name in LOCKS:
            path=inside(target,'runtime/'+name);path.parent.mkdir(parents=True,exist_ok=True)
            handle=stack.enter_context(path.open('a+b'));handle.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as error:raise RuntimeError('RUNTIME_LOCK_BUSY: '+name) from error
        yield

def atomic_bytes(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=path.name+'.overlay-',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        for attempt in range(8):
            try:os.replace(temporary,path);break
            except OSError as error:
                if not isinstance(error,PermissionError) and getattr(error,'winerror',None) not in (5,32):raise
                if attempt==7:raise
                time.sleep(min(.5,.015*2**attempt))
    finally:
        if temporary.exists():temporary.unlink()

def restore(backup,target,records):
    for rel,record in records.items():
        source=inside(backup/'files',rel)
        if digest(source)!=record['before']:raise RuntimeError('BACKUP_HASH_MISMATCH: '+rel)
        atomic_bytes(inside(target,rel),source.read_bytes())
    verify_identity(target)

def apply(bundle,target,backup_root,*,approved=False):
    if not approved:raise PermissionError('EXPLICIT_USER_APPROVAL_REQUIRED')
    bundle=Path(bundle).resolve();target=Path(target).resolve();backup_root=Path(backup_root).resolve()
    # Backups must stay outside the running tree and cannot overwrite a prior run.
    if backup_root==target or target in backup_root.parents:raise ValueError('BACKUP_MUST_BE_OUTSIDE_LIVE')
    with runtime_locks(target):
        manifest=check(bundle,target)
        backup=backup_root/('overlay-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'));backup.mkdir(parents=True,exist_ok=False)
        for rel in manifest['files']:
            dest=inside(backup/'files',rel);dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(inside(target,rel).read_bytes())
        receipt={'target':str(target),'release_id':manifest['release_id'],'files':manifest['files'],
            'protected_hashes':manifest['protected_hashes'],'machine':manifest['machine'],'status':'BACKED_UP'}
        atomic_bytes(backup/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8'))
        try:
            for rel in sorted(manifest['files'],key=lambda r:r=='runtime/RELEASE_IDENTITY.json'):
                atomic_bytes(inside(target,rel),inside(bundle/'payload',rel).read_bytes())
            verify_identity(target)
            for rel,r in manifest['files'].items():
                if digest(inside(target,rel))!=r['after']:raise RuntimeError('POSTCHECK_FAILED: '+rel)
            for rel,h in manifest['protected_hashes'].items():
                if digest(inside(target,rel))!=h:raise RuntimeError('PROTECTED_STATE_CHANGED_DURING_DEPLOY: '+rel)
        except Exception:
            restore(backup,target,manifest['files'])
            receipt['status']='FAILED_ROLLED_BACK'
            atomic_bytes(backup/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8'))
            raise
        receipt['status']='APPLIED_STOPPED';receipt['completed_at']=datetime.now().astimezone().isoformat()
        atomic_bytes(backup/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8'))
        return {'status':receipt['status'],'backup':str(backup),'release_id':manifest['release_id'],'trading_started':False}

def rollback(backup,target,*,approved=False):
    if not approved:raise PermissionError('EXPLICIT_USER_APPROVAL_REQUIRED')
    backup=Path(backup).resolve();target=Path(target).resolve();receipt=read(backup/'receipt.json')
    if str(target)!=receipt['target']:raise RuntimeError('EXACT_TARGET_REQUIRED')
    if platform.node().upper()!=receipt['machine'].upper():raise RuntimeError('WRONG_MACHINE')
    if set(receipt['files'])!=ALLOWED:raise RuntimeError('EXACT_FIVE_FILE_ALLOWLIST_REQUIRED')
    with runtime_locks(target):
        standby(target)
        for rel in ('config.json','automation.json'):
            if digest(inside(target,rel))!=receipt['protected_hashes'][rel]:raise RuntimeError('SETTINGS_CHANGED_SINCE_DEPLOY: '+rel)
        for rel,record in receipt['files'].items():
            if digest(inside(target,rel))!=record['after']:raise RuntimeError('TARGET_CHANGED_SINCE_DEPLOY: '+rel)
        protected={rel:digest(inside(target,rel)) for rel in receipt['protected_hashes']}
        restore(backup,target,receipt['files'])
        if any(digest(inside(target,p))!=h for p,h in protected.items()):raise RuntimeError('ROLLBACK_CHANGED_PROTECTED_STATE')
        receipt['status']='ROLLED_BACK_STOPPED'
        atomic_bytes(backup/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2).encode('utf-8'))
        return {'status':receipt['status'],'ledger_restored':False,'trading_started':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=('check','apply','rollback'),default='check')
    parser.add_argument('--target',type=Path,required=True);parser.add_argument('--bundle',type=Path)
    parser.add_argument('--backup-root',type=Path);parser.add_argument('--backup',type=Path)
    parser.add_argument('--approved',action='store_true');args=parser.parse_args()
    if args.mode=='check':
        manifest=check(args.bundle,args.target);result={'status':'READY_PENDING_USER_APPROVAL','release_id':manifest['release_id'],'changes':sorted(manifest['files']),'config_changes':[]}
    elif args.mode=='apply':result=apply(args.bundle,args.target,args.backup_root,approved=args.approved)
    else:result=rollback(args.backup,args.target,approved=args.approved)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
