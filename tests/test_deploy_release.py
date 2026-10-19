r"""
배포 및 롤백 도구 단위 테스트 (tests/test_deploy_release.py)
"""
import unittest
import tempfile
import shutil
import json
from pathlib import Path
from deploy_release import DEPLOY_FILES, perform_deploy, perform_rollback, check_deployment_readiness, sha256_file

class TestDeployRelease(unittest.TestCase):
    def setUp(self):
        self.dev_root = Path(__file__).resolve().parent.parent

    def test_deploy_and_rollback_in_isolated_target(self):
        """격리된 모의 타깃 디렉터리에서 배포, 해시 검증, 롤백이 100% 정상 작동하는지 검증한다."""
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as tgt_tmp:
            src = Path(src_tmp)
            tgt = Path(tgt_tmp)

            # 1. 소스 디렉터리에 현재 개발본 파일 복사
            for rel in DEPLOY_FILES + ['automation/engine.py', 'automation/decisions.py', 'automation/runner.py']:
                s_file = self.dev_root / rel
                d_file = src / rel
                d_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s_file, d_file)

            # 2. 타깃 디렉터리에 구버전(미패치) 파일 작성
            (tgt / 'automation').mkdir(parents=True)
            (tgt / 'runtime').mkdir(parents=True)
            for rel in ['automation/engine.py', 'automation/decisions.py', 'automation/runner.py']:
                shutil.copy2(self.dev_root / rel, tgt / rel)
            for rel in DEPLOY_FILES:
                t_file = tgt / rel
                t_file.parent.mkdir(parents=True, exist_ok=True)
                t_file.write_text(f"# old version of {rel}\n", encoding='utf-8')
            (tgt / 'runtime/automation_policy.json').write_text(json.dumps({'mode': 'STOP'}), encoding='utf-8')

            # 3. 배포 전 점검
            readiness = check_deployment_readiness(src, tgt)
            self.assertTrue(readiness['can_deploy'])
            self.assertFalse(readiness['target_integrity']['valid'])

            # 4. 배포 실행
            deploy_result = perform_deploy(src, tgt)
            self.assertEqual(deploy_result['status'], 'SUCCESS')
            self.assertTrue(deploy_result['post_integrity']['valid'])

            # 5. 타깃 파일 해시가 소스와 완전히 동일한지 확인
            for rel in DEPLOY_FILES:
                self.assertEqual(sha256_file(src / rel), sha256_file(tgt / rel))

            # 6. 롤백 실행
            backup_dir = deploy_result['backup_dir']
            restored = perform_rollback(backup_dir, tgt)
            self.assertEqual(len(restored), len(DEPLOY_FILES))

            # 7. 롤백 후 구버전 내용으로 복원 확인
            for rel in DEPLOY_FILES:
                self.assertIn(f"# old version of {rel}", (tgt / rel).read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main()
