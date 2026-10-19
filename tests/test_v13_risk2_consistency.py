"""V1.3 risk2/daily4 설정 일관성 및 승인 검증 회귀 테스트.

A. 실제 운영 config loader가 risk_fraction=0.02를 읽음
B. 실제 운영 config loader가 max_daily_loss_fraction=0.04를 읽음
C. cap 20,000에서 allowed_risk_krw=400
D. daily remaining risk=800
E. config 변경 시 기존 DAILY_PREOPEN_V13 authorization invalid
F. 재승인 전 trading_on=False / BUY 차단
G. 재승인 후에만 OPEN_READY + LIVE 조건 충족 시 trading_on=True
H. BUILD_ID 또는 STRATEGY_EXPERIMENT에 risk2/daily4가 적혀 있는데 runtime risk_fraction/max_daily_loss_fraction이 0.02/0.04가 아니면 FAIL
"""
import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from core.settings import Config, KST, load_config, now
from core.util import atomic_json
from automation.policy import Policy, AutomationConfig
from automation.decisions import (
    BUILD_ID, STRATEGY_EXPERIMENT, STRATEGY_VERSION,
    DecisionLog, check_experiment_consistency
)
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.readiness import authorization_valid, digest, policy_hash, security_state
from operator_service import OperatorService
from operator_ui_service import UserOperations
from operator_process import process_identity
from tests.test_automation import FakeBroker


class TestV13Risk2Consistency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = [datetime(2026, 10, 7, 9, 10, 0, tzinfo=KST)]
        
        # 오늘 승인된 실험 설정: risk 2%, daily 4%, cap 20,000원
        self.cfg_risk2 = Config(
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_positions=1,
            max_entries_per_day=3,
            max_watchlist=5
        )
        self.auto_cap20000 = AutomationConfig(
            AUTO_TRADING_CAP_KRW=20000,
            costs_confirmed=True
        )
        
        # 구 설정 (0.01 / 0.01)
        self.cfg_old = Config(
            live_mode=True,
            risk_fraction=0.01,
            max_daily_loss_fraction=0.01,
            max_positions=1,
            max_entries_per_day=3,
            max_watchlist=5
        )
        
        atomic_json(self.root / 'config.json', asdict(self.cfg_risk2))
        atomic_json(self.root / 'automation.json', asdict(self.auto_cap20000))
        
        self.policy = Policy(self.root / 'runtime/automation_policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.policy.state['live_authorized'] = True
        atomic_json(self.root / 'runtime/automation_policy.json', self.policy.state)

    def test_a_and_b_operational_config_loader_reads_risk2_and_daily4(self):
        """A, B: config loader가 risk_fraction=0.02와 max_daily_loss_fraction=0.04를 정상 로드해야 한다."""
        loaded = load_config(self.root / 'config.json')
        self.assertEqual(loaded.risk_fraction, 0.02, "risk_fraction must be 0.02")
        self.assertEqual(loaded.max_daily_loss_fraction, 0.04, "max_daily_loss_fraction must be 0.04")
        self.assertEqual(loaded.max_positions, 1)
        self.assertEqual(loaded.max_entries_per_day, 3)
        self.assertEqual(loaded.max_watchlist, 5)
        
        # repo의 config.example.json 또한 0.02 / 0.04인지 확인
        repo_example = Path(__file__).resolve().parents[1] / 'config.example.json'
        if repo_example.exists():
            ex_cfg = load_config(repo_example)
            self.assertEqual(ex_cfg.risk_fraction, 0.02)
            self.assertEqual(ex_cfg.max_daily_loss_fraction, 0.04)

    def test_c_and_d_allowed_risk_and_daily_remaining_risk(self):
        """C, D: cap 20,000에서 allowed_risk_krw=400, remaining_daily_risk_krw=800이어야 한다."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        broker = FakeBroker()
        engine = AutonomousEngine(
            self.cfg_risk2, self.auto_cap20000, self.policy, folder, broker,
            clock=lambda: self.clock[0], shadow=True
        )
        engine.connected = True
        engine.account_ok = True
        engine.market = '3'
        engine.available_cash = 20000.0
        engine.s['cash'] = 20000.0
        engine.s['daily_loss'] = 0.0
        
        # 1. 수량 계산 단위 시험
        signal = {'limit': 5000, 'stop': 4900, 'pattern': 'A', 'time': self.clock[0].isoformat()}
        details = engine.quantity_details(signal, equity=20000.0, cash=20000.0, remaining=800.0)
        
        # risk_budget은 allowed_risk(400)과 remaining(800) 중 작은 값 = 400
        self.assertEqual(details['risk_budget'], 400.0, "risk budget must be 400 KRW for 2% of 20,000")
        
        # 2. DecisionLog / ENTRY_EVAL 이벤트 기록 시험
        events_captured = []
        original_event = engine.event
        def capture_event(kind, **info):
            events_captured.append((kind, info))
            return original_event(kind, **info)
        engine.event = capture_event
        
        engine.decisions.emit(
            'ENTRY_EVAL', '069540', 'NO_BUY', 'SETUP_NOT_READY',
            gates={'risk_quantity': {'result': 'PASS', 'values': {'equity': 20000.0, 'remaining_risk': 800.0}}},
            signal=signal, price=5000.0
        )
        
        self.assertTrue(events_captured)
        kind, info = events_captured[-1]
        self.assertEqual(kind, 'ENTRY_EVAL')
        self.assertEqual(info['build_id'], BUILD_ID)
        self.assertEqual(info['strategy_experiment'], STRATEGY_EXPERIMENT)
        self.assertEqual(info['risk_fraction'], 0.02)
        self.assertEqual(info['max_daily_loss_fraction'], 0.04)
        self.assertEqual(info['allowed_risk_krw'], 400.0)
        self.assertEqual(info['remaining_daily_risk_krw'], 800.0)
        self.assertEqual(info['approved_cap'], 20000)

    def test_trading_on_state_before_and_after_reauthorization(self):
        """E, F, G: config 변경 시 authorization invalid 및 재승인 전/후 trading_on 상태 전환 검증."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        cur_pid = os.getpid()

        # 구 0.01 / 0.01 승인 영수증
        atomic_json(self.root / 'runtime/operator_user_authorization.json', {
            'timestamp': self.clock[0].isoformat(),
            'cap': 20000,
            'authorization_kind': 'DAILY_PREOPEN_V13',
            'trading_day': self.clock[0].date().isoformat(),
            'policy_hash': policy_hash(self.policy),
            'security_state_hash': digest(security_state(self.root)),
            'build_id': BUILD_ID,
            'config_hash': self.cfg_old.fingerprint(),  # 불일치
            'auto_hash': self.auto_cap20000.fingerprint(),
        })

        # authorization_valid 검사: INVALID 확인
        valid, reasons = authorization_valid(
            self.root, self.cfg_risk2, self.auto_cap20000, self.policy, self.clock[0]
        )
        self.assertFalse(valid, "Authorization must be INVALID when config changes")
        self.assertIn('LIVE_APPROVAL_SETTINGS_MISMATCH', reasons)

        # 신선한 OPEN_READY heartbeat
        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': cur_pid,
            'identity': process_identity(cur_pid),
            'readiness': {'state': 'OPEN_READY','order_ready':True,'missing':[],
                          'last_regular_tick_at':self.clock[0].isoformat(),'regular_tick_age':0},
        })
        atomic_json(folder / 'state.json', {'positions': {}, 'orders': {}, 'trades': [], 'halt': '', 'cash': 20000})

        ui_service = UserOperations(self.root, clock=lambda: self.clock[0], gateway=Mock())
        with patch.object(ui_service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': cur_pid, 'identity': process_identity(cur_pid),
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            snapshot = ui_service.snapshot()
            self.assertFalse(snapshot['daily_authorization_valid'])
            self.assertFalse(snapshot['trading_on'], "trading_on must be False before re-authorization")

        # 새 0.02 / 0.04 config_hash로 재승인된 영수증
        atomic_json(self.root / 'runtime/operator_user_authorization.json', {
            'timestamp': self.clock[0].isoformat(),
            'cap': 20000,
            'authorization_kind': 'DAILY_PREOPEN_V13',
            'trading_day': self.clock[0].date().isoformat(),
            'policy_hash': policy_hash(self.policy),
            'security_state_hash': digest(security_state(self.root)),
            'build_id': BUILD_ID,
            'config_hash': self.cfg_risk2.fingerprint(),  # 일치!
            'auto_hash': self.auto_cap20000.fingerprint(),
        })

        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': cur_pid,
            'identity': process_identity(cur_pid),
            'readiness': {'state': 'OPEN_READY','order_ready':True,'missing':[],
                          'last_regular_tick_at':self.clock[0].isoformat(),'regular_tick_age':0},
            'session': 'SUPPORTED_SESSION',
        })

        with patch.object(ui_service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': cur_pid, 'identity': process_identity(cur_pid),
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            snapshot2 = ui_service.snapshot()
            self.assertTrue(snapshot2['daily_authorization_valid'])
            self.assertTrue(snapshot2['trading_on'], "trading_on must be True after valid re-authorization")

    def test_h_identity_consistency_check_fails_on_mismatch(self):
        """H: BUILD_ID 또는 STRATEGY_EXPERIMENT에 risk2/daily4가 명시되어 있는데
        runtime risk_fraction/max_daily_loss_fraction이 0.02/0.04가 아니면 FAIL이어야 한다."""
        bad_cfg1 = Config(risk_fraction=0.01, max_daily_loss_fraction=0.04)
        with self.assertRaises(ValueError) as ctx:
            check_experiment_consistency(bad_cfg1, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT)
        self.assertIn("risk2", str(ctx.exception))
        self.assertIn("0.01", str(ctx.exception))

        bad_cfg2 = Config(risk_fraction=0.02, max_daily_loss_fraction=0.01)
        with self.assertRaises(ValueError) as ctx:
            check_experiment_consistency(bad_cfg2, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT)
        self.assertIn("daily4", str(ctx.exception))
        self.assertIn("0.01", str(ctx.exception))

        bad_cfg3 = Config(risk_fraction=0.01, max_daily_loss_fraction=0.01)
        with self.assertRaises(ValueError) as ctx:
            check_experiment_consistency(bad_cfg3, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT)
        self.assertIn("risk2", str(ctx.exception))

        self.assertTrue(check_experiment_consistency(self.cfg_risk2, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT))

        repo_example = Path(__file__).resolve().parents[1] / 'config.example.json'
        if repo_example.exists():
            ex_cfg = load_config(repo_example)
            self.assertTrue(check_experiment_consistency(ex_cfg, build_id=BUILD_ID, experiment=STRATEGY_EXPERIMENT))

    def test_main_startup_consistency_fail_closed_and_success_paths(self):
        """I, J: main.py 실행 시 mismatched config는 fail closed, matching config는 정상 진입 검증."""
        import main
        mismatched_cfg = Config(live_mode=True, risk_fraction=0.01, max_daily_loss_fraction=0.04)

        with patch.object(main, 'ROOT', self.root), \
             patch.object(main, 'load_config', return_value=mismatched_cfg), \
             patch.object(main, 'prerequisites'), \
             patch.object(main, 'load_credentials'), \
             patch.object(main, 'now', return_value=self.clock[0]), \
             patch('automation.runner.run') as mock_run, \
             patch('sys.argv', ['main.py']):
            with self.assertRaises(ValueError) as ctx:
                main.main()
            self.assertIn("EXPERIMENT_CONSISTENCY_FAILURE", str(ctx.exception))
            mock_run.assert_not_called()

        with patch.object(main, 'ROOT', self.root), \
             patch.object(main, 'load_config', return_value=self.cfg_risk2), \
             patch.object(main, 'prerequisites'), \
             patch.object(main, 'load_credentials'), \
             patch.object(main, 'now', return_value=self.clock[0]), \
             patch('automation.runner.run', return_value={'passed': True}) as mock_run2, \
             patch('sys.argv', ['main.py']):
            res = main.main()
            self.assertEqual(res, 0)
            mock_run2.assert_called_once()

    def test_k_operator_runner_fails_closed_on_mismatch(self):
        """K: operator_runner 실행 경로에서 mismatched live config 시 fail-closed 및 CRASHED 기록, trading_on=False 확인."""
        import operator_runner
        folder = self.root / 'runtime'
        folder.mkdir(parents=True, exist_ok=True)
        mismatched_cfg = Config(live_mode=True, risk_fraction=0.01, max_daily_loss_fraction=0.04)
        atomic_json(self.root / 'config.json', asdict(mismatched_cfg))
        atomic_json(self.root / 'automation.json', asdict(self.auto_cap20000))
        atomic_json(self.root / 'runtime/automation_policy.json', {
            'mode': 'LIVE', 'live_authorized': True, 'releases': {}
        })
        
        # operator_runner.run() 직접 실행 시 CRASHED 기록 및 ValueError
        with patch.object(operator_runner, 'ROOT', self.root), \
             patch.object(operator_runner, 'control_supported', return_value=True), \
             patch.object(operator_runner, 'now', return_value=self.clock[0]):
            with self.assertRaises(ValueError) as ctx:
                operator_runner.run(root=self.root, clock=lambda: self.clock[0], purpose='CONTINUOUS')
            self.assertIn("EXPERIMENT_CONSISTENCY_FAILURE", str(ctx.exception))
            
            run_json = json.loads((self.root / 'runtime/operator_runner.json').read_text(encoding='utf-8'))
            self.assertEqual(run_json['status'], 'CRASHED')
            self.assertEqual(run_json['error_event'], 'EXPERIMENT_CONSISTENCY_FAILURE')

        # operator_service.public_status() 확인: CRASHED 상태이므로 trading_on=False
        svc = OperatorService(self.root, clock=lambda: self.clock[0], gateway=Mock())
        status = svc.public_status()
        self.assertFalse(status['trading_on'])
        self.assertFalse(status['process']['running'])


if __name__ == '__main__':
    unittest.main()
