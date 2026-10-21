"""Early discovery ranking configuration and contract validation for active market hours.

Official Kiwoom REST API (/api/dostk/rkinfo) contracts for early discovery:
- ka10023: 거래량급증요청 (Volume Surge, HTS [0168]), response key 'trde_qty_sdnin'
- ka10030: 당일거래량상위요청 (Today's Top Volume, HTS [0184]), response key 'tdy_trde_qty_upper'
"""
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from core.settings import now, KST
from core.util import code, number
from candidate_builder import RANKS

# Official Kiwoom REST API (/api/dostk/rkinfo) contracts for early discovery
OFFICIAL_EARLY_RANK_CONTRACTS: Dict[str, Dict[str, Any]] = {
    'ka10023': {
        'name': '거래량급증요청',
        'path': '/api/dostk/rkinfo',
        'response_key': 'trde_qty_sdnin',
        'required_request_fields': (
            'mrkt_tp', 'sort_tp', 'tm_tp', 'trde_qty_tp', 'tm', 'stk_cnd', 'pric_tp', 'stex_tp'
        ),
        'required_row_fields': ('stk_cd', 'stk_nm', 'cur_prc', 'trde_qty', 'sdnin_rt'),
    },
    'ka10030': {
        'name': '당일거래량상위요청',
        'path': '/api/dostk/rkinfo',
        'response_key': 'tdy_trde_qty_upper',
        'required_request_fields': (
            'mrkt_tp', 'sort_tp', 'mang_stk_incls', 'crd_tp', 'trde_qty_tp',
            'pric_tp', 'trde_prica_tp', 'mrkt_open_tp', 'stex_tp'
        ),
        'required_row_fields': ('stk_cd', 'stk_nm', 'cur_prc', 'trde_qty', 'trde_prica'),
    },
}

def get_volume_surge_rank_spec(session: str = 'REGULAR', venue: str = 'KRX') -> Tuple[str, str, int, Dict[str, str]]:
    """Return ka10023 rank specification adapted for the session/venue.

    stex_tp values according to Kiwoom OpenAPI contract:
    - '1': KRX only (Regular market)
    - '2': NXT only
    - '3': 통합 (KRX + NXT combined) - Optimal for AFTER session / SOR routing
    """
    stex_tp = '1'
    if session == 'AFTER' or venue == 'SOR':
        stex_tp = '3'
    elif venue == 'NXT':
        stex_tp = '2'
    return (
        'ka10023', 'trde_qty_sdnin', 30,
        {
            'mrkt_tp': '000',
            'sort_tp': '1',
            'tm_tp': '1',
            'trde_qty_tp': '5',
            'tm': '3',
            'stk_cnd': '4',
            'pric_tp': '0',
            'stex_tp': stex_tp,
        }
    )

# ka10023: 거래량급증 (Intraday rapid volume surge)
INTRADAY_VOLUME_SURGE_RANK: Tuple[str, str, int, Dict[str, str]] = get_volume_surge_rank_spec('REGULAR', 'KRX')

# ka10030: 당일거래량상위요청 (Today's cumulative top volume)
INTRADAY_TOP_VOLUME_RANK: Tuple[str, str, int, Dict[str, str]] = (
    'ka10030', 'tdy_trde_qty_upper', 30,
    {
        'mrkt_tp': '000',
        'sort_tp': '1',
        'mang_stk_incls': '1',
        'crd_tp': '0',
        'trde_qty_tp': '0',
        'pric_tp': '0',
        'trde_prica_tp': '0',
        'mrkt_open_tp': '0',
        'stex_tp': '1',
    }
)


def validate_rank_spec(spec) -> bool:
    """Validate (api, response_key, limit, payload) against official Kiwoom REST API contract."""
    if not isinstance(spec, (tuple, list)) or len(spec) != 4:
        raise ValueError('랭킹 스펙 형식 오류: (api, key, limit, payload) 4-튜플 필요')
    api, key, limit, payload = spec
    if not isinstance(api, str) or not isinstance(key, str) or not isinstance(limit, int) or limit <= 0:
        raise ValueError(f'{api}: 랭킹 스펙 메타데이터 오류')
    if not isinstance(payload, dict):
        raise ValueError(f'{api}: 요청 payload dict 필요')
    contract = OFFICIAL_EARLY_RANK_CONTRACTS.get(api)
    if contract is not None:
        expected_key = contract['response_key']
        if key != expected_key:
            raise ValueError(
                f"{api} 응답 키 계약 불일치: expected='{expected_key}', got='{key}'"
            )
        missing = [f for f in contract['required_request_fields'] if f not in payload]
        if missing:
            raise ValueError(f"{api} 요청 필수 필드 누락: {missing}")
    return True


def validate_rank_response(api: str, key: str, data: dict) -> list:
    """Validate broker response structure against the official contract."""
    if api in OFFICIAL_EARLY_RANK_CONTRACTS:
        dummy_payload = {f: '1' for f in OFFICIAL_EARLY_RANK_CONTRACTS[api]['required_request_fields']}
    else:
        dummy_payload = {'mrkt_tp': '000'}
    validate_rank_spec((api, key, 1, dummy_payload))
    if not isinstance(data, dict):
        raise ValueError(f'{api}: 응답 객체 dict 아님')
    rows = data.get(key)
    if not isinstance(rows, list):
        raise ValueError(f'{api}: {key} 필드 없음/형식 오류')
    return rows


def _safe_number(val, signed=False) -> Optional[float]:
    """Converts value to float safely without throwing exceptions on missing or empty data."""
    if val in (None, ''):
        return None
    try:
        return number(val, signed=signed)
    except (TypeError, ValueError):
        return None


def parse_ka10023_response(data: dict, received_at: str = None) -> List[dict]:
    """Parses ka10023 (거래량급증) API response according to Kiwoom OpenAPI contract."""
    rows = validate_rank_response('ka10023', 'trde_qty_sdnin', data)
    parsed = []
    for rank, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        sym = code(r.get('stk_cd') or '')
        nm = (r.get('stk_nm') or '').strip()
        if not sym or not nm:
            continue
        parsed.append({
            'rank': rank,
            'code': sym,
            'name': nm,
            'cur_prc': _safe_number(r.get('cur_prc'), signed=True),
            'now_trde_qty': _safe_number(r.get('now_trde_qty') or r.get('trde_qty')),
            'prev_trde_qty': _safe_number(r.get('prev_trde_qty')),
            'sdnin_qty': _safe_number(r.get('sdnin_qty'), signed=True),
            'flu_rt': _safe_number(r.get('flu_rt'), signed=True),
            'sdnin_rt': _safe_number(r.get('sdnin_rt'), signed=True),
            'api': 'ka10023',
            'received_at': received_at,
            'raw': dict(r),
        })
    return parsed


def parse_ka10030_response(data: dict, received_at: str = None) -> List[dict]:
    """Parses ka10030 (당일거래량상위) API response according to Kiwoom OpenAPI contract."""
    rows = validate_rank_response('ka10030', 'tdy_trde_qty_upper', data)
    parsed = []
    for rank, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        sym = code(r.get('stk_cd') or '')
        nm = (r.get('stk_nm') or '').strip()
        if not sym or not nm:
            continue
        parsed.append({
            'rank': rank,
            'code': sym,
            'name': nm,
            'cur_prc': _safe_number(r.get('cur_prc'), signed=True),
            'trde_qty': _safe_number(r.get('trde_qty')),
            'flu_rt': _safe_number(r.get('flu_rt'), signed=True),
            'pred_rt': _safe_number(r.get('pred_rt'), signed=True),
            'trde_tern_rt': _safe_number(r.get('trde_tern_rt'), signed=True),
            'trde_prica': _safe_number(r.get('trde_prica')),
            'api': 'ka10030',
            'received_at': received_at,
            'raw': dict(r),
        })
    return parsed


def extract_candidate_row(r: dict, rank: int, api: str, now_iso: str = None) -> dict:
    """Normalize a raw ranking row into a candidate evaluation dictionary."""
    if not isinstance(r, dict):
        raise ValueError('랭킹 행 형식 오류')
    sym = code(r.get('stk_cd', ''))
    name = (r.get('stk_nm') or '').strip()
    if not sym or not name:
        raise ValueError('종목코드/종목명 결측')

    cur_px = None
    for field in ('cur_prc', 'past_curr_prc', 'exp_cntr_pric'):
        px = _safe_number(r.get(field), signed=False)
        if px is not None and px > 0:
            cur_px = px
            break

    flu_rt = _safe_number(r.get('flu_rt'), signed=True)
    vol = _safe_number(r.get('now_trde_qty') or r.get('trde_qty') or r.get('pred_trde_qty'), signed=False)
    sdnin_rt = _safe_number(r.get('sdnin_rt'), signed=True)

    iso_stamp = now_iso or now().isoformat()
    return {
        'code': sym,
        'name': name,
        'api': api,
        'rank': rank,
        'price': cur_px,
        'reference_price': cur_px,
        'first_raw_price': cur_px,
        'first_raw_at': iso_stamp,
        'flu_rt': flu_rt,
        'volume': vol,
        'sdnin_rt': sdnin_rt,
        'source_details': {
            api: {
                'observed_rank': rank,
                'raw': dict(r),
                'received_at': iso_stamp,
                'values': {
                    'flu_rt': flu_rt,
                    'now_trde_qty': vol,
                    'sdnin_rt': sdnin_rt,
                }
            }
        }
    }


def get_active_ranks(at: datetime = None, mode: str = 'LIVE', include_today_top_volume: bool = False) -> List[tuple]:
    """Return active ranking API list based on execution mode and KST market time.

    In LIVE mode:
        Always returns the 5 production RANKS unmodified to guarantee operational isolation.

    In SHADOW mode:
        Before 09:05:00 KST: retains ka10029 (preopen expected prints).
        From 09:05:00 KST onward: replaces preopen ka10029 with ka10023 (거래량급증요청, trde_qty_sdnin).
        The total request count remains EXACTLY 5 to prevent rate limit spikes and event loop lag.
        If include_today_top_volume is explicitly True: appends ka10030 (당일거래량상위요청, tdy_trde_qty_upper).
    """
    if mode != 'SHADOW':
        return list(RANKS)

    if at is None:
        at = now()
    elif isinstance(at, datetime) and at.tzinfo is not None:
        at = at.astimezone(KST)
    t_str = at.strftime('%H:%M:%S')

    if t_str < '09:05:00':
        return list(RANKS)

    active = []
    for r in RANKS:
        if r[0] == 'ka10029':
            validate_rank_spec(INTRADAY_VOLUME_SURGE_RANK)
            active.append(INTRADAY_VOLUME_SURGE_RANK)
        else:
            active.append(r)

    if include_today_top_volume:
        validate_rank_spec(INTRADAY_TOP_VOLUME_RANK)
        active.append(INTRADAY_TOP_VOLUME_RANK)

    return active


def get_shadow_ranks(at: datetime = None, include_today_top_volume: bool = False) -> List[tuple]:
    """Convenience alias for get_active_ranks(at, mode='SHADOW')."""
    return get_active_ranks(at, mode='SHADOW', include_today_top_volume=include_today_top_volume)


def verify_window_capture_feasibility(snapshots, symbol: str, start_time: str = '10:06:00', end_time: str = '10:17:00') -> dict:
    """Verify whether a candidate could be captured in [start_time, end_time] from real ranking snapshots.

    Never asserts 'IMPOSSIBLE' when snapshot data for the window is absent; returns 'UNKNOWN'.
    """
    if not snapshots:
        return {
            'status': 'UNKNOWN',
            'reason': 'RANK_SNAPSHOT_DATA_ABSENT',
            'symbol': symbol,
            'window': (start_time, end_time),
            'required_evidence': [
                'runtime/radar_history.jsonl (time, rows[].code, rows[].source_details)',
                'raw Kiwoom rkinfo snapshots for ka10023/ka10030/ka10027/ka10032/ka00198 in 10:06~10:17 KST',
            ],
        }
    in_window = []
    found_at = []
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        ts = str(snap.get('time') or snap.get('source_observed_at') or '')
        hhmmss = ts[11:19] if 'T' in ts and len(ts) >= 19 else ts
        if start_time <= hhmmss <= end_time:
            in_window.append(snap)
            rows = snap.get('rows') or []
            for row in rows:
                if isinstance(row, dict) and row.get('code') == symbol:
                    found_at.append({'time': ts, 'sources': list((row.get('source_details') or {}).keys())})
    if not in_window:
        return {
            'status': 'UNKNOWN',
            'reason': 'NO_SNAPSHOTS_IN_TARGET_WINDOW',
            'symbol': symbol,
            'window': (start_time, end_time),
        }
    return {
        'status': 'CAPTURED' if found_at else 'NOT_IN_RECORDED_SNAPSHOTS',
        'reason': 'FOUND_IN_SNAPSHOT' if found_at else 'ABSENT_IN_RECORDED_SOURCES',
        'symbol': symbol,
        'window': (start_time, end_time),
        'snapshot_count': len(in_window),
        'matches': found_at,
    }

def collect_shadow_early_candidates(
    rest,
    at: Optional[datetime] = None,
    include_today_top_volume: bool = False
) -> List[dict]:
    """Collect early discovery candidates strictly for independent SHADOW research observation.

    Architectural Guarantees:
    1. 100% Isolated from LIVE WATCH: All candidates are explicitly tagged with
       SHADOW_ONLY=True and actual_order_permission=False.
    2. Zero Live Order Risk: DynamicRadar and RadarWorker filter out SHADOW_ONLY candidates.
    3. Rate Limit Preservation: Uses observation_rest with diagnostic_only=True, yielding
       automatically to live broker operations.
    4. Validates API requests and broker responses against official contracts.
    """
    if at is None:
        at = now()
    elif isinstance(at, datetime) and at.tzinfo is not None:
        at = at.astimezone(KST)

    candidates = []
    iso_stamp = at.isoformat()

    # 1. Query ka10023 (Volume surge: trde_qty_sdnin)
    api, key, limit, payload = INTRADAY_VOLUME_SURGE_RANK
    validate_rank_spec((api, key, limit, payload))
    try:
        data = rest.query(api, payload, path='/api/dostk/rkinfo')
        rows = validate_rank_response(api, key, data)
        for rank, r in enumerate(rows, 1):
            if not isinstance(r, dict):
                continue
            try:
                cand = extract_candidate_row(r, rank, api, now_iso=iso_stamp)
            except (ValueError, KeyError, TypeError):
                continue
            cand['SHADOW_ONLY'] = True
            cand['actual_order_permission'] = False
            cand['pipeline'] = 'SHADOW_RESEARCH_EARLY_DISCOVERY'
            cand['status'] = 'SHADOW_RAW'
            cand['affordability'] = {'entry_eligible': True, 'reason_code': 'SHADOW_OBSERVATION_ONLY'}
            candidates.append(cand)
    except Exception as e:
        if hasattr(rest, 'record_error'):
            rest.record_error(e)

    # 2. Optionally query ka10030 (Today's top volume: tdy_trde_qty_upper)
    if include_today_top_volume:
        api30, key30, limit30, payload30 = INTRADAY_TOP_VOLUME_RANK
        validate_rank_spec((api30, key30, limit30, payload30))
        try:
            data30 = rest.query(api30, payload30, path='/api/dostk/rkinfo')
            rows30 = validate_rank_response(api30, key30, data30)
            for rank, r in enumerate(rows30, 1):
                if not isinstance(r, dict):
                    continue
                try:
                    cand = extract_candidate_row(r, rank, api30, now_iso=iso_stamp)
                except (ValueError, KeyError, TypeError):
                    continue
                cand['SHADOW_ONLY'] = True
                cand['actual_order_permission'] = False
                cand['pipeline'] = 'SHADOW_RESEARCH_EARLY_DISCOVERY'
                cand['status'] = 'SHADOW_RAW'
                cand['affordability'] = {'entry_eligible': True, 'reason_code': 'SHADOW_OBSERVATION_ONLY'}
                candidates.append(cand)
        except Exception as e:
            if hasattr(rest, 'record_error'):
                rest.record_error(e)

    return candidates

