"""Create a hash-checked identity for an isolated Git worktree, never for LIVE.

The file is test metadata, not a release or a deployment authorization. It is
intentionally not overwritten: an existing identity needs manual examination.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm-isolated-test-only', action='store_true', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    live = Path(r'C:\Users\MJS\Desktop\주식자동매매').resolve()
    if root == live or live in root.parents or not (root / '.git').exists():
        raise SystemExit('Refusing non-isolated or LIVE directory')
    top = Path(subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], text=True, encoding='utf-8').strip()).resolve()
    if top != root:
        raise SystemExit('Git root must match the isolated worktree')
    identity = root / 'runtime' / 'RELEASE_IDENTITY.json'
    if identity.exists():
        raise SystemExit('Existing identity preserved')
    sys.path.insert(0, str(root))
    from automation.decisions import BUILD_ID
    from automation.release_integrity import CRITICAL_RUNTIME_FILES, MANDATORY_PATCH_SIGNATURES
    files = set(CRITICAL_RUNTIME_FILES) | {v['file'] for v in MANDATORY_PATCH_SIGNATURES.values()}
    files.update({'automation/decisions.py', 'automation/release_integrity.py',
                  'automation/observation_worker.py', 'automation/patterns.py',
                  'automation/focus_selection.py', 'automation/position_health.py',
                  'automation/trade_review_capture.py', 'automation/lightweight_decisions.py',
                  'operator_service.py', 'core/broker.py'})
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, encoding='utf-8').strip()
    manifest = {'release_id': 'TEST-ONLY-' + commit[:12], 'commit': commit,
                'build_id': BUILD_ID,
                'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                          for name in sorted(files)}}
    identity.parent.mkdir(exist_ok=True)
    with identity.open('x', encoding='utf8') as out:
        json.dump(manifest, out, indent=2)
    print('Created isolated TEST-ONLY identity:', identity)


if __name__ == '__main__':
    main()
