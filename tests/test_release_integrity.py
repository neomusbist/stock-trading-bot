"""
운영본-개발본 일치 검증 및 배포 불일치 방어 단위 테스트 (tests/test_release_integrity.py)
"""
import unittest
import tempfile
import shutil
import json
from pathlib import Path
from automation.release_integrity import (
    check_runtime_integrity,
    record_mode_transition,
    read_mode_transitions,
    CURRENT_RELEASE_COMMIT,
    MANDATORY_PATCH_SIGNATURES
)
from automation.policy import Policy

class TestReleaseIntegrity(unittest.TestCase):
    def setUp(self):
        self.dev_root = Path(__file__).resolve().parent.parent

    def test_dev_repo_integrity_passes(self):
        """현재 개발본 저장소는 모든 필수 사고 수정 패치를 포함하며 무결성 검증을 100% 통과해야 한다."""
        res = check_runtime_integrity(self.dev_root)
        self.assertTrue(res['valid'], f"개발본 무결성 검증 실패: {res['reasons']}")
        self.assertEqual(res['commit'], CURRENT_RELEASE_COMMIT)
        self.assertEqual(len(res['missing_patches']), 0)
        self.assertEqual(len(res['verified_patches']), len(MANDATORY_PATCH_SIGNATURES))
        self.assertIn('VERIFIED', res['summary'])

    def test_unpatched_code_detected_as_invalid(self):
        """17:43 또는 18:32 필수 패치가 누락된 구버전 코드는 즉시 invalid로 감지되어야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            (tmp_root / 'automation').mkdir(parents=True)
            
            # 1. 45초 윈도우와 2틱 필터가 없는 구버전 fast_momentum.py 모의 작성
            unpatched_code = """
def evaluate_exit(position, t, current_time):
    highs = [1, 2]; lows = [1]
    if len(highs) >= 2 and lows and highs[-1] < highs[-2]:
        return True, 'MOMENTUM_FAILURE_EXIT'
    return False, 'HOLD'
"""
            (tmp_root / 'automation/fast_momentum.py').write_text(unpatched_code, encoding='utf-8')
            (tmp_root / 'automation/engine.py').write_text("FAST_{exit_reason}", encoding='utf-8')
            (tmp_root / 'automation/decisions.py').write_text("BUILD_ID='TEST'", encoding='utf-8')
            (tmp_root / 'automation/runner.py').write_text("# runner", encoding='utf-8')
            (tmp_root / 'automation/policy.py').write_text("# policy", encoding='utf-8')

            res = check_runtime_integrity(tmp_root)
            self.assertFalse(res['valid'], "구버전 미패치 코드는 무결성 검증을 통과할 수 없음")
            self.assertIn('FAST_MOMENTUM_45S_SETTLING', res['missing_patches'])
            self.assertIn('FAST_MOMENTUM_2TICK_SWING_FILTER', res['missing_patches'])
            self.assertIn('UNVERIFIED_MISMATCH', res['summary'])

    def test_policy_blocks_live_on_unpatched_code(self):
        """필수 패치가 누락된 환경에서는 Policy.command('LIVE')가 예외를 발생시키며 차단되어야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            (tmp_root / 'automation').mkdir(parents=True)
            (tmp_root / 'runtime').mkdir(parents=True)
            
            # 구버전 파일 작성 (패치 누락)
            (tmp_root / 'automation/fast_momentum.py').write_text("# unpatched", encoding='utf-8')
            (tmp_root / 'automation/engine.py').write_text("# no prefix", encoding='utf-8')
            (tmp_root / 'automation/decisions.py').write_text("# decisions", encoding='utf-8')
            (tmp_root / 'automation/runner.py').write_text("# runner", encoding='utf-8')
            (tmp_root / 'automation/policy.py').write_text("# policy", encoding='utf-8')

            policy_path = tmp_root / 'runtime/automation_policy.json'
            policy = Policy(policy_path)
            
            # LIVE 명령 실행 시도 -> 무결성 불일치로 ValueError 차단 검증
            with self.assertRaises(ValueError) as ctx:
                policy.command('LIVE', confirmation='LIVE')
            
            self.assertIn("UNVERIFIED_CODE_BLOCKED_FOR_LIVE", str(ctx.exception))
            # 상태가 LIVE로 변경되지 않고 STOP/ARMED 유지 확인
            policy.refresh()
            self.assertFalse(policy.state.get('live_authorized', False))

    def test_mode_transition_logging(self):
        """STOP <-> LIVE 상태 전환 시 감사 로그(mode_transitions.jsonl)에 승인 근거와 해시가 정상 기록되어야 한다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            (tmp_root / 'automation').mkdir(parents=True)
            for f in ('fast_momentum.py', 'engine.py', 'decisions.py', 'runner.py', 'policy.py'):
                (tmp_root / 'automation' / f).write_text(f"# {f}\nholding_seconds >= 45.0\n2 * min_step\nFAST_{{exit_reason}}\n", encoding='utf-8')

            rec = record_mode_transition(
                root=tmp_root,
                from_mode='STOP',
                to_mode='LIVE',
                approval_basis='USER_EXPLICIT_TEST_APPROVAL',
                user='TEST_OPERATOR',
                pid=9999
            )

            self.assertEqual(rec['from_mode'], 'STOP')
            self.assertEqual(rec['to_mode'], 'LIVE')
            self.assertEqual(rec['approval_basis'], 'USER_EXPLICIT_TEST_APPROVAL')
            self.assertTrue(rec['code_verified'])
            self.assertEqual(rec['commit'], CURRENT_RELEASE_COMMIT)

            history = read_mode_transitions(tmp_root)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['approval_basis'], 'USER_EXPLICIT_TEST_APPROVAL')

    def test_ui_start_block_rejects_unpatched_code(self):
        """UI 서비스의 start_block은 코드 무결성이 불일치할 경우 LIVE 시작을 차단해야 한다."""
        from operator_ui_service import start_block
        status = {
            'process': {'running': False},
            'control_supported': True,
            'supported': True,
            'halt': False,
            'hard_ok': True,
            'uncertain': False,
            'mode': 'STOP',
            'positions': {},
            'pending': {},
            'time': '2026-10-15T09:30:00+09:00',
            'live_mode': True,
            'live_approved': True,
            'costs_confirmed': True,
            'cap': 1000000,
            'guided_start': True,
            'code_integrity': {
                'valid': False,
                'summary': 'Commit 0dd8884 | FAST:77fbde67 | UNVERIFIED_MISMATCH'
            }
        }
        reason = start_block(status)
        self.assertIn("코드 무결성 불일치", reason)
        self.assertIn("UNVERIFIED_MISMATCH", reason)

        # 무결성 검증 통과 시에는 차단되지 않음
        status['code_integrity'] = {
            'valid': True,
            'summary': 'Commit 0dd8884 | FAST:0e7bb388 | VERIFIED'
        }
        self.assertEqual(start_block(status), '')


if __name__ == '__main__':
    unittest.main()

