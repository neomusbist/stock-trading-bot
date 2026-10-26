"""Explicit operator recovery after a stopped, fresh, flat broker reconciliation."""
from datetime import datetime
import math
from core.util import atomic_json
from core.event_log import append_jsonl

REVIEWABLE={'관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요','데이터/상태 오류: PermissionError'}
DATA_EXIT_HALT='보유종목 시세 지연: 긴급 청산 요청'
EXTERNAL_ORDER_HALTS={'관리 종목 외부 미체결 주문','관리 종목에 외부 주문 감지: 계좌 대조 필요'}
TERMINAL={'FILLED','CANCELED','REJECTED'}

def recover_reviewed_halt(path,state,snapshot,stamp,reason):
    h = state.get('halt') or ''
    if not (h in REVIEWABLE or h == DATA_EXIT_HALT or h in EXTERNAL_ORDER_HALTS or 'TypeError' in h or 'AttributeError' in h or 'PermissionError' in h):
        raise ValueError('HALT_REASON_REQUIRES_SEPARATE_REVIEW')
    if state.get('positions') or any(o['status'] not in TERMINAL for o in state['orders'].values()):
        raise ValueError('NON_FLAT_ORDER_LEDGER')
    if snapshot.get('account_identity')!=state.get('identity'):raise ValueError('ACCOUNT_IDENTITY_MISMATCH')
    query=datetime.fromisoformat(snapshot['query_started_at'])
    incident=datetime.fromisoformat(state['halt_info']['occurred_at'])
    if not incident<query<=stamp or (stamp-query).total_seconds()>15:raise ValueError('RECOVERY_SNAPSHOT_STALE')
    if state.get('halt')==DATA_EXIT_HALT:
        # Explicit review only, after the emergency exit is fully confirmed.
        # A flat local ledger alone cannot prove that an uncertain SELL completed.
        trade=state.get('trades',[])[-1] if state.get('trades') else {}
        try:closed=datetime.fromisoformat(trade.get('closed',''))
        except (ValueError,TypeError):closed=None
        sells=[o for o in state['orders'].values() if o.get('symbol')==trade.get('symbol')
            and o.get('side')=='SELL' and o.get('status')=='FILLED' and o.get('filled',0)>0]
        if (trade.get('reason')!='시세 지연/연결 단절' or trade.get('ownership','BOT')!='BOT'
                or closed is None or closed.tzinfo is None or not incident<=closed<=query
                or not trade.get('qty') or sum(o['filled'] for o in sells)<trade['qty']):
            raise ValueError('DATA_EXIT_FILL_CONFIRMATION_REQUIRED')
    if snapshot['orders']:raise ValueError('BROKER_PENDING_ORDERS')
    if state.get('halt') in EXTERNAL_ORDER_HALTS:
        review=(state.get('external_close_reviews') or [None])[-1]
        if not isinstance(review,dict) or review.get('source')!='BROKER_CONFIRMED_USER_EXTERNAL_SELL':
            raise ValueError('EXTERNAL_CLOSE_REVIEW_REQUIRED')
        order=state['orders'].get(review.get('local'),{})
        rows=[r for r in snapshot.get('fills',[]) if str(r.get('ord_no'))==review.get('order_id')]
        try:
            qty=review['quantity'];price=review['price'];sym=review['symbol']
            observed=datetime.fromisoformat(review['observed_at'])
            row=rows[0]
            valid=(len(rows)==1 and type(qty) is int and qty>0 and type(price) in (int,float) and math.isfinite(price) and price>0
                and incident<observed<=query and (query-observed).total_seconds()<=300
                and row.get('stk_cd','').lstrip('A')==sym and row.get('io_tp_nm')=='-매도'
                and row.get('ord_stt')=='체결' and int(row['cntr_qty'])==int(row['ord_qty'])==qty
                and int(row['oso_qty'])==0 and float(row['cntr_pric'])==price
                and order.get('id')==review['order_id'] and order.get('symbol')==sym and order.get('side')=='SELL'
                and order.get('status')=='FILLED' and order.get('filled')==order.get('qty')==qty
                and order.get('amount')==qty*price and order.get('signal',{}).get('ownership')=='USER_EXTERNAL')
        except (KeyError,IndexError,TypeError,ValueError):valid=False
        if not valid:raise ValueError('EXTERNAL_CLOSE_FILL_PROOF_INVALID')
    existing=state['automation']['existing']
    expected={s:h['qty'] for s,h in existing.items() if h['qty']}
    actual={s:h['qty'] for s,h in snapshot['holdings'].items() if h['qty']}
    if actual!=expected:raise ValueError('BROKER_HOLDINGS_MISMATCH')
    # This explicit recovery never resets day, losses, cash, counters or approval.
    old=dict(state['halt_info']);old.update(active=False,recovered_at=stamp.isoformat(),
        recovery='EXPLICIT_FRESH_FLAT_RECONCILIATION',recovery_reason=reason)
    updated={**state,'halt':'','halt_info':old,'halt_history':[*state.get('halt_history',[]),old]}
    try:
        atomic_json(path,updated)
        append_jsonl(path.parent/'events.jsonl',{'time':stamp.isoformat(),'event':'MANUAL_HALT_RECOVERED',
            'halt_info':old,'account_identity_match':True,'bot_positions':0,'broker_unfilled':0,
            'query_started_at':snapshot['query_started_at'],'actual_orders':0})
    except Exception:
        atomic_json(path,state)
        raise
    return updated
