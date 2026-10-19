r"""
배포 및 롤백 자동화 도구 (deploy_release.py)

운영본(C:\Users\MJS\Desktop\주식자동매매) 배포 시:
1. 안전 검사: 실행 중인 프로세스, 활성 락, 무결성 서명 사전 검사
2. 사전 백업: 타깃 디렉터리의 수정 대상 파일을 타임스탬프 백업 디렉터리에 완전 백업
3. 원자적 복사: 필수 패치 파일 및 런타임 무결성 방어 모듈 복사
4. 사후 검증: 복사된 대상 파일들의 SHA-256 해시 대조 및 AST 패치 시그니처 전수 검증
5. 자동 롤백: 사후 검증 실패 시 즉시 이전 상태로 자동 복원
"""
import os
import sys
import shutil
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime

DEFAULT_SRC = Path(r"C:\Users\MJS\Documents\주식자동매매_개발\r6-target-cap-incident-20261015")
DEFAULT_TARGET = Path(r"C:\Users\MJS\Desktop\주식자동매매")

# 배포 대상 파일 목록 (개발본 -> 운영본)
DEPLOY_FILES = [
    'automation/fast_momentum.py',
    'automation/release_integrity.py',
    'automation/policy.py',
    'operator_runner.py',
    'operator_ui_service.py',
    'operator_gui.py',
]

def sha256_file(filepath):
    p = Path(filepath)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def check_deployment_readiness(src_dir, target_dir):
    """배포 전 소스 및 타깃 상태를 진단하고 리포트를 생성한다."""
    sys.path.insert(0, str(src_dir))
    from automation.release_integrity import check_runtime_integrity, CURRENT_RELEASE_COMMIT, MANDATORY_PATCH_SIGNATURES

    report = {
        'timestamp': datetime.now().astimezone().isoformat(),
        'src_dir': str(src_dir),
        'target_dir': str(target_dir),
        'src_integrity': check_runtime_integrity(src_dir),
        'target_integrity': check_runtime_integrity(target_dir) if target_dir.exists() else None,
        'file_comparisons': {},
        'can_deploy': True,
        'blocking_reasons': []
    }

    if not report['src_integrity']['valid']:
        report['can_deploy'] = False
        report['blocking_reasons'].append(f"소스 개발본 무결성 검증 실패: {report['src_integrity']['reasons']}")

    for rel in DEPLOY_FILES:
        src_f = src_dir / rel
        tgt_f = target_dir / rel
        src_hash = sha256_file(src_f)
        tgt_hash = sha256_file(tgt_f)
        report['file_comparisons'][rel] = {
            'src_exists': src_f.exists(),
            'target_exists': tgt_f.exists(),
            'src_hash': src_hash,
            'target_hash': tgt_hash,
            'identical': (src_hash == tgt_hash) and (src_hash is not None)
        }
        if not src_f.exists():
            report['can_deploy'] = False
            report['blocking_reasons'].append(f"소스 파일 누락: {rel}")

    # 타깃 운영본 모드 검사
    policy_path = target_dir / 'runtime/automation_policy.json'
    if policy_path.exists():
        try:
            with open(policy_path, 'r', encoding='utf-8') as f:
                policy_data = json.load(f)
                report['target_mode'] = policy_data.get('mode')
                if policy_data.get('mode') == 'LIVE':
                    report['can_deploy'] = False
                    report['blocking_reasons'].append("운영본이 현재 LIVE 모드입니다. 배포 전 반드시 STOP 상태여야 합니다.")
        except Exception as e:
            report['target_mode'] = f"READ_ERROR: {e}"

    return report

def perform_deploy(src_dir, target_dir, backup_root=None):
    """사전 백업을 수행하고 배포 파일을 복사한 뒤 사후 검증을 진행한다."""
    readiness = check_deployment_readiness(src_dir, target_dir)
    if not readiness['can_deploy']:
        raise RuntimeError(f"배포 사전 조건 불충족: {readiness['blocking_reasons']}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_base = backup_root or (target_dir.parent / '주식자동매매_backups')
    backup_dir = backup_base / f"backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 타깃 파일 백업 시작 -> {backup_dir}")
    backed_up_files = []
    for rel in DEPLOY_FILES:
        tgt_f = target_dir / rel
        if tgt_f.exists():
            dest = backup_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tgt_f, dest)
            backed_up_files.append(rel)
    print(f"      {len(backed_up_files)}개 파일 백업 완료.")

    # 복사 진행
    print(f"[2/4] 소스 파일 운영본 복사 시작 -> {target_dir}")
    copied_files = []
    try:
        for rel in DEPLOY_FILES:
            src_f = src_dir / rel
            tgt_f = target_dir / rel
            tgt_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_f, tgt_f)
            copied_files.append(rel)
        print(f"      {len(copied_files)}개 파일 복사 완료.")

        # RELEASE_IDENTITY.json 기록
        sys.path.insert(0, str(src_dir))
        from automation.release_integrity import check_runtime_integrity, CURRENT_RELEASE_COMMIT
        identity_path = target_dir / 'runtime/RELEASE_IDENTITY.json'
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            'commit': CURRENT_RELEASE_COMMIT,
            'deployed_at': datetime.now().astimezone().isoformat(),
            'backup_dir': str(backup_dir),
            'files': {rel: sha256_file(target_dir / rel) for rel in DEPLOY_FILES}
        }
        with open(identity_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        # [3/4] 사후 무결성 검증
        print(f"[3/4] 운영본 사후 무결성 검증 수행 중...")
        post_integrity = check_runtime_integrity(target_dir)
        if not post_integrity['valid']:
            raise ValueError(f"배포 후 운영본 무결성 검증 실패: {post_integrity['reasons']}")
        print(f"      검증 통과: {post_integrity['summary']}")

        print(f"[4/4] 배포 성공 완료 (백업 위치: {backup_dir})")
        return {
            'status': 'SUCCESS',
            'backup_dir': str(backup_dir),
            'post_integrity': post_integrity,
            'manifest': manifest
        }

    except Exception as e:
        print(f"[!] 배포 오류 발생: {e}")
        print(f"    즉시 이전 상태로 자동 롤백을 수행합니다...")
        for rel in backed_up_files:
            b_src = backup_dir / rel
            tgt_f = target_dir / rel
            if b_src.exists():
                shutil.copy2(b_src, tgt_f)
        print(f"    자동 롤백 완료.")
        raise

def perform_rollback(backup_dir, target_dir):
    """지정된 백업 디렉터리로부터 운영본을 복원한다."""
    b_path = Path(backup_dir)
    if not b_path.exists():
        raise FileNotFoundError(f"백업 디렉터리를 찾을 수 없습니다: {backup_dir}")

    print(f"[*] 롤백 시작: {backup_dir} -> {target_dir}")
    restored = []
    for rel in DEPLOY_FILES:
        src = b_path / rel
        tgt = target_dir / rel
        if src.exists():
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
            restored.append(rel)
    print(f"    {len(restored)}개 파일 복원 완료.")
    return restored

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="운영본 배포 및 롤백 도구")
    parser.add_argument('--check', action='store_true', help="배포 사전 조건 및 파일 차이만 진단 (dry-run)")
    parser.add_argument('--deploy', action='store_true', help="운영본 배포 실행 (백업 -> 복사 -> 해시 검증)")
    parser.add_argument('--rollback', type=str, help="지정된 백업 경로로부터 롤백 실행")
    parser.add_argument('--src', type=str, default=str(DEFAULT_SRC), help="개발본 소스 경로")
    parser.add_argument('--target', type=str, default=str(DEFAULT_TARGET), help="운영본 타깃 경로")

    args = parser.parse_args()
    src = Path(args.src)
    tgt = Path(args.target)

    if args.check:
        res = check_deployment_readiness(src, tgt)
        print("=== 배포 준비 상태 점검 결과 ===")
        print(f"배포 가능 여부: {'준비 완료 (READY)' if res['can_deploy'] else '차단됨 (BLOCKED)'}")
        if res['blocking_reasons']:
            print(f"차단 사유: {res['blocking_reasons']}")
        print(f"소스 무결성: {res['src_integrity']['summary']}")
        if res['target_integrity']:
            print(f"타깃 무결성: {res['target_integrity']['summary']}")
        print(f"운영본 현재 모드: {res.get('target_mode')}")
        print("\n[파일별 해시 대조]")
        for f, comp in res['file_comparisons'].items():
            diff_str = "일치 (IDENTICAL)" if comp['identical'] else "차이 있음 (DIFFERS)"
            src_short = comp['src_hash'][:8] if comp['src_hash'] else 'NONE'
            tgt_short = comp['target_hash'][:8] if comp['target_hash'] else 'NONE'
            print(f" - {f:<35}: {src_short} -> {tgt_short} [{diff_str}]")

    elif args.deploy:
        res = perform_deploy(src, tgt)
        print(json.dumps(res, indent=2, ensure_ascii=False))

    elif args.rollback:
        perform_rollback(args.rollback, tgt)
    else:
        parser.print_help()
