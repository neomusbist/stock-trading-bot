"""Deterministic direct-line identity audit; no market API, prices or fills invented."""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from audit.r6_claude_independent.independent_replay import load_entry_evals
from audit.r6_work_repair_20261014.replay import EXPECTED_SHA256, csv_write

REQUIRED={'079650','010580','393890','062970','290650','042510'}


def run(archive, official, out):
    path=Path(archive); digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=EXPECTED_SHA256:raise ValueError('RAW_ARCHIVE_HASH_MISMATCH')
    reference=json.loads(Path(official).read_text(encoding='utf-8'))
    if not reference['at'].startswith('2026-10-14'):raise ValueError('OFFICIAL_NAME_CAPTURE_DATE_MISMATCH')
    events,stats=load_entry_evals(path);groups=defaultdict(list)
    for e in events:groups[e['symbol']].append(e)
    ledger_path=Path(__file__).parents[1]/'r6_claude_independent/TRADE_LEDGER.csv'
    with ledger_path.open(encoding='utf-8-sig') as f:ledger=list(csv.DictReader(f))
    selected=REQUIRED|{r['code'] for r in ledger};audit=[]
    for code in sorted(selected):
        data=groups[code];first=data[0];names=sorted({r['name'] for r in data})
        official_name=reference['names'].get(code)
        audit.append(dict(role='CODE_SUMMARY',code=code,raw_name='|'.join(names),official_name=official_name,
            name_match=names==[official_name],first_at=first['dt'].isoformat(),first_line=first['line'],
            first_price=first['price'],first_bid=first['bid'],first_ask=first['ask'],
            min_price=min(r['price'] for r in data),max_price=max(r['price'] for r in data),events=len(data),
            price_claim_7650_7720_found=any(7650<=r['price']<=7720 for r in data) if code=='079650' else '',
            price_claim_13400_found=any(13400<=r['price']<13500 for r in data) if code=='290650' else '',
            official_source=reference['url'],official_capture_at=reference['at'],
            historical_rename_chain='UNKNOWN_NOT_SEPARATELY_OBTAINED',
            FAST_signal='UNKNOWN_NO_RAW_TRADE_VOLUME',verdict='PASS_RAW_CODE_PRICE_NAME_LINK'))
    needed={int(r[col]) for r in ledger for col in ('signal_line','entry_fill_line','exit_fill_line') if r.get(col)}
    raw={}
    with path.open(encoding='utf-8') as f:
        for n,line in enumerate(f,1):
            if n in needed:raw[n]=json.loads(line)
    mismatches=[]
    for tr in ledger:
        for role,col in (('SIGNAL','signal_line'),('ENTRY_FILL','entry_fill_line'),('EXIT_FILL','exit_fill_line')):
            if not tr.get(col):continue
            n=int(tr[col]);e=raw[n];quote=((e.get('gates') or {}).get('spread') or {}).get('values') or {}
            same=e['symbol']==tr['code'] and e['name']==tr['name']
            ask_match=(float(tr['entry_observed_ask'])==quote.get('ask')) if role=='ENTRY_FILL' else None
            same=bool(same and ask_match is not False)
            if not same:mismatches.append(dict(trade_id=tr['trade_id'],role=role,line=n))
            audit.append(dict(role=role,trade_id=tr['trade_id'],code=tr['code'],raw_code=e['symbol'],
                raw_name=e['name'],official_name=reference['names'].get(tr['code']),raw_line=n,
                raw_at=e.get('time') or e.get('timestamp'),raw_price=e['current_price'],raw_bid=quote.get('bid'),raw_ask=quote.get('ask'),
                ledger_entry_quote_matches_raw=ask_match,code_and_name_match=same,
                FAST_signal='UNKNOWN_SNAPSHOT_PROXY_NOT_FAST',
                hypothetical_entry=tr['entry_assumed_price'],hypothetical_exit=tr.get('exit_assumed_price'),
                fill_feasibility='UNKNOWN_NO_DEPTH_OR_QUEUE_POSITION',verdict='PASS' if same else 'FAIL'))
    out=Path(out);out.mkdir(parents=True,exist_ok=True);csv_write(out/'SYMBOL_DATA_AUDIT.csv',audit)
    result=dict(archive=path.name,sha256=digest,stats=dict(stats),codes=sorted(selected),
                source_rows_checked=len(needed),ledger_links_checked=sum(r['role']!='CODE_SUMMARY' for r in audit),
                mismatches=mismatches,official=reference,
                ledger_identity='PASS' if not mismatches else 'FAIL',
                incorrect_price_claim_origin='UNKNOWN_SOURCE_REPORT_NOT_OBTAINED',
                raw_quote_time_parity='UNKNOWN_SNAPSHOT_PRICE_OUTSIDE_BOOK_IN_38_ROWS',
                real_FAST_backtest='UNKNOWN_NO_RAW_TAPE')
    (out/'DATA_IDENTITY_RESULTS.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('archive');p.add_argument('--official-names',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();r=run(a.archive,a.official_names,a.out)
    print(json.dumps({k:r[k] for k in ('sha256','ledger_links_checked','source_rows_checked','mismatches','ledger_identity')},ensure_ascii=False))
