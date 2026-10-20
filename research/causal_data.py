"""Point-in-time market evidence. Unknown availability is never a bar-close fill.

PriceContext labels denote minute STARTs (PriceContext.record validates end <=
asof). Its legacy last_complete_at is a mathematical bar end, not a receipt.
This module has no broker, credentials, network, or operating-directory writes.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from core.strategy import Tick
from core.market_time import tick_freshness, book_freshness


from core.market_evidence import aware,finite,bar_evidence,known_signal_bars


def file_sha(path,limit=None):
    digest=hashlib.sha256();remaining=limit
    with Path(path).open('rb') as stream:
        while remaining is None or remaining>0:
            data=stream.read(min(1048576,remaining) if remaining is not None else 1048576)
            if not data:break
            digest.update(data)
            if remaining is not None:remaining-=len(data)
    if remaining not in (None,0):raise ValueError('INPUT_TRUNCATED_DURING_HASH')
    return digest.hexdigest()


def frozen_lines(path,limit=None):
    """Only complete lines within the captured byte extent; ignore live appends."""
    limit=Path(path).stat().st_size if limit is None else limit
    offset=0
    with Path(path).open('rb') as stream:
        for line in stream:
            offset+=len(line)
            if offset>limit or not line.endswith(b'\n'):break
            yield line


def json_values(value):
    """Preserve evidence in engine journals without leaking datetime objects."""
    if isinstance(value,datetime):return value.isoformat()
    if isinstance(value,dict):return {str(k):json_values(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_values(v) for v in value]
    return value


def audit_cache(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    sources=Counter();reasons=Counter();availability=Counter();bars=0;verified=0
    first=None;last=None
    for row in data.get('rows',{}).values():
        for raw in row.get('bars',[]):
            ev=bar_evidence(raw,row=row);bars+=1;sources[ev['source']]+=1
            reasons[ev['reason'] or 'KNOWN_REFERENCE']+=1;availability[ev['availability_basis']]+=1
            verified+=ev['signal_source_verified']
            if ev['start']:
                first=min(first,ev['start']) if first else ev['start']
                last=max(last,ev['start']) if last else ev['start']
    return {'path':str(path),'sha256':file_sha(path),'symbols':len(data.get('rows',{})),
        'bars':bars,'sources':dict(sources),'reasons':dict(reasons),
        'availability_basis':dict(availability),'verified_signal_bars':verified,
        'label_semantics':'MINUTE_START_PROVEN_BY_PRICE_CONTEXT_RECORD_SOURCE',
        'first_bar_start':first.isoformat() if first else None,'last_bar_start':last.isoformat() if last else None,
        'bar_end_is_not_proof_of_receipt':True,'actual_trades':None,'net_pnl':None}


@dataclass(frozen=True)
class Frame:
    tick: Tick
    received_at: datetime
    available_at: datetime
    basis: str
    source_id: str

    @classmethod
    def from_record(cls,raw,source_id='',clock_basis='processed'):
        if raw.get('event') not in {'TRADE','TICK'}:raise ValueError('NOT_TRADE_EVIDENCE')
        exchange=aware(raw.get('time') or raw.get('exchange_at'))
        receipt=aware(raw.get('received') or raw.get('received_at'))
        processed=aware(raw.get('processed_at'))
        if not exchange or not receipt:raise ValueError('TRADE_RECEIPT_OR_EXCHANGE_UNKNOWN')
        if clock_basis not in {'processed','receiver_counterfactual'}:raise ValueError('CLOCK_BASIS_UNKNOWN')
        if clock_basis=='processed' and not processed:raise ValueError('STRATEGY_AVAILABILITY_UNKNOWN')
        available=max(receipt,processed) if clock_basis=='processed' else receipt
        # Historical monotonic counters belong to the original process, not
        # this interpreter. Receipt wall times remain explicit evidence.
        t=Tick(str(raw['symbol']),exchange,raw['price'],raw.get('volume',0),raw.get('cumulative',0),
            raw.get('ask',0),raw.get('bid',0),raw.get('regular'),
            ask_qty=raw.get('ask_qty',0),bid_qty=raw.get('bid_qty',0),
            depth_available=raw.get('depth_available'),book_time=aware(raw.get('book_time')),
            depth_source=raw.get('depth_source','UNKNOWN'),venue=raw.get('venue','KRX'),
            vi_active=raw.get('vi_active',False),
            received_at=receipt.isoformat(),book_received_at=raw.get('book_received_at'))
        return cls(t,receipt,available,'MEASURED_PAIRED_TRADE_QUOTE',source_id)

    def quote_reason(self,asof,stale_seconds=5):
        t=self.tick
        if not tick_freshness(t,asof,stale_seconds)['fresh']:return 'TRADE_STALE_OR_FUTURE'
        if t.vi_active:return 'VI_AUCTION_NO_BUY'
        if not t.regular or not (finite(t.price) and finite(t.bid) and finite(t.ask)) or t.bid>t.ask:return 'QUOTE_INVALID'
        if t.depth_source not in {'0D','0B_FIELDS'} or t.depth_available is not True:return 'MEASURED_DEPTH_UNKNOWN'
        if not book_freshness(t,asof,stale_seconds)['fresh']:return 'BOOK_STALE_OR_FUTURE'
        if not finite(t.bid_qty) or not finite(t.ask_qty):return 'MEASURED_DEPTH_UNKNOWN'
        return None

    def book_key(self):
        t=self.tick
        return (t.symbol,t.book_received_at,t.book_time,t.bid,t.ask,t.bid_qty,t.ask_qty,t.depth_source)


def read_frames(paths,day,*,clock_basis='processed',stats=None,limits=None):
    """Paths must be archive-rotation order. Keep ordering failures visible."""
    stats=stats if stats is not None else Counter()
    last=None
    for path in paths:
        for line_no,line in enumerate(frozen_lines(path,(limits or {}).get(str(path))),1):
                try:raw=json.loads(line)
                except ValueError:stats['MALFORMED_JSON']+=1;continue
                if (raw.get('time') or raw.get('exchange_at') or '')[:10]!=day:continue
                if (raw.get('time') or raw.get('exchange_at'))[11:19]>='15:30:00':continue
                try:f=Frame.from_record(raw,str(Path(path).name)+':'+str(line_no),clock_basis)
                except (KeyError,TypeError,ValueError) as e:stats[str(e)]+=1;continue
                if last and f.available_at<last:stats['OUT_OF_ORDER_AVAILABILITY']+=1;continue
                last=f.available_at;stats['FRAMES']+=1;yield f


def captured_bars(paths,day,limits=None):
    events=[];seen=set();stats=Counter()
    for path in paths:
        for line in frozen_lines(path,(limits or {}).get(str(path))):
                try:r=json.loads(line)
                except ValueError:stats['MALFORMED_JSON']+=1;continue
                if r.get('event')!='COMPLETED_BAR':continue
                b=dict(r.get('bar',{}));start=aware(b.get('time'));known=aware(r.get('known_at'))
                if not start or start.date().isoformat()!=day:continue
                if not '09:00:00'<=start.strftime('%H:%M:%S')<'15:30:00':continue
                # Earlier capture versions already stored the actual trigger
                # receipt and processing. Their maximum, never the minute
                # label alone, establishes a conservative availability cutoff.
                if known is None:
                    receipt=aware(r.get('confirmed_at'));processed=aware(r.get('processed_at'))
                    if receipt and processed:known=max(start+timedelta(minutes=1),receipt,processed)
                b['observation_source']='ACTUAL_RECEIVED' if r.get('source')=='ACTUAL_RECEIVED_DETECTOR_HISTORY' else 'UNKNOWN'
                if known:b['available_at']=known.isoformat()
                b['capture_session']=r.get('capture_session');b['capture_losses']=r.get('capture_losses')
                ev=bar_evidence(b);stats[ev['reason'] or 'VERIFIED']+=1
                if not ev['signal_source_verified']:continue
                key=(r['symbol'],start,known)
                if key in seen:stats['DUPLICATE']+=1;continue
                seen.add(key);events.append((known,r['symbol'],{**b,'time':start}))
    return sorted(events,key=lambda e:(e[0],e[1],e[2]['time'])),dict(stats)
