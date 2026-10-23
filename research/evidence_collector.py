"""Read-only sidecar for the next session; no production hooks or broker.

Tails existing journals. It adds no subscription, token, REST call, order,
detector evaluation or synchronous write to the LIVE process. The first two
formed/ready opportunities in arrival order get bounded pre/post evidence,
irrespective of subsequent return. This is a resource sample, not a census.
"""
import argparse
from collections import Counter,deque
from dataclasses import asdict
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import time
import shutil
import os

from automation.trade_review_capture import TradeReviewCapture
from core.market_evidence import aware
from core.settings import KST
from research.causal_data import Frame,json_values


class JsonTail:
    """Bounded cursor; partial records and truncation never become evidence."""
    def __init__(self,path,*,start_at_end=False,max_line=262144):
        self.path=Path(path);st=self.path.stat() if self.path.exists() else None
        self.offset=st.st_size if start_at_end and st else 0
        self.identity=st.st_ino if st else None;self.max_line=max_line;self.stats=Counter()

    def read(self,max_rows=2000):
        if not self.path.exists():return []
        st=self.path.stat();identity=st.st_ino
        if self.identity is not None and identity!=self.identity:
            self.offset=0;self.stats['ROTATED_WITH_POSSIBLE_GAP']+=1
        if st.st_size<self.offset:self.offset=0;self.stats['TRUNCATED_GAP']+=1
        self.identity=identity;rows=[]
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            for _ in range(max_rows):
                start=stream.tell();line=stream.readline(self.max_line+1)
                if not line:break
                if len(line)>self.max_line:
                    self.stats['OVERSIZE_LINE_GAP']+=1
                    while line and not line.endswith(b'\n'):line=stream.readline(self.max_line+1)
                    self.offset=stream.tell();continue
                if not line.endswith(b'\n'):break
                self.offset=stream.tell()
                try:rows.append(json.loads(line))
                except (ValueError,UnicodeError):self.stats['MALFORMED_JSON_GAP']+=1
        return rows


def session_check(source_root,at,day):
    """Read-only local readiness evidence; never starts/authenticates a worker.

    Persistent authorization, a GUI window and a fresh receiving worker are
    separate facts. This check cannot certify the full session from a tail.
    """
    from core.util import shared_binary_reader
    root=Path(source_root);problems=[]
    def read(name):
        try:
            with shared_binary_reader(root/name) as stream:return json.load(stream)
        except (OSError,ValueError):return {}
    def fresh(value,seconds=20):
        stamp=aware(value)
        return bool(stamp and stamp.date().isoformat()==day and 0<=(at-stamp).total_seconds()<=seconds)
    h=read('runtime/automation-production/heartbeat.json');p=read('runtime/automation_policy.json')
    receipt=read('runtime/operator_user_authorization.json')
    status=read('runtime/automation-production/trade-review/capture-status.json')
    identity=read('runtime/RELEASE_IDENTITY.json');cfg=read('config.json');auto=read('automation.json')
    if not fresh(h.get('time')):problems.append('NO_FRESH_TARGET_DAY_WORKER_HEARTBEAT')
    if h.get('connected') is not True:problems.append('RECEIVER_NOT_CONNECTED')
    if not fresh(status.get('at')):problems.append('NO_FRESH_TARGET_DAY_CAPTURE_STATUS')
    if status.get('storage_blocked') or status.get('capture_losses',0) or status.get('write_errors',0):
        problems.append('CAPTURE_LOSS_ERROR_OR_STORAGE_BLOCK')
    limits=status.get('limits',{});free=shutil.disk_usage(root).free
    daily_left=max(0,limits.get('day_bytes',0)-(status.get('day_bytes',0) if str(status.get('at',''))[:10]==day else 0))
    total_left=max(0,limits.get('total_bytes',0)-status.get('total_bytes',0))
    reserve=limits.get('min_free_bytes',0)
    if not limits or free-reserve<min(daily_left,total_left):problems.append('FREE_SPACE_CANNOT_COVER_REMAINING_CONFIGURED_DAY_BUDGET')
    paths=sorted((root/'runtime/automation-production/trade-review').glob('trade-review-'+day.replace('-','')+'-*.jsonl'))
    first_capture=None;last_trade=None;last_book=None
    # Bounded metadata/tail sample, never a claim to have scanned all tape.
    for path in ([paths[0]] if paths else []):
        with shared_binary_reader(path) as stream:
            for line in stream.read(262144).splitlines():
                try:row=json.loads(line)
                except ValueError:continue
                if row.get('event')=='CAPTURE_STARTED':first_capture=row.get('at');break
    for path in paths[-2:]:
        with shared_binary_reader(path) as stream:
            stream.seek(max(0,path.stat().st_size-262144));raw=stream.read(262144)
        for line in raw.splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            stamp=row.get('received_at')
            if row.get('event')=='TRADE' and stamp:last_trade=max(last_trade or stamp,stamp)
            if row.get('event')=='BOOK' and stamp:last_book=max(last_book or stamp,stamp)
    regular=day==at.date().isoformat() and '09:00:00'<=at.strftime('%H:%M:%S')<'15:30:00'
    if regular and (not fresh(last_trade,10) or not fresh(last_book,10)):problems.append('NO_RECENT_REGULAR_TRADE_AND_BOOK_IN_BOUNDED_TAIL')
    return {'at':at.isoformat(),'target_day':day,'live_release':identity.get('release_id'),
        'policy_mode':p.get('mode'),'persistent_live_authorization':p.get('live_authorized'),
        'approval_receipt_day':receipt.get('trading_day'),
        'receipt_targets_requested_day':receipt.get('trading_day')==day,
        'approval_validation':'USE_INSTALLED_READINESS_GATES_NOT_THIS_RESEARCH_SCHEMA',
        'authorization_is_current_receiving_proof':False,'gui_presence':'NOT_INSPECTED_NOT_RECEIPT_PROOF',
        'worker_heartbeat_at':h.get('time'),'worker_pid':h.get('pid'),'connected':h.get('connected'),
        'readiness_snapshot':h.get('readiness'),'capture_started_at':first_capture,
        'last_bounded_trade_receipt':last_trade,'last_bounded_book_receipt':last_book,
        'regular_receipt_status':'SAMPLED_RECENT_RECEIPTS' if regular and fresh(last_trade,10) and fresh(last_book,10) else 'UNKNOWN_OR_PREOPEN',
        'capture_status':status,'free_bytes':free,'reserved_free_bytes':reserve,
        'remaining_day_budget':daily_left,'remaining_total_budget':total_left,
        'watch_limit':cfg.get('max_watchlist'),'fast_enabled':auto.get('fast_momentum_enabled'),
        'full_regular_session_coverage':'UNKNOWN_REQUIRES_FROZEN_SEQUENCE_GAP_AND_SCOPE_AUDIT',
        'ready_for_target_day_capture':not problems,'problems':problems,'LIVE_modified':False,'actual_order_permission':False}


def freeze_session(source_root,output,day,preopen_at=None):
    """Freeze selected exact byte prefixes outside LIVE; no delete or startup.

    Use after-session at low priority. An incomplete directory without a final
    MANIFEST is not a complete freeze. Missing target-day capture fails closed.
    """
    from core.util import shared_binary_reader
    day_key=datetime.strptime(day,'%Y-%m-%d').strftime('%Y%m%d')
    root=Path(source_root).resolve();out=Path(output).resolve()
    if out==root or root in out.parents:raise ValueError('EXTERNAL_NEW_OUTPUT_REQUIRED')
    if out.exists():raise FileExistsError('PRESERVE_EXISTING_FREEZE')
    if preopen_at is not None:
        if (not isinstance(preopen_at,datetime) or preopen_at.tzinfo is None
                or preopen_at.date().isoformat()!=day or preopen_at.weekday()>=5
                or not '08:00:00'<=preopen_at.strftime('%H:%M:%S')<'09:00:00'):
            raise ValueError('TARGET_WEEKDAY_PREOPEN_CLOCK_REQUIRED')
        required=('config.json','automation.json','runtime/RELEASE_IDENTITY.json','runtime/automation-production/state.json')
        if any(not (root/n).is_file() for n in required):raise RuntimeError('PREOPEN_BASE_STATE_MISSING')
        paths=[root/'runtime/price_context_cache.json']
    else:
        paths=list((root/'runtime/automation-production/trade-review').glob('trade-review-'+day_key+'-*.jsonl'))
        if not paths:raise RuntimeError('TARGET_DAY_CAPTURE_NOT_FOUND_NO_COVERAGE_INVENTED')
        paths+=list((root/'runtime').glob('shadow_ticks.jsonl*'))
        paths+=list((root/'runtime/automation-production/archive').glob('events.jsonl.'+day_key+'*'))
        paths+=[root/'runtime/automation-production/events.jsonl',root/'runtime/radar_history.jsonl']
    paths+=[root/n for n in ('config.json','automation.json','observation.json','runtime/RELEASE_IDENTITY.json',
        'runtime/automation_policy.json','runtime/automation-production/heartbeat.json',
        'runtime/automation-production/state.json','runtime/operator_user_authorization.json',
        'runtime/automation-production/trade-review/capture-status.json')]
    extents=[(p,p.stat()) for p in sorted(set(paths)) if p.is_file()]
    size=sum(st.st_size for _,st in extents)
    if shutil.disk_usage(out.parent).free-size<32*1024**3:
        raise RuntimeError('EXTERNAL_FREEZE_WOULD_BREAK_32GiB_RESERVE_NO_COPY_PERFORMED')
    out.mkdir();records=[]
    for source,expected in extents:
        end=expected.st_size;relative=source.relative_to(root);dest=out/relative
        dest.parent.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256();read=0;complete_end=0
        with shared_binary_reader(source) as stream,dest.open('wb') as target:
            if os.fstat(stream.fileno()).st_ino!=expected.st_ino:raise RuntimeError('SOURCE_ROTATED_BEFORE_FREEZE')
            while read<end:
                block=stream.read(min(1024*1024,end-read))
                if not block:raise RuntimeError('SOURCE_ROTATED_OR_TRUNCATED_FREEZE_INCOMPLETE')
                target.write(block);digest.update(block);i=block.rfind(b'\n')
                if i>=0:complete_end=read+i+1
                read+=len(block)
        records.append({'relative':relative.as_posix(),'byte_end':end,
            'complete_jsonl_byte_end':complete_end if source.suffix=='.jsonl' or 'jsonl.' in source.name else None,
            'sha256':digest.hexdigest(),'frozen':str(dest)})
    result={'day':day,'at':datetime.now(KST).isoformat(),'files':records,'source_modified':False,
        'snapshot_kind':'PREOPEN_STATE_NOT_AN_ACCOUNT_OR_ORDER_APPROVAL' if preopen_at is not None else 'SESSION_EVIDENCE',
        'requested_preopen_at':preopen_at.isoformat() if preopen_at is not None else None,
        'coverage':'FULL_SELECTED_BYTE_PREFIXES_NOT_FULL_MARKET_CERTIFICATION'}
    (out/'MANIFEST.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


class ResearchEvidenceCollector:
    def __init__(self,output,source_root,*,clock=None,max_episodes=2,window_seconds=1800,
                 start_writer=True,day_bytes=16*1024*1024):
        output=Path(output).resolve();source_root=Path(source_root).resolve()
        if output==source_root or source_root in output.parents:raise ValueError('OUTPUT_MUST_BE_OUTSIDE_LIVE')
        if not 1<=max_episodes<=2 or not 60<=window_seconds<=1800:raise ValueError('BOUNDED_RESEARCH_LIMITS_REQUIRED')
        self.clock=clock or (lambda:datetime.now(KST));self.maximum=max_episodes;self.window=window_seconds
        self.capture=TradeReviewCapture(output,start=start_writer,clock=self.clock,day_bytes=day_bytes,
            total_bytes=64*1024*1024,queue_bytes=2*1024*1024,queue_rows=2048)
        self.active={};self.recent=deque(maxlen=256);self.watch={};self.stats=Counter()
        self.capture._record({'event':'RESEARCH_CAPTURE_SCOPE','at':self.clock(),
            'method':'FIRST_ARRIVAL_FORMED_OR_READY_NOT_POSTHOC_WINNERS',
            'max_parallel_episodes':max_episodes,'post_window_seconds':window_seconds,
            'window_is_observation_not_a_time_exit':True,'new_subscriptions':0,
            'reads_existing_journals_only':True,'actual_order_permission':False},force=True)

    def prune(self,at):
        ended=[key for key,row in self.active.items() if row['until']<=at]
        for key in ended:
            row=self.active.pop(key)
            self.capture._record({'event':'RESEARCH_EPISODE_END','at':at,'episode_id':key,
                'symbol':row['symbol'],'reason':'OBSERVATION_WINDOW_END_NOT_A_VIRTUAL_FILL',
                'coverage':'UNKNOWN_IF_CAPTURE_LOSS_OR_MISSING_SOURCE'},force=True)
        # Metadata/rings are bounded to the original 12-symbol recorder scope.
        for sym in list(self.watch):
            if (at-self.watch[sym]).total_seconds()>180:self.watch.pop(sym)
        self.capture.set_scope(list(self.watch)[:12],{},at)

    def candidate(self,symbol,pattern,at,evidence):
        basis=(symbol,pattern,evidence.get('entry'),evidence.get('stop'),evidence.get('target'))
        key=hashlib.sha256(repr(basis).encode()).hexdigest()[:24]
        if key in self.recent or key in self.active:self.stats['REPEATED_EVENT']+=1;return
        self.prune(at)
        if len(self.active)>=self.maximum:
            self.stats['EPISODE_CAPACITY_EXCLUDED']+=1;self.recent.append(key)
            self.capture._record({'event':'RESEARCH_EPISODE_EXCLUDED','episode_id':key,
                'symbol':symbol,'pattern':pattern,'at':at,'reason':'FIXED_OBSERVATION_CAPACITY',
                'actual_order_permission':False},force=True);return
        self.recent.append(key);self.active[key]={'symbol':symbol,'until':at+timedelta(seconds=self.window)}
        self.capture.promote(symbol,at)
        self.capture._record({'event':'RESEARCH_EPISODE','episode_id':key,'symbol':symbol,
            'at':at,'pattern':pattern,'evidence':evidence,'actual_order_permission':False,
            'coverage':'BOUNDED_PREBUFFER_AND_AVAILABLE_SOURCE_ONLY'},symbol,force=True)
        self.stats['INDEPENDENT_EPISODES_SELECTED']+=1

    def metadata(self,row):
        at=aware(row.get('asof') or row.get('processed_at') or row.get('at') or row.get('received_at'))
        if not at:return
        if row.get('event')=='OPPORTUNITY_SAMPLE':
            symbol=row['symbol'];self.watch[symbol]=at
            while len(self.watch)>12:
                self.watch.pop(min(self.watch,key=lambda s:self.watch[s]));self.stats['WATCH_METADATA_EVICTED']+=1
            self.prune(at)
            clean={k:v for k,v in row.items() if k not in {'capture_session','capture_sequence','capture_losses'}}
            clean['source_capture']={k:row.get(k) for k in ('capture_session','capture_sequence','capture_losses')}
            self.capture._record(clean,symbol,force=True)
            for trial in row.get('trials',[]):
                if trial.get('formed') or trial.get('ready'):self.candidate(symbol,trial.get('setup','UNKNOWN'),at,trial)
            fast=row.get('fast') or {}
            if fast.get('status') in {'SIGNAL','READY','PASS'}:self.candidate(symbol,'FAST',at,fast)
        elif row.get('event') in {'COMPLETED_BAR','STREAM_STATE','SUBSCRIPTION_SCOPE','ENTRY_BASIS','EVENT_REF','POSITION_LINES','BOOK','TRADE'}:
            # Link authoritative order/fee logs; never create a second ledger.
            clean={k:v for k,v in row.items() if k not in {'capture_session','capture_sequence','capture_losses'}}
            clean['source_capture']={k:row.get(k) for k in ('capture_session','capture_sequence','capture_losses')}
            self.capture._record(clean,row.get('symbol'),force=True)

    def frame(self,row):
        try:f=Frame.from_record(row)
        except (KeyError,TypeError,ValueError):self.stats['FRAME_AVAILABILITY_UNKNOWN']+=1;return
        self.prune(f.available_at);sym=f.tick.symbol
        active=any(r['symbol']==sym for r in self.active.values())
        if active:
            # Extending the capture is an observation window, never a hold or
            # sell instruction. Existing recorder queue/disk ceilings apply.
            self.capture.promoted[sym]=time.monotonic()+2.
        self.capture._record({**row,'event':'TRADE','processed_at':f.available_at.isoformat(),
            'availability_basis':'RECORDED_SOURCE_PROCESSING_NOT_SIDECAR_POLL_TIME'},sym)
        self.stats['SOURCE_TRADE_ROWS']+=1

    def close(self):
        self.capture._record({'event':'RESEARCH_CAPTURE_SUMMARY','at':self.clock(),
            'collector_counts':dict(self.stats),'recorder':self.capture.stats(),
            'orders_enabled':False,'actual_order_permission':False},force=True)
        return self.capture.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--duration-seconds',type=int,default=21600)
    p.add_argument('--include-existing',action='store_true',help='offline evidence copy; default starts at current EOF')
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--check-only',action='store_true',help='read local readiness/capture status only; no worker or API startup')
    p.add_argument('--day',default=None,help='target session YYYY-MM-DD for --check-only')
    modes.add_argument('--freeze-session',action='store_true',help='after-session exact-prefix freeze outside LIVE; no startup')
    modes.add_argument('--freeze-preopen',action='store_true',help='weekday 08:00-09:00 state snapshot outside LIVE; no approval/startup')
    a=p.parse_args(argv)
    if a.freeze_session or a.freeze_preopen:
        result=freeze_session(a.source_root,a.output,a.day or datetime.now(KST).date().isoformat(),
            preopen_at=datetime.now(KST) if a.freeze_preopen else None)
        print(json.dumps({'files':len(result['files']),'LIVE_modified':False}));return
    if a.check_only:
        root=a.source_root.resolve();out=a.output.resolve()
        if out==root or root in out.parents:p.error('check output must be outside LIVE')
        result=session_check(root,datetime.now(KST),a.day or datetime.now(KST).date().isoformat())
        out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'ready_for_target_day_capture':result['ready_for_target_day_capture'],'problems':result['problems'],'LIVE_modified':False}))
        return
    if not 1<=a.duration_seconds<=28800:p.error('duration must be 1..28800 seconds')
    if not (a.source_root/'runtime/shadow_ticks.jsonl').is_file():p.error('existing source journal not found; no live startup is attempted')
    collector=ResearchEvidenceCollector(a.output,a.source_root)
    tails={};initial=True;deadline=time.monotonic()+a.duration_seconds
    try:
        while time.monotonic()<deadline and not collector.capture.storage_blocked:
            day=datetime.now(KST).strftime('%Y%m%d')
            captures=sorted((a.source_root/'runtime/automation-production/trade-review').glob('trade-review-'+day+'-*.jsonl'))
            paths=[*captures,a.source_root/'runtime/shadow_ticks.jsonl']
            for path in paths:
                if path not in tails:
                    tails[path]=JsonTail(path,start_at_end=initial and not a.include_existing)
                for row in tails[path].read():
                    if path.name.startswith('shadow_ticks'):collector.frame(row)
                    else:collector.metadata(row)
            initial=False
            # Bounded metadata cadence, no polling of production credentials or
            # positions and no detector/order work in the LIVE process.
            time.sleep(.5)
    finally:
        gaps={str(k):dict(v.stats) for k,v in tails.items() if v.stats}
        collector.capture._record({'event':'SIDECAR_INPUT_GAPS','at':datetime.now(KST),
            'tail_gaps':gaps,'complete_coverage':False},force=True)
        collector.close()
    print(json.dumps({'output':str(a.output),'counts':dict(collector.stats),'LIVE_modified':False},ensure_ascii=False))


if __name__=='__main__':main()
