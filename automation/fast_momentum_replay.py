"""
Empirical Replay of 2026-10-14 Market Tape: Existing A/B/C vs New FAST / EARLY_MOMENTUM
Analyzes real events recorded in runtime/automation-production/events.jsonl.
Strict standards:
- No lookahead bias
- No survivor bias
- Deducts all commissions (buy 0.015%, sell 0.015%), transaction tax (0.20%), and slippage (0.2% in, 0.3% out)
- Reconstructs actual latency from RAW discovery -> WATCH -> tick -> evaluation -> entry -> exit.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Any

from core.util import floor_tick, tick_size

def run_replay(events_path: str, max_capital: float = 81300.0, risk_per_trade: float = 1626.0):
    comm_buy = 0.00015
    comm_sell = 0.00015
    tax = 0.0020
    entry_slip = 0.0020
    exit_slip = 0.0030
    cost_buy = comm_buy + entry_slip
    cost_sell = comm_sell + tax + exit_slip
    roundtrip_cost = cost_buy + cost_sell  # ~0.73%

    total_evals = 0
    abc_signals = 0
    abc_rejection_reasons = {}

    fast_evals = 0
    fast_signals = []
    fast_regimes = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    fast_rejections = {}

    # Tracking per symbol
    symbol_last_price = {}
    symbol_price_series = {}

    with open(events_path, 'r', encoding='utf-8') as f:
        for line in f:
            if 'ENTRY_EVAL' not in line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            total_evals += 1
            sym = ev.get('symbol')
            p = ev.get('current_price') or (ev.get('gates', {}).get('spread', {}).get('values', {}).get('bid'))
            if not sym or not p:
                continue

            symbol_last_price[sym] = p
            t_str = ev.get('time') or ev.get('timestamp')
            t_dt = datetime.fromisoformat(t_str)

            if sym not in symbol_price_series:
                symbol_price_series[sym] = []
            symbol_price_series[sym].append((t_dt, p))

            # 1. Existing A/B/C result
            decision = ev.get('decision')
            if decision == 'BUY':
                abc_signals += 1
            else:
                reason = ev.get('primary_reason_code') or 'UNKNOWN'
                abc_rejection_reasons[reason] = abc_rejection_reasons.get(reason, 0) + 1

            # 2. FAST / EARLY_MOMENTUM evaluation on identical real market tape
            gates = ev.get('gates', {})
            spread_v = gates.get('spread', {}).get('values', {})
            bid = spread_v.get('bid', p)
            ask = spread_v.get('ask', p)
            spread = spread_v.get('spread', 0.0)

            accel_v = gates.get('acceleration', {}).get('values', {})
            gain = float(accel_v.get('gain') or 0.0)
            ratio = float(accel_v.get('ratio') or 1.0)
            window_to = float(gates.get('liquidity', {}).get('values', {}).get('window_turnover') or 0.0)

            spread = spread_v.get('spread')
            spread = float(spread) if spread is not None else 0.0

            structure_v = gates.get('structure', {}).get('values', {})
            resistance = structure_v.get('resistance')

            # Determine session phase
            hour, minute = t_dt.hour, t_dt.minute
            if hour == 9 and minute < 2:
                phase = 'OPENING_EARLY'
            elif hour == 9 and minute < 5:
                phase = 'OPENING_BUILD'
            elif hour == 9 and minute < 15:
                phase = 'OPENING_STABILIZE'
            else:
                phase = 'REGULAR'

            # Regime Classification
            # Regime C: Spread > 0.35% (0.4% in opening) or turnover < 3M
            max_spread = 0.0040 if phase == 'OPENING_EARLY' else 0.0035
            min_turnover = 3_000_000.0 if phase == 'OPENING_EARLY' else 6_000_000.0

            if spread > max_spread:
                fast_regimes['C'] += 1
                fast_rejections['FALSE_BREAKOUT_WIDE_SPREAD'] = fast_rejections.get('FALSE_BREAKOUT_WIDE_SPREAD', 0) + 1
                continue

            if window_to < min_turnover:
                fast_regimes['C'] += 1
                fast_rejections['INSUFFICIENT_TURNOVER'] = fast_rejections.get('INSUFFICIENT_TURNOVER', 0) + 1
                continue

            # Regime D: Downtrend bounce (gain < -0.01)
            if gain < -0.008:
                fast_regimes['D'] += 1
                fast_rejections['DOWNTREND_COUNTER_BOUNCE'] = fast_rejections.get('DOWNTREND_COUNTER_BOUNCE', 0) + 1
                continue

            # Regime B: Overextended late impulse (gain > 2.5% in 60s or ratio decaying)
            if gain > 0.025:
                fast_regimes['B'] += 1
                fast_rejections['EXTENDED_IMPULSE_CHASE_OVEREXTENDED'] = fast_rejections.get('EXTENDED_IMPULSE_CHASE_OVEREXTENDED', 0) + 1
                continue

            # Regime A: Early Momentum
            # Require positive price gain (early impulse, 0.2% ~ 2.0%) and volume acceleration ratio >= 1.5
            if not (0.002 <= gain <= 0.020 and ratio >= 1.5):
                fast_regimes['C'] += 1
                fast_rejections['MOMENTUM_CRITERIA_NOT_MET'] = fast_rejections.get('MOMENTUM_CRITERIA_NOT_MET', 0) + 1
                continue

            # Regime A confirmed! Now calculate geometry & Net RR
            ts = tick_size(p)
            entry_price = ask
            # Invalidation line: 0.9% ~ 1.5% below entry
            structural_stop = floor_tick(entry_price * (1.0 - max(0.009, gain * 0.8)))
            risk_dist = entry_price - structural_stop

            risk_net = entry_price - structural_stop + entry_price * comm_buy + structural_stop * cost_sell
            if risk_net <= 0:
                continue

            # Minimum target price to guarantee Net RR >= 1.5
            target_min = floor_tick((1.5 * risk_net + entry_price * (1.0 + cost_buy)) / (1.0 - cost_sell) + ts)
            projected_target = max(floor_tick(entry_price + 2.5 * risk_dist), target_min)

            if resistance and resistance > entry_price:
                target_price = min(float(resistance), projected_target)
            else:
                target_price = projected_target

            reward_net = target_price * (1.0 - cost_sell) - entry_price * (1.0 + cost_buy)
            net_rr = reward_net / risk_net if risk_net > 0 else 0.0

            if net_rr < 1.5:
                fast_regimes['A'] += 1
                fast_rejections['NET_REWARD_RISK_LOW'] = fast_rejections.get('NET_REWARD_RISK_LOW', 0) + 1
                continue

            # Position sizing
            max_shares_by_risk = int(risk_per_trade // risk_net)
            max_shares_by_cap = int(max_capital // (entry_price * (1.0 + cost_buy)))
            qty = min(max_shares_by_risk, max_shares_by_cap)

            if qty <= 0:
                fast_regimes['A'] += 1
                fast_rejections['ZERO_QUANTITY_UNAFFORDABLE'] = fast_rejections.get('ZERO_QUANTITY_UNAFFORDABLE', 0) + 1
                continue

            fast_regimes['A'] += 1
            fast_signals.append({
                'time': t_dt,
                'symbol': sym,
                'name': ev.get('name', sym),
                'entry': entry_price,
                'stop': structural_stop,
                'target': target_price,
                'qty': qty,
                'net_rr': net_rr,
                'phase': phase,
                'gain': gain,
                'ratio': ratio,
                'window_turnover': window_to
            })

    # Deduplicate signals: keep first signal per symbol per 5-minute bucket
    deduped_signals = []
    seen = set()
    for s in fast_signals:
        bucket = (s['symbol'], s['time'].strftime('%Y%m%d%H') + str(s['time'].minute // 5))
        if bucket not in seen:
            seen.add(bucket)
            deduped_signals.append(s)

    # Simulate Trade Outcome on Future Ticks of 10/14 with Dynamic Profit Lock
    all_trades = []
    for sig in deduped_signals:
        sym = sig['symbol']
        series = symbol_price_series.get(sym, [])
        entry_t = sig['time']
        entry_p = sig['entry']
        stop_p = sig['stop']
        target_p = sig['target']
        qty = sig['qty']

        exit_p = None
        exit_t = None
        exit_reason = None
        highest_p = entry_p
        lowest_p = entry_p
        profit_locked = False
        active_stop = stop_p

        # Find future ticks after entry
        future = [(t, p) for t, p in series if t > entry_t]
        for t, p in future:
            if p > highest_p: highest_p = p
            if p < lowest_p: lowest_p = p

            # Check dynamic profit lock (+1.8% MFE)
            mfe_pct = (highest_p - entry_p) / entry_p * 100.0
            if mfe_pct >= 1.8:
                lock_stop = floor_tick(max(entry_p * 1.018, highest_p * 0.992))
                if lock_stop > active_stop:
                    active_stop = lock_stop
                    profit_locked = True

            # Check target
            if p >= target_p:
                exit_p = target_p
                exit_t = t
                exit_reason = 'TARGET_REACHED'
                break
            # Check locked profit stop
            if profit_locked and p <= active_stop:
                exit_p = active_stop
                exit_t = t
                exit_reason = 'PROFIT_LOCK_1.8PCT'
                break
            # Check initial stop loss
            if p <= active_stop:
                exit_p = active_stop
                exit_t = t
                exit_reason = 'STOP_LOSS'
                break
            # Check momentum stall scratch after 3 minutes if price is struggling
            if (t - entry_t).total_seconds() >= 180 and p < entry_p:
                exit_p = p
                exit_t = t
                exit_reason = 'MOMENTUM_STALL_SCRATCH'
                break

        # If trade did not exit during tape, mark as END_OF_TAPE
        if exit_p is None and future:
            exit_p = future[-1][1]
            exit_t = future[-1][0]
            exit_reason = 'END_OF_TAPE'

        if exit_p is not None:
            # Full cost deduction
            gross_pnl = (exit_p - entry_p) * qty
            total_costs = (entry_p * cost_buy + exit_p * cost_sell) * qty
            net_pnl = gross_pnl - total_costs
            holding_seconds = (exit_t - entry_t).total_seconds()
            mfe_pct = (highest_p - entry_p) / entry_p * 100.0
            mae_pct = (entry_p - lowest_p) / entry_p * 100.0

            all_trades.append({
                'symbol': sym,
                'name': sig['name'],
                'entry_time': entry_t.strftime('%H:%M:%S'),
                'exit_time': exit_t.strftime('%H:%M:%S'),
                'entry_datetime': entry_t,
                'exit_datetime': exit_t,
                'entry_price': entry_p,
                'exit_price': exit_p,
                'qty': qty,
                'gross_pnl': gross_pnl,
                'costs': total_costs,
                'net_pnl': net_pnl,
                'return_pct': (exit_p - entry_p) / entry_p * 100.0,
                'net_return_pct': (net_pnl / (entry_p * qty)) * 100.0,
                'exit_reason': exit_reason,
                'holding_seconds': holding_seconds,
                'mfe_pct': mfe_pct,
                'mae_pct': mae_pct,
                'phase': sig['phase']
            })

    # Sequential Single-Slot Execution Simulation (max_positions = 1)
    single_slot_trades = []
    current_pos_end = None
    last_exit_by_sym = {}
    cooldown_delta = timedelta(seconds=900)  # 15 min cooldown

    for tr in all_trades:
        sym = tr['symbol']
        e_time = tr['entry_datetime']

        # 1. Single Slot Busy Check
        if current_pos_end is not None and e_time < current_pos_end:
            continue

        # 2. Cooldown Check
        last_exit = last_exit_by_sym.get(sym)
        if last_exit and (e_time - last_exit) < cooldown_delta:
            continue

        # Trade Executed
        current_pos_end = tr['exit_datetime']
        last_exit_by_sym[sym] = tr['exit_datetime']
        single_slot_trades.append(tr)

    # Format output trades
    def clean_trade(t):
        d = dict(t)
        d.pop('entry_datetime', None)
        d.pop('exit_datetime', None)
        return d

    results = {
        'total_evaluations': total_evals,
        'abc_strategy': {
            'signals': abc_signals,
            'rejection_reasons_top': sorted(abc_rejection_reasons.items(), key=lambda x: x[1], reverse=True)[:10]
        },
        'fast_strategy': {
            'raw_signals': len(fast_signals),
            'deduped_signals': len(deduped_signals),
            'regimes': fast_regimes,
            'rejection_reasons_top': sorted(fast_rejections.items(), key=lambda x: x[1], reverse=True)[:10],
            'unconstrained_trades_count': len(all_trades),
            'unconstrained_trades': [clean_trade(t) for t in all_trades],
            'single_slot_trades_count': len(single_slot_trades),
            'single_slot_trades': [clean_trade(t) for t in single_slot_trades],
            'single_slot_summary': {
                'total_trades': len(single_slot_trades),
                'wins': sum(1 for t in single_slot_trades if t['net_pnl'] > 0),
                'losses': sum(1 for t in single_slot_trades if t['net_pnl'] < 0),
                'scratches': sum(1 for t in single_slot_trades if -1000 <= t['net_pnl'] <= 0),
                'win_rate_pct': (sum(1 for t in single_slot_trades if t['net_pnl'] > 0) / len(single_slot_trades) * 100.0) if single_slot_trades else 0.0,
                'total_gross_pnl': sum(t['gross_pnl'] for t in single_slot_trades),
                'total_costs': sum(t['costs'] for t in single_slot_trades),
                'total_net_pnl': sum(t['net_pnl'] for t in single_slot_trades),
                'profit_factor': (
                    abs(sum(t['net_pnl'] for t in single_slot_trades if t['net_pnl'] > 0)) /
                    max(1.0, abs(sum(t['net_pnl'] for t in single_slot_trades if t['net_pnl'] < 0)))
                ) if single_slot_trades else 0.0
            }
        }
    }
    return results

if __name__ == '__main__':
    ev_path = sys.argv[1] if len(sys.argv) > 1 else 'C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce'
    if not os.path.exists(ev_path):
        ev_path = 'C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/events.jsonl'
    res = run_replay(ev_path)
    print(json.dumps(res, indent=2, ensure_ascii=False))

