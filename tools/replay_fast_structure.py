"""Replay actual recorded 0B prints into MicroTape; this never places orders.

Input is a frozen capture containing exchange_at, receiver_at or received_at,
processed_at and cumulative volume. Missing coverage is never inferred as flat.
"""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from automation.fast_momentum import MicroTape
from core.strategy import Tick


def stamp(s):
    try:
        value=datetime.fromisoformat(s)
        return value if value.tzinfo is not None else None
    except (TypeError,ValueError):return None


def replay(path,symbol,start,asof):
    start=stamp(start);asof=stamp(asof)
    if start is None or asof is None or start>asof:raise ValueError('INVALID_AWARE_TIME')
    tape=MicroTape();counts=Counter();before=Path(path).stat()
    with Path(path).open('rb') as f:
        for line in f:
            if symbol.encode() not in line:continue
            try:r=json.loads(line)
            except (ValueError,UnicodeDecodeError):counts['malformed']+=1;continue
            if r.get('event')!='TRADE' or r.get('symbol')!=symbol:continue
            received=stamp(r.get('receiver_at') or r.get('received_at'));processed=stamp(r.get('processed_at'))
            exchange=stamp(r.get('exchange_at') or r.get('time'))
            if not received or not processed or not exchange:counts['missing_time']+=1;continue
            if received<start or processed>asof or received>asof:continue
            try:
                t=Tick(symbol,exchange,r['price'],r['volume'],r['cumulative'],r.get('ask') or 0,r.get('bid') or 0,
                    regular=r.get('regular') is True,book_time=stamp(r.get('book_time')),
                    received_at=received.isoformat(),received_monotonic=r.get('received_monotonic'))
                if tape.ingest(t,received):counts['accepted']+=1
                else:counts[tape.last_rejection]+=1
            except (KeyError,TypeError,ValueError):counts['invalid']+=1
    after=Path(path).stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('SOURCE_CHANGED_DURING_READ')
    return {'counts':dict(counts),'structure':tape.structure(asof),
            'scope':'OBSERVED_CAPTURE_ONLY','coverage':'UNKNOWN','order_or_fill':'NOT_SIMULATED'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',required=True)
    p.add_argument('--symbol',required=True);p.add_argument('--start',required=True);p.add_argument('--asof',required=True)
    a=p.parse_args();print(json.dumps(replay(a.capture,a.symbol,a.start,a.asof),ensure_ascii=False,indent=2,default=str))

if __name__=='__main__':main()
