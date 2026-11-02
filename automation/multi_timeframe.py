"""Multi-Timeframe Price Structure & Entry Timing Strategy (MTF).

Evaluates 3~5 minute higher-timeframe price structure for confirmed trend,
healthy impulse-pullback geometry, and sufficient room to overhead resistance.
Confirms entry timing and execution limit on the 1-minute lower timeframe
using structural support holds, candle reclaims, and strict non-chase guards.
Enforces net RR >= 1.50 after all roundtrip frictions and sizing on emergency stop.
"""
from datetime import datetime, timedelta
import math
from typing import Any, Dict, List, Optional, Tuple

from core.settings import KST
from core.util import floor_tick, tick_size
from automation.capital import Costs, entry_economics


def resample_bars(bars_1m: List[Dict[str, Any]], interval_minutes: int = 3) -> List[Dict[str, Any]]:
    """Resample causal 1-minute bars into interval_minutes bars."""
    if not bars_1m or interval_minutes <= 1:
        return list(bars_1m)

    sorted_bars = sorted(bars_1m, key=lambda b: b['time'])
    htf_bars: List[Dict[str, Any]] = []
    current_bucket: Optional[datetime] = None
    chunk: List[Dict[str, Any]] = []

    for b in sorted_bars:
        t: datetime = b['time']
        # Align buckets by minutes since midnight
        minutes_since_midnight = t.hour * 60 + t.minute
        bucket_minute = (minutes_since_midnight // interval_minutes) * interval_minutes
        bucket_time = t.replace(hour=bucket_minute // 60, minute=bucket_minute % 60, second=0, microsecond=0)

        if current_bucket is None:
            current_bucket = bucket_time

        if bucket_time == current_bucket:
            chunk.append(b)
        else:
            # Finalize previous HTF bar
            if chunk:
                htf_bars.append({
                    'time': current_bucket,
                    'open': chunk[0]['open'],
                    'high': max(x['high'] for x in chunk),
                    'low': min(x['low'] for x in chunk),
                    'close': chunk[-1]['close'],
                    'volume': sum(x.get('volume', 0) for x in chunk),
                    'bar_count': len(chunk)
                })
            current_bucket = bucket_time
            chunk = [b]

    # Current forming or finalized bar
    if chunk:
        htf_bars.append({
            'time': current_bucket,
            'open': chunk[0]['open'],
            'high': max(x['high'] for x in chunk),
            'low': min(x['low'] for x in chunk),
            'close': chunk[-1]['close'],
            'volume': sum(x.get('volume', 0) for x in chunk),
            'bar_count': len(chunk)
        })

    return htf_bars


class MultiTimeframeStrategy:
    def __init__(self, cfg, auto, costs: Optional[Costs] = None):
        self.c = cfg
        self.a = auto
        self.costs = costs or Costs(
            getattr(auto, 'commission_buy', 0.00015),
            getattr(auto, 'commission_sell', 0.00015),
            getattr(auto, 'sell_tax', 0.002),
            getattr(cfg, 'entry_slippage_fraction', 0.002),
            getattr(cfg, 'exit_slippage_fraction', 0.003),
        )

    def evaluate(
        self,
        bars_1m: List[Dict[str, Any]],
        tick: Any,
        watch: Dict[str, Any],
        capital_limit: float = 81300.0,
        risk_budget: float = 1626.0,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate multi-timeframe structure on 3m/5m and trigger on 1m."""
        if not getattr(self.a, 'mtf_entry_enabled', False):
            return None

        if len(bars_1m) < 8:
            return None

        # 1. Build 3-minute and 5-minute bars
        bars_3m = resample_bars(bars_1m, interval_minutes=3)
        if len(bars_3m) < 3:
            return None

        # Use completed 3m bars prior to current minute
        completed_3m = [b for b in bars_3m if b['time'] + timedelta(minutes=3) <= tick.time]
        if len(completed_3m) < 3:
            completed_3m = bars_3m[:-1] if len(bars_3m) >= 2 else bars_3m

        if len(completed_3m) < 3:
            return None

        # 2. HTF (3m) Structure Identification
        # Find impulse base (swing low) and impulse peak (swing high)
        window = completed_3m[-12:]
        peak_idx = max(range(len(window)), key=lambda i: window[i]['high'])
        if peak_idx == 0 or peak_idx >= len(window) - 1:
            # Peak must be intermediate (not first bar, and followed by at least 1 pullback bar)
            return None

        impulse_bars = window[:peak_idx + 1]
        pullback_bars = window[peak_idx + 1:]

        base_price = min(b['low'] for b in impulse_bars)
        peak_price = window[peak_idx]['high']
        impulse_rise = peak_price - base_price

        # Minimum impulse rise requirement: >= 1.5%
        if base_price <= 0 or (impulse_rise / base_price) < 0.015:
            return None

        # Check upward momentum in impulse
        if impulse_bars[-1]['close'] <= impulse_bars[0]['open']:
            return None

        # Pullback support level
        support_price = min(b['low'] for b in pullback_bars)
        if support_price <= base_price or support_price >= peak_price:
            return None

        # Retracement depth check: 20% to 65% of impulse
        retrace = (peak_price - support_price) / impulse_rise
        if not (0.20 <= retrace <= 0.65):
            return None

        # Overhead target determination: confirmed peak or lower resistance
        overhead_res = watch.get('resistance')
        target_ceiling = peak_price
        if overhead_res and overhead_res > tick.price:
            target_ceiling = min(target_ceiling, overhead_res)

        # 3. LTF (1m) Entry Timing & Reclaim Trigger
        recent_1m = [b for b in bars_1m if b['time'] >= pullback_bars[0]['time']]
        if len(recent_1m) < 2:
            return None

        # Must have touched/tested the support zone (within 0.3% of support_price)
        touched = any(b['low'] <= support_price * 1.003 for b in recent_1m)
        if not touched:
            return None

        # Support must NOT have been breached below support - 0.5%
        if any(b['low'] < support_price * 0.995 for b in recent_1m):
            return None

        last_1m = recent_1m[-1]
        prev_1m = recent_1m[-2]

        # Reclaim trigger: last 1m candle held support and rebounded
        # (close > prev high, or close > open with positive bounce from support)
        reclaimed = (last_1m['close'] > prev_1m['high'] or
                     (last_1m['close'] > last_1m['open'] and last_1m['low'] >= support_price))
        if not reclaimed:
            return None

        entry_limit = int(tick.ask) if tick.ask > 0 else int(tick.price)

        # Non-chase guard: cannot enter if price already rebounded > 45% back toward peak
        max_chase_price = support_price + 0.45 * (peak_price - support_price)
        if entry_limit > max_chase_price:
            return None

        # Minimum room to target: at least 2.0%
        target = float(target_ceiling)
        if entry_limit <= 0 or (target - entry_limit) / entry_limit < 0.020:
            return None

        # 4. Invalidation and Emergency Stop Lines
        step = tick_size(entry_limit)
        spread = tick.ask - tick.bid if (tick.ask > 0 and tick.bid > 0) else step
        buffer_amount = max(step, support_price * 0.003, spread)
        structural_stop = floor_tick(support_price - buffer_amount)

        # Emergency stop (worst-tick stop buffer)
        noise_band = max(step * 2, entry_limit * 0.006, spread * 1.5)
        emergency_stop = floor_tick(structural_stop - noise_band)

        if emergency_stop >= entry_limit or structural_stop >= entry_limit:
            return None

        # Minimum stop distance guard: at least 1.2%
        stop_dist_pct = (entry_limit - emergency_stop) / entry_limit
        if stop_dist_pct < 0.012:
            return None

        # 5. Economics & Net RR Evaluation
        econ = entry_economics(
            entry_limit,
            structural_stop,
            emergency_stop,
            target,
            self.costs,
            minimum_rr=getattr(self.c, 'min_reward_risk', 1.50)
        )

        if econ.get('status') != 'KNOWN' or not econ.get('rr_pass', False):
            return None

        net_rr = econ.get('net_rr', 0.0)
        if net_rr < getattr(self.c, 'min_reward_risk', 1.50):
            return None

        # 6. Account Affordability & Sizing Check
        one_share_cash = econ.get('one_share_cash', entry_limit * 1.00015)
        risk_per_share = econ.get('risk_per_share', (entry_limit - emergency_stop) * 1.01)

        if one_share_cash > capital_limit or risk_per_share > risk_budget:
            # Cannot afford even 1 share with current capital or risk budget
            return None

        max_cash_shares = int(capital_limit // one_share_cash)
        max_risk_shares = int(risk_budget // risk_per_share)
        planned_qty = max(1, min(max_cash_shares, max_risk_shares))

        signal = {
            'pattern': 'MTF',
            'setup_variant': 'MTF_3M_1M',
            'symbol': tick.symbol,
            'limit': entry_limit,
            'stop': emergency_stop,
            'structural_stop': structural_stop,
            'emergency': emergency_stop,
            'target': target,
            'net_reward_risk': net_rr,
            'planned_qty': planned_qty,
            'support': support_price,
            'peak': peak_price,
            'base': base_price,
            'retrace': retrace,
            'invalidation_policy': {
                'enabled': True,
                'structural_stop': structural_stop,
                'emergency_stop': emergency_stop,
                'target_ceiling': target,
            },
            'use_limit_lifecycle': True,
            'entry_basis': 'CONFIRMED_MTF_SUPPORT_RECLAIM',
            'economics': econ,
            'created_at': tick.time.isoformat() if hasattr(tick.time, 'isoformat') else str(tick.time),
        }
        return signal
