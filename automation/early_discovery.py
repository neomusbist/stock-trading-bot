"""Early Discovery Collector for rapid intraday volume surge candidates.

Operates within Kiwoom REST rate limits. Swaps or augments ranking definitions
for SHADOW research mode during regular market hours, while strictly isolating
LIVE trading from unverified rank endpoints.

Kiwoom API Contracts:
- ka10030: 당일거래량상위요청
  Path: /api/dostk/rkinfo
  Response key: 'tdy_trde_qty_upper'
  Payload: {'mrkt_tp': '000', 'sort_tp': '1', 'trde_qty_cnd': '0', 'stk_cnd': '4', 'crd_cnd': '0', 'pric_cnd': '0', 'stex_tp': '1'}

- ka10023: 거래량급증요청
  Path: /api/dostk/rkinfo
  Response key: 'trde_qty_sdnin'
  Payload: {'mrkt_tp': '000', 'sort_tp': '1', 'time_tp': '1', 'time': '1', 'trde_qty_tp': '0', 'stk_cnd': '0', 'stex_tp': '1'}
"""
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from candidate_builder import RANKS
from core.util import code, number

# Official Kiwoom REST API definitions
KA10030_TODAY_VOLUME_TOP: Tuple[str, str, int, Dict[str, str]] = (
    'ka10030',
    'tdy_trde_qty_upper',
    30,
    {
        'mrkt_tp': '000',
        'sort_tp': '1',
        'trde_qty_cnd': '0',
        'stk_cnd': '4',
        'crd_cnd': '0',
        'pric_cnd': '0',
        'stex_tp': '1'
    }
)

KA10023_VOLUME_SURGE: Tuple[str, str, int, Dict[str, str]] = (
    'ka10023',
    'trde_qty_sdnin',
    30,
    {
        'mrkt_tp': '000',
        'sort_tp': '1',
        'time_tp': '1',
        'time': '1',
        'trde_qty_tp': '0',
        'stk_cnd': '0',
        'stex_tp': '1'
    }
)


def parse_ka10030_response(data: dict, received_at: str = None) -> List[dict]:
    """Parses ka10030 (당일거래량상위) API response according to Kiwoom OpenAPI contract."""
    if not isinstance(data, dict):
        return []
    rows = data.get('tdy_trde_qty_upper')
    if not isinstance(rows, list):
        return []
    parsed = []
    for rank, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        sym = code(r.get('stk_cd') or '')
        nm = (r.get('stk_nm') or '').strip()
        if not sym or not nm:
            continue
        cur_prc = number(r.get('cur_prc'), signed=True)
        trde_qty = number(r.get('trde_qty'))
        flu_rt = number(r.get('flu_rt'), signed=True)
        parsed.append({
            'rank': rank,
            'code': sym,
            'name': nm,
            'cur_prc': cur_prc,
            'trde_qty': trde_qty,
            'flu_rt': flu_rt,
            'pred_rt': number(r.get('pred_rt'), signed=True),
            'trde_tern_rt': number(r.get('trde_tern_rt'), signed=True),
            'api': 'ka10030',
            'received_at': received_at,
            'raw': dict(r)
        })
    return parsed


def parse_ka10023_response(data: dict, received_at: str = None) -> List[dict]:
    """Parses ka10023 (거래량급증) API response according to Kiwoom OpenAPI contract."""
    if not isinstance(data, dict):
        return []
    rows = data.get('trde_qty_sdnin')
    if not isinstance(rows, list):
        return []
    parsed = []
    for rank, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            continue
        sym = code(r.get('stk_cd') or '')
        nm = (r.get('stk_nm') or '').strip()
        if not sym or not nm:
            continue
        cur_prc = number(r.get('cur_prc'), signed=True)
        now_trde_qty = number(r.get('now_trde_qty'))
        prev_trde_qty = number(r.get('prev_trde_qty'))
        sdnin_qty = number(r.get('sdnin_qty'), signed=True)
        flu_rt = number(r.get('flu_rt'), signed=True)
        parsed.append({
            'rank': rank,
            'code': sym,
            'name': nm,
            'cur_prc': cur_prc,
            'now_trde_qty': now_trde_qty,
            'prev_trde_qty': prev_trde_qty,
            'sdnin_qty': sdnin_qty,
            'flu_rt': flu_rt,
            'api': 'ka10023',
            'received_at': received_at,
            'raw': dict(r)
        })
    return parsed


def extract_candidate_row(r: dict, api: str, rank: int, now_iso: str) -> dict:
    """Parses a candidate row with defensive validation and exact attribution."""
    sym = code(r.get('stk_cd') or '')
    name = (r.get('stk_nm') or '').strip()
    if not sym or not name:
        return {}
    item = {
        'code': sym,
        'name': name,
        'source_details': {},
        'reference_price': None,
        'first_raw_at': now_iso
    }
    cur_px = number(r.get('cur_prc') or r.get('past_curr_prc') or r.get('exp_cntr_pric'), signed=True)
    if cur_px and cur_px > 0:
        item['reference_price'] = cur_px
        item['first_raw_price'] = cur_px
    item['source_details'][api] = {
        'observed_rank': rank,
        'raw': dict(r),
        'received_at': now_iso
    }
    return item


def get_shadow_ranks(at: datetime = None) -> List[tuple]:
    """Returns rank definitions for SHADOW research mode.
    
    During regular market (09:00~15:30), replaces preopen-only ka10029
    with intraday volume top (ka10030) and volume surge (ka10023).
    """
    if at is None:
        return list(RANKS)
    t_str = at.strftime('%H:%M:%S')
    if '09:00:00' <= t_str < '15:30:00':
        active = []
        for api, key, limit, payload in RANKS:
            if api == 'ka10029':
                active.append(KA10030_TODAY_VOLUME_TOP)
                active.append(KA10023_VOLUME_SURGE)
            else:
                active.append((api, key, limit, payload))
        return active
    return list(RANKS)


def get_active_ranks(at: datetime = None, mode: str = 'LIVE') -> List[tuple]:
    """Returns active rank definitions respecting execution isolation.
    
    LIVE mode returns the verified production RANKS unmodified.
    SHADOW mode incorporates intraday volume top and surge ranking.
    """
    if mode == 'SHADOW':
        return get_shadow_ranks(at)
    return list(RANKS)


def verify_historical_early_discovery_status(
    symbol: str,
    target_time_str: str,
    radar_records: List[dict]
) -> dict:
    """Verifies whether an early discovery was actually provable at the target time.
    
    If no rank snapshot for ka10030 or ka10023 is recorded at target_time,
    early discovery feasibility at that time is strictly UNKNOWN.
    """
    provable_sources = []
    for rec in radar_records:
        t = rec.get('time', '')
        if target_time_str in t:
            for row in rec.get('rows', []):
                if row.get('code') == symbol:
                    sources = row.get('source_details', {})
                    for s in ('ka10030', 'ka10023'):
                        if s in sources:
                            provable_sources.append(s)
    if not provable_sources:
        return {
            'status': 'UNKNOWN',
            'reason': 'NO_INTRADAY_VOLUME_RANK_SNAPSHOT_RECORDED',
            'symbol': symbol,
            'target_time': target_time_str,
            'provable_sources': provable_sources
        }
    return {
        'status': 'CONFIRMED',
        'reason': 'RECORDED_IN_INTRADAY_VOLUME_RANK',
        'symbol': symbol,
        'target_time': target_time_str,
        'provable_sources': provable_sources
    }
