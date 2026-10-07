"""V1.2 자동매매 운영 세션 계획 및 V1.3 AFTER 시장 세션 계획.
시작·신규매수·마감·청산·지원종료의 단일 기준을 제공한다."""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from core.settings import KST

@dataclass(frozen=True)
class SessionPlan:
    preopen_control_start: str = '08:00:00'
    observe_start: str = '09:00:00'
    new_entry_start: str = '09:00:00'
    hard_new_entry_end: str = '15:05:00'
    force_flatten_start: str = '15:10:00'
    minimum_remaining_minutes: int = 15
    support_end: str = '15:20:00'
    allow_overnight: bool = False

    @property
    def effective_new_entry_end(self) -> str:
        if self.minimum_remaining_minutes <= 0:
            return self.hard_new_entry_end
        flatten_dt = datetime.strptime(self.force_flatten_start, '%H:%M:%S')
        earlier_dt = flatten_dt - timedelta(minutes=self.minimum_remaining_minutes)
        computed = earlier_dt.strftime('%H:%M:%S')
        return min(self.hard_new_entry_end, computed)

    def validate(self):
        assert self.preopen_control_start < self.observe_start <= self.new_entry_start < self.effective_new_entry_end < self.force_flatten_start < self.support_end, "Invalid session sequence"
        assert not self.allow_overnight, "Overnight trading not permitted"
        return self

    def fingerprint(self) -> str:
        data = f"{self.preopen_control_start}|{self.observe_start}|{self.new_entry_start}|{self.effective_new_entry_end}|{self.hard_new_entry_end}|{self.force_flatten_start}|{self.minimum_remaining_minutes}|{self.support_end}|{self.allow_overnight}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def is_supported(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.observe_start <= local.strftime('%H:%M:%S') < self.support_end

    def is_control_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.preopen_control_start <= local.strftime('%H:%M:%S') < self.support_end

    def phase(self, stamp: datetime) -> str:
        if not self.is_control_window(stamp): return 'SUPPORT_END' if stamp.tzinfo is not None and stamp.astimezone(KST).strftime('%H:%M:%S') >= self.support_end else 'OUT_OF_CONTROL_SESSION'
        local_time = stamp.astimezone(KST).strftime('%H:%M:%S')
        if local_time < self.observe_start: return 'PREOPEN_CONTROL'
        if local_time >= self.force_flatten_start: return 'FORCE_FLATTEN'
        if self.is_entry_window(stamp): return 'NEW_ENTRY'
        return 'MARKET_OBSERVATION'

    def is_entry_window(self, stamp: datetime) -> bool:
        if not self.is_supported(stamp): return False
        local = stamp.astimezone(KST)
        return self.new_entry_start <= local.strftime('%H:%M:%S') < self.effective_new_entry_end

    def is_flatten_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.strftime('%H:%M:%S') >= self.force_flatten_start

    def summary(self) -> dict:
        return {
            'preopen_control_start': self.preopen_control_start,
            'observe_start': self.observe_start,
            'new_entry_start': self.new_entry_start,
            'effective_new_entry_end': self.effective_new_entry_end,
            'hard_new_entry_end': self.hard_new_entry_end,
            'force_flatten_start': self.force_flatten_start,
            'minimum_remaining_minutes': self.minimum_remaining_minutes,
            'support_end': self.support_end,
            'allow_overnight': self.allow_overnight,
            'plan_hash': self.fingerprint()
        }

@dataclass(frozen=True)
class AfterSessionPlan:
    prep_start: str = '15:40:00'
    market_start: str = '16:00:00'
    entry_cutoff: str = '19:45:00'
    force_flatten_start: str = '19:55:00'
    market_close: str = '20:00:00'
    support_end: str = '20:00:00'
    allow_overnight: bool = False

    def validate(self):
        assert self.prep_start < self.market_start < self.entry_cutoff < self.force_flatten_start < self.market_close <= self.support_end, "Invalid after session sequence"
        assert not self.allow_overnight, "Overnight trading not permitted"
        return self

    def fingerprint(self) -> str:
        data = f"{self.prep_start}|{self.market_start}|{self.entry_cutoff}|{self.force_flatten_start}|{self.market_close}|{self.support_end}|{self.allow_overnight}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def is_supported(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.prep_start <= local.strftime('%H:%M:%S') < self.market_close

    def is_control_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.prep_start <= local.strftime('%H:%M:%S') < self.support_end

    def is_market_open(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.market_start <= local.strftime('%H:%M:%S') < self.market_close

    def is_entry_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.market_start <= local.strftime('%H:%M:%S') < self.entry_cutoff

    def is_prep_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.prep_start <= local.strftime('%H:%M:%S') < self.market_start

    def is_exit_only_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.entry_cutoff <= local.strftime('%H:%M:%S') < self.force_flatten_start

    def is_flatten_window(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.force_flatten_start <= local.strftime('%H:%M:%S') < self.market_close

    def phase(self, stamp: datetime, live_approved: bool = False) -> str:
        if stamp.tzinfo is None: return 'OUT_OF_CONTROL_SESSION'
        local = stamp.astimezone(KST)
        if local.weekday() >= 5: return 'OUT_OF_CONTROL_SESSION'
        t = local.strftime('%H:%M:%S')
        if t < self.prep_start: return 'OUT_OF_CONTROL_SESSION'
        if t < self.market_start: return 'AFTER_PREP'
        if t < self.entry_cutoff: return 'AFTER_LIVE' if live_approved else 'AFTER_READY'
        if t < self.force_flatten_start: return 'AFTER_EXIT_ONLY'
        if t < self.market_close: return 'FORCE_FLATTEN'
        return 'CLOSED'

    def summary(self) -> dict:
        return {
            'prep_start': self.prep_start,
            'market_start': self.market_start,
            'entry_cutoff': self.entry_cutoff,
            'force_flatten_start': self.force_flatten_start,
            'market_close': self.market_close,
            'support_end': self.support_end,
            'allow_overnight': self.allow_overnight,
            'plan_hash': self.fingerprint()
        }

DEFAULT_SESSION_PLAN = SessionPlan().validate()
DEFAULT_AFTER_SESSION_PLAN = AfterSessionPlan().validate()

START = DEFAULT_SESSION_PLAN.observe_start
END = DEFAULT_SESSION_PLAN.support_end

def supported(stamp):
    return DEFAULT_SESSION_PLAN.is_supported(stamp)

def after_supported(stamp):
    return DEFAULT_AFTER_SESSION_PLAN.is_supported(stamp)

def after_entry_supported(stamp):
    return DEFAULT_AFTER_SESSION_PLAN.is_entry_window(stamp)

def is_after_market(stamp):
    return DEFAULT_AFTER_SESSION_PLAN.is_supported(stamp)

def control_supported(stamp):
    return DEFAULT_SESSION_PLAN.is_control_window(stamp)

def after_control_supported(stamp):
    return DEFAULT_AFTER_SESSION_PLAN.is_control_window(stamp)

def observation_supported(stamp):
    return supported(stamp) or after_supported(stamp)

def current_session(stamp):
    if DEFAULT_SESSION_PLAN.is_supported(stamp): return 'REGULAR'
    if DEFAULT_AFTER_SESSION_PLAN.is_supported(stamp): return 'AFTER'
    return 'CLOSED'

def market_phase(stamp, live_approved=False):
    if DEFAULT_SESSION_PLAN.is_control_window(stamp):
        return DEFAULT_SESSION_PLAN.phase(stamp)
    if DEFAULT_AFTER_SESSION_PLAN.is_supported(stamp):
        return DEFAULT_AFTER_SESSION_PLAN.phase(stamp, live_approved=live_approved)
    return 'CLOSED'

def assessment(stamp, plan=None):
    active_plan = plan or DEFAULT_SESSION_PLAN
    after_plan = DEFAULT_AFTER_SESSION_PLAN
    is_reg = active_plan.is_supported(stamp)
    is_after = after_plan.is_supported(stamp)
    classification = 'SUPPORTED_SESSION' if is_reg else 'AFTER_SESSION' if is_after else 'PREOPEN_CONTROL' if active_plan.is_control_window(stamp) else 'OUT_OF_SUPPORTED_SESSION'
    phase = active_plan.phase(stamp) if active_plan.is_control_window(stamp) else after_plan.phase(stamp) if is_after else 'OUT_OF_CONTROL_SESSION'
    return {
        'classification': classification,
        'phase': phase,
        'support': f'평일 KST {active_plan.observe_start[:5]} 이상 {active_plan.support_end[:5]} 미만 (정규장) / {after_plan.market_start[:5]} 이상 {after_plan.market_close[:5]} 미만 (AFTER)',
        'checked_at': stamp.isoformat(),
        'regression_failure': False,
        'real_orders_sent': 0,
        'session_plan': active_plan.summary(),
        'after_session_plan': after_plan.summary()
    }
