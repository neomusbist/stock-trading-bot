from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import math
from datetime import datetime
from core.util import atomic_json,read_json_retry
from core.settings import now

MODES={'OFF','ARMED','LIVE','STOP','STOP_FLAT','KILL'}
HARD_LOCKS={'005930','017900'}
LEGACY_SYMBOL='052260'
BREAKEVEN_EXITS={'052260':5834}
BREAKEVEN_MAX_INVENTORY={'052260':13}
TEST_RELEASE_SYMBOL='267320'
LEGACY_TEST_RELEASE_ENABLED=False  # compatibility replay only; no config/UI switch
def legacy_test_release_enabled(): return LEGACY_TEST_RELEASE_ENABLED
EXISTING_POLICIES={'005930':'MANUAL_HOLD','017900':'HARD_LOCK','052260':'BREAKEVEN_EXIT','267320':'EXTERNAL_HOLD'}

@dataclass(frozen=True)
class AutomationConfig:
    AUTO_TRADING_CAP_KRW: int = 0
    ALLOW_OVERNIGHT: bool = False
    new_entry_cutoff: str = '15:05:00'
    force_flatten_start: str = '15:10:00'
    decision_snapshot_seconds: int = 30
    commission_buy: float = 0.00015
    commission_sell: float = 0.00015
    sell_tax: float = 0.002
    costs_confirmed: bool = False
    scan_seconds: int = 60
    rank_decay_cycles: int = 2
    max_scan_charts: int = 20
    min_source_count: int = 1
    fallback_source1: bool = True
    weakening_drop_points: float = 2.0
    cooldown_seconds: int = 900
    max_consecutive_losses: int | None = 3  # null: 연속 손실 횟수 제한 없음. 일일 위험예산은 별도 유지.
    daily_risk_override_day: str | None = None
    daily_risk_override_budget_krw: float | None = None
    entry_context_enabled: bool = False  # separately approved strategy experiment
    stable_position_book_confirmation: bool = False  # opt-in read-only cross-check for quiet standing books
    confirmed_box_direction: bool = False  # separately approved C descending-range exclusion
    confirmed_structure_protection: bool = False  # separately approved post-entry pivot protection
    pullback_min_rise: float = 0.008
    pullback_max_retrace: float = 0.65
    pullback_volume_ratio: float = 0.75
    box_max_range: float = 0.015
    box_min_bars: int = 5
    min_efficiency_score: float = 0.10
    replacement_margin: float = 0.20
    replacement_min_hold_minutes: int = 5
    replacement_enabled: bool = False
    empirical_min_trades: int = 50
    horizon_a_minutes: int = 10
    horizon_b_minutes: int = 30
    horizon_c_minutes: int = 20
    unrealized_giveback_fraction: float = 0.40
    after_prep_start: str = '15:40:00'
    after_market_start: str = '16:00:00'
    after_entry_cutoff: str = '19:45:00'
    after_force_flatten_start: str = '19:55:00'
    after_market_close: str = '20:00:00'

    @property
    def capital_limit(self): return self.AUTO_TRADING_CAP_KRW

    def validate(self):
        day=self.daily_risk_override_day; budget=self.daily_risk_override_budget_krw
        if (day is None)!=(budget is None): raise ValueError('일일 위험예산 예외는 날짜와 총 한도가 함께 필요합니다.')
        if day is not None:
            if type(day) is not str or datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day:
                raise ValueError('일일 위험예산 예외 날짜 오류')
            if type(budget) not in (int,float) or not math.isfinite(budget) or not 0<budget<=self.capital_limit:
                raise ValueError('일일 위험예산 예외 한도는 승인자본 이내 양수여야 합니다.')
        for k,v in asdict(self).items():
            if k in ('daily_risk_override_day','daily_risk_override_budget_krw'): continue
            if k=='max_consecutive_losses' and v is None: continue
            if k in ('new_entry_cutoff','force_flatten_start','after_prep_start','after_market_start','after_entry_cutoff','after_force_flatten_start','after_market_close'): continue
            if isinstance(v,bool): continue
            if not isinstance(v,(int,float)) or not math.isfinite(v) or v<0:
                raise ValueError(f'{k}: 유효한 0 이상 숫자 필요')
        for k in ('decision_snapshot_seconds','scan_seconds','rank_decay_cycles','max_scan_charts','min_source_count','cooldown_seconds','max_consecutive_losses','box_min_bars','empirical_min_trades','horizon_a_minutes','horizon_b_minutes','horizon_c_minutes'):
            if k=='max_consecutive_losses' and getattr(self,k) is None: continue
            if type(getattr(self,k)) is not int or getattr(self,k)<=0: raise ValueError(f'{k}: 양의 정수 필요')
        if type(self.ALLOW_OVERNIGHT) is not bool or type(self.costs_confirmed) is not bool or type(self.replacement_enabled) is not bool or type(self.entry_context_enabled) is not bool or type(self.stable_position_book_confirmation) is not bool:
            raise ValueError('불리언 설정 오류')
        if type(self.confirmed_box_direction) is not bool or type(self.confirmed_structure_protection) is not bool:
            raise ValueError('불리언 설정 오류')
        for k in ('new_entry_cutoff','force_flatten_start','after_prep_start','after_market_start','after_entry_cutoff','after_force_flatten_start','after_market_close'):
            datetime.strptime(getattr(self,k),'%H:%M:%S')
        if not '09:00:00'<self.new_entry_cutoff<self.force_flatten_start<'15:20:00':
            raise ValueError('신규진입 마감 < 강제청산 시작 < 15:20 필요')
        if not '15:30:00'<=self.after_prep_start<self.after_market_start<self.after_entry_cutoff<self.after_force_flatten_start<=self.after_market_close<='20:30:00':
            raise ValueError('AFTER 운영시간 설정 오류: prep < start < cutoff < flatten <= close')
        if type(self.AUTO_TRADING_CAP_KRW) is not int: raise ValueError('승인 한도는 원 단위 정수여야 합니다')
        if self.scan_seconds<15 or self.max_scan_charts>50 or self.min_source_count>5: raise ValueError('레이더 범위 오류')
        for k in ('commission_buy','commission_sell','sell_tax','pullback_max_retrace','pullback_volume_ratio','box_max_range','unrealized_giveback_fraction'):
            if not 0<=getattr(self,k)<1: raise ValueError(f'{k}: 비율 범위 오류')
        return self

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()

def load_automation(path):
    path=Path(path)
    if not path.exists() and path.name=='automation.json' and path.with_name('automation.example.json').exists():
        path=path.with_name('automation.example.json')
    return AutomationConfig(**json.loads(path.read_text(encoding='utf-8-sig'))).validate()

class Policy:
    def __init__(self,path):
        self.path=Path(path)
        if not self.path.exists():
            atomic_json(self.path,{'mode':'ARMED','live_authorized':False,'releases':{},'revision':0,'commands':[]})
        self.refresh()

    def refresh(self):
        self.state=read_json_retry(self.path)
        if self.state['mode'] not in MODES: raise ValueError('운영상태 오류')
        return self.state

    @property
    def mode(self): return self.state['mode']

    def locked(self,symbol):
        return symbol in HARD_LOCKS and symbol not in self.state['releases']

    def approve_test_release(self,symbol,confirmation):
        if not legacy_test_release_enabled(): raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 완료된 시험기능은 비활성입니다.')
        if symbol!=TEST_RELEASE_SYMBOL or confirmation!='TEST_CAPITAL_RELEASE':
            raise ValueError('267320 1주에 대한 명시적 TEST_CAPITAL_RELEASE 확인 필요')
        if self.state.get('test_capital_release'): raise ValueError('이미 사용 또는 승인된 1회성 정책')
        self.state['test_capital_release']={'symbol':symbol,'side':'SELL','quantity':1,'max_qty':1,'one_shot':True,'status':'APPROVED','time':now().isoformat()}
    
    def cancel_test_release(self,confirmation):
        if not legacy_test_release_enabled(): raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 완료 기록은 취소하지 않습니다.')
        approval=self.state.get('test_capital_release')
        if not approval:
            raise ValueError('취소할 시험매도 승인이 없습니다.')
        if confirmation!='CANCEL_TEST_CAPITAL_RELEASE':
            raise ValueError('명시적 시험매도 승인 취소 확인 필요')
        if approval.get('status')!='APPROVED':
            raise ValueError('아직 주문 제출 전인 APPROVED 상태만 취소할 수 있습니다.')
        if approval.get('order_local'):
            raise ValueError('주문 기록이 연결된 승인은 취소할 수 없습니다.')
        self.state.pop('test_capital_release',None)    
    
    def command(self,command,symbol=None,confirmation=None):
        self.refresh()
        if command=='TEST_CAPITAL_RELEASE':
            self.approve_test_release(symbol,confirmation)
        elif command=='CANCEL_TEST_CAPITAL_RELEASE':
            self.cancel_test_release(confirmation)
        elif command=='RELEASE':
            if symbol not in HARD_LOCKS or confirmation!='RELEASE': raise ValueError('명시적 RELEASE 확인 필요')
            self.state['releases'][symbol]={'time':now().isoformat(),'actor':'USER_COMMAND'}
        elif command=='RESET_KILL':
            if self.state.get('mode')!='KILL': raise ValueError('KILL 상태에서만 비상정지를 해제할 수 있습니다.')
            if confirmation!='RESET KILL': raise ValueError('명시적 RESET KILL 확인 필요')
            self.state['mode']='STOP'
            self.state['live_authorized']=False
            self.state['after_live_authorized']=False
        elif command in MODES:
            if command=='LIVE':
                if confirmation!='LIVE': raise ValueError('명시적 LIVE 확인 필요')
                self.state['live_authorized']=True
            elif command=='KILL':
                self.state['live_authorized']=False
                self.state['after_live_authorized']=False
            self.state['mode']=command
        else: raise ValueError('지원하지 않는 명령')
        self.state['revision']+=1
        self.state['commands'].append({'time':now().isoformat(),'command':command,'symbol':symbol,'revision':self.state['revision']})
        atomic_json(self.path,self.state)

