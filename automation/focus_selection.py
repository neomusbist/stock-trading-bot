"""V1.2 Fast Focus Selection and Notification.
전체 raw discovery(60초)와 별개로 구독 관찰집합 내 SHADOW readiness 변화 시
main thread에 bounded notification을 전달하고, main thread가 WATCH focus를 결정한다.
"""
import time
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

def candidate_priority_tuple(row: dict, tier: int) -> tuple:
    """동일 tier 비교는 discovery_score(내림차순), overhead_share(오름차순), symbol(오름차순)."""
    score = row.get('discovery_score', row.get('source_count', 0) * 10)
    overhead = row.get('overhead_share_estimate', 0.0) or 0.0
    return (tier, -score, overhead, row.get('code', ''))

def select_focus(
    current_watch: Dict[str, dict],
    candidates: List[dict],
    readiness_map: Dict[str, dict],
    max_watchlist: int = 5,
    fallback_source1: bool = True,
    clock_iso: str = ''
) -> FocusSelection:
    """Main thread에서 focus 선택을 재계산하고 FocusSelection을 반환한다.
    
    규칙:
    1. primary(source_count >= 2)가 fallback(source_count == 1)보다 엄격히 우선.
    2. primary 후보 중 strictly higher tier (tier < current_tier)만 기존 WATCH를 교체 가능.
    3. same tier에서는 기존 WATCH를 유지 (zero churn).
    4. fallback은 빈 슬롯(< max_watchlist)에만 배치.
    5. fallback이 primary를 밀어낼 수 없음.
    """
    prev_watch_symbols = list(current_watch.keys()) if isinstance(current_watch, dict) else list(current_watch)
    tier_map: Dict[str, int] = {}
    priority_tuples: Dict[str, tuple] = {}
    by_code: Dict[str, dict] = {r['code']: r for r in candidates}

    # 1. 모든 후보의 tier 및 priority tuple 계산
    for r in candidates:
        sym = r['code']
        r_info = readiness_map.get(sym, {})
        tier = r_info['tier'] if 'tier' in r_info else calculate_tier(r_info.get('gates', {}), r_info.get('setup_trials', []), r)
        tier_map[sym] = tier
        priority_tuples[sym] = candidate_priority_tuple(r, tier)

    # 기존 WATCH 종목 중 아직 후보에 남아있는 종목들
    existing_watch_candidates = [by_code[s] for s in prev_watch_symbols if s in by_code]

    # Primary 후보군 (source_count >= 2)과 Fallback 후보군 (source_count == 1)
    primary_candidates = [
        r for r in candidates
        if r.get('source_count', 0) >= 2
        and r.get('status') in {'ELIGIBLE', 'WATCH', 'RESERVE', 'SHADOW'}
    ]
    primary_candidates.sort(key=lambda r: priority_tuples[r['code']])

    fallback_candidates = [
        r for r in candidates
        if r.get('source_count', 0) == 1
        and (r.get('affordability', {}).get('entry_eligible') is True or r.get('fallback_source1') or r.get('watch_type') == 'FALLBACK_SOURCE1' or r.get('status') in {'ELIGIBLE', 'WATCH', 'RESERVE', 'SHADOW'})
        and r.get('status') in {'ELIGIBLE', 'WATCH', 'RESERVE', 'REJECT', 'SHADOW'}
        and not r.get('reason', '').startswith('HARD_LOCK')
    ]
    fallback_candidates.sort(key=lambda r: priority_tuples[r['code']])

    # 2. Focus Selection 계산
    next_watch: Dict[str, dict] = {}
    blocked_reasons: Dict[str, str] = {}

    # (A) 먼저 기존 WATCH 중 primary였던 종목들을 우선 유지 후보로 유지
    for s in prev_watch_symbols:
        if s in by_code:
            r = by_code[s]
            if r.get('source_count', 0) >= 2:
                next_watch[s] = r

    # (B) 더 높은 tier의 새 primary 후보가 기존 lower-tier primary를 교체할 수 있는지 검사
    for p in primary_candidates:
        sym = p['code']
        if sym in next_watch:
            continue
        p_tier = tier_map[sym]

        if len(next_watch) < max_watchlist:
            next_watch[sym] = p
        else:
            # 현재 next_watch 중 가장 낮은 tier (숫자가 가장 큰 tier) 탐색
            # 동률이면 discovery_score가 가장 낮은 종목
            worst_sym = max(next_watch.keys(), key=lambda s: priority_tuples[s])
            worst_tier = tier_map[worst_sym]

            # strictly higher tier만 교체 허용 (p_tier < worst_tier)
            if p_tier < worst_tier:
                del next_watch[worst_sym]
                next_watch[sym] = p
                blocked_reasons[worst_sym] = f"DISPLACED_BY_HIGHER_TIER_{TIER_NAMES[p_tier]}"
            else:
                blocked_reasons[sym] = f"SAME_OR_LOWER_TIER_PRESERVED_{TIER_NAMES[worst_tier]}"

    # (C) 빈 슬롯이 남았고 fallback_source1 허용인 경우, fallback 후보로 채운다
    if fallback_source1 and len(next_watch) < max_watchlist:
        # 기존 WATCH 중 fallback이었던 종목을 먼저 유지
        for s in prev_watch_symbols:
            if s in by_code and s not in next_watch:
                r = by_code[s]
                if r.get('source_count', 0) == 1 and len(next_watch) < max_watchlist:
                    next_watch[s] = r

        # 그래도 빈 자리가 있으면 새 fallback 후보 추가
        for fb in fallback_candidates:
            if len(next_watch) >= max_watchlist:
                break
            sym = fb['code']
            if sym not in next_watch:
                next_watch[sym] = fb

    next_watch_symbols = list(next_watch.keys())
    promoted = [s for s in next_watch_symbols if s not in prev_watch_symbols]
    demoted = [s for s in prev_watch_symbols if s not in next_watch_symbols]
    applied = bool(promoted or demoted)

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
