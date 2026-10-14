"""
R6 FAST Momentum Independent Audit & Replay Script
Enforces strict chronological execution, single-slot account constraint,
transaction friction (0.73%), and line-by-line provenance tracing.
"""
import os
import sys
import json
import csv
from datetime import datetime, timedelta
from pathlib import Path

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.util import floor_tick, tick_size

DEFAULT_ARCHIVE = 'C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce'

def load_events_with_provenance(archive_path: str):
    events = []
    line_number = 0
    with open(archive_path, 'r', encoding='utf-8') as f:
        for line in f:
            line_number += 1
            if 'ENTRY_EVAL' not in line:
                continue
            try:
                ev = json.loads(line)
                t_str = ev.get('time') or ev.get('timestamp')
                t_dt = datetime.fromisoformat(t_str)
                sym = ev.get('symbol')
                p = ev.get('current_price') or ev.get('gates', {}).get('spread', {}).get('values', {}).get('bid')
                if not sym or not p:
                    continue
                sp = ev.get('gates', {}).get('spread', {}).get('values', {})
                acc = ev.get('gates', {}).get('acceleration', {}).get('values', {})
                to = float(ev.get('gates', {}).get('liquidity', {}).get('values', {}).get('window_turnover') or 0.0)
                
                events.append({
                    'line_number': line_number,
                    'dt': t_dt,
                    'symbol': sym,
                    'name': ev.get('name', sym),
                    'p': float(p),
                    'bid': float(sp.get('bid') or p),
                    'ask': float(sp.get('ask') or p),
                    'spread': float(sp.get('spread') or 0.0),
                    'gain': float(acc.get('gain') or 0.0),
                    'ratio': float(acc.get('ratio') or 1.0),
                    'to': to,
                })
            except Exception:
                pass
                
    # Deterministic chronological sort: timestamp primary, line_number secondary
    events.sort(key=lambda x: (x['dt'], x['line_number']))
    return events

def run_simulation(events, capital=81300.0, risk_per_trade=1626.0, cooldown_minutes=15, adverse_ticks=0, use_profit_lock=True):
    comm_buy = 0.00015
    comm_sell = 0.00015
    tax = 0.0020
    entry_slip = 0.0020
    exit_slip = 0.0030
    
    current_cash = capital
    active_pos = None
    trades = []
    equity_curve = []
    last_exit_by_sym = {}
    cooldown_delta = timedelta(minutes=cooldown_minutes)
    
    # Record initial equity point
    equity_curve.append({
        'timestamp': events[0]['dt'].strftime('%H:%M:%S'),
        'event_id': f"LINE_{events[0]['line_number']}",
        'cash': current_cash,
        'position_val': 0.0,
        'total_equity': current_cash,
        'action': 'INIT'
    })
    
    for ev in events:
        sym = ev['symbol']
        t_dt = ev['dt']
        p = ev['p']
        bid = ev['bid']
        ask = ev['ask']
        ts = tick_size(p)
        line_id = f"LINE_{ev['line_number']}"
        
        # 1. Active Position Exit Evaluation
        if active_pos is not None:
            if active_pos['sym'] == sym:
                holding_sec = (t_dt - active_pos['entry_dt']).total_seconds()
                entry_p = active_pos['entry_price']
                highest_bid = max(active_pos.get('highest_bid', bid), bid)
                lowest_bid = min(active_pos.get('lowest_bid', bid), bid)
                active_pos['highest_bid'] = highest_bid
                active_pos['lowest_bid'] = lowest_bid
                
                exit_price = None
                exit_reason = None
                
                # Check target
                if bid >= active_pos['target'] or p >= active_pos['target']:
                    exit_price = active_pos['target'] - adverse_ticks * ts
                    exit_reason = 'TARGET_REACHED'
                # Check stop loss
                elif bid <= active_pos['stop'] or p <= active_pos['stop']:
                    exit_price = bid - adverse_ticks * ts
                    exit_reason = 'STOP_LOSS'
                # Check profit lock
                elif use_profit_lock:
                    mfe_pct = (highest_bid - entry_p) / entry_p * 100.0
                    if mfe_pct >= 1.8:
                        active_pos['profit_locked'] = True
                        active_pos['lock_stop'] = floor_tick(max(entry_p * 1.018, highest_bid * 0.992))
                    
                    if active_pos.get('profit_locked'):
                        if bid <= active_pos['lock_stop'] or p <= active_pos['lock_stop']:
                            exit_price = bid - adverse_ticks * ts
                            exit_reason = 'PROFIT_LOCK_CHRONOLOGICAL'
                            
                # Check momentum stall scratch after 180s
                if exit_price is None and holding_sec >= 180 and bid < entry_p:
                    exit_price = bid - adverse_ticks * ts
                    exit_reason = 'MOMENTUM_STALL_SCRATCH'
                    
                if exit_price is not None:
                    qty = active_pos['qty']
                    assumed_sell_price = exit_price * (1.0 - exit_slip)
                    
                    gross_pnl = (assumed_sell_price - active_pos['assumed_buy_price']) * qty
                    buy_comm_val = active_pos['assumed_buy_price'] * qty * comm_buy
                    sell_comm_val = assumed_sell_price * qty * comm_sell
                    sell_tax_val = assumed_sell_price * qty * tax
                    slip_cost = (active_pos['entry_price'] * entry_slip + exit_price * exit_slip) * qty
                    costs_val = buy_comm_val + sell_comm_val + sell_tax_val
                    net_pnl = gross_pnl - costs_val
                    
                    current_cash += (assumed_sell_price * qty) - (sell_comm_val + sell_tax_val)
                    
                    trades.append({
                        'code': sym,
                        'name': active_pos['name'],
                        'buy_time': active_pos['entry_dt'].strftime('%H:%M:%S'),
                        'buy_event_id': active_pos['buy_event_id'],
                        'buy_recorded_price': active_pos['entry_price'],
                        'buy_bid': active_pos['buy_bid'],
                        'buy_ask': active_pos['buy_ask'],
                        'assumed_buy_price': active_pos['assumed_buy_price'],
                        'qty': qty,
                        'buy_logic': 'FAST_EARLY_MOMENTUM_REGIME_A',
                        'stop_price': active_pos['stop'],
                        'target_price': active_pos['target'],
                        'net_rr': active_pos['net_rr'],
                        'sell_time': t_dt.strftime('%H:%M:%S'),
                        'sell_event_id': line_id,
                        'sell_recorded_price': p,
                        'sell_bid': bid,
                        'sell_ask': ask,
                        'assumed_sell_price': assumed_sell_price,
                        'sell_reason': exit_reason,
                        'holding_seconds': holding_sec,
                        'buy_comm': buy_comm_val,
                        'sell_comm': sell_comm_val,
                        'sell_tax': sell_tax_val,
                        'slippage_cost': slip_cost,
                        'gross_pnl': gross_pnl,
                        'net_pnl': net_pnl,
                        'cash_after': current_cash,
                        'position_after': 'NONE'
                    })
                    
                    equity_curve.append({
                        'timestamp': t_dt.strftime('%H:%M:%S'),
                        'event_id': line_id,
                        'cash': current_cash,
                        'position_val': 0.0,
                        'total_equity': current_cash,
                        'action': f"SELL_{sym}_{exit_reason}"
                    })
                    
                    last_exit_by_sym[sym] = t_dt
                    active_pos = None
                    continue

        # 2. Entry Evaluation if no position open
        if active_pos is None:
            last_exit = last_exit_by_sym.get(sym)
            if last_exit and (t_dt - last_exit) < cooldown_delta:
                continue
                
            gain = ev['gain']
            ratio = ev['ratio']
            to = ev['to']
            spread = ev['spread']
            
            if spread > 0.0035 or to < 6_000_000.0 or gain < 0.002 or gain > 0.020 or ratio < 1.5:
                continue
                
            entry_price = ask
            structural_stop = floor_tick(entry_price * (1.0 - max(0.009, gain * 0.8)))
            risk_dist = entry_price - structural_stop
            risk_net = entry_price - structural_stop + entry_price * (comm_buy + entry_slip) + structural_stop * (comm_sell + tax + exit_slip)
            if risk_net <= 0:
                continue
                
            target_min = floor_tick((1.5 * risk_net + entry_price * (1.0 + comm_buy + entry_slip)) / (1.0 - (comm_sell + tax + exit_slip)) + ts)
            target_price = max(floor_tick(entry_price + 2.5 * risk_dist), target_min)
            reward_net = target_price * (1.0 - (comm_sell + tax + exit_slip)) - entry_price * (1.0 + comm_buy + entry_slip)
            net_rr = reward_net / risk_net
            if net_rr < 1.5:
                continue
                
            max_by_risk = int(risk_per_trade // risk_net)
            max_by_cap = int(current_cash // (entry_price * (1.0 + comm_buy + entry_slip)))
            qty = min(max_by_risk, max_by_cap)
            if qty <= 0:
                continue
                
            assumed_buy_price = (entry_price + adverse_ticks * ts) * (1.0 + entry_slip)
            order_cost = assumed_buy_price * qty * (1.0 + comm_buy)
            current_cash -= order_cost
            
            active_pos = {
                'sym': sym,
                'name': ev['name'],
                'entry_dt': t_dt,
                'buy_event_id': line_id,
                'entry_price': entry_price,
                'buy_bid': bid,
                'buy_ask': ask,
                'assumed_buy_price': assumed_buy_price,
                'stop': structural_stop,
                'target': target_price,
                'net_rr': net_rr,
                'qty': qty,
                'lowest_bid': bid,
                'highest_bid': bid,
                'profit_locked': False
            }
            
            equity_curve.append({
                'timestamp': t_dt.strftime('%H:%M:%S'),
                'event_id': line_id,
                'cash': current_cash,
                'position_val': order_cost,
                'total_equity': current_cash + order_cost,
                'action': f"BUY_{sym}"
            })

    # Close any open trade at end of tape
    if active_pos is not None:
        last_ev = [e for e in events if e['symbol'] == active_pos['sym']][-1]
        last_p = last_ev['bid'] - adverse_ticks * tick_size(last_ev['p'])
        assumed_sell_price = last_p * (1.0 - exit_slip)
        qty = active_pos['qty']
        
        gross_pnl = (assumed_sell_price - active_pos['assumed_buy_price']) * qty
        buy_comm_val = active_pos['assumed_buy_price'] * qty * comm_buy
        sell_comm_val = assumed_sell_price * qty * comm_sell
        sell_tax_val = assumed_sell_price * qty * tax
        slip_cost = (active_pos['entry_price'] * entry_slip + last_p * exit_slip) * qty
        costs_val = buy_comm_val + sell_comm_val + sell_tax_val
        net_pnl = gross_pnl - costs_val
        
        current_cash += (assumed_sell_price * qty) - (sell_comm_val + sell_tax_val)
        
        trades.append({
            'code': active_pos['sym'],
            'name': active_pos['name'],
            'buy_time': active_pos['entry_dt'].strftime('%H:%M:%S'),
            'buy_event_id': active_pos['buy_event_id'],
            'buy_recorded_price': active_pos['entry_price'],
            'buy_bid': active_pos['buy_bid'],
            'buy_ask': active_pos['buy_ask'],
            'assumed_buy_price': active_pos['assumed_buy_price'],
            'qty': qty,
            'buy_logic': 'FAST_EARLY_MOMENTUM_REGIME_A',
            'stop_price': active_pos['stop'],
            'target_price': active_pos['target'],
            'net_rr': active_pos['net_rr'],
            'sell_time': last_ev['dt'].strftime('%H:%M:%S'),
            'sell_event_id': f"LINE_{last_ev['line_number']}",
            'sell_recorded_price': last_ev['p'],
            'sell_bid': last_ev['bid'],
            'sell_ask': last_ev['ask'],
            'assumed_sell_price': assumed_sell_price,
            'sell_reason': 'END_OF_TAPE',
            'holding_seconds': (last_ev['dt'] - active_pos['entry_dt']).total_seconds(),
            'buy_comm': buy_comm_val,
            'sell_comm': sell_comm_val,
            'sell_tax': sell_tax_val,
            'slippage_cost': slip_cost,
            'gross_pnl': gross_pnl,
            'net_pnl': net_pnl,
            'cash_after': current_cash,
            'position_after': 'NONE'
        })
        
        equity_curve.append({
            'timestamp': last_ev['dt'].strftime('%H:%M:%S'),
            'event_id': f"LINE_{last_ev['line_number']}",
            'cash': current_cash,
            'position_val': 0.0,
            'total_equity': current_cash,
            'action': f"SELL_{active_pos['sym']}_END_OF_TAPE"
        })

    return trades, equity_curve

def write_csv(data, filepath, fieldnames):
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow(row)

def main():
    archive_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    out_dir = Path(__file__).resolve().parent
    
    print(f"Loading archive: {archive_path}")
    events = load_events_with_provenance(archive_path)
    print(f"Loaded {len(events)} ENTRY_EVAL events.")
    
    # 1. Baseline Simulation (0 adverse ticks, 15m cooldown, profit lock enabled)
    baseline_trades, equity = run_simulation(events, adverse_ticks=0, cooldown_minutes=15, use_profit_lock=True)
    
    # Write TRADE_LEDGER.csv
    ledger_fields = [
        'code', 'name', 'buy_time', 'buy_event_id', 'buy_recorded_price', 'buy_bid', 'buy_ask',
        'assumed_buy_price', 'qty', 'buy_logic', 'stop_price', 'target_price', 'net_rr',
        'sell_time', 'sell_event_id', 'sell_recorded_price', 'sell_bid', 'sell_ask',
        'assumed_sell_price', 'sell_reason', 'holding_seconds', 'buy_comm', 'sell_comm',
        'sell_tax', 'slippage_cost', 'gross_pnl', 'net_pnl', 'cash_after', 'position_after'
    ]
    write_csv(baseline_trades, out_dir / 'TRADE_LEDGER.csv', ledger_fields)
    print(f"Saved TRADE_LEDGER.csv ({len(baseline_trades)} trades)")
    
    # Write EQUITY_CURVE.csv
    equity_fields = ['timestamp', 'event_id', 'cash', 'position_val', 'total_equity', 'action']
    write_csv(equity, out_dir / 'EQUITY_CURVE.csv', equity_fields)
    print(f"Saved EQUITY_CURVE.csv ({len(equity)} points)")
    
    # 2. Slippage Sensitivity Analysis (0, 1, 2, 3 ticks)
    sensitivity_rows = []
    for adv in [0, 1, 2, 3]:
        t_list, _ = run_simulation(events, adverse_ticks=adv, cooldown_minutes=15, use_profit_lock=True)
        wins = sum(1 for t in t_list if t['net_pnl'] > 0)
        losses = sum(1 for t in t_list if t['net_pnl'] <= 0)
        tot_net = sum(t['net_pnl'] for t in t_list)
        tot_gross = sum(t['gross_pnl'] for t in t_list)
        tot_costs = sum(t['buy_comm'] + t['sell_comm'] + t['sell_tax'] + t['slippage_cost'] for t in t_list)
        pos_pnl = sum(t['net_pnl'] for t in t_list if t['net_pnl'] > 0)
        neg_pnl = abs(sum(t['net_pnl'] for t in t_list if t['net_pnl'] < 0))
        pf = (pos_pnl / max(1.0, neg_pnl)) if neg_pnl > 0 else 99.0
        
        # Max drawdown calculation
        peak = 81300.0
        mdd = 0.0
        running = 81300.0
        for t in t_list:
            running += t['net_pnl']
            if running > peak: peak = running
            dd = peak - running
            if dd > mdd: mdd = dd
            
        sensitivity_rows.append({
            'adverse_ticks': adv,
            'total_trades': len(t_list),
            'wins': wins,
            'losses': losses,
            'win_rate_pct': f"{(wins / len(t_list) * 100):.1f}%" if t_list else "0.0%",
            'total_gross_pnl': f"{tot_gross:+,.1f}",
            'total_costs': f"{tot_costs:,.1f}",
            'total_net_pnl': f"{tot_net:+,.1f}",
            'profit_factor': f"{pf:.2f}",
            'max_drawdown_krw': f"{mdd:,.1f}",
            'verdict': 'PROFIT' if tot_net > 0 else 'LOSS'
        })
        
    sens_fields = ['adverse_ticks', 'total_trades', 'wins', 'losses', 'win_rate_pct', 'total_gross_pnl', 'total_costs', 'total_net_pnl', 'profit_factor', 'max_drawdown_krw', 'verdict']
    write_csv(sensitivity_rows, out_dir / 'SLIPPAGE_SENSITIVITY.csv', sens_fields)
    print("Saved SLIPPAGE_SENSITIVITY.csv")
    
    # Print console summary
    tot_net = sum(t['net_pnl'] for t in baseline_trades)
    print(f"\n=== BASELINE EXECUTION SUMMARY ===")
    print(f"Total Trades: {len(baseline_trades)}")
    print(f"Total Net PnL: {tot_net:+,.1f} KRW")
    for t in baseline_trades:
        print(f"  {t['name']} ({t['code']}) {t['buy_time']}~{t['sell_time']}: Buy {t['buy_ask']:,.0f}, Sell {t['sell_bid']:,.0f}, Net {t['net_pnl']:+8.1f} ({t['sell_reason']}) [Buy:{t['buy_event_id']}, Sell:{t['sell_event_id']}]")

if __name__ == '__main__':
    main()
