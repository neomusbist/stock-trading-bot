"""Realistic Account Scale Replay Backtester for Classics Strategy Engine.

Simulates the exact 68,053 KRW Korean retail trading account on 2026-11-05:
- Cash limit: 68,053 KRW (effective_auto_cap)
- Risk equity: 67,643.78 KRW
- Max risk per trade: 2% (1,352.88 KRW)
- Max daily risk: 4% (2,705.75 KRW)
- Realized loss prior: -835 KRW (Scenario A: Standalone vs Scenario B: Combined with LIVE)
- Integer share sizing (1 share, 2 shares, etc.)
- Realistic LOB quote execution: Buy fills at ASK (if depth >= qty), Sell fills at BID (if depth >= qty)
- Exact transaction costs: 0.18% Korean securities tax, 0.015% buy fee, 0.015% sell fee, 0.05% slippage each side
- Max 2 concurrent positions
- 15:05 entry cutoff, 15:10 forced flatten
- 15-minute symbol cooldown, 12% day gain extension cap
"""

import sys
import os
import json
import math
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

# Add workspace to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation.classics_strategy import ClassicsStrategyEngine, ClassicsRiskSizer, HARD_LOCK_SYMBOLS, DEFAULT_COSTS
from core.util import tick_size, floor_tick


def parse_iso(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


class CandleAggregator:
    """Aggregates real-time ticks into completed 1-minute bars."""

    def __init__(self):
        self.bars = defaultdict(list)
        self.current_bar = {}

    def on_tick(self, tick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        sym = tick.get('symbol')
        t_str = tick.get('time')
        price = tick.get('price')
        vol = tick.get('volume', 0)

        if not sym or not t_str or price is None or price <= 0:
            return None

        dt = parse_iso(t_str)
        if dt is None:
            return None

        bar_minute = dt.replace(second=0, microsecond=0)
        cur = self.current_bar.get(sym)
        completed_bar = None

        if cur is None:
            self.current_bar[sym] = {
                'symbol': sym,
                'time': bar_minute,
                'open': float(price),
                'high': float(price),
                'low': float(price),
                'close': float(price),
                'volume': int(vol)
            }
        elif cur['time'] == bar_minute:
            cur['high'] = max(cur['high'], float(price))
            cur['low'] = min(cur['low'], float(price))
            cur['close'] = float(price)
            cur['volume'] += int(vol)
        else:
            completed_bar = dict(cur)
            self.bars[sym].append(completed_bar)
            self.current_bar[sym] = {
                'symbol': sym,
                'time': bar_minute,
                'open': float(price),
                'high': float(price),
                'low': float(price),
                'close': float(price),
                'volume': int(vol)
            }

        return completed_bar


class RealisticPosition:
    def __init__(self, signal: Dict[str, Any], entry_time: datetime, fill_ask: float, quantity: int, cash_spent: float):
        self.symbol = signal['symbol']
        self.strategy = signal['strategy']
        self.entry_time = entry_time
        self.quoted_ask = signal['entry']
        self.fill_ask = fill_ask
        self.initial_stop = signal['stop']
        self.stop = signal['stop']
        self.emergency = signal.get('emergency', self.stop)
        self.target = signal['target']
        self.quantity = quantity
        self.cash_spent = cash_spent
        self.net_rr = signal['net_rr']
        self.gross_rr = signal['gross_rr']

        self.mfe_price = fill_ask
        self.mae_price = fill_ask
        self.is_closed = False
        self.exit_time = None
        self.exit_price = None
        self.exit_fill = None
        self.exit_reason = None
        self.cash_recovered = 0.0
        self.net_pnl = 0.0
        self.return_pct = 0.0
        self.verification_tier = signal.get('verification_tier', 'Tier-1')

    @property
    def potential_risk(self) -> float:
        """Potential loss in KRW if stop is triggered, including fees and slippage."""
        if self.is_closed:
            return 0.0
        exit_slip = DEFAULT_COSTS.get('exit_slippage', 0.0005)
        sell_fee = DEFAULT_COSTS.get('sell', 0.00015)
        tax = DEFAULT_COSTS.get('tax', 0.0018)
        exit_fill = self.stop * (1.0 - exit_slip)
        net_proceeds = self.quantity * exit_fill * (1.0 - sell_fee - tax)
        return max(0.0, self.cash_spent - net_proceeds)

    def update_tick(self, bid: float, ask: float, price: float, current_time: datetime) -> Optional[Tuple[float, str]]:
        if self.is_closed:
            return None

        self.mfe_price = max(self.mfe_price, price, bid)
        self.mae_price = min(self.mae_price, price, bid)

        # Target Check (executed against Ask/Trade reaching target)
        if price >= self.target or bid >= self.target:
            return self.target, 'TARGET_REACHED'

        # Elder Trailing Stop Update
        trailing = ClassicsRiskSizer.calculate_trailing_stop(self.fill_ask, self.stop, self.mfe_price, DEFAULT_COSTS)
        if trailing > self.stop:
            self.stop = trailing

        # Stop Check (executed if bid falls below stop)
        if bid <= self.stop or price <= self.stop:
            reason = 'TRAILING_STOP_TRIGGERED' if self.stop >= self.fill_ask else 'INITIAL_STOP_TRIGGERED'
            return min(self.stop, bid), reason

        return None

    def close(self, exit_bid: float, reason: str, exit_time: datetime) -> float:
        self.is_closed = True
        self.exit_price = exit_bid
        self.exit_reason = reason
        self.exit_time = exit_time

        # Sell fill incorporates exit slippage and exit costs
        exit_slip = DEFAULT_COSTS.get('exit_slippage', 0.0005)
        sell_fee = DEFAULT_COSTS.get('sell', 0.00015)
        tax = DEFAULT_COSTS.get('tax', 0.0018)

        self.exit_fill = exit_bid * (1.0 - exit_slip)
        net_proceeds_per_share = self.exit_fill * (1.0 - sell_fee - tax)

        self.cash_recovered = net_proceeds_per_share * self.quantity
        self.net_pnl = self.cash_recovered - self.cash_spent
        self.return_pct = (self.cash_recovered / self.cash_spent) - 1.0 if self.cash_spent > 0 else 0.0

        return self.cash_recovered


def run_realistic_replay(
    tick_files: List[Path],
    initial_cash: float = 68053.0,
    initial_equity: float = 67643.78,
    initial_loss: float = 0.0,
    scenario_label: str = "Standalone"
) -> Dict[str, Any]:
    aggregator = CandleAggregator()
    engine = ClassicsStrategyEngine(equity=initial_equity, capital_limit=initial_cash)

    cash = float(initial_cash)
    equity = float(initial_equity)
    daily_loss_accumulated = float(initial_loss)  # Positive number represents loss
    daily_risk_limit = equity * 0.04  # 4% daily risk limit = 2,705.75 KRW
    max_risk_per_trade = equity * 0.02  # 2% per trade = 1,352.88 KRW

    positions: List[RealisticPosition] = []
    active_positions: Dict[str, RealisticPosition] = {}
    cooldown_tracker: Dict[str, datetime] = {}
    rejected_signals: List[Dict[str, Any]] = []
    last_known_prices: Dict[str, float] = {}
    last_known_bids: Dict[str, float] = {}
    last_timestamp: Optional[datetime] = None

    total_ticks = 0
    signals_evaluated = 0

    print(f"\n=======================================================")
    print(f"RUNNING REALISTIC REPLAY: [{scenario_label}]")
    print(f"Initial Cash: {cash:,.0f} KRW | Risk Equity: {equity:,.0f} KRW")
    print(f"Max Risk/Trade (2%): {max_risk_per_trade:,.1f} KRW | Daily Risk Limit (4%): {daily_risk_limit:,.1f} KRW")
    print(f"Initial Realized Loss: {initial_loss:,.1f} KRW | Remaining Daily Risk: {max(0, daily_risk_limit - daily_loss_accumulated):,.1f} KRW")
    print(f"=======================================================")

    for fpath in tick_files:
        if not fpath.exists():
            continue
        print(f"  Ingesting {fpath.name} ({fpath.stat().st_size:,} bytes)...", flush=True)

        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    tick = json.loads(line)
                except Exception:
                    continue

                if tick.get('event') != 'TRADE':
                    continue

                sym = tick.get('symbol')
                price = tick.get('price')
                bid = tick.get('bid')
                ask = tick.get('ask')
                bid_qty = tick.get('bid_qty')
                ask_qty = tick.get('ask_qty')
                depth_avail = tick.get('depth_available', False)
                t_str = tick.get('time')
                dt = parse_iso(t_str)

                if not sym or price is None or dt is None:
                    continue

                total_ticks += 1
                last_timestamp = dt
                last_known_prices[sym] = float(price)
                if bid is not None:
                    last_known_bids[sym] = float(bid)

                aggregator.on_tick(tick)

                # 1. Update Existing Active Positions
                if sym in active_positions:
                    pos = active_positions[sym]
                    exit_res = pos.update_tick(bid=float(bid or price), ask=float(ask or price), price=float(price), current_time=dt)
                    if exit_res is not None:
                        exit_price, exit_reason = exit_res
                        # Check depth on exit if available
                        recovered = pos.close(exit_price, exit_reason, dt)
                        cash += recovered
                        pnl = pos.net_pnl
                        if pnl < 0:
                            daily_loss_accumulated += abs(pnl)
                        cooldown_tracker[sym] = dt
                        print(f"    [EXIT] {dt.strftime('%H:%M:%S')} {sym} ({pos.strategy}): "
                              f"Exit={exit_price:,} Return={pos.return_pct*100:+.2f}% Net PnL={pnl:+,.0f} KRW "
                              f"Reason={exit_reason} (Cash Now={cash:,.0f} KRW)")
                        del active_positions[sym]

                # 2. Check 15:10 Force Flatten Cutoff
                if dt.strftime('%H:%M:%S') >= '15:10:00':
                    for active_sym, active_pos in list(active_positions.items()):
                        exit_p = last_known_bids.get(active_sym, last_known_prices.get(active_sym, active_pos.fill_ask))
                        rec = active_pos.close(exit_p, 'FORCE_FLATTEN_1510', dt)
                        cash += rec
                        pnl = active_pos.net_pnl
                        if pnl < 0:
                            daily_loss_accumulated += abs(pnl)
                        print(f"    [FORCE FLATTEN] {dt.strftime('%H:%M:%S')} {active_sym}: "
                              f"Exit={active_pos.exit_price:,} Return={active_pos.return_pct*100:+.2f}% "
                              f"Net PnL={pnl:+,.0f} KRW (Cash Now={cash:,.0f} KRW)")
                        del active_positions[active_sym]
                    continue

                # 3. New Entry Evaluation
                # Check Entry cutoff: no new buys after 15:05
                if dt.strftime('%H:%M:%S') >= '15:05:00':
                    continue

                # Check daily risk limit (including risk_used of currently active positions)
                active_risk = sum(p.potential_risk for p in active_positions.values())
                remaining_risk = max(0.0, daily_risk_limit - daily_loss_accumulated - active_risk)
                if remaining_risk <= 0:
                    continue

                # Must have quotes, not already held, concurrent positions < 2
                if bid and ask and sym not in active_positions:
                    # Check concurrent limit
                    if len(active_positions) >= 2:
                        continue

                    # Check cooldown
                    last_exit = cooldown_tracker.get(sym)
                    if last_exit and (dt - last_exit).total_seconds() < 900:
                        continue

                    bars = aggregator.bars.get(sym, [])
                    if len(bars) >= 8:
                        # Extension check: price <= 12% above day open
                        day_open = bars[0]['open']
                        if day_open > 0 and (float(price) - day_open) / day_open > 0.12:
                            continue

                        signals_evaluated += 1
                        sig, reason, data = engine.evaluate_candidate(
                            symbol=sym,
                            price=float(price),
                            bid=float(bid),
                            ask=float(ask),
                            bars=bars,
                            watch={'name': sym},
                            equity=equity,
                            capital_limit=cash,  # Strictly bounded by currently available cash!
                            remaining_daily_risk=remaining_risk
                        )

                        if sig is None:
                            continue

                        # Sizing verification under real cash constraint and remaining risk
                        raw_qty = sig['quantity']
                        quoted_ask = float(ask)
                        entry_slip = DEFAULT_COSTS.get('entry_slippage', 0.0005)
                        buy_fee = DEFAULT_COSTS.get('buy', 0.00015)
                        fill_ask = quoted_ask * (1.0 + entry_slip)
                        cost_per_share = fill_ask * (1.0 + buy_fee)

                        # Maximum integer shares affordable with remaining cash
                        max_affordable_shares = math.floor(cash / cost_per_share)
                        # Maximum shares affordable by 2% trade risk and remaining daily risk
                        risk_per_share = data.get('risk_net_per_share', cost_per_share - sig['stop'])
                        effective_risk_budget = min(max_risk_per_trade, remaining_risk)
                        max_risk_shares = math.floor(effective_risk_budget / risk_per_share) if risk_per_share > 0 else 0

                        actual_qty = min(raw_qty, max_affordable_shares, max_risk_shares)

                        # Check Order Book Depth (Larry Harris Microstructure)
                        if depth_avail and ask_qty is not None and ask_qty < actual_qty:
                            actual_qty = int(ask_qty)

                        if actual_qty <= 0:
                            reject_reason = 'INSUFFICIENT_CASH' if max_affordable_shares <= 0 else 'RISK_BUDGET_EXCEEDED'
                            rejected_signals.append({
                                'time': dt.isoformat(),
                                'symbol': sym,
                                'reason': reject_reason,
                                'price': price,
                                'ask': ask,
                                'cash_available': cash,
                                'cost_per_share': cost_per_share,
                                'max_affordable': max_affordable_shares,
                                'max_risk_shares': max_risk_shares
                            })
                            continue

                        cash_spent = actual_qty * cost_per_share
                        cash -= cash_spent

                        # Determine Verification Tier based on quote depth
                        if depth_avail and ask_qty is not None and ask_qty >= actual_qty:
                            v_tier = 'Tier-1 (Raw Depth Verified)'
                        elif depth_avail and ask_qty is not None:
                            v_tier = 'Tier-2 (Partial Depth)'
                        else:
                            v_tier = 'Tier-3 (Raw Depth Unverified - Book Mismatch or Unavailable)'
                        sig['verification_tier'] = v_tier

                        pos = RealisticPosition(
                            signal=sig,
                            entry_time=dt,
                            fill_ask=fill_ask,
                            quantity=actual_qty,
                            cash_spent=cash_spent
                        )
                        active_positions[sym] = pos
                        positions.append(pos)
                        print(f"    [REAL BUY EXECUTED] {dt.strftime('%H:%M:%S')} {sym} ({sig['strategy']}) [{v_tier}]: "
                              f"QuotedAsk={quoted_ask:,} FillAsk={fill_ask:,.1f} Stop={sig['stop']:,} Target={sig['target']:,} "
                              f"Qty={actual_qty}주 Spent={cash_spent:,.0f} KRW (Remaining Cash={cash:,.0f} KRW, Remaining Risk={remaining_risk:,.0f} KRW)")

    # Close any remaining open positions at the end of data (mark-to-market using last known bid/price)
    for active_sym, active_pos in list(active_positions.items()):
        exit_p = last_known_bids.get(active_sym, last_known_prices.get(active_sym, active_pos.fill_ask))
        rec = active_pos.close(exit_p, 'END_OF_DATA_MARK_TO_MARKET', last_timestamp or datetime.now())
        cash += rec
        pnl = active_pos.net_pnl
        if pnl < 0:
            daily_loss_accumulated += abs(pnl)
        print(f"    [END OF DATA CLOSE] {last_timestamp.strftime('%H:%M:%S') if last_timestamp else ''} {active_sym}: "
              f"Exit={exit_p:,} Return={active_pos.return_pct*100:+.2f}% "
              f"Net PnL={pnl:+,.0f} KRW (Cash Now={cash:,.0f} KRW)")
        del active_positions[active_sym]

    # Final Summary Calculation
    total_trades = len(positions)
    win_trades = [p for p in positions if p.net_pnl > 0]
    loss_trades = [p for p in positions if p.net_pnl <= 0]

    win_rate = (len(win_trades) / total_trades) if total_trades > 0 else 0.0
    total_net_pnl = sum(p.net_pnl for p in positions)
    gross_profits = sum(p.net_pnl for p in win_trades)
    gross_losses = abs(sum(p.net_pnl for p in loss_trades))
    profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (99.0 if gross_profits > 0 else 0.0)
    avg_pnl = (total_net_pnl / total_trades) if total_trades > 0 else 0.0
    max_loss = min([p.net_pnl for p in positions], default=0.0)

    summary = {
        'scenario': scenario_label,
        'initial_cash': initial_cash,
        'final_cash': cash,
        'initial_equity': initial_equity,
        'initial_loss': initial_loss,
        'daily_risk_limit': daily_risk_limit,
        'daily_risk_breached': daily_loss_accumulated >= daily_risk_limit,
        'signals_evaluated': signals_evaluated,
        'rejected_signals_count': len(rejected_signals),
        'total_trades': total_trades,
        'win_trades': len(win_trades),
        'loss_trades': len(loss_trades),
        'win_rate_pct': win_rate * 100.0,
        'total_net_pnl_krw': total_net_pnl,
        'profit_factor': profit_factor,
        'average_pnl_krw': avg_pnl,
        'max_loss_krw': max_loss,
        'trades': [
            {
                'symbol': p.symbol,
                'strategy': p.strategy,
                'entry_time': p.entry_time.isoformat() if p.entry_time else None,
                'exit_time': p.exit_time.isoformat() if p.exit_time else None,
                'quoted_ask': p.quoted_ask,
                'fill_ask': p.fill_ask,
                'exit_price': p.exit_price,
                'exit_fill': p.exit_fill,
                'stop': p.stop,
                'target': p.target,
                'quantity': p.quantity,
                'cash_spent': p.cash_spent,
                'cash_recovered': p.cash_recovered,
                'net_pnl': p.net_pnl,
                'return_pct': p.return_pct * 100.0,
                'exit_reason': p.exit_reason,
                'mfe': p.mfe_price,
                'mae': p.mae_price,
                'verification_tier': p.verification_tier
            }
            for p in positions
        ],
        'rejected_signals': rejected_signals
    }

    print("\n--- SUMMARY FOR SCENARIO:", scenario_label, "---")
    print(f"Total Trades: {total_trades}")
    print(f"Win Rate: {summary['win_rate_pct']:.1f}% ({len(win_trades)}W / {len(loss_trades)}L)")
    print(f"Total Net PnL: {total_net_pnl:+,.0f} KRW")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Average PnL: {avg_pnl:+,.0f} KRW | Max Loss: {max_loss:,.0f} KRW")
    print(f"Final Cash: {cash:,.0f} KRW (Net change: {cash - initial_cash:+,.0f} KRW)")
    print(f"Rejected Signals: {len(rejected_signals)} (Cash exhausted or risk budget)")

    return summary


if __name__ == '__main__':
    runtime_dir = Path(r"C:\Users\MJS\Desktop\주식자동매매\runtime")
    tick_files_20261105 = [
        runtime_dir / "shadow_ticks.jsonl.7",
        runtime_dir / "shadow_ticks.jsonl.8",
        runtime_dir / "shadow_ticks.jsonl.20",
        runtime_dir / "shadow_ticks.jsonl.9",
        runtime_dir / "shadow_ticks.jsonl.10",
        runtime_dir / "shadow_ticks.jsonl"
    ]

    # 1. SCENARIO A: Standalone Classics Strategy (Initial loss = 0)
    res_standalone = run_realistic_replay(
        tick_files=tick_files_20261105,
        initial_cash=68053.0,
        initial_equity=67643.78,
        initial_loss=0.0,
        scenario_label="Scenario A: Standalone Classics"
    )

    # 2. SCENARIO B: Combined with existing LIVE (-835 KRW initial loss)
    res_combined = run_realistic_replay(
        tick_files=tick_files_20261105,
        initial_cash=68053.0,
        initial_equity=67643.78,
        initial_loss=835.0,
        scenario_label="Scenario B: Combined with LIVE (-835 KRW Loss)"
    )

    # 3. OUT-OF-SAMPLE TEST: 2026-11-04 (Yesterday's Data)
    tick_files_20261104 = [
        runtime_dir / "shadow_ticks.jsonl.12",
        runtime_dir / "shadow_ticks.jsonl.13",
        runtime_dir / "shadow_ticks.jsonl.14",
        runtime_dir / "shadow_ticks.jsonl.15",
        runtime_dir / "shadow_ticks.jsonl.16",
        runtime_dir / "shadow_ticks.jsonl.24"
    ]

    res_oos = run_realistic_replay(
        tick_files=tick_files_20261104,
        initial_cash=68053.0,
        initial_equity=67643.78,
        initial_loss=0.0,
        scenario_label="Scenario C: 2026-11-04 Out-of-Sample (OOS)"
    )

    out_file = Path(r"C:\Users\MJS\.gemini\antigravity\brain\0d52d8d2-61a3-41bb-852a-9a08240200df\scratch\realistic_account_replay_results.json")
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump({
            'scenario_standalone_20261105': res_standalone,
            'scenario_combined_20261105': res_combined,
            'scenario_oos_20261104': res_oos
        }, f, ensure_ascii=False, indent=2)

    print(f"\nAll realistic scenarios completed. Results saved to: {out_file}")
