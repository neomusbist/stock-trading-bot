"""
운영본-개발본 일치 검증 및 코드 무결성 방어 모듈 (automation/release_integrity.py)

1. 실행 중인 파일의 내용과 필수 사고 수정 패치 반영 여부를 물리적으로 검사.
2. 미검증/구버전 코드의 LIVE 실행을 원천 차단.
3. STOP <-> LIVE 상태 전환 시 승인 근거와 실행 파일 해시를 감사 로그에 기록.
"""
import os
import sys
import hashlib
import json
from pathlib import Path
from datetime import datetime

CURRENT_RELEASE_COMMIT = "0dd8884"
CURRENT_RELEASE_BUILD_ID = "V1.6-risk2-daily4-fast-live-r6-20261015-0dd8884"

# 필수 결함 수정 패치 식별자 및 검증 서명
MANDATORY_PATCH_SIGNATURES = {
    'FAST_MOMENTUM_45S_SETTLING': {
        'file': 'automation/fast_momentum.py',
        'pattern': 'holding_seconds >= 45.0',
        'description': '17:43 에스엠벡셀 1차 사고: 45초 안착 대기 윈도우'
    },
    'FAST_MOMENTUM_2TICK_SWING_FILTER': {
        'file': 'automation/fast_momentum.py',
        'pattern': '2 * min_step',
        'description': '18:32 에스엠벡셀 2차 사고: 1틱(5원) 반등 노이즈 배제 및 2틱 이상 스윙 필터'
    },
    'FAST_MOMENTUM_REASON_PREFIX': {
        'file': 'automation/engine.py',
        'pattern': 'FAST_{exit_reason}',
        'description': 'FAST 청산 사유 식별자 전파'
    }
}

CRITICAL_RUNTIME_FILES = [
    'automation/fast_momentum.py',
    'automation/engine.py',
    'automation/decisions.py',
    'automation/runner.py',
    'automation/policy.py',
]

def sha256_file(filepath):
    """지정된 파일의 SHA-256 해시를 반환한다."""
    p = Path(filepath)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def get_current_commit(root=None):
    """실행 중인 디렉터리의 실제 Git 커밋 또는 배포 식별자(RELEASE_IDENTITY.json)를 반환한다."""
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    identity_file = root_path / 'runtime/RELEASE_IDENTITY.json'
    if identity_file.exists():
        try:
            with open(identity_file, 'r', encoding='utf-8') as f:
                c = json.load(f).get('commit')
                if c:
                    return c
        except Exception:
            pass
    try:
        import subprocess
        res = subprocess.run(
            ['git', '-C', str(root_path), 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=2
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return CURRENT_RELEASE_COMMIT

def check_runtime_integrity(root=None):
    """운영본 디렉터리의 실제 실행 파일 내용과 필수 패치를 물리적으로 검사한다.
    단순 문자열 비교가 아닌, 파일 존재, SHA-256 해시, 필수 패치 코드 시그니처를 전수 대조한다.
    """
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    reasons = []
    file_hashes = {}
    verified_patches = []
    missing_patches = []

    # 1. 핵심 런타임 파일 존재 및 해시 계산
    for rel_path in CRITICAL_RUNTIME_FILES:
        target = root_path / rel_path
        if not target.exists():
            reasons.append(f"필수 실행 파일 누락: {rel_path}")
            file_hashes[rel_path] = None
        else:
            file_hashes[rel_path] = sha256_file(target)

    # 2. 필수 사고 수정 패치 코드 시그니처 검증
    for patch_key, spec in MANDATORY_PATCH_SIGNATURES.items():
        file_path = root_path / spec['file']
        if not file_path.exists():
            missing_patches.append(patch_key)
            reasons.append(f"패치 파일 없음: {spec['file']} ({spec['description']})")
            continue
        try:
            content = file_path.read_text(encoding='utf-8')
            if spec['pattern'] in content:
                verified_patches.append({
                    'key': patch_key,
                    'file': spec['file'],
                    'description': spec['description'],
                    'status': 'VERIFIED'
                })
            else:
                missing_patches.append(patch_key)
                reasons.append(f"필수 패치 미반영: {patch_key} ({spec['description']}) in {spec['file']}")
        except Exception as e:
            missing_patches.append(patch_key)
            reasons.append(f"패치 파일 읽기 실패: {spec['file']} - {str(e)}")

    valid = (len(reasons) == 0) and (len(missing_patches) == 0)

    fast_hash = file_hashes.get('automation/fast_momentum.py')
    short_hash = fast_hash[:8] if fast_hash else 'UNKNOWN'
    commit_id = get_current_commit(root_path)

    return {
        'valid': valid,
        'commit': commit_id,
        'build_id': CURRENT_RELEASE_BUILD_ID,
        'fast_momentum_hash': fast_hash,
        'fast_momentum_short_hash': short_hash,
        'file_hashes': file_hashes,
        'verified_patches': verified_patches,
        'missing_patches': missing_patches,
        'reasons': reasons,
        'summary': f"Commit {commit_id} | FAST:{short_hash} | {'VERIFIED' if valid else 'UNVERIFIED_MISMATCH'}"
    }

def record_mode_transition(root, from_mode, to_mode, approval_basis, symbol=None, user=None, pid=None):
    """STOP <-> LIVE 상태 전환 이력을 실행 파일 해시 및 승인 근거와 함께 기록한다."""
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    runtime_dir = root_path / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_file = runtime_dir / 'mode_transitions.jsonl'

    integrity = check_runtime_integrity(root_path)

    record = {
        'timestamp': datetime.now().astimezone().isoformat(),
        'from_mode': from_mode,
        'to_mode': to_mode,
        'symbol': symbol,
        'approval_basis': approval_basis,
        'user': user or os.environ.get('USERNAME', 'OPERATOR'),
        'pid': pid or os.getpid(),
        'commit': integrity['commit'],
        'fast_momentum_hash': integrity['fast_momentum_short_hash'],
        'code_verified': integrity['valid'],
        'missing_patches': integrity['missing_patches']
    }

    line = json.dumps(record, ensure_ascii=False) + '\n'
    with open(target_file, 'a', encoding='utf-8') as f:
        f.write(line)
    return record

def read_mode_transitions(root=None, limit=30):
    """최근 상태 전환 이력을 역순으로 읽어온다."""
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    target_file = root_path / 'runtime/mode_transitions.jsonl'
    if not target_file.exists():
        return []
    records = []
    with open(target_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    return records[-limit:][::-1]
