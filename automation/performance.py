"""부분청산을 묶는다. 확인된 증권사 비용을 우선하고 미확인 추정을 구분한다."""
from collections import defaultdict
from statistics import mean

def completed_trades(fragments):
    groups=defaultdict(list)
    for t in fragments: groups[t.get('trade_id',t['symbol']+'@'+t['opened'])].append(t)
    out=[]
    for key,rows in groups.items():
        if not any(r.get('completed',False) for r in rows): continue
        last=rows[-1]
        totals={}
        for field in ('gross_pnl','commission','tax','entry_value'):
            values=[r.get(field) for r in rows]
            totals[field]=sum(values) if all(isinstance(x,(int,float)) for x in values) else None
        out.append({**last,'trade_id':key,'qty':sum(r['qty'] for r in rows),
            **totals,'net_pnl':sum(r['pnl_estimate'] for r in rows),
            'exit_price':sum(r['exit']*r['qty'] for r in rows)/sum(r['qty'] for r in rows) if all(isinstance(r.get('exit'),(int,float)) for r in rows) and sum(r['qty'] for r in rows)>0 else None,
            'pnl_estimate':sum(r['pnl_estimate'] for r in rows),'fee_estimate':sum(r.get('fee_actual',r.get('fee_estimate',0)) for r in rows),
            'MFE':max(r['MFE'] for r in rows),'MAE':max(r['MAE'] for r in rows)})
    return sorted(out,key=lambda r:r['closed'])

def summary(rows):
    wins=[r['pnl_estimate'] for r in rows if r['pnl_estimate']>0]
    losses=[r['pnl_estimate'] for r in rows if r['pnl_estimate']<0]
    streak=maximum=0
    for r in rows:
        streak=streak+1 if r['pnl_estimate']<0 else 0; maximum=max(maximum,streak)
    avgwin=mean(wins) if wins else None; avgloss=-mean(losses) if losses else None
    return {'count':len(rows),'win_rate':len(wins)/len(rows) if rows else None,
        'average_win':avgwin,'average_loss':avgloss,'payoff':avgwin/avgloss if avgwin is not None and avgloss else None,
        'net_expectancy_krw':mean(r['pnl_estimate'] for r in rows) if rows else None,
        'average_holding_seconds':mean(r['holding_seconds'] for r in rows) if rows else None,
        'average_MFE':mean(r['MFE'] for r in rows) if rows else None,
        'average_MAE':mean(r['MAE'] for r in rows) if rows else None,'max_consecutive_losses':maximum,
        'cost_basis':('증권사 체결조회 수수료·세금 기준' if rows and all(r.get('pnl_source')=='BROKER_QUERY' for r in rows)
            else '증권사 비용 우선; 미확인 거래는 설정 수수료/세금 추정' if any(r.get('pnl_source')=='BROKER_QUERY' for r in rows)
            else '실제 체결가격 + 설정 수수료/세금 추정. 호가 슬리피지는 체결가격에 이미 포함')}

def report(fragments):
    rows=completed_trades(fragments); patterns=defaultdict(list); hours=defaultdict(list)
    for r in rows: patterns[r.get('pattern','UNKNOWN')].append(r); hours[r.get('entry_hour','UNKNOWN')].append(r)
    return {'all':summary(rows),'patterns':{k:summary(v) for k,v in patterns.items()},
            'hours':{k:summary(v) for k,v in hours.items()},'completed_trades':rows}

