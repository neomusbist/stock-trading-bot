"""V1.3 당일 명시 승인. 시간/설정/보호/중요 장부 상태에 결합하며 주문을 생성하지 않는다."""
import hashlib,json
from pathlib import Path
from core.util import read_json_retry
from automation.policy import HARD_LOCKS,EXISTING_POLICIES,BREAKEVEN_EXITS
from automation.decisions import BUILD_ID
from automation.session import control_supported, after_control_supported

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def policy_hash(policy):
    # STOP/LIVE 이동과 명령 이력은 정책 내용이 아니다. 보호정책/해제/시험승인은 내용이다.
    policy=getattr(policy,'state',policy)
    return digest({'hard_locks':sorted(HARD_LOCKS),'existing':EXISTING_POLICIES,
        'breakeven':BREAKEVEN_EXITS,'releases':policy.get('releases',{}),
        'test_capital_release':policy.get('test_capital_release',{})})

def security_state(root,own_intent=None):
    # 주문 guard가 현재 제출 중임을 증명한 정확한 장부/레코드 하나만 제외한다.
    # 상태조회/승인 생성은 기본값을 사용하므로 모든 INTENT를 계속 불명확으로 본다.
    states=[]
    for path in sorted((Path(root)/'runtime').glob('automation-*/state.json')):
        if path.parent.name=='automation-smoke':continue
        value=read_json_retry(path)
        allowed=None
        if own_intent is not None:
            expected_path,expected=own_intent
            local=expected.get('local')
            if (path.resolve()==Path(expected_path).resolve() and local
                and expected.get('status')=='INTENT' and expected.get('side')=='BUY'
                and value.get('orders',{}).get(local)==expected):
                allowed=local
        states.append({'ledger':path.parent.name,'halt':value.get('halt') or '',
            'uncertain':sorted(k for k,o in value.get('orders',{}).items() if o.get('status') in {'UNKNOWN','INTENT'} and k!=allowed)})
    # Empty healthy ledgers may be created by first start. This is not a safety-state change.
    return [s for s in states if s['halt'] or s['uncertain']]

def authorization_valid(root,cfg,auto,policy,stamp,own_intent=None):
    path=Path(root)/'runtime/operator_user_authorization.json'
    receipt=read_json_retry(path) if path.exists() else {}
    reasons=[]
    if receipt.get('authorization_kind')!='DAILY_PREOPEN_V13':reasons.append('DAILY_LIVE_APPROVAL_REQUIRED')
    if receipt.get('trading_day')!=stamp.date().isoformat():reasons.append('DAILY_LIVE_APPROVAL_EXPIRED')
    if receipt.get('build_id')!=BUILD_ID:reasons.append('LIVE_APPROVAL_BUILD_MISMATCH')
    if receipt.get('config_hash')!=cfg.fingerprint() or receipt.get('auto_hash')!=auto.fingerprint():reasons.append('LIVE_APPROVAL_SETTINGS_MISMATCH')
    if receipt.get('policy_hash')!=policy_hash(policy):reasons.append('LIVE_APPROVAL_POLICY_MISMATCH')
    current=security_state(root,own_intent=own_intent)
    if current or receipt.get('security_state_hash')!=digest(current):reasons.append('LIVE_APPROVAL_CRITICAL_STATE_CHANGED')
    is_ctrl=control_supported(stamp) or (getattr(cfg,'after_enabled',False) and after_control_supported(stamp))
    if not is_ctrl:reasons.append('OUTSIDE_PREOPEN_CONTROL')
    return not reasons,reasons
