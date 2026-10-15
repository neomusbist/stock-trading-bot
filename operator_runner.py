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
from automation.session import supported,control_supported,after_control_supported
from operator_process import process_identity

def session_end(root,clock):
    cfg=load_config(root/'config.json')
    after_ok=getattr(cfg,'after_enabled',False) and after_control_supported(clock())
    if control_supported(clock()) or after_ok: return False
    with ProcessLock(root/'runtime/control.lock'):
        policy=Policy(root/'runtime/automation_policy.json')
        # Hold an already authorized LIVE runner through the regular/AFTER gap.
        # The engine closes session clients and its order guard remains OFF.
        # Explicit STOP/KILL and invalid/expired approvals never auto-resume.
        stamp=clock();t_str=stamp.strftime('%H:%M:%S')
        if (cfg.after_enabled and '15:20:00'<=t_str<'15:40:00' and policy.mode=='LIVE'
                and policy.state.get('live_authorized') and policy.state.get('after_live_authorized')):
            from operator_service import OperatorService
            from automation.readiness import authorization_valid
            auto=load_automation(root/'automation.json')
            valid,reasons=authorization_valid(root,cfg,auto,policy.state,stamp)
            counts=OperatorService(root,clock=clock).state_counts()
            # Only the known support-window gap may be missing; all identity,
            # approval and ledger checks still apply. No fake approval is saved.
            if reasons==['OUTSIDE_PREOPEN_CONTROL'] and not any(counts.values()):return False
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
        cfg=load_config(root/'config.json'); auto=load_automation(root/'automation.json')
        after_ok=getattr(cfg,'after_enabled',False) and after_control_supported(clock())
        if not control_supported(clock()) and not after_ok: raise ValueError('지원시간 밖: 계속 감시를 시작하지 않습니다.')
        with ProcessLock(root/'runtime/supervisor.lock'),ProcessLock(root/'runtime/process.lock'): pass
        policy=Policy(root/'runtime/automation_policy.json'); initial=policy.mode
        from automation.release_integrity import check_runtime_integrity
        integrity = check_runtime_integrity(root)
        if purpose=='CONTINUOUS' and cfg.live_mode:
            from automation.decisions import check_experiment_consistency
            try:
                check_experiment_consistency(cfg)
            except ValueError as e:
                atomic_json(root/'runtime/operator_runner.json',{'pid':os.getpid(),'identity':process_identity(os.getpid()),'token':token,'purpose':purpose,
                    'started':clock().isoformat(),'initial_mode':initial,'mode':initial,'status':'CRASHED',
                    'finished':clock().isoformat(),'heartbeat':clock().isoformat(),'exit_code':1,
                    'monitor_error':str(e),'error_event':'EXPERIMENT_CONSISTENCY_FAILURE'})
                raise
            if policy.mode == 'LIVE' and not integrity['valid']:
                err_msg = f"UNVERIFIED_CODE_BLOCKED_FOR_LIVE: {integrity['reasons']}"
                atomic_json(root/'runtime/operator_runner.json',{'pid':os.getpid(),'identity':process_identity(os.getpid()),'token':token,'purpose':purpose,
                    'started':clock().isoformat(),'initial_mode':initial,'mode':initial,'status':'CRASHED',
                    'finished':clock().isoformat(),'heartbeat':clock().isoformat(),'exit_code':1,
                    'monitor_error':err_msg,'error_event':'CODE_INTEGRITY_MISMATCH'})
                raise ValueError(err_msg)
        record={'pid':os.getpid(),'identity':process_identity(os.getpid()),'token':token,'purpose':purpose,
                'started':clock().isoformat(),'initial_mode':initial,'mode':initial,'status':'STARTING','heartbeat':clock().isoformat(),
                'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),
                'code_commit':integrity['commit'],
                'fast_momentum_hash':integrity['fast_momentum_short_hash'],
                'code_verified':integrity['valid']}
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
    cfg=load_config(ROOT/'config.json')
    after_ok=getattr(cfg,'after_enabled',False) and after_control_supported(now())
    if not control_supported(now()) and not after_ok:
        from local_diagnostics import diagnose
        import json
        print(json.dumps(diagnose(ROOT,now()),ensure_ascii=False)); raise SystemExit(0)
    try: raise SystemExit(run(token=args.token,purpose=args.purpose))
    except Exception as error: print('감시 시작/상태 오류:',error); raise SystemExit(1)
