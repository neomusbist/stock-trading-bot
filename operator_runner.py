"""별도 운영 프로세스: 기존 supervisor.main을 그대로 실행하고 상태만 기록한다."""
import os
import sys
import threading
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
from core.settings import now,load_config
from core.util import ProcessLock,atomic_json,exception_details
from automation.policy import Policy,load_automation,legacy_test_release_enabled
from automation.session import supported,control_supported
from operator_process import process_identity

def session_end(root,clock):
    if control_supported(clock()): return False
    with ProcessLock(root/'runtime/control.lock'):
        policy=Policy(root/'runtime/automation_policy.json')
        if policy.mode in {'ARMED','LIVE'}: policy.command('STOP')
    return True

def restore_armed(root,initial_mode):
    if initial_mode!='ARMED': return False
    from operator_service import OperatorService
    service=OperatorService(root)
    with ProcessLock(root/'runtime/control.lock'):
        cfg,auto,data=service.settings(); counts=service.state_counts()
        if (data['mode']!='STOP'
            or counts['positions'] or counts['pending'] or data.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}): return False
        Policy(root/'runtime/automation_policy.json').command('ARMED'); return True

def run(root=ROOT,clock=now,supervise=None,token=None,purpose='CONTINUOUS'):
    if purpose=='TEST_CAPITAL_RELEASE' and not legacy_test_release_enabled():
        raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 시험 전용 실행은 종료됐습니다.')
    root=Path(root); token=token or uuid.uuid4().hex
    with ProcessLock(root/'runtime/operator_runner.lock'):
        if not control_supported(clock()): raise ValueError('지원시간 밖: 계속 감시를 시작하지 않습니다.')
        with ProcessLock(root/'runtime/supervisor.lock'),ProcessLock(root/'runtime/process.lock'): pass
        policy=Policy(root/'runtime/automation_policy.json'); initial=policy.mode
        record={'pid':os.getpid(),'identity':process_identity(os.getpid()),'token':token,'purpose':purpose,
                'started':clock().isoformat(),'initial_mode':initial,'mode':initial,'status':'STARTING','heartbeat':clock().isoformat(),
                'config_hash':load_config(root/'config.json').fingerprint(),'auto_hash':load_automation(root/'automation.json').fingerprint()}
        path=root/'runtime/operator_runner.json'; atomic_json(path,record)
        done=threading.Event()
        def update():
            while not done.wait(.5):
                try:
                    ended=session_end(root,clock); policy.refresh()
                    record.update(status='SESSION_END_WAIT' if ended else 'RUNNING',mode=policy.mode,heartbeat=clock().isoformat())
                    atomic_json(path,record)
                except Exception as error:
                    record['monitor_error']=str(error)
                    # 상태 파일 자체가 실패해도 stderr에 전체 원인을 보존한다.
                    import json
                    print(json.dumps({'event':'PROCESS_HEARTBEAT_ERROR','target_path':str(path),
                        **exception_details(error)},ensure_ascii=False),file=sys.stderr,flush=True)
        thread=threading.Thread(target=update,name='operator-status',daemon=True); thread.start()
        code=1
        try:
            if supervise is None:
                import supervisor
                supervise=supervisor.main
            # 사용자 메뉴 인자를 기존 supervisor/main에 전달하지 않는다.
            old=sys.argv; sys.argv=[str(root/'supervisor.py')]
            try: code=supervise()
            finally: sys.argv=old
            return code
        finally:
            done.set(); thread.join(timeout=2)
            if code==0: restore_armed(root,initial)
            policy.refresh(); record.update(status='STOPPED' if code==0 else 'CRASHED',mode=policy.mode,
                finished=clock().isoformat(),heartbeat=clock().isoformat(),exit_code=code)
            atomic_json(path,record)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--token',required=True); parser.add_argument('--purpose',default='CONTINUOUS')
    args=parser.parse_args()
    if not control_supported(now()):
        from local_diagnostics import diagnose
        import json
        print(json.dumps(diagnose(ROOT,now()),ensure_ascii=False)); raise SystemExit(0)
    try: raise SystemExit(run(token=args.token,purpose=args.purpose))
    except Exception as error: print('감시 시작/상태 오류:',error); raise SystemExit(1)
