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
    a=p.parse_args(argv)
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
