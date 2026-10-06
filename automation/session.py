"""V1.2 자동매매 운영 세션 계획. 시작·신규매수·마감·청산·지원종료의 단일 기준을 제공한다."""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from core.settings import KST

@dataclass(frozen=True)
class SessionPlan:
    observe_start: str = '09:00:00'
    new_entry_start: str = '09:02:00'
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
        assert self.observe_start <= self.new_entry_start < self.effective_new_entry_end < self.force_flatten_start < self.support_end, "Invalid session sequence"
        assert not self.allow_overnight, "Overnight trading not permitted"
        return self

    def fingerprint(self) -> str:
        data = f"{self.observe_start}|{self.new_entry_start}|{self.effective_new_entry_end}|{self.hard_new_entry_end}|{self.force_flatten_start}|{self.minimum_remaining_minutes}|{self.support_end}|{self.allow_overnight}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def is_supported(self, stamp: datetime) -> bool:
        if stamp.tzinfo is None: return False
        local = stamp.astimezone(KST)
        return local.weekday() < 5 and self.observe_start <= local.strftime('%H:%M:%S') < self.support_end

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

DEFAULT_SESSION_PLAN = SessionPlan().validate()

START = DEFAULT_SESSION_PLAN.observe_start
END = DEFAULT_SESSION_PLAN.support_end

def supported(stamp):
    return DEFAULT_SESSION_PLAN.is_supported(stamp)

def assessment(stamp, plan=None):
    active_plan = plan or DEFAULT_SESSION_PLAN
    return {
        'classification': 'SUPPORTED_SESSION' if active_plan.is_supported(stamp) else 'OUT_OF_SUPPORTED_SESSION',
        'support': f'평일 KST {active_plan.observe_start[:5]} 이상 {active_plan.support_end[:5]} 미만 + 거래소 정규장 상태 확인',
        'checked_at': stamp.isoformat(),
        'regression_failure': False,
        'real_orders_sent': 0,
        'session_plan': active_plan.summary()
    }
