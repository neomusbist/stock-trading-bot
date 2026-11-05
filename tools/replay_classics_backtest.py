"""Empirical Replay Backtester for Classics Strategy Engine (투자 원전 9권 기반 과거 데이터 검증 리플레이).

Reconstructs 1-minute bars from actual raw tick streams (shadow_ticks.jsonl)
and evaluates ClassicsStrategyEngine with realistic execution modeling (taxes, fees, slippage).
"""

import sys
import os
import json
import math
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
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
        self.bars = defaultdict(list)  # symbol -> list of bars
        self.current_bar = {}          # symbol -> dict

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

        # Truncate to minute
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
            # Previous bar finished
            completed_bar = dict(cur)
            self.bars[sym].append(completed_bar)

            # Start new bar
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


class ReplayPosition:
    def __init__(self, signal: Dict[str, Any], entry_time: datetime):
        self.symbol = signal['symbol']
        self.name = signal.get('name', self.symbol)
        self.strategy = signal['strategy']
        self.entry_time = entry_time
        self.entry = signal['entry']
        self.stop = signal['stop']
        self.emergency = signal.get('emergency', self.stop)
        self.target = signal['target']
        self.quantity = signal['quantity']
        self.net_rr = signal['net_rr']
        self.gross_rr = signal['gross_rr']

        # Tracking metrics
        self.mfe_price = self.entry
        self.mae_price = self.entry
        self.is_closed = False
        self.exit_time = None
        self.exit_price = None
        self.exit_reason = None
        self.net_pnl = 0.0
        self.return_pct = 0.0

    def update(self, high: float, low: float, close: float, current_time: datetime) -> bool:
        if self.is_closed:
            return True

        self.mfe_price = max(self.mfe_price, high)
        self.mae_price = min(self.mae_price, low)

        # Target Check First (Limit target reached)
        if high >= self.target:
            self.close(self.target, 'TARGET_REACHED', current_time)
            return True

        # Elder Breakeven & Trailing Profit Lock
        trailing_stop = ClassicsRiskSizer.calculate_trailing_stop(self.entry, self.stop, self.mfe_price, DEFAULT_COSTS)
        if trailing_stop > self.stop:
            self.stop = trailing_stop

        # Stop Check (Stop triggered)
        if low <= self.stop:
            reason = 'TRAILING_STOP_TRIGGERED' if self.stop >= self.entry else 'INITIAL_STOP_TRIGGERED'
            self.close(self.stop, reason, current_time)
            return True

        return False

    def close(self, exit_price: float, reason: str, exit_time: datetime):
        self.is_closed = True
        self.exit_price = exit_price
        self.exit_reason = reason
        self.exit_time = exit_time

        # Calculate exact net PnL (with 0.18% tax, 0.015% fee, 0.05% slippage)
        buy_outlay_per_share = self.entry * (1 + 0.0005) * (1 + 0.00015)
        sell_net_per_share = self.exit_price * (1 - 0.0005) * (1 - 0.00015 - 0.0018)

        net_per_share = sell_net_per_share - buy_outlay_per_share
        self.net_pnl = net_per_share * self.quantity
        self.return_pct = (sell_net_per_share / buy_outlay_per_share) - 1.0


def run_replay(tick_files: List[Path], max_positions: int = 2) -> Dict[str, Any]:
    aggregator = CandleAggregator()
    engine = ClassicsStrategyEngine(equity=10000000.0, capital_limit=1000000.0)

    positions: List[ReplayPosition] = []
    active_positions: Dict[str, ReplayPosition] = {}
    signal_candidates = []
    eval_count = 0

    cooldown_tracker = {}  # symbol -> last exit time
    print(f"Starting replay across {len(tick_files)} tick files...")

    for fpath in tick_files:
        if not fpath.exists():
            continue
        print(f"  Ingesting: {fpath.name} ({fpath.stat().st_size:,} bytes)...", flush=True)

        with open(fpath, 'r', encoding='utf-8') as f:
            for line_idx, line in enumerate(f):
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
                t_str = tick.get('time')
                dt = parse_iso(t_str)

                if not sym or price is None or dt is None:
                    continue

                # 1. Update Candle Aggregator
                aggregator.on_tick(tick)

                # 2. Update existing active position if open
                if sym in active_positions:
                    pos = active_positions[sym]
                    if pos.update(high=price, low=price, close=price, current_time=dt):
                        cooldown_tracker[sym] = dt
                        del active_positions[sym]

                # 3. Evaluate New Entry Condition periodically
                # Check cooldown: at least 15 minutes after exit
                last_exit = cooldown_tracker.get(sym)
                is_cooled_down = (last_exit is None) or ((dt - last_exit).total_seconds() >= 900)

                if bid and ask and sym not in active_positions and len(active_positions) < max_positions and is_cooled_down:
                    bars = aggregator.bars.get(sym, [])
                    if len(bars) >= 8:
                        # Extension filter (O'Neil): do not buy if already > 12% above day's first open
                        day_open = bars[0]['open']
                        if day_open > 0 and (float(price) - day_open) / day_open > 0.12:
                            continue

                        eval_count += 1
                        sig, reason, data = engine.evaluate_candidate(
                            symbol=sym,
                            price=float(price),
                            bid=float(bid),
                            ask=float(ask),
                            bars=bars,
                            watch={'name': sym}
                        )

                        if sig is not None:
                            signal_candidates.append(sig)
                            pos = ReplayPosition(sig, dt)
                            active_positions[sym] = pos
                            positions.append(pos)
                            print(f"    [ENTRY TRIGGERED] {dt.strftime('%H:%M:%S')} {sym} ({sig['strategy']}): "
                                  f"Entry={sig['entry']:,} Stop={sig['stop']:,} Target={sig['target']:,} "
                                  f"Qty={sig['quantity']} Net RR={sig['net_rr']:.2f}")

    # Close any positions still active at end of replay
    final_time = datetime(2026, 11, 5, 15, 10, 0)
    for sym, pos in list(active_positions.items()):
        latest_bars = aggregator.bars.get(sym, [])
        final_price = latest_bars[-1]['close'] if latest_bars else pos.entry
        pos.close(final_price, 'END_OF_SESSION_FLATTEN', final_time)
        del active_positions[sym]

    # Summarize Performance Metrics
    total_trades = len(positions)
    win_trades = [p for p in positions if p.net_pnl > 0]
    loss_trades = [p for p in positions if p.net_pnl <= 0]

    win_rate = (len(win_trades) / total_trades) if total_trades > 0 else 0.0
    total_net_pnl = sum(p.net_pnl for p in positions)
    gross_profits = sum(p.net_pnl for p in win_trades)
    gross_losses = abs(sum(p.net_pnl for p in loss_trades))
    profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (99.0 if gross_profits > 0 else 0.0)

    results = {
        'total_ticks_evaluated': eval_count,
        'signals_generated': len(signal_candidates),
        'total_trades': total_trades,
        'win_trades': len(win_trades),
        'loss_trades': len(loss_trades),
        'win_rate_pct': win_rate * 100.0,
        'total_net_pnl_krw': total_net_pnl,
        'profit_factor': profit_factor,
        'trades': [
            {
                'symbol': p.symbol,
                'name': p.name,
                'strategy': p.strategy,
                'entry_time': p.entry_time.isoformat() if p.entry_time else None,
                'exit_time': p.exit_time.isoformat() if p.exit_time else None,
                'entry': p.entry,
                'exit': p.exit_price,
                'stop': p.stop,
                'target': p.target,
                'quantity': p.quantity,
                'net_pnl_krw': p.net_pnl,
                'return_pct': p.return_pct * 100.0,
                'exit_reason': p.exit_reason,
                'net_rr': p.net_rr,
                'mfe_price': p.mfe_price,
                'mae_price': p.mae_price
            }
            for p in positions
        ]
    }

    return results


if __name__ == '__main__':
    runtime_dir = Path(r"C:\Users\MJS\Desktop\주식자동매매\runtime")
    # Order of morning rotated files: .7 (09:01), .8 (09:11), .20 (09:22), .9 (09:37), .10 (10:03)
    target_files = [
        runtime_dir / "shadow_ticks.jsonl.7",
        runtime_dir / "shadow_ticks.jsonl.8",
        runtime_dir / "shadow_ticks.jsonl.20",
        runtime_dir / "shadow_ticks.jsonl.9",
        runtime_dir / "shadow_ticks.jsonl.10",
        runtime_dir / "shadow_ticks.jsonl"
    ]

    res = run_replay(target_files)

    out_file = Path(r"C:\Users\MJS\.gemini\antigravity\brain\0d52d8d2-61a3-41bb-852a-9a08240200df\scratch\replay_classics_results.json")
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    print("\n=== REPLAY BACKTEST SUMMARY ===")
    print(f"Total Trades Taken: {res['total_trades']}")
    print(f"Win Rate: {res['win_rate_pct']:.1f}% ({res['win_trades']} wins / {res['loss_trades']} losses)")
    print(f"Total Net PnL: {res['total_net_pnl_krw']:+,.0f} KRW")
    print(f"Profit Factor: {res['profit_factor']:.2f}")
    print(f"Detailed results saved to: {out_file}")
