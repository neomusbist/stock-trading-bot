"""Offline opening replay of the unchanged R5 detector, never an order path.

Only received trade prints are fed to PatternDetector. Reference candles are
not accepted as a substitute for complete observed candles or tick warmup.
Candidate evaluations are not independent opportunities, fills or round trips.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from automation.capital import Costs
from automation.patterns import PatternDetector
from core.strategy import Tick


@dataclass(frozen=True)
class OpeningPrint:
    tick: Tick
    received_at: datetime
    watch: Mapping
    provenance: str  # REAL_WS or SIMULATED; never infer this from the price.
    processed_at: datetime | None = None  # Missing processing delay stays explicitly unknown.


def opening_prerequisites(cfg, auto):
    """Earliest prerequisites, not a promise that any pattern/order qualifies."""
    return {
        'warmup_seconds': cfg.baseline_seconds + cfg.acceleration_seconds,
        'A': {'complete_bars': 2, 'earliest_from_open_seconds': max(120, cfg.baseline_seconds + cfg.acceleration_seconds)},
        'A_continuation': {'complete_bars': 3, 'earliest_from_open_seconds': max(180, cfg.baseline_seconds + cfg.acceleration_seconds)},
        'B': {'complete_bars': 4, 'earliest_from_open_seconds': max(240, cfg.baseline_seconds + cfg.acceleration_seconds)},
        'C': {'complete_bars': auto.box_min_bars, 'earliest_from_open_seconds': max(60 * auto.box_min_bars, cfg.baseline_seconds + cfg.acceleration_seconds)},
        'requires_observation_from_open': True,
        'coverage_and_current_quote_required': True,
        'actual_order_eligibility': 'UNKNOWN',
        'roundtrip_duration': None,
    }


def replay_opening(samples, cfg, auto, cutoff, *, continuous_coverage_verified=False):
    """Compare 1/3/5/10-minute prefixes without injecting history or future data.

The optional continuous flag requires caller evidence of uninterrupted actual
subscription/queue coverage. A sparse event log alone does not establish it.
This research API has no broker, account journal, policy or runtime writes.
"""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise ValueError('cutoff requires an aware datetime')
    if type(continuous_coverage_verified) is not bool:
        raise ValueError('continuous coverage must be explicit boolean evidence')
    costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
                  cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
    detector = PatternDetector(cfg, auto, costs)
    detector.continuous_observation = continuous_coverage_verified
    opened = cutoff.replace(hour=9, minute=0, second=0, microsecond=0)
    ended = opened + timedelta(minutes=15)
    rejections = Counter()
    frames = []
    provenance = set()
    # Arrival order is the only ordering available to the detector in real time.
    # Do not sort by exchange time, which would repair a delayed stream in hindsight.
    for sample in sorted(samples, key=lambda x: x.received_at):
        if not isinstance(sample, OpeningPrint):
            raise TypeError('OpeningPrint trade evidence is required')
        if sample.provenance not in {'REAL_WS', 'SIMULATED'}:
            raise ValueError('explicit REAL_WS or SIMULATED provenance is required')
        tick = sample.tick
        receipt = sample.received_at
        if not isinstance(tick, Tick) or tick.time.tzinfo is None or receipt.tzinfo is None:
            raise ValueError('aware trade and receipt timestamps are required')
        if receipt > cutoff:
            continue
        if tick.time > receipt:
            rejections['FUTURE_TRADE_TIME'] += 1
            continue
        if tick.time.date() != opened.date() or not opened <= tick.time < ended:
            rejections['NOT_OPENING_REGULAR_PRINT'] += 1
            continue
        provenance.add(sample.provenance)
        if not detector.ingest(tick, receipt):
            rejections['DETECTOR_REJECTED_TRADE'] += 1
            continue
        signal = detector.signal(tick, dict(sample.watch))
        evaluation = detector.last_eval[tick.symbol]
        quality = detector.quality_summary(tick.symbol)
        frame = {
            'exchange_at': tick.time.isoformat(), 'received_at': receipt.isoformat(),
            'symbol': tick.symbol, 'provenance': sample.provenance,
            'receipt_lag_seconds': (receipt - tick.time).total_seconds(),
            'completed_bars': quality['usable_completed_bars'],
            'warmup': evaluation['gates']['warmup'],
            'trials': [{k: trial[k] for k in ('setup', 'candidate_generated', 'entry_ready', 'failed_reasons')}
                       for trial in evaluation['trials']],
            'signal_candidate': None,
            'actual_order_eligibility': 'UNKNOWN',
            'fill_feasibility': 'UNKNOWN',
            'completed_roundtrip': None,
        }
        if signal is not None:
            entry, target = signal['limit'], signal['target']
            frame['signal_candidate'] = {
                'pattern': signal['pattern'], 'entry': entry, 'stop': signal['stop'],
                'target': target, 'risk_per_share': signal['risk_per_share'],
                'modeled_reward_after_cost_per_share': target - entry - costs.estimate(entry, target),
                'cost_basis': 'CONFIG_ESTIMATE_NOT_BROKER_SETTLEMENT',
                'actual_net_pnl': None,
                'not_an_order': True,
            }
        frames.append(frame)
    prefixes = []
    for minute in (0, 1, 3, 5, 10, 15):
        requested = opened + timedelta(minutes=minute)
        at = min(requested, cutoff)
        rows = [r for r in frames if datetime.fromisoformat(r['received_at']) <= at]
        prefixes.append({
            'cutoff': at.isoformat(), 'requested_cutoff': requested.isoformat(),
            'minutes_from_open': minute, 'prefix_complete': requested <= cutoff,
            'accepted_prints': len(rows), 'observed_symbols': len({r['symbol'] for r in rows}),
            'maximum_complete_bars': max((r['completed_bars'] for r in rows), default=0),
            'warmup_ready_symbols': len({r['symbol'] for r in rows if r['warmup']['result'] == 'PASS'}),
            # Repeat passing ticks are evaluations, never count them as trades.
            'candidate_evaluations': sum(r['signal_candidate'] is not None for r in rows),
            'actual_completed_roundtrips': None,
            'net_expectancy': None,
        })
    return {
        'policy': 'UNCHANGED_R5_OPENING_REPLAY', 'orders_enabled': False,
        'cutoff': cutoff.isoformat(),
        'data_kind': next(iter(provenance)) if len(provenance) == 1 else 'MIXED' if provenance else 'NO_PRINTS',
        'continuous_coverage_verified': continuous_coverage_verified,
        'prerequisites': opening_prerequisites(cfg, auto), 'rejections': dict(rejections),
        'frames': frames, 'prefixes': prefixes,
        'profitability_proven': False,
        'limitations': ['candidate does not establish engine/order eligibility',
                        'quotes/depth/queue and both actual fills required for roundtrip result',
                        'synthetic paths test invariants, not market frequency or expectancy'],
    }
