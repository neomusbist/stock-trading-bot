"""Early Discovery Collector for rapid intraday volume surge candidates.

Operates within Kiwoom REST rate limits. Swaps preopen-only ranking (ka10029)
for intraday volume/turnover acceleration ranking during regular market hours.
"""
from datetime import datetime
from typing import Dict, List, Any
from candidate_builder import RANKS
from core.util import code, number

# ka10030: 당일거래량급증요청 (Intraday rapid volume surge)
INTRADAY_VOLUME_SURGE_RANK = (
    'ka10030', 'pred_trde_qty_flu_upper', 30,
    {'mrkt_tp': '000', 'qry_tp': '1', 'rank_strt': '0', 'rank_end': '100', 'stex_tp': '1'}
)


def get_active_ranks(at: datetime = None) -> List[tuple]:
    """Returns rank API definitions tuned for regular vs preopen session.

    During regular market (09:00~15:30), ka10029 (preopen expected prints)
    is replaced with ka10030 (intraday volume surge) to avoid stale rankings
    and catch early breakout volume without adding extra API request count.
    """
    if at is None:
        return RANKS
    t_str = at.strftime('%H:%M:%S')
    if '09:00:00' <= t_str < '15:30:00':
        active = []
        for api, key, limit, payload in RANKS:
            if api == 'ka10029':
                active.append(INTRADAY_VOLUME_SURGE_RANK)
            else:
                active.append((api, key, limit, payload))
        return active
    return RANKS


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
    cur_px = number(r.get('cur_prc') or r.get('past_curr_prc') or r.get('exp_cntr_pric'))
    if cur_px and cur_px > 0:
        item['reference_price'] = cur_px
        item['first_raw_price'] = cur_px
    item['source_details'][api] = {
        'observed_rank': rank,
        'raw': dict(r),
        'received_at': now_iso
    }
    return item
