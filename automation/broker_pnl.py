"""Confirmed broker charges settle realized PnL; entry risk reservations stay modeled."""
import math
from datetime import datetime

def _number(value):
    if isinstance(value,bool) or value is None or str(value).strip()=='':raise ValueError('MISSING_CHARGE')
    result=float(str(value).replace(',',''))
    if not math.isfinite(result) or result<0:raise ValueError('INVALID_CHARGE')
    return result

def _exit_order(state,trade):
    explicit=trade.get('exit_order_local') or trade.get('external_close_review',{}).get('local')
    if explicit:return state['orders'].get(explicit)
    matches=[]
    for order in state['orders'].values():
        if order.get('side')!='SELL' or order.get('symbol')!=trade['symbol'] or not order.get('last_fill_at'):continue
        try:delta=abs((datetime.fromisoformat(order['last_fill_at'])-datetime.fromisoformat(trade['closed'])).total_seconds())
        except (TypeError,ValueError):continue
        if delta<=2 and order.get('filled',0)>=trade['qty']:matches.append(order)
    return matches[0] if len(matches)==1 else None

def settle_broker_pnl(state,fills,costs,asof,broker_identity):
    report={'source':'ka10076','asof':asof.isoformat(),'cash_adjustment':0.,'pnl_adjustment':0.,'loss_adjustment':0.,
            'orders_confirmed':0,'trades_confirmed':0,'unknown':[]}
    if broker_identity!=state['identity'] or asof.date().isoformat()!=state['day']:
        return {**report,'reason':'IDENTITY_OR_DAY_UNCONFIRMED'}
    groups={}
    for row in fills:groups.setdefault(str(row.get('ord_no','')),[]).append(row)
    ids={}
    for order in state['orders'].values():
        if order.get('filled'):ids[str(order.get('id',''))]=ids.get(str(order.get('id','')),0)+1
    for local,order in state['orders'].items():
        if not order.get('filled') or not str(order.get('created','')).startswith(state['day']):continue
        if order.get('signal',{}).get('ownership') in {'EXISTING_POLICY','LEGACY_REPLACEMENT'}:continue
        rows=groups.get(str(order.get('id','')),[])
        try:
            if ids[str(order.get('id',''))]!=1:raise ValueError('DUPLICATE_JOURNAL_ORDER_ID')
            if len(rows)!=1:raise ValueError('MISSING_OR_AMBIGUOUS_ORDER_CHARGES')
            row=rows[0];qty=_number(row['cntr_qty']);price=_number(row['cntr_pric'])
            fee=_number(row['tdy_trde_cmsn']);tax=_number(row['tdy_trde_tax'])
            if not (row.get('ord_stt')=='체결' and row.get('stk_cd','').lstrip('A')==order['symbol']
                    and row.get('io_tp_nm')==('+매수' if order['side']=='BUY' else '-매도')
                    and qty==order['filled'] and _number(row['ord_qty'])==order['qty']
                    and qty>0 and price>0 and abs(qty*price-order['amount'])<.01):
                raise ValueError('ORDER_FILL_BASIS_MISMATCH')
        except (ValueError,KeyError,TypeError) as error:
            report['unknown'].append({'local':local,'reason':str(error)});continue
        previous=order.get('broker_charges')
        model=costs.buy_fee if order['side']=='BUY' else costs.sell_fee
        booked=(previous['commission']+previous['tax']+model(order['amount']-previous['amount'])
                if previous else model(order['amount']))
        cash_delta=booked-fee-tax
        state['cash']+=cash_delta
        if 'automation' in state:state['automation']['cash']+=cash_delta
        report['cash_adjustment']+=cash_delta;report['orders_confirmed']+=1
        order['broker_charges']={'source':'BROKER_QUERY','quantity':int(qty),'amount':order['amount'],
            'commission':fee,'tax':tax,'observed_at':asof.isoformat()}
    for trade in state.get('trades',[]):
        if not str(trade.get('closed','')).startswith(state['day']):continue
        buy=state['orders'].get(trade.get('trade_id'));sell=_exit_order(state,trade)
        if not buy or not sell or not buy.get('broker_charges') or not sell.get('broker_charges'):continue
        if ids.get(str(buy.get('id')))!=1 or ids.get(str(sell.get('id')))!=1:continue
        bc,sc=buy['broker_charges'],sell['broker_charges'];qty=trade['qty']
        if any(ch['quantity']!=order['filled'] or ch['amount']!=order['amount'] for ch,order in ((bc,buy),(sc,sell))):continue
        if qty>bc['quantity'] or qty>sc['quantity']:continue
        commission=bc['commission']*qty/bc['quantity']+sc['commission']*qty/sc['quantity']
        tax=bc['tax']*qty/bc['quantity']+sc['tax']*qty/sc['quantity']
        gross=(trade['exit']-trade['entry'])*qty;actual=gross-commission-tax
        previous=trade['pnl_estimate'];delta=actual-previous
        trade.setdefault('modeled_pnl_at_fill',previous)
        trade.update(pnl_estimate=actual,net_pnl=actual,net_pnl_completed=actual if trade.get('completed') else None,
            gross_pnl=gross,commission=commission,tax=tax,fee_actual=commission+tax,pnl_source='BROKER_QUERY',
            broker_pnl_observed_at=min(bc['observed_at'],sc['observed_at']),exit_order_local=sell['local'],exit_order_id=sell['id'])
        state['realized']+=delta
        loss_delta=max(0,-actual)-max(0,-previous)
        state['daily_loss']+=loss_delta
        report['pnl_adjustment']+=delta;report['loss_adjustment']+=loss_delta;report['trades_confirmed']+=1
    if report['trades_confirmed'] and 'automation' in state:
        completed={t.get('trade_id') for t in state['trades'] if t.get('completed') and str(t.get('closed','')).startswith(state['day'])}
        totals={}
        for trade in state['trades']:
            key=trade.get('trade_id')
            if key in completed:totals[key]=totals.get(key,0)+trade['pnl_estimate']
        consecutive=0
        for net in reversed(list(totals.values())):
            if net>=0:break
            consecutive+=1
        state['automation']['consecutive_losses']=consecutive
    if abs(state['daily_loss'])<1e-7:state['daily_loss']=0.
    return report
