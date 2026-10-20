"""Reproducibility script for the 89-signal full universe backtest on 2026-10-20.

Reads audit_data/price_context_cache_20261020.json and evaluates
LocalBreakoutStrategy per symbol independently without portfolio cash/slot limits.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import json
from datetime import datetime
from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.capital import Costs
from core.strategy import Tick
from automation.local_breakout import LocalBreakoutStrategy

cfg = Config()
auto = AutomationConfig()
costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
              cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
strategy = LocalBreakoutStrategy(cfg, auto, costs)

cache_file = REPO_ROOT / 'audit_data' / 'price_context_cache_20261020.json'
with open(cache_file, 'r', encoding='utf-8') as f:
    cache = json.load(f)

code_to_name_file = REPO_ROOT / 'audit_data' / 'code_to_name.json'
try:
    with open(code_to_name_file, 'r', encoding='utf-8') as f:
        code_to_name = json.load(f)
except Exception:
    code_to_name = {}

rows = cache.get('rows', {})
all_trades = []
universe_symbols = sorted(rows.keys())

for sym in universe_symbols:
    row = rows[sym]
    bars = row.get('bars', [])
    if len(bars) < 15:
        continue
    name = code_to_name.get(sym, sym)
    watch = {'code': sym, 'name': name}

    pos_exit_idx = -1

    for i in range(10, len(bars) - 1):
        if i <= pos_exit_idx:
            continue

        ref_bars = bars[:i]
        cur_bar = bars[i]

        sim_tick = Tick(
            symbol=sym,
            time=datetime.fromisoformat(cur_bar['time']) if isinstance(cur_bar['time'], str) else cur_bar['time'],
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

        sig, det = strategy.evaluate(sim_tick, ref_bars, watch, capital_limit=81300., risk_budget=1626.)
        if sig:
            entry_time = cur_bar['time']
            entry_price = sig.limit
            stop_price = sig.stop
            target_price = sig.target
            qty = sig.quantity

            future_bars = bars[i+1:i+16]
            if not future_bars:
                continue

            mfe = max([b['high'] for b in future_bars] + [entry_price])
            mae = min([b['low'] for b in future_bars] + [entry_price])
            mfe_pct = (mfe - entry_price) / entry_price
            mae_pct = (entry_price - mae) / entry_price

            exit_price = None
            exit_reason = None
            exit_time = None
            exit_idx = i + len(future_bars)

            for step_idx, fb in enumerate(future_bars):
                if fb['low'] <= stop_price:
                    exit_price = stop_price
                    exit_reason = 'STOP_LOSS'
                    exit_time = fb['time']
                    exit_idx = i + 1 + step_idx
                    break
                elif fb['high'] >= target_price:
                    exit_price = target_price
                    exit_reason = 'TARGET_HIT'
                    exit_time = fb['time']
                    exit_idx = i + 1 + step_idx
                    break

            if not exit_price:
                exit_price = future_bars[-1]['close']
                exit_reason = 'HORIZON_TIMEOUT'
                exit_time = future_bars[-1]['time']
                exit_idx = i + len(future_bars)

            pos_exit_idx = exit_idx

            gross_pnl = (exit_price - entry_price) * qty
            fee_entry = costs.buy_fee(entry_price * qty) + (entry_price * costs.entry_slippage * qty)
            fee_exit = costs.sell_fee(exit_price * qty) + (exit_price * costs.exit_slippage * qty)
            total_fees = fee_entry + fee_exit
            net_pnl = gross_pnl - total_fees
            pnl_pct = (net_pnl / (entry_price * qty)) * 100

            all_trades.append({
                'symbol': sym,
                'name': name,
                'entry_time': entry_time,
                'entry_price': entry_price,
                'stop_price': stop_price,
                'target_price': target_price,
                'qty': qty,
                'capital_committed': entry_price * qty,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'mfe_pct': mfe_pct * 100,
                'mae_pct': mae_pct * 100,
                'gross_pnl': gross_pnl,
                'total_fees': total_fees,
                'net_pnl': net_pnl,
                'pnl_pct': pnl_pct,
                'is_win': net_pnl > 0
            })

all_trades.sort(key=lambda x: x['entry_time'])
wins = [t for t in all_trades if t['is_win']]
losses = [t for t in all_trades if not t['is_win']]
win_rate = (len(wins) / len(all_trades)) * 100 if all_trades else 0
total_net_pnl = sum(t['net_pnl'] for t in all_trades)
avg_net_pnl = total_net_pnl / len(all_trades) if all_trades else 0
total_win_krw = sum(t['net_pnl'] for t in wins)
total_loss_krw = abs(sum(t['net_pnl'] for t in losses)) if losses else 0
profit_factor = (total_win_krw / total_loss_krw) if total_loss_krw > 0 else 0

max_consec = 0
cur_consec = 0
for t in all_trades:
    if not t['is_win']:
        cur_consec += 1
        max_consec = max(max_consec, cur_consec)
    else:
        cur_consec = 0

print(f"REPRODUCE RESULT: Trades={len(all_trades)}, Wins={len(wins)}, Losses={len(losses)}, WinRate={win_rate:.1f}%, NetPnL={total_net_pnl:.1f}, PF={profit_factor:.2f}, MaxConsecLoss={max_consec}")
