"""로컬 실행은 시간 제한 없음. 계좌 진단은 명시 선택한 읽기 API만 사용한다."""
import os
from dataclasses import replace
from pathlib import Path
from core.settings import load_config
from core.util import atomic_json, read_json_retry
from automation.policy import load_automation
from automation.session import assessment, supported

def diagnose(root,stamp):
    root=Path(root); cfg=load_config(root/'config.json'); auto=load_automation(root/'automation.json')
    policy_path=root/'runtime/automation_policy.json'
    policy=read_json_retry(policy_path) if policy_path.exists() else {'mode':'ARMED','live_authorized':False}
    result={**assessment(stamp),'timestamp':stamp.isoformat(),'heartbeat':stamp.isoformat(),
        'status':'NORMAL','reason_code':'LOCAL_DIAGNOSTICS' if supported(stamp) else 'OUTSIDE_TRADING_WINDOW',
        'mode':policy['mode'],'live_mode':cfg.live_mode,'approved_cap':auto.capital_limit,
        'live_authorized':bool(policy.get('live_authorized')),'network_calls':0,'real_orders_sent':0,
        'notice':'15:20은 V1.1 자동매매 운영 종료입니다. 프로그램 실행/메뉴/로컬 진단은 시간 제한이 없습니다.',
        'state_files_checked':0,'file_io':'PASS','halt_present':False}
    for path in (root/'runtime').glob('automation-*/state.json'):
        state=read_json_retry(path); result['state_files_checked']+=1
        result['halt_present']|=bool(state.get('halt'))
    # 감시 heartbeat/장부/정책을 변경하지 않는 별도 로컬 진단 heartbeat.
    atomic_json(root/'runtime/local_diagnostics.json',result)
    read_json_retry(root/'runtime/local_diagnostics.json')
    return result

def redact(value):
    """응답코드·문구는 보존하되 인증정보 필드는 저장하지 않는다."""
    sensitive={'token','access_token','refresh_token','appkey','secretkey','authorization','password'}
    if isinstance(value,dict): return {k:('[REDACTED]' if k.lower() in sensitive else redact(v)) for k,v in value.items()}
    if isinstance(value,list): return [redact(v) for v in value]
    if isinstance(value,str):
        import re
        value=re.sub(r'(?i)Bearer\s+[A-Za-z0-9._-]+','Bearer [REDACTED]',value)
        for name in ('KIWOOM_APP_KEY','KIWOOM_APP_SECRET'):
            secret=os.getenv(name,'')
            if secret: value=value.replace(secret,'[REDACTED]')
    return value

def account_diagnostic(cfg,root,stamp,factory=None):
    from core.broker import Rest,Rejected
    from urllib.error import HTTPError
    import json
    class ReadOnlyRest(Rest):
        def _http(self,path,body,headers):
            api=headers.get('api-id')
            if not (path=='/oauth2/token' and api is None) and api not in {'kt00018','kt00001','ka10075','ka10076'}:
                raise Rejected('READ_ONLY_DIAGNOSTIC_API_DENIED',api=api)
            try: return super()._http(path,body,headers)
            except HTTPError as error:
                raw=error.read().decode('utf-8',errors='replace')
                try: response=json.loads(raw)
                except ValueError: response={'response_text':raw}
                raise Rejected(f'HTTP {error.code}: {error.reason}',api=api,
                    response={'http_code':error.code,'response':response}) from error
        def mutation_allowed(self,api,body): return False
    result={'timestamp':stamp.isoformat(),'read_only':True,'real_orders_sent':0,'verdict':'INCONCLUSIVE'}
    try:
        rest=(factory or ReadOnlyRest)(replace(cfg,live_mode=False))
        snap=rest.snapshot(); fills=rest.fills()
        result.update(verdict='PASS',cash=snap['cash'],holdings_count=len(snap['holdings']),orders_count=len(snap['orders']),fills_count=len(fills))
    except Exception as error:
        result.update(exception_type=type(error).__name__,message=redact(str(error)),api=getattr(error,'api',None),
            response=redact(getattr(error,'response',None)),http_code=getattr(error,'code',None),
            notice='원인을 장마감으로 단정하지 않음. 조회 실패는 별도 진단이며 HALT/주문 상태를 변경하지 않음.')
    atomic_json(Path(root)/'runtime/account_diagnostic.json',result)
    return result
