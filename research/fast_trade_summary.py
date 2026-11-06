"""Summarize broker-sourced FAST exits by entry, including partial exits.

Input is an already frozen state snapshot. This is a descriptive ledger report,
not an expectancy estimate or evidence that a strategy is profitable.
"""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path


def summarize(trades):
    groups=defaultdict(list); missing_id=0
    for row in trades:
        if row.get('ownership')!='BOT' or row.get('pattern')!='FAST':continue
        trade_id=row.get('trade_id')
        if not isinstance(trade_id,str) or not trade_id:
            missing_id+=1;continue
        groups[trade_id].append(row)
    accepted=[]; unknown=[]
    for trade_id,rows in groups.items():
        reasons=[]
        if sum(row.get('completed') is True for row in rows)!=1:
            reasons.append('FINAL_EXIT_COUNT_NOT_ONE')
        if len({row.get('symbol') for row in rows})!=1 or not rows[0].get('symbol'):
            reasons.append('SYMBOL_INCONSISTENT_OR_MISSING')
        if any((row.get('pnl_source') or row.get('pnl_basis'))!='BROKER_QUERY' for row in rows):
            reasons.append('BROKER_COST_SOURCE_INCOMPLETE')
        if any(type(row.get('pnl_estimate')) not in (int,float) or not math.isfinite(row['pnl_estimate']) for row in rows):
            reasons.append('NET_PNL_MISSING_OR_INVALID')
        if reasons:
            unknown.append({'trade_id':trade_id,'reasons':reasons});continue
        accepted.append({'trade_id':trade_id,'symbol':rows[0]['symbol'],
            'entry_at':rows[0].get('opened'),'exit_legs':len(rows),
            'net_pnl':sum(row['pnl_estimate'] for row in rows)})
    values=[x['net_pnl'] for x in accepted]
    wins=[x for x in values if x>0];losses=[x for x in values if x<0]
    return {'positions':len(accepted),'wins':len(wins),'losses':len(losses),
        'flat':len(values)-len(wins)-len(losses),'net_pnl':sum(values) if values else None,
        'average_net_pnl':sum(values)/len(values) if values else None,
        'average_win':sum(wins)/len(wins) if wins else None,
        'average_loss':sum(losses)/len(losses) if losses else None,
        'missing_trade_id_rows':missing_id,'unknown_positions':unknown,'trades':accepted,
        'version_attribution':'UNKNOWN_UNLESS_SEPARATELY_PROVEN',
        'expectancy':'UNKNOWN_MIXED_VERSIONS_AND_SMALL_SAMPLE'}


def main():
    p=argparse.ArgumentParser();p.add_argument('frozen_state',type=Path);a=p.parse_args()
    state=json.loads(a.frozen_state.read_text(encoding='utf-8-sig'))
    print(json.dumps(summarize(state.get('trades',[])),ensure_ascii=False))

if __name__=='__main__':main()
