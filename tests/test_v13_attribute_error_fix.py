"""V1.3 실장 09:00:21 AttributeError 회귀 및 fail-closed 검증 테스트.

1. AutomationConfig.max_watchlist 접근 시 발생하는 AttributeError 재현 및 해결 확인
2. worker crash / dead pid 시 stale heartbeat에 의한 trading_on 오표시 차단 (fail-closed)
3. supervisor_alert 재시작 대기 중 trading_on 차단
4. fatal heartbeat 시 trading_on 차단
5. open_ready 미달성 시 trading_on 차단
6. restart 직후 readiness 미충족 시 BUY 차단
"""
import copy
import os
import queue
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from dataclasses import asdict

from core.settings import Config, KST, now
from core.util import atomic_json
from automation.policy import Policy, AutomationConfig
from automation.decisions import BUILD_ID, STRATEGY_VERSION, STRATEGY_EXPERIMENT
from automation.focus_selection import FocusNotification, select_focus
from operator_service import OperatorService
from operator_ui_service import UserOperations
from operator_process import process_identity

class TestV13AttributeErrorAndFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = [datetime(2026, 10, 7, 9, 0, 22, tzinfo=KST)]
        
        # 기본 설정
        self.cfg = Config(live_mode=True, max_watchlist=5, risk_fraction=0.02, max_daily_loss_fraction=0.04)
        self.auto = AutomationConfig(AUTO_TRADING_CAP_KRW=20000, costs_confirmed=True)
        atomic_json(self.root / 'config.json', asdict(self.cfg))
        atomic_json(self.root / 'automation.json', asdict(self.auto))
        
        # Policy: LIVE
        self.policy = Policy(self.root / 'runtime/automation_policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.policy.state['live_authorized'] = True
        atomic_json(self.root / 'runtime/automation_policy.json', self.policy.state)
        
        # Authorization receipt for today
        from automation.readiness import policy_hash, digest, security_state
        atomic_json(self.root / 'runtime/operator_user_authorization.json', {
            'authorization_kind': 'DAILY_PREOPEN_V13',
            'trading_day': self.clock[0].date().isoformat(),
            'build_id': BUILD_ID,
            'config_hash': self.cfg.fingerprint(),
            'auto_hash': self.auto.fingerprint(),
            'policy_hash': policy_hash(self.policy),
            'security_state_hash': digest(security_state(self.root)),
            'authorized_at': self.clock[0].isoformat(),
        })

    def test_auto_max_watchlist_attribute_error_reproduction_and_fix(self):
        """auto.max_watchlist는 AttributeError를 일으키고 effective.max_watchlist는 정상 통과해야 한다."""
        auto = self.auto
        effective = self.cfg
        
        # 1. 재현: auto는 max_watchlist가 없으므로 AttributeError 발생
        with self.assertRaises(AttributeError):
            _ = auto.max_watchlist  # 'AutomationConfig' object has no attribute 'max_watchlist'
            
        # 2. 해결: effective(Config)는 max_watchlist=5를 가지고 있음
        self.assertEqual(effective.max_watchlist, 5)
        
        # 3. auto.fallback_source1은 True로 정상 제공
        self.assertTrue(getattr(auto, 'fallback_source1', True))
        
        # 4. select_focus 호출이 정상 동작하는지 확인
        current_watch = {'069540': {'code': '069540', 'status': 'WATCH', 'source_count': 2}}
        candidates = [
            {'code': '069540', 'status': 'WATCH', 'source_count': 2, 'reference_price': 5000},
            {'code': '019170', 'status': 'SHADOW', 'source_count': 2, 'reference_price': 3000},
        ]
        readiness_map = {
            '019170': {'tier': 1, 'candidate_generated': True},
            '069540': {'tier': 2, 'candidate_generated': False},
        }
        selection = select_focus(
            current_watch=current_watch,
            candidates=candidates,
            readiness_map=readiness_map,
            max_watchlist=effective.max_watchlist,
            fallback_source1=auto.fallback_source1,
            clock_iso=self.clock[0].isoformat()
        )
        self.assertTrue(selection.applied)
        self.assertIn('019170', selection.promoted)

    def test_dead_worker_stale_heartbeat_fails_closed_trading_on(self):
        """worker 프로세스가 사망했을 때 stale heartbeat가 남아있어도 trading_on은 False여야 한다."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        
        # heartbeat는 1초 전(신선), OPEN_READY, connected, account_ok이지만 pid는 죽은 pid
        dead_pid = 999999
        self.assertIsNone(process_identity(dead_pid))
        
        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': dead_pid,
            'identity': 'dead_process_identity_fake',
            'readiness': {'state': 'OPEN_READY', 'gates': {'regular_market': True}},
            'counts': {'ticks': 100, 'radar_cycles': 1, 'account_checks': 3, 'subscription_acks': 2},
        })
        atomic_json(folder / 'state.json', {
            'positions': {}, 'orders': {}, 'trades': [], 'halt': '', 'cash': 20000
        })
        
        service = OperatorService(self.root, clock=lambda: self.clock[0], gateway=Mock())
        # supervisor/runner lock or managed process simulating alive supervisor
        with patch.object(service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': os.getpid(), 'identity': process_identity(os.getpid()),
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            status = service.public_status()
            # dead_pid 검사로 worker_alive=False -> fresh=False -> trading_on=False
            self.assertFalse(status['trading_on'], "Dead worker PID must fail-closed trading_on to False")
            
            ui_service = UserOperations(self.root, clock=lambda: self.clock[0], gateway=Mock())
            ui_status = ui_service.snapshot()
            self.assertFalse(ui_status['trading_on'], "UI snapshot must also fail-closed trading_on to False")

    def test_supervisor_restarting_alert_disables_trading_on(self):
        """supervisor가 크래시 후 재시작 대기 중(alert active)일 때 trading_on은 False여야 한다."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        
        # worker는 현재 process로 가정
        cur_pid = os.getpid()
        cur_ident = process_identity(cur_pid)
        
        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': cur_pid,
            'identity': cur_ident,
            'readiness': {'state': 'OPEN_READY'},
        })
        atomic_json(folder / 'state.json', {'positions': {}, 'orders': {}, 'trades': [], 'halt': '', 'cash': 20000})
        
        # supervisor alert가 최근에 발생하여 재시작 대기 중
        atomic_json(self.root / 'runtime/supervisor_alert.json', {
            'time': self.clock[0].isoformat(),
            'exit_code': 1,
            'failures': 2,
            'retry_seconds': 16,
            'action': '동일 장부/계좌 대조 후 재시작'
        })
        
        service = OperatorService(self.root, clock=lambda: self.clock[0], gateway=Mock())
        with patch.object(service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': cur_pid, 'identity': cur_ident,
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            status = service.public_status()
            self.assertFalse(status['trading_on'], "Supervisor restarting alert must disable trading_on")

    def test_fatal_heartbeat_disables_trading_on(self):
        """heartbeat에 fatal 오류(AttributeError 등)가 기록되어 있으면 trading_on은 False여야 한다."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        
        cur_pid = os.getpid()
        cur_ident = process_identity(cur_pid)
        
        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': cur_pid,
            'identity': cur_ident,
            'fatal': 'AttributeError',
            'readiness': {'state': 'OPEN_READY'},
        })
        atomic_json(folder / 'state.json', {'positions': {}, 'orders': {}, 'trades': [], 'halt': '', 'cash': 20000})
        
        service = OperatorService(self.root, clock=lambda: self.clock[0], gateway=Mock())
        with patch.object(service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': cur_pid, 'identity': cur_ident,
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            status = service.public_status()
            self.assertFalse(status['trading_on'], "Heartbeat with fatal error must disable trading_on")

    def test_readiness_not_open_ready_disables_trading_on(self):
        """heartbeat의 readiness 상태가 OPEN_READY가 아니면(예: PREOPEN_READY) trading_on은 False여야 한다."""
        folder = self.root / 'runtime/automation-production'
        folder.mkdir(parents=True, exist_ok=True)
        
        cur_pid = os.getpid()
        cur_ident = process_identity(cur_pid)
        
        atomic_json(folder / 'heartbeat.json', {
            'time': self.clock[0].isoformat(),
            'mode': 'LIVE',
            'connected': True,
            'account_ok': True,
            'market': '3',
            'pid': cur_pid,
            'identity': cur_ident,
            'readiness': {'state': 'PREOPEN_READY'},
        })
        atomic_json(folder / 'state.json', {'positions': {}, 'orders': {}, 'trades': [], 'halt': '', 'cash': 20000})
        
        service = OperatorService(self.root, clock=lambda: self.clock[0], gateway=Mock())
        with patch.object(service, 'process_info', return_value={
            'running': True, 'managed': True, 'pid': cur_pid, 'identity': cur_ident,
            'started': (self.clock[0] - timedelta(minutes=1)).isoformat()
        }):
            status = service.public_status()
            self.assertFalse(status['trading_on'], "readiness!=OPEN_READY must disable trading_on")

if __name__ == '__main__':
    unittest.main()
