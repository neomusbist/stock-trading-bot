"""Replay observed FAST tape gates only; never infer orderability or fills.

Input rows must be captured trades with exchange/receipt timestamps, price,
volume and cumulative volume. Archived L1 may be paired to a trade, but this
tool cannot establish subscription, WATCH, broker balance, or feed continuity.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from automation.fast_momentum import FastMomentumStrategy
from core.strategy import Tick


def _time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None

def strict_breakout_trigger(tick, structure):
    if not structure.get('pass'):
        return 'UNKNOWN_NO_STRUCTURE'
    breakout=structure.get('breakout')
    if tick.ask < breakout:return 'UNKNOWN_OTHER_GEOMETRY_ROUTE'
    if tick.price <= breakout:return 'FAIL_TRADE_NOT_ABOVE_BREAKOUT'
    if tick.ask <= breakout:return 'FAIL_ASK_NOT_ABOVE_BREAKOUT'
    return 'PASS_STRICT_BREAKOUT_ONLY'


def replay(rows, symbol, start, end):
    """Use only rows received by each decision; out-of-order rows are rejected."""
    strategy = FastMomentumStrategy()
    output = []
    last_received = None
    accepted = rejected = 0
    for row in rows:
        if row.get('symbol') != symbol or row.get('event') != 'TRADE':
            continue
        at = _time(row.get('exchange_at') or row.get('time'))
        receipt = _time(row.get('receiver_at') or row.get('received'))
        if at is None or receipt is None or (last_received and receipt < last_received):
            rejected += 1
            continue
        last_received = receipt
        if receipt > end:
            continue
        try:
            tick = Tick(symbol, at, float(row['price']), int(row['volume']), int(row['cumulative']),
                float(row['ask']) if row.get('ask') is not None else 0.,
                float(row['bid']) if row.get('bid') is not None else 0.,
                regular=row.get('regular') is True, ask_qty=row.get('ask_qty') or 0,
                bid_qty=row.get('bid_qty') or 0, venue=row.get('venue') or 'UNKNOWN',
                depth_available=row.get('depth_available'), book_time=_time(row.get('book_time')),
                depth_source=row.get('depth_source') or 'UNKNOWN', received_at=receipt.isoformat(),
                book_received_at=row.get('book_received_at'))
        except (KeyError, ValueError, TypeError, OverflowError):
            rejected += 1
            continue
        if not strategy.ingest_tick(tick, receipt):
            rejected += 1
            continue
        accepted += 1
        if receipt < start:
            continue
        tape = strategy.get_tape(symbol)
        metrics = tape.metrics(receipt)
        if not metrics:
            output.append({'received_at': receipt.isoformat(), 'reason': 'TAPE_UNKNOWN'})
            continue
        metrics = {**metrics, 'spread': (tick.ask-tick.bid)/tick.bid if tick.bid > 0 and tick.ask >= tick.bid else None}
        quote_reason = strategy._quote(tick, receipt)
        phase = strategy.determine_phase(at)
        regime, reason, details = strategy.classify_regime(tick, metrics, phase, {}, asof=receipt)
        structure = tape.structure(receipt)
        trigger = strict_breakout_trigger(tick,structure)
        output.append({'received_at':receipt.isoformat(),'exchange_at':at.isoformat(),
            'source_sequence':row.get('transport_sequence'),'price':tick.price,'ask':tick.ask,
            'ticks_10':metrics.get('ticks_10'),'turnover_10':metrics.get('turnover_10'),
            'regime':regime,'regime_reason':reason,'details':details,'quote_reason':quote_reason,
            'micro_structure':structure,'strict_breakout_trigger':trigger,
            'actual_watch':'UNKNOWN','account':'UNKNOWN',
            'economic_eligibility':'UNKNOWN','orderable':'UNKNOWN'})
    return {'symbol':symbol,'accepted_rows':accepted,'rejected_rows':rejected,
            'scope':'OBSERVED_SUBSET_ONLY','feed_continuity':'UNKNOWN',
            'watch_context':'UNKNOWN','execution_performance':'UNKNOWN','rows':output}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('input',type=Path);p.add_argument('--symbol',required=True)
    p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    args=p.parse_args();start=_time(args.start);end=_time(args.end)
    if start is None or end is None or end < start:
        raise SystemExit('start/end must be ordered timezone-aware ISO timestamps')
    with args.input.open(encoding='utf-8') as f:
        result=replay((json.loads(line) for line in f),args.symbol,start,end)
    print(json.dumps(result,ensure_ascii=False,default=str))

if __name__=='__main__':main()
