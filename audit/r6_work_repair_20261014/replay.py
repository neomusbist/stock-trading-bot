"""Causal snapshot-account repair; this is NOT a raw-tick FAST backtest.

The inherited proxy entry rule is a frozen control, not a validated strategy.
Missing quotes never create fills. No trade is closed at the data boundary.
All hypothetical fills remain UNKNOWN for depth, queue and market impact.
Usage: python -m audit.r6_work_repair_20261014.replay ARCHIVE --out OUTPUT
"""
from dataclasses import dataclass, replace
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import argparse
import csv
import hashlib
import json
import math

from audit.r6_claude_independent import independent_replay as control
from core.util import floor_tick, tick_size

EXPECTED_SHA256 = '0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c'


@dataclass(frozen=True)
class Execution:
    capital: float = 81300.
    approved_cap: float = 81300.
    prior_daily_loss: float = 0.
    risk_fraction: float = .02
    daily_fraction: float = .04
    buy_fee: float = .00015
    sell_fee: float = .00015
    tax: float = .002
    entry_slippage: float = .002
    exit_slippage: float = .003
    adverse_ticks: int = 0
    delay_snapshots: int = 0
    ttl_seconds: float = 15.
    max_chase: float = .003
    cooldown_seconds: float = 900.


def quote_valid(ev):
    bid, ask = ev.get('bid'), ev.get('ask')
    return (type(bid) in (int, float) and type(ask) in (int, float)
            and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask)


def ceil_tick(price):
    value = floor_tick(price)
    return value if value >= price else value+tick_size(value)


def shift_ticks(price, count):
    for _ in range(abs(count)):
        price += tick_size(price) if count > 0 else -tick_size(max(1., price-1))
    return price


def entry_fill_price(ask, p):
    shifted = shift_ticks(ask, p.adverse_ticks)
    return ceil_tick(shifted*(1+p.entry_slippage))


def exit_fill_price(bid, p):
    shifted = shift_ticks(bid, -p.adverse_ticks)
    return floor_tick(shifted*(1-p.exit_slippage))


def simulate(events, p):
    """One chronological account, cumulative losing trades and marked risk cap.

    Signal geometry is inherited solely to compare execution/accounting with
    the original audit. Its artificial percentage stop/target is never claimed
    as a repaired FAST setup. Existing actual FAST needs raw trade volume.
    """
    cash = min(p.capital, p.approved_cap)
    loss_used = p.prior_daily_loss
    pos = None
    trades, latency, declines = [], [], Counter()
    last_exit = {}
    rule = control.Params(capital=p.capital, comm_buy=p.buy_fee, comm_sell=p.sell_fee,
                          tax=p.tax, entry_slip=p.entry_slippage, exit_slip=p.exit_slippage)

    def fill_entry(ev):
        nonlocal cash, pos
        if not quote_valid(ev):
            declines['ENTRY_QUOTE_UNKNOWN'] += 1
            return
        age = (ev['dt']-pos['signal_at']).total_seconds()
        if age < 0 or age > p.ttl_seconds or ev['ask'] > pos['signal_ask']*(1+p.max_chase):
            declines['EXPIRED_OR_CHASED_ENTRY'] += 1
            pos = None
            return
        price = entry_fill_price(ev['ask'], p)
        stop_fill = exit_fill_price(pos['stop'], p)
        unit_risk = price*(1+p.buy_fee)-stop_fill*(1-p.sell_fee-p.tax)
        equity = min(p.approved_cap, cash)
        remaining = max(0., equity*p.daily_fraction-loss_used)
        budget = min(equity*p.risk_fraction, remaining)
        qty = min(int(min(cash,p.approved_cap)//(price*(1+p.buy_fee))), int(budget//unit_risk)) if unit_risk > 0 else 0
        if qty <= 0:
            declines['RISK_OR_INTEGER_CASH'] += 1
            pos = None
            return
        cash -= qty*price*(1+p.buy_fee)
        if cash < -1e-6:
            raise AssertionError('negative simulation cash')
        pos.update(phase='OPEN', entry_at=ev['dt'], entry_line=ev['line'],
                   entry_ask=ev['ask'], buy_price=price, qty=qty, highest_bid=ev['bid'],
                   planned_risk=qty*unit_risk, account_risk=equity*p.risk_fraction,
                   entry_delay_seconds=age)
        latency.append({'code':pos['symbol'],'signal_line':pos['signal_line'],
                        'signal_at':pos['signal_at'].isoformat(),'order_eligible_at':ev['dt'].isoformat(),
                        'signal_to_assumed_fill_seconds':age,'raw_to_signal_seconds':'UNKNOWN',
                        'source':'ENTRY_EVAL_SNAPSHOT_PROXY_NOT_FAST',
                        'real_order_eligibility':'UNKNOWN_NO_DEPTH_OR_RAW_TAPE'})

    def close(ev):
        nonlocal cash, pos, loss_used
        if not quote_valid(ev):
            declines['EXIT_QUOTE_UNKNOWN_KEEP_PENDING'] += 1
            return
        sell = exit_fill_price(ev['bid'], p)
        qty = pos['qty']
        buy_fee, sell_fee = pos['buy_price']*qty*p.buy_fee, sell*qty*p.sell_fee
        tax = sell*qty*p.tax
        gross_fill = (sell-pos['buy_price'])*qty
        net = gross_fill-buy_fee-sell_fee-tax
        loss_used += max(0., -net)  # Profits cannot replenish cumulative loss allowance.
        cash += sell*qty-sell_fee-tax
        signal_gross = (pos['trigger_bid']-pos['signal_ask'])*qty
        quote_gross = (ev['bid']-pos['entry_ask'])*qty
        buy_adverse = shift_ticks(pos['entry_ask'], p.adverse_ticks)-pos['entry_ask']
        sell_adverse = ev['bid']-shift_ticks(ev['bid'], -p.adverse_ticks)
        entry_slip = (pos['buy_price']-pos['entry_ask']-buy_adverse)*qty
        exit_slip = (ev['bid']-sell-sell_adverse)*qty
        delay_drag = signal_gross-quote_gross
        trades.append({'trade_id':len(trades)+1,'status':'CLOSED','code':pos['symbol'],'name':pos['name'],
                       'signal_line':pos['signal_line'],'entry_line':pos['entry_line'],
                       'signal_at':pos['signal_at'].isoformat(),'entry_at':pos['entry_at'].isoformat(),
                       'entry_ask':pos['entry_ask'],'buy_assumed':pos['buy_price'],'qty':qty,
                       'stop':pos['stop'],'target':pos['target'],'geometry_basis':'INHERITED_PROXY_NOT_VALIDATED_FAST',
                       'exit_trigger_line':pos['trigger_line'],'exit_line':ev['line'],
                       'exit_trigger_at':pos['trigger_at'].isoformat(),'exit_at':ev['dt'].isoformat(),
                       'exit_bid':ev['bid'],'sell_assumed':sell,'reason':pos['exit_reason'],
                       'gross_at_fill_quotes':quote_gross,'gross_at_assumed_fills':gross_fill,
                       'buy_fee':buy_fee,'sell_fee':sell_fee,'sell_tax':tax,
                       'entry_slippage_including_tick_rounding':entry_slip,
                       'exit_slippage_including_tick_rounding':exit_slip,
                       'adverse_tick_drag':(buy_adverse+sell_adverse)*qty,
                       'delay_drag_in_gross_not_deducted_twice':delay_drag,
                       'spread_accounting':'ALREADY_IN_ASK_TO_BID_GROSS_NO_EXTRA_DEDUCTION',
                       'net_pnl':net,'loss_used':loss_used,'cash_after':cash,
                       'holding_seconds':(ev['dt']-pos['entry_at']).total_seconds(),
                       'entry_delay_seconds':pos['entry_delay_seconds'],
                       'exit_delay_seconds':(ev['dt']-pos['trigger_at']).total_seconds(),
                       'planned_risk':pos['planned_risk'],'account_risk':pos['account_risk'],
                       'fill_feasibility':'UNKNOWN_NO_DEPTH_QUEUE_OR_RAW_TAPE'})
        last_exit[pos['symbol']] = ev['dt']
        pos = None

    for ev in events:
        if pos is not None and pos['symbol'] != ev['symbol']:
            continue
        if pos is not None and pos['phase'] == 'PENDING_ENTRY':
            pos['wait'] -= 1
            if pos['wait'] <= 0:
                fill_entry(ev)
            continue
        if pos is not None and pos['phase'] == 'PENDING_EXIT':
            pos['wait'] -= 1
            if pos['wait'] <= 0:
                close(ev)
            continue
        if pos is not None:
            if not quote_valid(ev):
                declines['OPEN_QUOTE_UNKNOWN_NO_FABRICATED_EXIT'] += 1
                continue
            bid = ev['bid']
            pos['highest_bid'] = max(pos['highest_bid'], bid)
            reason = None
            # Frozen proxy exit policy. Never uses future MFE.
            if bid >= pos['target']:
                reason = 'PROXY_TARGET_REACHED'
            elif bid <= pos['stop']:
                reason = 'PROXY_STOP'
            elif pos['highest_bid']/pos['entry_ask']-1 >= .018:
                lock = floor_tick(max(pos['entry_ask']*1.018, pos['highest_bid']*.992))
                if bid <= lock:
                    reason = 'PROXY_PROFIT_LOCK'
            if reason is None and (ev['dt']-pos['entry_at']).total_seconds() >= 180 and bid < pos['entry_ask']:
                reason = 'PROXY_MOMENTUM_STALL'
            if reason:
                pos.update(phase='PENDING_EXIT',wait=p.delay_snapshots,trigger_line=ev['line'],
                           trigger_at=ev['dt'],trigger_bid=bid,exit_reason=reason)
                if p.delay_snapshots == 0:
                    close(ev)  # Fill at displayed bid, never automatically at target.
            continue
        if ev['symbol'] in last_exit and (ev['dt']-last_exit[ev['symbol']]).total_seconds() < p.cooldown_seconds:
            declines['SAME_SYMBOL_COOLDOWN'] += 1
            continue
        if not quote_valid(ev):
            declines['NO_ENTRY_QUOTE'] += 1
            continue
        geo = control.entry_geometry(ev, rule, cash)
        if not geo:
            continue
        pos = {'symbol':ev['symbol'],'name':ev['name'],'signal_line':ev['line'],'signal_at':ev['dt'],
               'signal_ask':ev['ask'],'stop':geo['stop'],'target':geo['target'],
               'phase':'PENDING_ENTRY','wait':p.delay_snapshots}
        if p.delay_snapshots == 0:
            fill_entry(ev)
    unresolved = None
    if pos:
        relevant = [e for e in events if e['symbol'] == pos['symbol']]
        last = relevant[-1]
        mark = last['bid'] if quote_valid(last) else None
        unresolved = {'trade_id':len(trades)+1,'status':'UNRESOLVED','code':pos['symbol'],'name':pos['name'],
                      'phase':pos['phase'],'qty':pos.get('qty',0),'signal_line':pos['signal_line'],
                      'entry_line':pos.get('entry_line'),'entry_ask':pos.get('entry_ask'),
                      'buy_assumed':pos.get('buy_price'),'stop':pos['stop'],'target':pos['target'],
                      'net_pnl':None,'fill_feasibility':'UNKNOWN_NO_DEPTH_QUEUE_OR_RAW_TAPE',
                      'signal_at':pos['signal_at'].isoformat(),'entry_at':pos.get('entry_at').isoformat() if pos.get('entry_at') else None,
                      'data_ends_at':events[-1]['dt'].isoformat(),'mark_at':last['dt'].isoformat(),
                      'mark_bid':mark,'unrealized_gross':(mark-pos['buy_price'])*pos['qty'] if mark and pos.get('qty') else None,
                      'basis':'OPEN_AT_DATA_END_NOT_MARKET_CLOSE','realized_pnl':None}
    return {'trades':trades,'latency':latency,'cash':cash,'loss_used':loss_used,'unresolved':unresolved,
            'declines':dict(declines),'closed_trades':len(trades),
            'net_pnl':sum(t['net_pnl']for t in trades),'actual_FAST_backtest':'UNKNOWN_NO_RAW_TICKS',
            'profitability_verdict':'UNKNOWN_SINGLE_DAY_SNAPSHOT_PROXY'}


def csv_write(path, rows):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',encoding='utf-8',newline='')as f:
        w = csv.DictWriter(f,fieldnames=keys,lineterminator='\n');w.writeheader();w.writerows(rows)


def run(archive, out):
    out = Path(out);out.mkdir(parents=True,exist_ok=True)
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError('RAW_ARCHIVE_HASH_MISMATCH')
    events, stats = control.load_entry_evals(archive)
    rows, baseline = [], None
    for loss, capital in ((0.,81300.),(1256.,72448.7759)):
        for delay in (0,1,2):
            for ticks in range(4):
                p = Execution(capital=capital,prior_daily_loss=loss,adverse_ticks=ticks,delay_snapshots=delay)
                result = simulate(events,p)
                rows.append({'capital':capital,'starting_used_loss':loss,'adverse_ticks':ticks,'delay_snapshots':delay,
                             'closed_virtual_trades':result['closed_trades'],'unresolved':bool(result['unresolved']),
                             'net_pnl':result['net_pnl'],'cumulative_loss_used':result['loss_used'],
                             'verdict':'UNKNOWN_SINGLE_DAY_PROXY','actual_fills':'UNKNOWN'})
                if loss == 0 and delay == 0 and ticks == 0:
                    baseline = result
    csv_write(out/'SLIPPAGE_SENSITIVITY.csv',rows)
    csv_write(out/'TRADE_LEDGER.csv',baseline['trades']+([baseline['unresolved']] if baseline['unresolved'] else []))
    csv_write(out/'ENTRY_LATENCY.csv',baseline['latency'])
    report = {'archive':Path(archive).name,'sha256':digest,'stats':dict(stats),'execution_costs':Execution().__dict__,
              'first_event':events[0]['dt'].isoformat(),'last_event':events[-1]['dt'].isoformat(),
              'baseline':baseline,'sensitivity':rows,'independent_evaluation_days':0,
              'FAST_actual_strategy_replay':'UNKNOWN_NO_RAW_TAPE_NOT_SNAPSHOT_PROXY'}
    (out/'REPLAY_RESULTS.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('archive');parser.add_argument('--out',required=True)
    args=parser.parse_args();result=run(args.archive,args.out)
    print(json.dumps({'sha256':result['sha256'],'baseline_net':result['baseline']['net_pnl'],
                      'closed':result['baseline']['closed_trades'],'unresolved':result['baseline']['unresolved'],
                      'sensitivity':result['sensitivity']},ensure_ascii=False,indent=2))
