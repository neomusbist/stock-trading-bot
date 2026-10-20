"""Strict Account-Level Portfolio Replay for LocalBreakoutStrategy.

Implements realistic R6 portfolio execution rules:
1. Unified chronological event loop across all 129 symbols minute-by-minute.
2. Initial Cash: 71,822 KRW, Equity: 81,300 KRW.
3. Max Concurrent Positions: 1 (R6 single-slot limit) and 2 (comparison).
4. Daily Risk Halt: 4% of equity (3,252 KRW). When cumulative realized losses
   reach 3,252 KRW, all further entries for the day are hard-blocked.
5. Capital constraint: Cash must cover purchase + entry fee + slippage.
6. Position risk constraint: Single trade risk <= 2% (1,626 KRW).
7. Full friction accounting: buy 0.015%, sell 0.015%, tax 0.20%, entry slip 0.20%, exit slip 0.30% (total 0.73%).
"""
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import json
from datetime import datetime
from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.capital import Costs
from core.strategy import Tick
from automation.local_breakout import LocalBreakoutStrategy

def run_account_replay(cache_file_path, max_positions=1, initial_cash=71822.0, account_equity=81300.0):
    cfg = Config()
    auto = AutomationConfig()
    costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
                  cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
    strategy = LocalBreakoutStrategy(cfg, auto, costs)

    with open(cache_file_path, 'r', encoding='utf-8') as f:
        cache = json.load(f)

    code_to_name_file = REPO_ROOT / 'audit_data' / 'code_to_name.json'
    try:
        with open(code_to_name_file, 'r', encoding='utf-8') as f:
            code_to_name = json.load(f)
    except Exception:
        code_to_name = {}

    rows = cache.get('rows', {})
    
    # Organize bars by symbol and build unified sorted timestamps
    symbol_bars = {}
    symbol_bar_map = {}
    all_timestamps = set()

    for sym, row in rows.items():
        bars = row.get('bars', [])
        if len(bars) >= 15:
            symbol_bars[sym] = bars
            b_map = {}
            for idx, b in enumerate(bars):
                t_str = b['time']
                b_map[t_str] = (idx, b)
                all_timestamps.add(t_str)
            symbol_bar_map[sym] = b_map

    sorted_timestamps = sorted(all_timestamps)
    print(f"Loaded {len(symbol_bars)} active symbols across {len(sorted_timestamps)} distinct minutes.")

    # Account State
    cash = initial_cash
    equity = account_equity
    daily_risk_limit = account_equity * 0.04  # 3,252 KRW
    cumulative_loss = 0.0
    daily_risk_halt = False
    
    open_positions = {}  # sym -> pos_dict
    executed_trades = []
    blocked_entries = defaultdict(int)

    for current_time in sorted_timestamps:
        # Step 1: Manage open positions against current minute bar
        closed_syms = []
        for sym, pos in list(open_positions.items()):
            b_info = symbol_bar_map[sym].get(current_time)
            if not b_info:
                continue
            cur_idx, cur_bar = b_info
            pos['elapsed_bars'] += 1

            exit_price = None
            exit_reason = None

            # Check stop loss hit
            if cur_bar['low'] <= pos['stop']:
                exit_price = pos['stop']
                exit_reason = 'STOP_LOSS'
            # Check target hit
            elif cur_bar['high'] >= pos['target']:
                exit_price = pos['target']
                exit_reason = 'TARGET_HIT'
            # Check horizon timeout (15 bars)
            elif pos['elapsed_bars'] >= 15:
                exit_price = cur_bar['close']
                exit_reason = 'HORIZON_TIMEOUT'

            if exit_price is not None:
                qty = pos['qty']
                gross_pnl = (exit_price - pos['entry']) * qty
                fee_exit = costs.sell_fee(exit_price * qty) + (exit_price * costs.exit_slippage * qty)
                net_pnl = gross_pnl - pos['fee_entry'] - fee_exit
                
                # Return cash: principal + gross_pnl - exit_fee
                cash += (exit_price * qty) - fee_exit
                
                if net_pnl < 0:
                    loss_amount = abs(net_pnl)
                    cumulative_loss += loss_amount
                    if cumulative_loss >= daily_risk_limit:
                        daily_risk_halt = True

                executed_trades.append({
                    'symbol': sym,
                    'name': code_to_name.get(sym, sym),
                    'entry_time': pos['entry_time'],
                    'entry_price': pos['entry'],
                    'stop_price': pos['stop'],
                    'target_price': pos['target'],
                    'qty': qty,
                    'capital_committed': pos['entry'] * qty,
                    'exit_time': current_time,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'gross_pnl': gross_pnl,
                    'total_fees': pos['fee_entry'] + fee_exit,
                    'net_pnl': net_pnl,
                    'pnl_pct': (net_pnl / (pos['entry'] * qty)) * 100,
                    'is_win': net_pnl > 0,
                    'remaining_cash_after': cash,
                    'cumulative_loss_after': cumulative_loss
                })
                closed_syms.append(sym)

        for sym in closed_syms:
            del open_positions[sym]

        # Step 2: Evaluate new entry candidates at current_time
        # Cutoff check: no new entries after 15:05:00
        cur_hhmmss = current_time[11:19] if len(current_time) >= 19 else ""
        if cur_hhmmss >= "15:05:00":
            continue

        if daily_risk_halt:
            # All further entries are hard blocked
            blocked_entries['DAILY_RISK_HALT'] += 1
            continue

        if len(open_positions) >= max_positions:
            blocked_entries['POSITION_SLOT_FULL'] += 1
            continue
            continue

        # Look for signals across symbols that have a bar at current_time
        for sym, bars in symbol_bars.items():
            if sym in open_positions:
                continue
            if len(open_positions) >= max_positions:
                break

            b_info = symbol_bar_map[sym].get(current_time)
            if not b_info:
                continue
            cur_idx, cur_bar = b_info
            if cur_idx < 10:
                continue

            ref_bars = bars[:cur_idx]

            sim_tick = Tick(
                symbol=sym,
                time=datetime.fromisoformat(current_time) if isinstance(current_time, str) else current_time,
                price=float(cur_bar['close']),
                volume=int(cur_bar.get('volume', 100)),
                cumulative=100000,
                ask=float(cur_bar['close']),
                bid=float(cur_bar['close']) - 5.0 if cur_bar['close'] < 5000 else float(cur_bar['close']) - 10.0,
                regular=True,
                ask_qty=1000,
                bid_qty=1000,
                depth_available=True
            )

            remaining_risk = max(0.0, daily_risk_limit - cumulative_loss)
            trade_risk_budget = min(1626.0, remaining_risk)

            sig, det = strategy.evaluate(sim_tick, ref_bars, {'code': sym, 'name': code_to_name.get(sym, sym)},
                                         capital_limit=cash, risk_budget=trade_risk_budget)
            if sig:
                required_cash = sig.limit * sig.quantity
                entry_fee = costs.buy_fee(required_cash) + (sig.limit * costs.entry_slippage * sig.quantity)
                total_entry_cost = required_cash + entry_fee

                if cash < total_entry_cost:
                    blocked_entries['INSUFFICIENT_CASH'] += 1
                    continue

                cash -= total_entry_cost
                open_positions[sym] = {
                    'symbol': sym,
                    'entry_time': current_time,
                    'entry': sig.limit,
                    'stop': sig.stop,
                    'target': sig.target,
                    'qty': sig.quantity,
                    'fee_entry': entry_fee,
                    'elapsed_bars': 0
                }

    # Force flatten any remaining open positions at session end (EOD)
    for sym, pos in list(open_positions.items()):
        bars = symbol_bars.get(sym, [])
        last_bar = bars[-1] if bars else None
        exit_price = last_bar['close'] if last_bar else pos['entry']
        exit_time = last_bar['time'] if last_bar else sorted_timestamps[-1]
        qty = pos['qty']
        gross_pnl = (exit_price - pos['entry']) * qty
        fee_exit = costs.sell_fee(exit_price * qty) + (exit_price * costs.exit_slippage * qty)
        net_pnl = gross_pnl - pos['fee_entry'] - fee_exit
        cash += (exit_price * qty) - fee_exit

        if net_pnl < 0:
            cumulative_loss += abs(net_pnl)

        executed_trades.append({
            'symbol': sym,
            'name': code_to_name.get(sym, sym),
            'entry_time': pos['entry_time'],
            'entry_price': pos['entry'],
            'stop_price': pos['stop'],
            'target_price': pos['target'],
            'qty': qty,
            'capital_committed': pos['entry'] * qty,
            'exit_time': exit_time,
            'exit_price': exit_price,
            'exit_reason': 'EOD_FORCE_FLATTEN',
            'gross_pnl': gross_pnl,
            'total_fees': pos['fee_entry'] + fee_exit,
            'net_pnl': net_pnl,
            'pnl_pct': (net_pnl / (pos['entry'] * qty)) * 100,
            'is_win': net_pnl > 0,
            'remaining_cash_after': cash,
            'cumulative_loss_after': cumulative_loss
        })
    open_positions.clear()

    # Summary
    print(f"\n=======================================================")
    print(f"=== ACCOUNT-LEVEL REPLAY (Max Positions = {max_positions}) ===")
    print(f"=======================================================")
    print(f"Starting Cash: {initial_cash:,.1f} KRW, Final Cash: {cash:,.1f} KRW")
    print(f"Daily Risk Limit (4%): {daily_risk_limit:,.1f} KRW, Realized Cumulative Loss: {cumulative_loss:,.1f} KRW")
    print(f"Daily Risk Halt Triggered: {daily_risk_halt}")
    print(f"Blocked Entry Attempts: {dict(blocked_entries)}")
    print(f"Total Completed Account Trades: {len(executed_trades)}")

    wins = [t for t in executed_trades if t['is_win']]
    losses = [t for t in executed_trades if not t['is_win']]
    win_rate = (len(wins) / len(executed_trades)) * 100 if executed_trades else 0
    total_net = sum(t['net_pnl'] for t in executed_trades)
    print(f"Wins: {len(wins)}, Losses: {len(losses)}, Win Rate: {win_rate:.1f}%")
    print(f"Account Net PnL: {total_net:+.1f} KRW")

    print("\n--- Account Trade Ledger ---")
    for idx, t in enumerate(executed_trades, 1):
        res_str = "WIN" if t['is_win'] else "LOSS"
        print(f"{idx:02d}. [{t['entry_time']} -> {t['exit_time']}] {t['symbol']} ({t['name']}): Entry={t['entry_price']:.0f}, Exit={t['exit_price']:.0f} ({t['exit_reason']}), Qty={t['qty']}, NetPnL={t['net_pnl']:+.1f} KRW, CashAfter={t['remaining_cash_after']:.0f} [{res_str}]")

    return executed_trades

if __name__ == '__main__':
    cache_path = REPO_ROOT / 'audit_data' / 'price_context_cache_20261020.json'
    print("--- 1. Single Slot Limit (Max Position = 1) ---")
    run_account_replay(cache_path, max_positions=1)

    print("\n--- 2. Dual Slot Limit (Max Positions = 2) ---")
    run_account_replay(cache_path, max_positions=2)
