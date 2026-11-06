"""Validate a broker-confirmed external close without mutating the ledger.

This module is deliberately not a recovery command. A production mutation
requires a stopped single writer, a fresh broker snapshot and an audited atomic
transaction; a websocket notice alone must never authorize a ledger change.
"""
from datetime import datetime
from decimal import Decimal,InvalidOperation

def _integer(value):
    try:d=Decimal(str(value).strip().replace(',',''))
    except (InvalidOperation,ValueError):raise ValueError('INVALID_NUMERIC_FIELD') from None
    if not d.is_finite() or d<0 or d!=d.to_integral_value():raise ValueError('INVALID_NUMERIC_FIELD')
    return int(d)

def validate_external_close(state,snapshot,now,*,symbol,order_id,execution_id):
    """Fail closed on absent, stale, mismatched or duplicate evidence."""
    from automation.policy import HARD_LOCKS
    if symbol in HARD_LOCKS:raise ValueError('HARD_LOCK')
    if not all(isinstance(x,str) and x for x in (symbol,order_id,execution_id)):
        raise ValueError('IDENTIFIER_REQUIRED')
    if not state.get('identity') or snapshot.get('account_identity')!=state.get('identity'):
        raise ValueError('ACCOUNT_IDENTITY_MISMATCH')
    try:started=datetime.fromisoformat(snapshot['query_started_at'])
    except (KeyError,TypeError,ValueError):raise ValueError('SNAPSHOT_TIME_UNKNOWN') from None
    if not started.tzinfo or not now.tzinfo or not 0<=(now-started).total_seconds()<=15:
        raise ValueError('SNAPSHOT_STALE')
    holdings=snapshot.get('holdings');orders=snapshot.get('orders');fills=snapshot.get('fills')
    if not isinstance(holdings,dict) or not isinstance(orders,list) or not isinstance(fills,list):
        raise ValueError('BROKER_SNAPSHOT_INCOMPLETE')
    # An empty first page is not proof of a flat account. The caller must
    # explicitly establish that every broker query completed all its pages.
    if any(snapshot.get(k) is not True for k in ('holdings_complete','orders_complete','fills_complete')):
        raise ValueError('BROKER_PAGINATION_NOT_PROVEN')
    if _integer((holdings.get(symbol) or {}).get('qty',0))!=0:raise ValueError('BROKER_POSITION_NOT_FLAT')
    if any(str(r.get('stk_cd','')).lstrip('A')==symbol for r in orders):raise ValueError('BROKER_SYMBOL_OPEN_ORDER')
    position=(state.get('positions') or {}).get(symbol)
    if not isinstance(position,dict) or position.get('ownership')!='BOT':raise ValueError('BOT_POSITION_UNKNOWN')
    qty=_integer(position.get('qty'))
    if qty<=0:raise ValueError('INVALID_BOT_QUANTITY')
    local_orders=state.get('orders') or {}
    if any(o.get('symbol')==symbol and o.get('status') not in {'FILLED','REJECTED','CANCELED'} for o in local_orders.values()):
        raise ValueError('LOCAL_UNRESOLVED_ORDER')
    if any(str(o.get('id',''))==order_id for o in local_orders.values()):raise ValueError('ORDER_ALREADY_IN_LEDGER')
    if any(r.get('order_id')==order_id or r.get('execution_id')==execution_id for r in state.get('external_close_reviews',[])):
        raise ValueError('EXTERNAL_CLOSE_ALREADY_RECORDED')
    events=[e for e in state.get('unowned_order_events',[]) if e.get('symbol')==symbol and str(e.get('order_id',''))==order_id and str((e.get('fields') or {}).get('909',''))==execution_id]
    if len(events)!=1:raise ValueError('UNIQUE_WEBSOCKET_FILL_REQUIRED')
    try:observed=datetime.fromisoformat(events[0]['observed_at'])
    except (KeyError,TypeError,ValueError):raise ValueError('WEBSOCKET_TIME_UNKNOWN') from None
    if not observed.tzinfo or not observed<=started<=now:raise ValueError('BROKER_SNAPSHOT_PRECEDES_FILL')
    fields=events[0].get('fields') or {}
    if (fields.get('913')!='체결' or fields.get('905')!='-매도' or _integer(fields.get('900'))!=qty
            or _integer(fields.get('902'))!=0 or _integer(fields.get('911'))!=qty):
        raise ValueError('WEBSOCKET_FILL_MISMATCH')
    matches=[r for r in fills if str(r.get('ord_no',''))==order_id]
    if len(matches)!=1:raise ValueError('UNIQUE_BROKER_FILL_REQUIRED')
    row=matches[0]
    if (str(row.get('stk_cd','')).lstrip('A')!=symbol or row.get('io_tp_nm')!='-매도'
            or row.get('ord_stt')!='체결' or _integer(row.get('ord_qty'))!=qty
            or _integer(row.get('cntr_qty'))!=qty or _integer(row.get('oso_qty'))!=0
            or _integer(row.get('cntr_pric'))!=_integer(fields.get('910'))):
        raise ValueError('BROKER_FILL_MISMATCH')
    # Charges must be verified, not inferred from strategy settings.
    commission=_integer(row.get('tdy_trde_cmsn'));tax=_integer(row.get('tdy_trde_tax'))
    return {'status':'PROOF_ONLY_NOT_APPLIED','symbol':symbol,'order_id':order_id,'execution_id':execution_id,
            'quantity':qty,'price':_integer(fields.get('910')),'observed_at':events[0].get('observed_at'),
            'broker_sell_commission':commission,'broker_sell_tax':tax,
            'account_query_started_at':started.isoformat()}

def validate_maintenance_shutdown(state,snapshot,now,*,symbol,order_id,execution_id,policy_mode):
    """Read-only preflight; never signals, stops, or changes an active worker.

    This is deliberately not a shutdown mechanism. A caller must separately
    establish that no request or writer is in flight before any takeover.
    """
    if policy_mode!='STOP':raise ValueError('STOP_REQUIRED')
    if not state.get('halt'):raise ValueError('ACTIVE_HALT_REQUIRED')
    proof=validate_external_close(state,snapshot,now,symbol=symbol,order_id=order_id,execution_id=execution_id)
    if snapshot['orders']:raise ValueError('BROKER_PENDING_ORDERS')
    orders=state.get('orders') or {}
    if any(o.get('status') not in {'FILLED','REJECTED','CANCELED'} for o in orders.values()):
        raise ValueError('LOCAL_UNRESOLVED_ORDER')
    if set(state.get('positions') or {})!={symbol}:raise ValueError('OTHER_BOT_POSITION_PRESENT')
    existing=(state.get('automation') or {}).get('existing')
    if not isinstance(existing,dict):raise ValueError('EXISTING_HOLDINGS_UNKNOWN')
    expected={s:_integer(h.get('qty')) for s,h in existing.items() if _integer(h.get('qty'))}
    actual={s:_integer(h.get('qty')) for s,h in snapshot['holdings'].items() if _integer(h.get('qty'))}
    if actual!=expected:raise ValueError('OTHER_HOLDINGS_MISMATCH')
    return {'status':'PREFLIGHT_ONLY_NOT_STOPPED','proof':proof,
            'writer_quiescence':'UNKNOWN','inflight_requests':'UNKNOWN'}
