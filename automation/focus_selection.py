"""V1.2 Fast Focus Selection and Notification.
전체 raw discovery(60초)와 별개로 구독 관찰집합 내 SHADOW readiness 변화 시
main thread에 bounded notification을 전달하고, main thread가 WATCH focus를 결정한다.
"""
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

# Tier Definitions (Section ⑥-3)
TIER_SIGNAL_READY = 0       # 0: fresh/유효 warmup, 공통 gate 및 trial gate 모두 PASS
TIER_GEOMETRY_BASE_READY = 1# 1: A/B/C 형상 있음, 공통 gate (warmup/가속/유동성/spread/구조/상태) PASS
TIER_MOMENTUM_BASE_READY = 2# 2: 형상 미완성, 공통 gate PASS
TIER_RANK_ONLY = 3          # 3: 신선한 비교용 readiness 없음/UNKNOWN, 기존 압축 통과
TIER_FRESH_WEAK = 4         # 4: 신선한 가속 또는 유동성 FAIL, 기존 압축 통과

TIER_NAMES = {
    TIER_SIGNAL_READY: 'SIGNAL_READY',
    TIER_GEOMETRY_BASE_READY: 'GEOMETRY_BASE_READY',
    TIER_MOMENTUM_BASE_READY: 'MOMENTUM_BASE_READY',
    TIER_RANK_ONLY: 'RANK_ONLY',
    TIER_FRESH_WEAK: 'FRESH_WEAK'
}

COMMON_GATE_KEYS = ('warmup', 'acceleration', 'liquidity', 'spread', 'structure', 'market_status')

@dataclass
class FocusNotification:
    """Diagnostic worker에서 main thread로 전달하는 경량 readiness 알림.
    주문 side/qty/intent/권한을 포함하지 않는다.
    """
    symbol: str
    event_seq: int
    observation_seq: int
    transport_epoch: str
    detected_at: str
    tier: int
    readiness_kind: str  # 'GEOMETRY_READY', 'SIGNAL_READY', 'MOMENTUM_BASE_READY', 'MOMENTUM_LOST', 'TIER_CHANGED'
    source_snapshot_id: str = ''
    metadata_version: str = '1.0'
    config_hash: str = ''
    capital_version: str = '1.0'

@dataclass
class FocusSelection:
    """Main thread가 원자적으로 commit하는 WATCH focus 선택 결과."""
    focus_epoch: str
    previous_watch: List[str]
    next_watch: List[str]
    promoted: List[str]
    demoted: List[str]
    primary_reason: str
    tier_map: Dict[str, int]
    priority_tuples: Dict[str, tuple]
    applied: bool
    blocked_reasons: Dict[str, str] = field(default_factory=dict)
    retained_subscription_reg_zero: bool = True

def calculate_tier(gates: dict, setup_trials: list, row: dict = None) -> int:
    """후보의 gates와 setup_trials로부터 boolean tier를 계산한다. (새 가중 점수 금지)"""
    if not gates:
        return TIER_RANK_ONLY

    # An explicitly evaluated B/C uses its existing price gates; acceleration is observational.
    independent=[t for t in (setup_trials or []) if t.get('setup') in {'B','C'}
        and t.get('acceleration_required') is False and t.get('candidate_generated') is True]
    safe_keys=('warmup','spread','structure','market_status')
    independent=[t for t in independent if t.get('gates',{}).get('liquidity',{}).get('result')=='PASS']
    # A formed box/pullback with impossible RR, invalidation or costs is not
    # geometry readiness. Only the actual breakout can still be waiting.
    price_keys={'invalidation','chase','risk_reward','resistance','economic','entry_context','box_direction','box_origin'}
    viable=[t for t in independent if all(v.get('result')=='PASS' for k,v in t.get('gates',{}).items()
        if k in price_keys)]
    blocked_geometry=bool(independent and not viable)
    independent=viable
    if independent and all(gates.get(k,{}).get('result')=='PASS' for k in safe_keys):
        if any(t.get('entry_ready') is True and t.get('gates') and
               all(v.get('result')=='PASS' for v in t['gates'].values()) for t in independent):return TIER_SIGNAL_READY
        return TIER_GEOMETRY_BASE_READY

    other_viable=any(t.get('setup')=='A' and t.get('candidate_generated') and t.get('gates') and
        all(v.get('result')=='PASS' for k,v in t['gates'].items() if k not in {'breakout','order_price'})
        for t in (setup_trials or []))
    if blocked_geometry and not other_viable:return TIER_FRESH_WEAK

    # 가속 또는 유동성 명시적 FAIL이면 TIER_FRESH_WEAK
    accel = gates.get('acceleration', {}).get('result')
    liq = gates.get('liquidity', {}).get('result')
    if accel == 'FAIL' or liq == 'FAIL':
        return TIER_FRESH_WEAK

    # 공통 gate PASS 여부
    common_pass = all(gates.get(k, {}).get('result') == 'PASS' for k in COMMON_GATE_KEYS if k in gates)
    if not common_pass:
        return TIER_RANK_ONLY

    # 형상 및 trial gate 검사
    has_geometry = False
    has_full_signal = False
    for trial in (setup_trials or []):
        if trial.get('candidate_generated') is True:
            has_geometry = True
            trial_gates = trial.get('gates', {})
            if trial_gates and all(v.get('result') == 'PASS' for v in trial_gates.values()):
                has_full_signal = True
                break

    if has_full_signal:
        return TIER_SIGNAL_READY
    if has_geometry:
        return TIER_GEOMETRY_BASE_READY
    return TIER_MOMENTUM_BASE_READY

def candidate_priority_tuple(row: dict, tier: int, is_existing: bool = False) -> tuple:
    """우선순위 튜플: (tier, -effective_score, incumbent_tie, overhead_share, symbol)
    - tier: 0 (SIGNAL_READY) ~ 4 (FRESH_WEAK). 작을수록 우선.
    - effective_score: discovery_score 또는 score.
      단일 틱 flip-flop 방지를 위해 동일 티어 내 기존 WATCH 종목에는 +2.0점 히스테리시스 부여.
    - incumbent_tie: +2 유지여유와 정확히 동점이면 기존 WATCH를 유지.
    - overhead_share: 매물대 부담. 작을수록 우선.
    - symbol: 동률 시 결정적(deterministic) 정렬.
    """
    raw_score = row.get('discovery_score', row.get('score', row.get('source_count', 0) * 10))
    score = float(raw_score if raw_score is not None else 0.0)
    effective_score = score + (2.0 if is_existing else 0.0)
    overhead = float(row.get('overhead_share_estimate', 0.0) or 0.0)
    return (tier, -effective_score, 0 if is_existing else 1, overhead, row.get('code', ''))

def select_focus(
    current_watch: Dict[str, dict],
    candidates: List[dict],
    readiness_map: Dict[str, dict],
    max_watchlist: int = 8,
    fallback_source1: bool = True,
    clock_iso: str = ''
) -> FocusSelection:
    """Main thread에서 focus 선택을 재계산하고 FocusSelection을 반환한다.
    
    규칙:
    1. source_count >= 1 종목 전체가 단일 풀에서 순위 경쟁 참여.
    2. source_count는 scoring 보너스(+10 per source)로 반영됨.
    3. higher tier 또는 higher score(동일 티어 내 2.0점 초과 우위)를 가진 신규 후보는 기존 WATCH를 교체 가능.
    4. 동일 티어 내 기존 WATCH 종목에는 단일 틱 뒤집힘(flip-flop) 방지를 위한 +2.0점 히스테리시스 부여.
    5. 상위 max_watchlist(기본 8개) 종목이 원자적으로 선택됨.
    """
    prev_watch_symbols = list(current_watch.keys()) if isinstance(current_watch, dict) else list(current_watch)
    tier_map: Dict[str, int] = {}
    priority_tuples: Dict[str, tuple] = {}
    by_code: Dict[str, dict] = {r['code']: r for r in candidates if 'code' in r}
    for s in prev_watch_symbols:
        if s not in by_code and isinstance(current_watch, dict) and isinstance(current_watch.get(s), dict):
            by_code[s] = current_watch[s]

    # 적격 후보 필터링
    eligible_candidates = []
    for r in by_code.values():
        sym = r.get('code')
        if not sym:
            continue
        reason = str(r.get('reason', ''))
        if reason.startswith('HARD_LOCK') or reason.startswith('EXISTING_POLICY'):
            continue
        if r.get('affordability', {}).get('entry_eligible') is False:
            continue
        status = r.get('status')
        if status and status not in {'ELIGIBLE', 'WATCH', 'RESERVE', 'SHADOW'}:
            continue
        src_count = r.get('source_count', 0)
        if src_count < 1:
            continue
        if not fallback_source1 and src_count < 2:
            continue
        eligible_candidates.append(r)

    # 각 후보의 tier 및 priority tuple 계산
    for r in eligible_candidates:
        sym = r['code']
        r_info = readiness_map.get(sym, {})
        if clock_iso and r_info.get('timestamp'):
            try:fresh=0<=(datetime.fromisoformat(clock_iso)-datetime.fromisoformat(r_info['timestamp'])).total_seconds()<=15
            except (ValueError,TypeError):fresh=False
            if not fresh:r_info={}
        tier = r_info['tier'] if 'tier' in r_info else calculate_tier(r_info.get('gates', {}), r_info.get('setup_trials', []), r)
        tier_map[sym] = tier
        is_existing = (sym in prev_watch_symbols)
        priority_tuples[sym] = candidate_priority_tuple(r, tier, is_existing=is_existing)

    # 정렬: (tier, -effective_score, incumbent_tie, overhead, symbol)
    eligible_candidates.sort(key=lambda r: priority_tuples[r['code']])

    selected_candidates = eligible_candidates[:max_watchlist]
    next_watch_symbols = [r['code'] for r in selected_candidates]

    promoted = [s for s in next_watch_symbols if s not in prev_watch_symbols]
    demoted = [s for s in prev_watch_symbols if s not in next_watch_symbols]
    applied = bool(promoted or demoted)

    blocked_reasons: Dict[str, str] = {}
    for r in eligible_candidates[max_watchlist:]:
        sym = r['code']
        sym_tier = tier_map.get(sym, 3)
        if selected_candidates:
            worst_selected = selected_candidates[-1]
            w_sym = worst_selected['code']
            w_tier = tier_map.get(w_sym, 3)
            if sym in prev_watch_symbols:
                if w_tier < sym_tier:
                    blocked_reasons[sym] = f"DISPLACED_BY_HIGHER_TIER_{TIER_NAMES.get(w_tier, 'UNKNOWN')}"
                else:
                    blocked_reasons[sym] = f"DISPLACED_BY_HIGHER_SCORE_{w_sym}"
            else:
                if w_tier < sym_tier:
                    blocked_reasons[sym] = f"SAME_OR_LOWER_TIER_PRESERVED_{TIER_NAMES.get(w_tier, 'UNKNOWN')}"
                else:
                    blocked_reasons[sym] = f"SCORE_BELOW_THRESHOLD_{w_sym}"

    primary_reason = "FOCUS_UNCHANGED"
    if promoted:
        top_promoted = promoted[0]
        primary_reason = f"PROMOTED_{top_promoted}_{TIER_NAMES.get(tier_map.get(top_promoted, 3))}"
    elif demoted:
        primary_reason = f"DEMOTED_{demoted[0]}"

    return FocusSelection(
        focus_epoch=clock_iso or str(time.time()),
        previous_watch=prev_watch_symbols,
        next_watch=next_watch_symbols,
        promoted=promoted,
        demoted=demoted,
        primary_reason=primary_reason,
        tier_map=tier_map,
        priority_tuples=priority_tuples,
        applied=applied,
        blocked_reasons=blocked_reasons
    )
