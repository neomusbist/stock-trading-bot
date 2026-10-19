"""설정은 시작 시 검증·고정한다. 비밀키는 이 파일에 저장하지 않는다."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import math

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]

def now():
    return datetime.now(KST)

@dataclass(frozen=True)
class Config:
    live_mode: bool = False
    broker_environment: str = 'production'
    account_equity: float = 1000000
    paper_cash: float = 1000000
    risk_fraction: float = 0.003
    max_daily_loss_fraction: float = 0.01
    max_position_fraction: float = 0.20
    max_positions: int = 2
    max_entries_per_symbol: int | None = 1  # null: 횟수만 해제. 보유/미체결/실패 주문 경계는 유지.
    max_entries_per_day: int | None = 3  # null: 일일 횟수 제한 없음. 손실/수량/현금 한도는 별도 유지.
    history_bars: int = 750
    min_history_bars: int = 500
    max_history_age_days: int = 7
    max_watchlist: int = 12
    overhead_band: float = 0.10
    max_overhead_share: float = 0.25
    min_resistance_room: float = 0.025
    max_recent_range: float = 0.25
    min_daily_turnover: float = 1000000000
    acceleration_seconds: int = 10
    baseline_seconds: int = 60
    breakout_seconds: int = 60
    min_price_gain: float = 0.003
    min_turnover_ratio: float = 3.0
    min_window_turnover: float = 30000000
    min_bc_window_turnover: float = 20000000
    max_chase: float = 0.008
    max_gap: float = 0.12
    max_spread: float = 0.004
    min_stop_fraction: float = 0.002
    max_stop_fraction: float = 0.03
    min_reward_risk: float = 1.5
    structural_buffer: float = 0.001
    protect_at_r: float = 1.0
    trailing_fraction: float = 0.015
    roundtrip_cost_fraction: float = 0.003
    exit_slippage_fraction: float = 0.003
    entry_slippage_fraction: float = 0.002
    entry_start: str = '09:02:00'
    entry_end: str = '14:00:00'
    flatten_time: str = '15:15:00'
    stale_seconds: int = 5
    position_stale_seconds: int = 15
    order_timeout_seconds: int = 10
    reconcile_seconds: int = 5
    max_exit_attempts: int = 3
    after_enabled: bool = False
    after_a_accel_ratio: float = 2.5
    after_a_min_gain: float = 0.003
    after_a_liquidity_mode: str = 'depth'
    after_default_venue: str = 'SOR'
    enable_redesign_trials: bool = True
    enable_redesign_live: bool = False
    min_early_momentum_rr: float = 1.40
    min_pullback_reclaim_rr: float = 1.50

    def validate(self):
        if type(self.live_mode) is not bool or type(self.after_enabled) is not bool:
            raise ValueError('live_mode / after_enabled는 JSON true/false만 허용')
        if type(self.enable_redesign_trials) is not bool or type(self.enable_redesign_live) is not bool:
            raise ValueError('enable_redesign_trials / enable_redesign_live는 JSON true/false만 허용')
        if self.enable_redesign_live and not self.enable_redesign_trials:
            raise ValueError('enable_redesign_live는 enable_redesign_trials가 함께 활성화되어야 함')
        if self.broker_environment not in ('production', 'mock'):
            raise ValueError('broker_environment 오류')
        if self.after_default_venue not in ('SOR', 'KRX', 'NXT'):
            raise ValueError('after_default_venue 오류')
        if self.after_a_liquidity_mode not in ('depth', 'turnover'):
            raise ValueError('after_a_liquidity_mode 오류')
        for k, v in asdict(self).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if not math.isfinite(v) or v <= 0:
                    raise ValueError(f'{k}: 양의 유한 수 필요')
                if k=='max_position_fraction':
                    if v>1: raise ValueError('max_position_fraction: 1 이하여야 함')
                    continue
                if (k.endswith('_fraction') or k in ('max_chase','max_gap','max_spread','overhead_band','max_overhead_share','min_resistance_room','max_recent_range','min_price_gain','after_a_min_gain','structural_buffer')) and v >= 1:
                    raise ValueError(f'{k}: 1 미만이어야 함')
        for k in ('max_positions','max_entries_per_symbol','max_entries_per_day','history_bars','min_history_bars','max_history_age_days','max_watchlist','acceleration_seconds','baseline_seconds','breakout_seconds','stale_seconds','position_stale_seconds','order_timeout_seconds','reconcile_seconds','max_exit_attempts'):
            if k in ('max_entries_per_day','max_entries_per_symbol') and getattr(self,k) is None:
                continue
            if type(getattr(self,k)) is not int:
                raise ValueError(f'{k}: 정수 필요')
        for k in ('entry_start','entry_end','flatten_time'):
            datetime.strptime(getattr(self,k), '%H:%M:%S')
        if not ('09:00:00' <= self.entry_start < self.entry_end < self.flatten_time < '15:20:00'):
            raise ValueError('V1.1 운영시간 설정 오류 (거래소 폐장시각과 별개)')
        if self.min_history_bars > self.history_bars or self.min_history_bars < 60:
            raise ValueError('일봉 수 설정 오류')
        if self.min_stop_fraction >= self.max_stop_fraction or self.risk_fraction > self.max_daily_loss_fraction:
            raise ValueError('위험 한도 설정 오류')
        if self.max_positions > 10 or self.max_watchlist > 32:
            raise ValueError('V1 구독/포지션 한도 초과')
        return self

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()

def load_config(path):
    path=Path(path)
    if not path.exists() and path.name=='config.json' and path.with_name('config.example.json').exists():
        path=path.with_name('config.example.json')
    return Config(**json.loads(path.read_text(encoding='utf-8-sig'))).validate()

