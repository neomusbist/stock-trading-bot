"""Read existing receiver/capture journals into an isolated no-network PAPER.

--events is an offline point-in-time merge. --follow-root starts at current
EOF; never backdates old state into a past trade. No API, GUI or LIVE writes.
"""
import argparse,contextlib,hashlib,json,os,shutil,sqlite3,sys,time
from collections import deque
from datetime import datetime
from pathlib import Path
if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.settings import KST,Config
from core.market_evidence import aware
from core.util import shared_binary_reader
from automation.policy import AutomationConfig
from research.observed_paper import ObservedPaperSession
from research.evidence_collector import JsonTail
from tools.benchmark_all_strategies import read_settings

EVENTS={'WATCH_METADATA','DAILY_REFERENCE','REFERENCE_BARS_PART','SUBSCRIPTION_SCOPE',
        'STREAM_STATE','ENTRY_DECISION_EVIDENCE','OPPORTUNITY_SAMPLE','BOOK','TRADE','TICK'}


def known(row):
    return max((at for k in ('processed_at','known_at','asof','received_at','received','at','time')
                if (at:=aware(row.get(k))) is not None),default=None)


def offline(paths,output,cfg,auto,cash,day,evaluate_each_trade=False):
    """Disk-backed causal merge of bounded source extents, not exchange sort."""
    db=sqlite3.connect(output/'input-index.sqlite');manifest=[];session=None;unknown=0
    try:
        db.execute('CREATE TABLE events (known TEXT, ordinal INTEGER, source TEXT, row TEXT)')
        ordinal=0
        for path in paths:
            p=Path(path);extent=p.stat().st_size;offset=0;digest=hashlib.sha256()
            with shared_binary_reader(p) as stream:
                while offset<extent:
                    line=stream.readline(min(262145,extent-offset));begin=offset;offset+=len(line);digest.update(line)
                    if len(line)>262144 or not line.endswith(b'\n'):unknown+=1;continue
                    try:row=json.loads(line)
                    except (ValueError,UnicodeError):unknown+=1;continue
                    # Work evidence wrappers retain original source byte links.
                    row=row.get('record',row);stamp=known(row)
                    if row.get('event') not in EVENTS:continue
                    if stamp is None:unknown+=1;continue
                    if stamp.date().isoformat()!=day:continue
                    ordinal+=1
                    if ordinal>3000000:raise ValueError('OFFLINE_ROW_BUDGET_EXCEEDED')
                    if ordinal%10000==0 and shutil.disk_usage(output).free<32*1024**3:
                        raise ValueError('32GiB_FREE_SPACE_RESERVE_REQUIRED')
                    db.execute('INSERT INTO events VALUES (?,?,?,?)',(stamp.isoformat(),ordinal,
                        p.name+':'+str(begin),json.dumps(row,ensure_ascii=False)))
            manifest.append(dict(source_file=p.name,byte_end=extent,sha256=digest.hexdigest()))
        db.commit()
        for stamp,source,raw in db.execute('SELECT known,source,row FROM events ORDER BY known,ordinal'):
            if session is None:session=ObservedPaperSession(aware(stamp),cfg,auto,cash,output/'evidence',evaluate_each_trade=evaluate_each_trade)
            session.feed(json.loads(raw),source)
        result=session.result() if session else {'no_source_events':True}
        result.update(source_manifest=manifest,unparseable_or_unavailable_rows=unknown,
                      realtime_market_verification=False,actual_orders=False)
        return result
    finally:
        if session:
            session.close()
            if 'result' in locals():result['recorder_after_close']=session.capture.stats()
        db.close()


def follow(root,output,cfg,auto,cash,seconds,poll=1.,cpu_budget_ms=50.):
    """Bounded, idle-priority observer; overload is visible, never freshness relaxation."""
    root=Path(root).resolve();start=datetime.now(KST)
    session=ObservedPaperSession(start,cfg,auto,cash,output/'evidence',basis='REALTIME_SIDECAR')
    tails={};pending=deque();initial=True;deadline=time.monotonic()+seconds;budget_hits=0;rows=0;references={}
    reference_log=[];reference_bytes=0
    def reference_snapshot(symbol):
        nonlocal reference_bytes
        if not symbol or not symbol.isalnum() or len(symbol)>12:return
        from candidate_builder import bars_from_csv
        for folder in ('data/raw','data/observation'):
            path=root/folder/(symbol+'_daily.csv')
            if not path.is_file():continue
            st=path.stat();signature=(str(path),st.st_mtime_ns,st.st_size)
            if references.get(symbol)==signature:return
            if st.st_size>1024*1024:session.counts['REFERENCE_OVERSIZE_UNKNOWN']+=1;continue
            with shared_binary_reader(path) as stream:data=stream.read(1024*1024+1)
            sha=hashlib.sha256(data).hexdigest()
            target=output/'reference-inputs'/(path.stem+'.'+sha+'.csv')
            if not target.exists():
                if reference_bytes+len(data)>64*1024**2 or shutil.disk_usage(output).free-len(data)<32*1024**3:
                    session.counts['REFERENCE_STORAGE_BUDGET_UNKNOWN']+=1;return
                target.parent.mkdir(exist_ok=True);target.write_bytes(data);reference_bytes+=len(data)
            try:bars=bars_from_csv(target,datetime.now(KST).date())[-cfg.history_bars:]
            except (ValueError,KeyError):session.counts['REFERENCE_FORMAT_UNKNOWN']+=1;continue
            if len(bars)<cfg.min_history_bars:continue
            observed=datetime.now(KST)
            session.feed(dict(event='DAILY_REFERENCE',symbol=symbol,bars=bars,known_at=observed.isoformat()),path.name,observed)
            references[symbol]=signature
            reference_log.append(dict(source=str(path.relative_to(root)),known_at=observed.isoformat(),
                sha256=sha,snapshot=target.name,bars=len(bars),
                basis='SIDECAR_OBSERVED_DISK_REFERENCE_NOT_HISTORICAL_REST_CACHE_CERTIFICATION'))
            return
    try:
        while time.monotonic()<deadline and not session.capture.storage_blocked:
            paths=sorted((root/'runtime/automation-production/trade-review').glob('trade-review-'+start.strftime('%Y%m%d')+'-*.jsonl'))
            current=paths[-2:]  # catch rotation without opening the whole historical tape
            began=time.process_time()
            for path in current:
                if path not in tails:tails[path]=JsonTail(path,start_at_end=initial,with_provenance=True)
                if len(pending)<128:
                    pending.extend((path.name,row) for row in tails[path].read(max_rows=128))
            while pending:
                name,row=pending.popleft()
                session.feed(row,name,datetime.now(KST));rows+=1
                if row.get('event') in {'WATCH_METADATA','OPPORTUNITY_SAMPLE'}:reference_snapshot(row.get('symbol'))
                if (time.process_time()-began)*1000>=cpu_budget_ms:
                    budget_hits+=1;break
            initial=False
            if len(tails)>4:
                for old in sorted(tails)[:-4]:
                    tail=tails.pop(old)
                    try:unread=max(0,old.stat().st_size-tail.offset)
                    except OSError:unread=1
                    if unread:
                        session.counts['OBSERVER_ROTATION_GAP_BYTES_UNKNOWN']+=unread
                        for sym in session.last_trade:session.replay.detector.mark_coverage_gap(sym,datetime.now(KST),'OBSERVER_ROTATION_BACKLOG_GAP')
            time.sleep(min(poll,max(0,deadline-time.monotonic())))
        result=session.result()
        result.update(observer_cpu_budget_hits=budget_hits,observer_rows=rows,
            observer_pending_rows=len(pending),
            cpu_budget_ms_per_poll=cpu_budget_ms,poll_seconds=poll,
            realtime_market_verification='ONLY_ROWS_ACTUALLY_OBSERVED_DURING_THIS_RUN',
            tail_counters={p.name:dict(t.stats) for p,t in tails.items()},
            capacity_is_not_certified_for_market_open=True)
        result['reference_observations']=reference_log
        return result
    finally:
        session.close()
        if 'result' in locals():result['recorder_after_close']=session.capture.stats()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--events',type=Path,nargs='+');mode.add_argument('--follow-root',type=Path)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--automation',type=Path,required=True)
    p.add_argument('--initial-cash',type=float,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--day',required=True);p.add_argument('--seconds',type=int,default=60)
    p.add_argument('--evaluate-each-trade',action='store_true',help='offline research only; default follows native decision transitions')
    a=p.parse_args(argv);output=a.output.resolve()
    for source in [*(a.events or []),a.config,a.automation,*([a.follow_root] if a.follow_root else [])]:
        source=source.resolve()
        if output==source or (source.is_dir() and source in output.parents):
            p.error('output must be outside every source root')
    if output.exists():p.error('use a new output directory; existing evidence is preserved')
    if not 1<=a.seconds<=28800:p.error('seconds must be 1..28800')
    if a.follow_root and a.day!=datetime.now(KST).date().isoformat():p.error('follow day must be today')
    if a.follow_root and a.evaluate_each_trade:p.error('per-trade research replay is forbidden in the live observer')
    cfg=read_settings(a.config,Config);auto=read_settings(a.automation,AutomationConfig)
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),0x40)
    output.mkdir(parents=True)
    # Console mirroring is not another evidence journal; critical engine logs
    # and the persisted fake-account book remain intact in their owned root.
    with open(os.devnull,'w') as quiet,contextlib.redirect_stdout(quiet):
        result=(offline(a.events,output,cfg,auto,a.initial_cash,a.day,a.evaluate_each_trade) if a.events else
                follow(a.follow_root,output,cfg,auto,a.initial_cash,a.seconds))
    (output/'RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'actual_orders':False,'LIVE_modified':False,
        'paper_buy_intents':result.get('source_counts',{}).get('MEASURED_CONTEXT_PAPER_BUY_INTENTS',0)}))
    return result


if __name__=='__main__':main()
