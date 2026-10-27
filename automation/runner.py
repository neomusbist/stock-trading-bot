"""일봉/계좌 조회는 별도 스레드, 주문/체결/보호 상태는 주 스레드 하나에서 변경한다."""
import copy
import os
from contextlib import nullcontext
from operator_process import process_identity
import json
import hashlib
import queue
import threading
import time
from dataclasses import replace,asdict
from datetime import datetime
from pathlib import Path
from core.settings import now
from core.util import atomic_json,ProcessLock,exception_details
from automation.observation_retention import ObservationRetention
from core.broker import Rest,Rejected
from core.realtime import Feed,FeedMessageQueue,parse_tick,current_book_tick
_orig_feed_run = Feed.run

def _patched_feed_run(self):
    try:
        import websocket
        _orig_run_forever = websocket.WebSocketApp.run_forever
        def _safe_run_forever(ws_self, *args, **kwargs):
            kwargs['ping_interval'] = 0
            kwargs.pop('ping_timeout', None)
            return _orig_run_forever(ws_self, *args, **kwargs)
        websocket.WebSocketApp.run_forever = _safe_run_forever
    except Exception:
        pass
    return _orig_feed_run(self)

Feed.run = _patched_feed_run
from core.strategy import Tick
from core.market_time import market_freshness,tick_freshness
from core.report import export
from candidate_builder import structure
from automation.policy import Policy,load_automation,LEGACY_SYMBOL,BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,legacy_test_release_enabled
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import RadarWorker,observation_structure
from automation.allocation import comparison
from automation.entry_context_audit import review_entry_context
from automation.performance import report
from automation.session import supported,control_supported,assessment,market_phase,after_supported,after_control_supported,after_entry_supported
from automation.candidate_audit import CandidateAudit
from automation.entry_funnel import observe_evaluation
from automation.observation_settings import ObservationSettings,load_observation
from automation.focus_selection import select_focus
from automation.decisions import tick_source_identity

def error_record(error):
    record=exception_details(error)
    if isinstance(error,Rejected):
        record.update(error=str(error),api=error.api,raw_response=error.response,
            return_code=error.response.get('return_code') if error.response else None,
            return_msg=error.response.get('return_msg') if error.response else None)
    return record

def user_command(policy,command):
    with ProcessLock(policy.path.parent/'control.lock'): policy.command(command)

def check_diagnostic_event_writer(engine,writer):
    """Fail entry closed once, then keep fills/protection on the synchronous path."""
    if getattr(engine,'diagnostic_event_writer',None) is not writer:return
    try:writer.check_error()
    except OSError:
        engine.diagnostic_event_writer=None
        raise

def record_regular_tick_progress(engine,tick):
    """Historical FIFO ingestion cannot move global market evidence backwards."""
    prior=getattr(engine,'last_regular_tick',None)
    prior_at=getattr(prior,'time',None) or getattr(engine,'last_regular_tick_at',None)
    if isinstance(prior_at,datetime):
        if tick.time<prior_at:return False
        if tick.time==prior_at and prior is not None:
            old=getattr(prior,'received_at',None);new=getattr(tick,'received_at',None)
            if old and new and datetime.fromisoformat(new)<datetime.fromisoformat(old):return False
    engine.last_regular_tick_at=tick.time;engine.last_regular_tick=tick
    return True

class AccountWorker:
    def __init__(self,rest,interval):
        self.rest=rest; self.interval=interval; self.need_fills=False; self.q=queue.Queue(maxsize=2)
        self.stop=threading.Event(); self.request=threading.Event()
        self.external_symbols=set();self.external_prices={};self.last_book={};self.book_queries={}
        self.position_symbols=frozenset()
        self.position_context={}
        self.settlement_pending=False;self.settlement_interval=60.;self.last_fill_query=float('-inf')
        self.observation_settings=ObservationSettings()
        self.last_query_started=0.
        self.min_query_interval=1.0
        self.thread=threading.Thread(target=self.run,name='account-reader',daemon=True)
    def book_due(self,symbol,stamp=None):
        price=self.external_prices.get(symbol);stamp=time.monotonic() if stamp is None else stamp
        settings=self.observation_settings
        far=symbol in BREAKEVEN_EXITS and price and price<BREAKEVEN_EXITS[symbol]*(1-settings.near_threshold_fraction)
        return stamp-self.book_queries.get(symbol,float('-inf'))>=(settings.far_book_seconds if far else self.interval)
    def run(self):
        while not self.stop.is_set():
            now_mono=time.monotonic()
            elapsed=now_mono-self.last_query_started
            if elapsed<self.min_query_interval:
                time.sleep(self.min_query_interval-elapsed)
            started=time.monotonic(); query_started_at=now().isoformat()
            self.last_query_started=started
            try:
                fills_due=self.need_fills or (self.settlement_pending and started-self.last_fill_query>=self.settlement_interval)
                fills=self.rest.fills() if fills_due else []
                if fills_due:self.last_fill_query=started
                snapshot=self.rest.snapshot()
                snapshot['query_started_at']=query_started_at
                snapshot['books']={}
                snapshot['book_errors']={}
                book_started=time.perf_counter();book_calls=0
                for sym in sorted((set(self.external_symbols)|set(self.position_symbols))&set(snapshot['holdings'])):
                    context=self.position_context.get(sym)
                    due=self.book_due(sym) or sym in self.position_symbols
                    if not due:
                        if sym in self.last_book:snapshot['books'][sym]=dict(self.last_book[sym])
                        continue
                    try:
                        snapshot['books'][sym]=(self.rest.position_book(sym,**context) if context else self.rest.existing_book(sym))
                        self.last_book[sym]=dict(snapshot['books'][sym])
                        self.book_queries[sym]=time.monotonic();book_calls+=1
                    except (Rejected,ValueError,KeyError,RuntimeError,OSError) as e:
                        snapshot['book_errors'][sym]=error_record(e)  # 오류 원문 보존; 누락 호가는 HOLD
                snapshot['performance']={'account_ms':(time.monotonic()-started)*1000,'external_book_ms':(time.perf_counter()-book_started)*1000,'external_book_calls':book_calls}
                result={'snapshot':snapshot,'fills':fills,'started':started,'error':None}
            except Exception as e: result={**error_record(e),'started':started}
            if self.q.full(): self.q.get_nowait()
            self.q.put_nowait(result)
            self.request.wait(self.interval); self.request.clear()
    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.request.set(); self.thread.join(timeout=1)

class Reporter:
    def __init__(self,root):
        self.root=Path(root); self.q=queue.Queue(maxsize=1); self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='report-writer',daemon=True)
    def submit(self,rows,state):
        if self.q.full(): return
        self.q.put_nowait((copy.deepcopy(rows),copy.deepcopy(state)))
    def run(self):
        while not self.stop.is_set() or not self.q.empty():
            try: rows,state=self.q.get(timeout=.25)
            except queue.Empty: continue
            try:
                atomic_json(self.root/'performance.json',report(state['trades']))
                export(self.root,rows,state)
            except (OSError,ValueError,TypeError) as e:
                record={'time':now().isoformat(),**error_record(e)}
                try: atomic_json(self.root/'runtime/report_error.json',record)
                except OSError as logging_error:
                    import sys
                    print(json.dumps({'event':'REPORT_ERROR','original':record,'logging_error':error_record(logging_error)},ensure_ascii=False),file=sys.stderr,flush=True)
    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.thread.join(timeout=5)

def update_market(engine,value):
    if value not in {'0','3','2','4','8','9','a','b','c','d'}: return
    engine.market=value

def request_capital_refresh(engine,account,in_session):
    ledger=engine.s.get('automation',{})
    if in_session and (ledger.get('capital_refresh_pending') or ledger.get('reconcile_deferred_pending')):
        ledger['reconcile_deferred_pending']=False
        account.request.set()
        return True
    return False

def submit_candidate_observation(engine,shadow,rows,stamp,capital):
    """Keep cached observation live while orders/positions reserve API priority.

    Holding a position must not suppress the early RAW packet entirely. The
    existing worker can admit fresh verified metadata without REST; unfamiliar
    or stale rows remain UNKNOWN until the ordinary radar verifies them.
    """
    worker=shadow() if callable(shadow) else shadow
    if worker is None: return
    worker.submit(rows,stamp,capital,
                  allow_metadata=not bool(engine.s['positions'] or engine.active_orders()))

def bind_candidate_observation(radar,shadow,engine):
    radar.observation_sink=lambda rows,stamp,capital:submit_candidate_observation(engine,shadow,rows,stamp,capital)
    radar.observation_allowed=True

def qualify_raw(engine,rows,stamp,is_raw_phase=False):
    engine.source_qualification_seen=True
    current_qual=getattr(engine,'source_qualification',{})
    new_qual={}
    for row in rows:
        sym=row['code']
        fb=bool(row.get('fallback_source1'))
        if is_raw_phase and not fb and sym in current_qual:
            fb=bool(current_qual[sym].get('fallback_source1'))
        new_qual[sym]={
            'source_count':row.get('source_count',0),
            'timestamp':stamp,
            'economic_reason':row.get('affordability',{}).get('reason_code'),
            'fallback_source1':fb
        }
    engine.source_qualification=new_qual

def record_opening_focus_change(engine,selection):
    """A verified progress update is not necessarily a WATCH membership change."""
    if not selection.applied:return False
    engine.event('FOCUS_SELECTION_APPLIED',basis='OPENING_VERIFIED_PROGRESS',
        promoted=selection.promoted,demoted=selection.demoted,
        next_watch=selection.next_watch,previous_watch=selection.previous_watch,
        primary_reason=selection.primary_reason,applied_at=selection.focus_epoch,
        tier_map=selection.tier_map,priority_tuples=selection.priority_tuples,
        compression_batch_complete=False)
    return True


def apply_qualified_progress(engine,lookup,rows,result,readiness,cfg,auto,stamp):
    """Feed already verified opening candidates to the existing FOCUS selector.

    A partial batch cannot demote unprocessed incumbents. RAW, metadata UNKNOWN,
    stale results and diagnostic virtual signals never grant WATCH eligibility.
    """
    try:source=datetime.fromisoformat(result['source_observed_at'])
    except (KeyError,TypeError,ValueError):return rows,None
    if (not supported(stamp) or not '09:00:00'<=stamp.strftime('%H:%M:%S')<'09:10:00'
            or source.tzinfo is None or source.date()!=stamp.date()
            or not -1<=(stamp-source).total_seconds()<=auto.scan_seconds*3):return rows,None
    verified=[r for r in result.get('rows',[]) if r.get('status')=='ELIGIBLE'
              and r.get('market_status',{}).get('pass') is True
              and r.get('affordability',{}).get('entry_eligible') is True]
    if not verified:return rows,None
    pool={r['code']:dict(r) for r in rows}
    for sym,row in lookup.items():pool.setdefault(sym,dict(row))
    for row in verified:pool[row['code']]=dict(row)
    for row in result.get('raw_rows',[]):
        if isinstance(row,dict) and row.get('code'):pool.setdefault(row['code'],dict(row))
    merged=list(pool.values())
    selection=resolve_radar_focus(engine,lookup,merged,[],readiness,cfg,auto,stamp)
    for row in merged:
        if row['code'] in lookup:row.update(status='WATCH',fallback_source1=lookup[row['code']]['fallback_source1'])
        elif row.get('status')=='WATCH':row.update(status='RESERVE',fallback_source1=False)
        engine.names[row['code']]=row.get('name')
        engine.market_status[row['code']]=row.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
    # Full ranking lineage is retained even when RAW was coalesced in the bounded
    # two-packet radar queue. One-source fallback is the existing FOCUS permission.
    raw=[{**r,'fallback_source1':bool(lookup.get(r['code'],{}).get('fallback_source1'))}
         for r in result.get('raw_rows',[])]
    qualify_raw(engine,raw,result['source_observed_at'],is_raw_phase=True)
    timeline=getattr(engine,'observation_timeline',None)
    if timeline is not None:timeline.raw(raw,stamp)
    return merged,selection

class SubscriptionState:
    """구독 변경은 소켓/계좌 연결과 별개이며 추가 종목만 새 정규장 틱을 기다린다."""
    def __init__(self,symbols=()):
        self.symbols=set(symbols); self.waiting={}; self.detached=set(); self.detached_at={}; self.refresh_pending=False

    def disconnected(self,stamp):
        self.waiting={sym:stamp for sym in self.symbols}
        self.detached.clear(); self.detached_at.clear(); self.refresh_pending=False

    def acknowledge(self):
        self.refresh_pending=False

    def transport_failed(self,engine,detector,feed,stamp):
        if getattr(feed,'transport_failed',False) is not True:return
        # Block before another order evaluation in this same cycle. A true
        # socket failure still resets observation and requires account recovery.
        engine.disconnect();engine.account_ok=False;engine.reset_detector=True
        detector.reset();self.disconnected(stamp)
        engine.event('SUBSCRIPTION_TRANSPORT_LOST',worker_restart_required=False)

    def gap_expired(self,detector,symbol,stamp,limit_seconds=None):
        last=detector.last.get(symbol)
        if last and last.time.date()!=stamp.date():return True
        detached_at=self.detached_at.get(symbol)
        limit = limit_seconds if limit_seconds is not None else detector.cfg.stale_seconds
        # A quiet stock's last trade age is not the length of a subscription gap.
        return bool(detached_at and (stamp-detached_at).total_seconds()>limit
                    and not detector.observation_fresh(symbol,stamp))

    def refresh(self,engine,detector,feed,desired,stamp,grace_seconds=None):
        desired=set(desired)
        for sym in list(self.detached-desired):
            if self.gap_expired(detector,sym,stamp,limit_seconds=grace_seconds):
                gap=(stamp-self.detached_at[sym]).total_seconds()
                detector.forget(sym); self.detached.discard(sym);self.detached_at.pop(sym,None)
                engine.event('OBSERVATION_RELEASED',symbol=sym,data_action='RESET_STALE_GAP',
                             subscription_gap_seconds=gap,stored_completed_bars=len(detector.history.get(sym,[])))
        if desired==self.symbols:
            if feed.refresh_exchange() is not True:return False
            # Same observation universe, new exchange stream: preserve bars but
            # require a fresh post-subscription tick before any entry.
            self.waiting.update({sym:stamp for sym in desired});self.refresh_pending=True
            timeline=getattr(engine,'observation_timeline',None)
            if timeline is not None:timeline.subscription_request(desired,stamp)
            self.transport_failed(engine,detector,feed,stamp)
            engine.event('SUBSCRIPTION_REFRESH',added=[],removed=[],reason_code='EXCHANGE_STREAM_CHANGED',
                         connected=engine.connected,account_ok=engine.account_ok)
            return True
        added=desired-self.symbols; removed=self.symbols-desired
        for sym in removed:
            self.waiting.pop(sym,None)
            self.detached.add(sym);self.detached_at[sym]=stamp
            if hasattr(detector,'mark_coverage_gap'):detector.mark_coverage_gap(sym,stamp)
            fast=getattr(engine,'fast_strategy',None)
            if fast is not None:fast.forget(sym,'SUBSCRIPTION_GAP')
            engine.event('OBSERVATION_RELEASED',symbol=sym,data_action='RETAIN_UNTIL_STALE_GAP',
                stored_completed_bars=len(detector.history.get(sym,[])))
        for sym in added:
            had_data=sym in detector.last
            fresh=detector.observation_fresh(sym,stamp)
            expired=had_data and self.gap_expired(detector,sym,stamp,limit_seconds=grace_seconds)
            since=self.detached_at.get(sym)
            gap=(stamp-since).total_seconds() if since else 0.0
            if expired:detector.forget(sym)
            elif hasattr(detector,'preload_from_context'):detector.preload_from_context(sym,stamp)
            self.detached.discard(sym);self.detached_at.pop(sym,None);self.waiting[sym]=stamp
            engine.event('OBSERVATION_REJOINED',symbol=sym,
                data_action='RESET_STALE_GAP' if expired else 'RETAIN_FRESH' if fresh else 'RETAIN_OBSERVATION' if had_data else 'NO_DATA',
                subscription_gap_seconds=gap,stored_completed_bars=len(detector.history.get(sym,[])),
                fresh_regular_tick_required=True)
        feed.update_symbols(sorted(desired))
        self.symbols=desired; self.refresh_pending=True
        timeline=getattr(engine,'observation_timeline',None)
        if timeline is not None:timeline.subscription_request(added,stamp)
        self.transport_failed(engine,detector,feed,stamp)
        engine.event('SUBSCRIPTION_REFRESH',added=sorted(added),removed=sorted(removed),
            connected=engine.connected,account_ok=engine.account_ok)
        return True

    def tick_ready(self,t,received):
        since=self.waiting.get(t.symbol)
        return since is None or (t.regular and t.time.date()==since.date() and t.time>=since and received>=since)

    def accepted(self,symbol):
        self.waiting.pop(symbol,None)

class DiagnosticSnapshotWriter:
    """Two latest-only diagnostic snapshots; no ledger or order writes.

    A snapshot keeps its capture time. Delayed disk publication cannot make
    stale readiness fresh. The main loop must check errors and must join this
    writer before publishing its terminal STOP snapshot directly.
    """
    def __init__(self,paths,*,write=None,retry_seconds=.25,timeline_path=None):
        self.paths=frozenset(Path(p).absolute() for p in paths)
        if len(self.paths)!=2 or {p.name for p in self.paths}!={'heartbeat.json','candidate_summary.json'}:
            raise ValueError('EXACT_DIAGNOSTIC_PATHS_REQUIRED')
        if timeline_path is not None:
            timeline_path=Path(timeline_path).absolute()
            candidate=next(p for p in self.paths if p.name=='candidate_summary.json')
            if timeline_path.name!='observation_timeline.json' or timeline_path.parent!=candidate.parent:
                raise ValueError('EXACT_DIAGNOSTIC_TIMELINE_PATH_REQUIRED')
            self.paths=self.paths|{timeline_path}
        self.write=atomic_json if write is None else write
        self.retry_seconds=max(.001,float(retry_seconds))
        self.condition=threading.Condition();self.pending={};self.retry_at={};self.failures={}
        self.closing=False;self.inflight=False;self.error=None
        self.stats={'submitted':0,'writes':0,'errors':0,'coalesced':0,'last_write_seconds':0.}
        self.thread=threading.Thread(target=self.run,daemon=True,name='diagnostic-snapshots')
        self.thread.start()

    def submit(self,path,value):
        path=Path(path).absolute()
        if path not in self.paths:raise ValueError('UNREVIEWED_DIAGNOSTIC_PATH')
        snapshot=copy.deepcopy(value)
        with self.condition:
            if self.closing:raise RuntimeError('DIAGNOSTIC_WRITER_CLOSED')
            self.stats['submitted']+=1
            self.stats['coalesced']+=path in self.pending
            self.pending[path]=snapshot
            self.condition.notify_all()

    def check_error(self):
        with self.condition:
            failure=self.error;self.error=None
        if failure is not None:
            path,error=failure
            if isinstance(error,OSError):raise error
            raise OSError(None,'DIAGNOSTIC_SNAPSHOT_WRITE_FAILED: '+str(error),str(path)) from error

    def snapshot(self):
        with self.condition:
            return {**self.stats,'pending':len(self.pending),'inflight':self.inflight,
                    'closing':self.closing,'error_pending':self.error is not None}

    def run(self):
        while True:
            with self.condition:
                while not self.pending:
                    if self.closing:return
                    self.condition.wait()
                current=time.monotonic()
                due=[p for p in self.pending if self.closing or self.retry_at.get(p,0)<=current]
                if not due:
                    self.condition.wait(max(.001,min(self.retry_at[p]-current for p in self.pending)))
                    continue
                path=due[0];value=self.pending.pop(path);self.inflight=True
            started=time.perf_counter()
            try:
                self.write(path,value,fsync=False)
            except Exception as error:
                with self.condition:
                    self.stats['errors']+=1
                    if self.error is None:self.error=(path,error)
                    self.failures[path]=self.failures.get(path,0)+1
                    self.retry_at[path]=time.monotonic()+min(2.,self.retry_seconds*2**min(self.failures[path]-1,4))
                    # Never replace a more recent captured snapshot with a
                    # failed older one, and never retry forever on shutdown.
                    if not self.closing and path not in self.pending:self.pending[path]=value
            else:
                with self.condition:
                    self.stats['writes']+=1;self.retry_at.pop(path,None);self.failures.pop(path,None)
            finally:
                with self.condition:
                    self.inflight=False;self.stats['last_write_seconds']=time.perf_counter()-started
                    self.condition.notify_all()

    def close(self,timeout=5.):
        with self.condition:
            self.closing=True;self.condition.notify_all()
        self.thread.join(timeout=max(0.,timeout))
        # False means the caller MUST NOT race the in-flight filesystem write
        # with a direct STOP/summary write. Preserve the old timestamp so the
        # normal heartbeat expiry remains fail-closed.
        return not self.thread.is_alive()


class PriceContextCheckpoint:
    """One writer and one replaceable pending reference snapshot, never a ledger.

    Cache recovery is best effort; unlike order INTENTs it has never been an
    authority for fills, cash or tick warmup. Keep atomic fsync checkpoints but
    take their disk latency off the order/position event loop.
    """
    def __init__(self,path):
        self.path=Path(path);self.condition=threading.Condition()
        self.pending=None;self.latest=None;self.stopping=False;self.inflight=False
        self.generation=0;self.written_generation=0
        self.stats={'submitted':0,'writes':0,'errors':0,'last_error':None,'last_write_seconds':0.}
        self.thread=threading.Thread(target=self.run,daemon=True,name='reference-cache-checkpoint')
        self.thread.start()

    def submit(self,snapshot):
        # Ownership transfers to the writer. The caller supplies a detached
        # snapshot, never live PriceContext rows (see snapshot_reference_cache).
        with self.condition:
            if self.stopping:raise RuntimeError('REFERENCE_CACHE_CLOSED')
            self.generation+=1;self.latest=(self.generation,snapshot)
            self.pending=self.latest;self.stats['submitted']+=1
            self.condition.notify_all()

    def run(self):
        while True:
            with self.condition:
                while self.pending is None and not self.stopping:self.condition.wait()
                if self.pending is None:return
                generation,snapshot=self.pending;self.pending=None;self.inflight=True
            started=time.perf_counter()
            try:
                payload=snapshot
                if isinstance(snapshot.get('rows'),dict):
                    payload={**snapshot,'rows':{sym:{**row,'bars':[
                        {k:v.isoformat() if isinstance(v,datetime) else v for k,v in bar.items()}
                        for bar in row['bars']]} for sym,row in snapshot['rows'].items()}}
                atomic_json(self.path,payload,fsync=True)
                with self.condition:
                    self.written_generation=generation;self.stats['writes']+=1
                    self.stats['last_error']=None
            except Exception as error:
                with self.condition:
                    self.stats['errors']+=1;self.stats['last_error']=type(error).__name__
            finally:
                with self.condition:
                    self.inflight=False;self.stats['last_write_seconds']=time.perf_counter()-started
                    self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {**self.stats,'pending':int(self.pending is not None),'inflight':self.inflight,
                    'submitted_generation':self.generation,'written_generation':self.written_generation}

    def close(self,timeout=1.):
        with self.condition:
            # Retry the latest failed snapshot once at clean shutdown. Never
            # spin or hold up position protection on a permanently failed disk.
            if not self.inflight and self.pending is None and self.written_generation<self.generation:
                self.pending=self.latest
            self.stopping=True;self.condition.notify_all()
        self.thread.join(timeout=max(0.,timeout))
        with self.condition:
            return not self.thread.is_alive() and self.written_generation==self.generation


def restore_reference_cache(context,cached,asof):
    """Restore reference provenance/availability without restoring tick warmup."""
    if not isinstance(cached,dict) or cached.get('day')!=asof.date().isoformat():return 0
    sanitized={'day':cached['day'],'rows':{}};metadata={}
    for sym,row in list(cached.get('rows',{}).items())[:context.limit]:
        if not isinstance(row,dict):continue
        received=row.get('history_received_at');stamp=None
        try:
            stamp=datetime.fromisoformat(received) if received else None
            if stamp and (stamp.tzinfo is None or stamp.date()!=asof.date() or stamp>asof):continue
        except (TypeError,ValueError):continue
        bars=[]
        for raw in row.get('bars',[]):
            if not isinstance(raw,dict):continue
            available=raw.get('available_at') or received
            if raw.get('observation_source')=='REST_REFERENCE' and not available:continue
            if available:
                try:
                    known=datetime.fromisoformat(available)
                    if known.tzinfo is None or known.date()!=asof.date() or known>asof:continue
                except (TypeError,ValueError):continue
            bars.append(raw)
        # A legacy cache can have per-bar availability without a row receipt.
        # Keep the latest actual receipt on the row because reference_bars uses
        # that field to prevent a later REST result from entering an earlier asof.
        known=([received] if received else [])+[b.get('available_at') for b in bars
            if b.get('observation_source')=='REST_REFERENCE' and b.get('available_at')]
        if known:row={**row,'history_received_at':max(known,key=datetime.fromisoformat)}
        sanitized['rows'][sym]={**row,'bars':bars};metadata[sym]=(row,bars)
    recovered=context.load_cache(sanitized,asof)
    for sym,(saved,bars) in metadata.items():
        row=context.rows.get(sym)
        if row is None:continue
        if saved.get('context_venue') in {'KRX','NXT','SOR'}:row['context_venue']=saved['context_venue']
        if saved.get('history_received_at'):row['history_received_at']=saved['history_received_at']
        available={b['time']:b.get('available_at') or saved.get('history_received_at') for b in bars}
        for bar in row['bars']:
            known=available.get(bar['time'].isoformat())
            if known:bar['available_at']=known
    return recovered


def snapshot_reference_cache(detector):
    with detector._state_lock:
        context=detector.price_context
        if not context.day:return {}
        # Native datetimes and scalar fields are immutable. Copy the bounded
        # records under the detector lock, then perform ISO conversion and JSON
        # serialization only on the single checkpoint writer.
        snapshot={'day':context.day.isoformat(),'rows':{sym:{
            'bars':[dict(bar) for bar in row['bars']],'peaks':[dict(p) for p in row['peaks']],
            'first_observed_at':row['first_observed_at'],'last_complete_at':row['last_complete_at'],
            'context_venue':row.get('context_venue'),'history_received_at':row.get('history_received_at')}
            for sym,row in context.rows.items()}}
    return snapshot


def drain_intraday_context(engine,detector,shadow,worker,targets,venue,asof,checkpoint):
    """Bounded REST references for WATCH and SHADOW, with no tick warmup."""
    changed=False;processed=0
    shadow_detector=getattr(getattr(shadow,'observer',None),'detector',None)
    while processed<14:
        try:packet=worker.q.get_nowait()
        except queue.Empty:break
        processed+=1
        try:
            sym=packet['symbol'];received=datetime.fromisoformat(packet['received_at'])
            if (sym not in targets or packet['venue']!=venue or received.tzinfo is None
                    or received.date()!=asof.date() or received>asof):continue
        except (KeyError,TypeError,ValueError):continue
        if not packet.get('error'):
            for target in (detector,shadow_detector):
                if target is None:continue
                with target._state_lock:
                    target.price_context.merge_history(sym,packet['bars'],received,venue)
                    row=target.price_context.rows.get(sym)
                    if row:
                        # PriceContext.record intentionally keeps only structural
                        # fields; retain actual REST availability at this boundary.
                        rest_times={b['time'] for b in packet['bars']}
                        for bar in row['bars']:
                            if bar['time'] in rest_times:bar['available_at']=received.isoformat()
            changed=True
            capture=getattr(engine,'trade_review',None)
            if capture is not None:
                try:
                    with detector._state_lock:
                        reference_rows=[dict(b) for b in detector.price_context.reference_bars(sym,asof,venue)]
                    capture.reference_snapshot(sym,reference_rows,received,asof,venue)
                except Exception:pass  # Reference diagnostics cannot change order/protection behavior.
        engine.event('INTRADAY_REFERENCE_READY' if not packet.get('error') else 'INTRADAY_REFERENCE_PENDING',
            symbol=sym,venue=venue,received_at=packet['received_at'],known_bars=len(packet.get('bars',[])),
            source='REST_REFERENCE',restored_signal_bars=0,restored_warmup=False,error=packet.get('error'))
    if changed:
        checkpoint.submit(snapshot_reference_cache(detector))
    return processed


def _protected_feed_work_pending(feed,critical):
    main_q=getattr(feed,'q',None)
    if isinstance(main_q,FeedMessageQueue):return main_q.protected_pending(critical)
    if not isinstance(main_q,queue.Queue):return False
    with main_q.mutex:messages=tuple(item[1] for item in main_q.queue)
    return any(message.get('trnm') in {'REG','ERROR','DISCONNECTED'} or
        any(item.get('type') in {'00','0s'} or item.get('item') in critical
            for item in message.get('data',[])) for message in messages)


def prewarm_handoff(engine,detector,feed,observed,real_symbols,prior_priority,drops,sink=None,shadow_detector=None,
                    on_opportunity=None,opportunity_probe=None):
    """Preserve FIFO history and brief signals; trade only on current evidence.

    The callback uses existing order guards. Diagnostic depth/ticks stay off
    the protected order/position queue; no worker gains order authority.
    """
    priority=set(real_symbols)
    if shadow_detector is None:
        if hasattr(sink,'__self__') and not str(type(sink)).startswith("<class 'unittest.mock."):
            worker = getattr(sink, '__self__', None)
            shadow_detector = getattr(getattr(worker, 'observer', None), 'detector', None)
        elif hasattr(sink, 'shadow_detector') and not str(type(sink)).startswith("<class 'unittest.mock."):
            shadow_detector = sink.shadow_detector
    critical=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
    critical.update(set(engine.s['automation']['existing'])&set(BREAKEVEN_EXITS))
    # Defer the whole handoff, including queued promotion history, when a
    # protected/control frame is waiting. Keep the old routing epoch so the
    # same promotion can resume after the main FIFO handles that frame.
    if _protected_feed_work_pending(feed,critical):return drops,set(prior_priority)
    # The caller has already removed the new WATCH/critical set from
    # `observed`. Use the receiver's PREVIOUS routing scope as well, otherwise
    # this branch is empty in production and queued prewarm prints are dropped
    # exactly when their symbol becomes WATCH.
    prior_scope=getattr(feed,'observation_symbols',())
    prior_observed=set(prior_scope) if isinstance(prior_scope,(set,frozenset,list,tuple)) else set()
    promotions=(priority-set(prior_priority))&(set(observed)|prior_observed)-critical
    demotions=(set(prior_priority)-priority)&observed-critical
    copied_promotions=set()
    if shadow_detector:
        for sym in promotions:
            if detector.handoff_from(shadow_detector,sym):copied_promotions.add(sym)
        for sym in demotions:
            shadow_detector.handoff_from(detector, sym)
    if not isinstance(getattr(feed,'observation_q',None),queue.Queue):
        if hasattr(feed,'configure_observation'):feed.configure_observation(priority,observed,sink)
        return drops,priority
    pending=getattr(feed,'_opportunity_pending',None)
    if pending is None:pending={};feed._opportunity_pending=pending
    triggers=getattr(feed,'_opportunity_triggers',None)
    if triggers is None:triggers={};feed._opportunity_triggers=triggers
    evaluated=getattr(feed,'_opportunity_evaluated',{})
    for sym in list(pending):
        if sym not in observed|priority or sym in critical:pending.pop(sym,None)
    for sym in list(evaluated):
        if sym not in observed|priority:evaluated.pop(sym,None)
    for sym in list(triggers):
        if sym not in observed or sym in priority or sym in critical:triggers.pop(sym,None)
    promoted=[]
    gap_event=None
    with (getattr(feed,'subscription_lock',None) or nullcontext()):
        if feed.observation_drops!=drops:
            drops=feed.observation_drops
            gaps=getattr(feed,'observation_gaps',None)
            affected=dict(gaps) if isinstance(gaps,dict) else {sym:engine.clock() for sym in observed-priority-critical}
            if isinstance(gaps,dict):gaps.clear()
            gap_event=(drops,affected)
        feed.configure_observation(priority,observed,sink)
    if gap_event is not None:
        # Snapshot/clear drops and change routing atomically, but never hold the
        # receiver lock while detector locks or diagnostic disk I/O can wait.
        # New receiver gaps remain in feed.observation_gaps for the next pass.
        pending.clear()
        triggers.clear()
        gap_drops,affected=gap_event
        for sym,gap_at in affected.items():
            if sym not in observed or sym in priority or sym in critical:continue
            if hasattr(detector,'mark_coverage_gap'):detector.mark_coverage_gap(sym,gap_at,'OBSERVATION_QUEUE_GAP')
            fast=getattr(engine,'fast_strategy',None)
            if fast is not None:fast.forget(sym,'OBSERVATION_QUEUE_GAP')
        engine.event('OBSERVATION_DATA_GAP',SHADOW_ONLY=True,dropped=gap_drops,symbols=sorted(affected),
            data_action='INVALIDATE_ENTRY_CONTINUITY_RETAIN_COMPLETED_BARS',actual_order_permission=False)
    if promotions and hasattr(feed, 'observation_q'):
        # Partition under the queue lock. Pop-and-requeue can put new receiver
        # frames before retained history, or drop history when the queue fills.
        observation_q=feed.observation_q
        with observation_q.mutex:
            remaining=[]
            for item in observation_q.queue:
                (promoted if item[1].symbol in promotions else remaining).append(item)
            observation_q.queue.clear();observation_q.queue.extend(remaining)
            observation_q.unfinished_tasks-=len(promoted)
            if not observation_q.unfinished_tasks:observation_q.all_tasks_done.notify_all()
            observation_q.not_full.notify_all()
    for index,(received,tick) in enumerate(promoted):
        accepted=detector.ingest(tick,received)
        if getattr(engine,'fast_strategy',None) is not None:
            engine.fast_strategy.ingest_tick(tick,received)
        gap=getattr(detector,'coverage_gaps',{}).get(tick.symbol)
        copied=(tick.symbol in copied_promotions and detector.last.get(tick.symbol)==tick
                and (not gap or received>gap['at']))
        if (accepted or copied) and on_opportunity is not None:pending[tick.symbol]=(received,tick)
        if critical and (index+1)%64==0:engine.timer(engine.prices)
    if promoted:
        engine.event('OBSERVATION_PREWARM_HANDOFF',symbols=sorted(promotions),queued_ticks=len(promoted),
            SHADOW_ONLY=True,actual_order_permission=False)
    drained = 0
    drain_deadline = time.monotonic() + 0.015
    while drained < 512 and time.monotonic() < drain_deadline:
        if getattr(feed,'transport_failed',False) or feed.overflow.is_set():break
        if _protected_feed_work_pending(feed,critical):break
        try: observed_at, tick = feed.observation_q.get_nowait()
        except queue.Empty: break
        feed.observation_q.task_done()
        drained += 1
        if tick.symbol in observed and tick.symbol not in priority and tick.symbol not in critical:
            accepted=detector.ingest(tick, observed_at)
            if getattr(engine,'fast_strategy',None) is not None:
                engine.fast_strategy.ingest_tick(tick, observed_at)
            if accepted and on_opportunity is not None:
                pending[tick.symbol]=(observed_at,tick)
                # Preserve the first observed valid prefix as evidence, not
                # permission. Already received later prints may invalidate it.
                if (opportunity_probe is not None and engine.connected
                        and tick_freshness(tick,engine.clock(),engine.c.stale_seconds)['fresh']
                        and opportunity_probe(tick,observed_at)):
                    if tick.symbol not in triggers:
                        triggers[tick.symbol]={'trigger_tick_at':tick.time.isoformat(),
                            'trigger_received_at':observed_at.isoformat(),'detected_at':engine.clock().isoformat()}
                        engine.event('OBSERVATION_SIGNAL_QUEUED',symbol=tick.symbol,**triggers[tick.symbol],
                            actual_order_permission=False)
    # History ingestion and evaluation have separate existing bounded budgets.
    # A fully used 15ms ingestion slice must not eliminate the 2ms/2-evaluation
    # opportunity slice. Protected/control frames are rechecked by the drain.
    drain_observed_opportunities(engine,feed,on_opportunity)
    return drops, priority

def drain_observed_opportunities(engine,feed,on_opportunity,*,deadline=None,max_evaluations=2):
    """At most two fresh evaluations; controls and position ticks go first."""
    if on_opportunity is None:return 0
    pending=getattr(feed,'_opportunity_pending',{})
    if not isinstance(pending,dict):return 0
    completed=getattr(feed,'_opportunity_evaluated',None)
    if completed is None:completed={};feed._opportunity_evaluated=completed
    counters=getattr(feed,'_opportunity_delivery',None)
    if counters is None:counters={};feed._opportunity_delivery=counters
    def counted(reason):counters[reason]=counters.get(reason,0)+1
    deadline=time.monotonic()+.05 if deadline is None else deadline
    evaluated=0
    observation_q=getattr(feed,'observation_q',None)
    queued_symbols=set()
    if isinstance(observation_q,queue.Queue):
        with observation_q.mutex:
            queued_symbols={item[1].symbol for item in observation_q.queue}
    inspected=0
    pending_count=len(pending)
    while on_opportunity is not None and pending and inspected<pending_count and evaluated<max_evaluations and time.monotonic()<deadline:
        # The preceding callback may have created an order or position. Refresh
        # protection before considering the next candidate in this same pass.
        state=getattr(engine,'s',{})
        critical=set(state.get('positions',{}))|{o['symbol'] for o in engine.active_orders()}
        critical.update(set(state.get('automation',{}).get('existing',{}))&set(BREAKEVEN_EXITS))
        if (getattr(feed,'transport_failed',False) or feed.overflow.is_set()
                or _protected_feed_work_pending(feed,critical)):
            counted('PROTECTED_OR_TRANSPORT_DEFERRED');break
        sym=next(iter(pending));observed_at,tick=pending.pop(sym)
        inspected+=1
        latest=getattr(feed,'latest_ticks',{}).get(sym)
        # A FIFO prefix is useful history, but it cannot hide later data that
        # has already reached this receiver. Wait until this symbol catches up.
        if sym in queued_symbols or (latest is not None and
                (latest.time,latest.cumulative)!=(tick.time,tick.cumulative)):
            if latest is not None and getattr(engine,'_last_eval_frame',{}).get(sym)==(latest.time,latest.cumulative):
                counted('ALREADY_EVALUATED');continue
            counted('HISTORY_PENDING')
            pending[sym]=(observed_at,tick)
            continue
        if not tick_freshness(tick,engine.clock(),engine.c.stale_seconds)['fresh']:
            counted('STALE')
            getattr(feed,'_opportunity_triggers',{}).pop(sym,None)
            continue
        key=(tick.time,tick.cumulative)
        if completed.get(sym)==key:
            counted('DUPLICATE');continue
        completed[sym]=key
        counted('EVALUATED')
        evaluated+=1
        result=on_opportunity(tick,observed_at)
        trigger=getattr(feed,'_opportunity_triggers',{}).pop(sym,None)
        if trigger:
            fast_reason=getattr(engine.fast_strategy,'last_evaluation',{}).get(sym,{}) if getattr(engine,'fast_strategy',None) is not None else {}
            engine.event('OBSERVATION_OPPORTUNITY_REEVALUATED',symbol=sym,**trigger,
                latest_tick_at=tick.time.isoformat(),latest_received_at=observed_at.isoformat(),
                order_created=bool(result),fast_evaluation=fast_reason,
                actual_order_permission=False)
    return evaluated


def merge_verified_shadow_candidates(rows,shadow,lookup,clock=None,max_age_seconds=None):
    """Merge ObservationWorker-verified SHADOW candidates that hit RadarWorker's per-cycle scan limit."""
    shadow_rows=getattr(getattr(shadow,'observer',None),'rows',{}) if shadow is not None else {}
    if not shadow_rows:return rows
    from automation.shadow_observation import make_evaluation_view
    pool={r['code']:dict(r) for r in rows}
    scan_limit_reasons={'STATUS_SCAN_LIMIT','이번 주기 차트 조회 한도; 다음 주기 재시도',None,''}
    for sym,s_row in list(shadow_rows.items()):
        sv=s_row.get('shadow_view',{})
        if (sv.get('status')=='ELIGIBLE'
                and s_row.get('affordability',{}).get('entry_eligible') is True):
            cur=pool.get(sym)
            refresh_shadow=cur and (cur.get('status')=='SHADOW' or
                cur.get('status')=='UNKNOWN' and cur.get('shadow_view',{}).get('status')=='ELIGIBLE')
            if cur is None or refresh_shadow or (cur.get('status') in {'RAW','UNKNOWN',None} and cur.get('reason') in scan_limit_reasons):
                ev=make_evaluation_view(s_row)
                ev['market_status']=_newest_market_status((cur or {}).get('market_status'),ev.get('market_status'),clock=clock)
                ev=latest_evaluation_watch(ev,None,sym,shadow,clock,max_age_seconds)
                ms=ev.get('market_status',{})
                if clock is not None and not ms.get('checked_at'):
                    ms={**ms,'pass':None,'reason_code':ms.get('reason_code','MARKET_STATUS_UNKNOWN')};ev['market_status']=ms
                if ms.get('pass') is not True:
                    # Existing SHADOW rows must not retain an old normal state
                    # after a newer unknown/special/expired check is published.
                    if refresh_shadow:
                        pool[sym]={**cur,'status':'UNKNOWN','reason':ms.get('reason_code','MARKET_STATUS_UNKNOWN'),
                                   'market_status':dict(ms)}
                    continue
                if cur is not None:
                    for k in ('source_count','source_details','source_deltas','discovery_score','observed_at','first_raw_at','first_raw_price','reference_price','affordability'):
                        if cur.get(k) is not None:ev[k]=cur[k]
                ev['status']='WATCH' if sym in lookup else 'SHADOW'
                pool[sym]=ev
    return list(pool.values())

def apply_focus_selection(engine,lookup,by_symbol,selection):
    """main thread에서 WATCH 자격 표시만 동기화한다. detector/주문 상태는 바꾸지 않는다."""
    audit=getattr(engine,'candidate_audit',None)
    if isinstance(audit,CandidateAudit):
        try:audit.funnel.rows([{'code':s} for s in selection.promoted],selection.focus_epoch,'WATCH_ENTERED')
        except Exception:engine.entry_funnel_errors=getattr(engine,'entry_funnel_errors',0)+1
    for sym in selection.demoted:
        lookup.pop(sym,None)
        if sym in engine.source_qualification:
            engine.source_qualification[sym]['fallback_source1']=False
    for sym in selection.promoted:
        row=by_symbol.get(sym)
        if row is None:continue
        source1=row.get('source_count',0)==1
        from automation.entry_timing import watch_started
        first_watch=watch_started(row,lookup.get(sym),selection.focus_epoch)
        timeline=getattr(engine,'observation_timeline',None)
        if timeline is not None:
            first_watch=timeline.watch(sym,datetime.fromisoformat(selection.focus_epoch))['first_watch_at']
        lookup[sym]={**row,'status':'WATCH','fallback_source1':source1,
                     'watch_type':'FALLBACK_SOURCE1' if source1 else 'PRIMARY','first_watch_at':first_watch,
                     'watch_entered_at':selection.focus_epoch}
        engine.names[sym]=row.get('name')
        if row.get('market_status'):
            engine.market_status[sym]=row.get('market_status')
        if sym in engine.source_qualification:
            engine.source_qualification[sym]['fallback_source1']=source1
        elif getattr(engine,'source_qualification_seen',False) or getattr(engine,'source_qualification',None):
            engine.source_qualification[sym]={
                'source_count':row.get('source_count',0),
                'timestamp':row.get('observed_at') or selection.focus_epoch,
                'economic_reason':row.get('affordability',{}).get('reason_code'),
                'fallback_source1':source1,
            }
    capture=getattr(engine,'trade_review',None)
    if selection.applied and capture is not None:
        try:capture.set_scope(lookup,engine.s['positions'],engine.clock())
        except Exception:engine.trade_review_errors=getattr(engine,'trade_review_errors',0)+1

def resolve_radar_focus(engine,lookup,rows,rank_watch,readiness,cfg,auto,stamp):
    """Commit the final focus against actual incumbents, before subscription changes."""
    pool={row['code']:row for row in rows}
    for sym,prior in lookup.items():
        row=pool.get(sym,{})
        if row.get('affordability',{}).get('reason_code')=='ACCOUNT_CAPITAL_UNAVAILABLE':
            # Cash uncertainty blocks execution, not the collection of quotes.
            # Only an existing observation slot can survive this transient state.
            pool[sym]={**prior,'reason':'ACCOUNT_CAPITAL_UNAVAILABLE','observation_only':True}
    # Preserve the radar's existing bounded grace for temporarily missing ranks.
    for row in rank_watch:pool.setdefault(row['code'],row)
    previous=list(lookup)
    incumbents={sym:row for sym,row in lookup.items() if sym in pool}
    selection=select_focus(incumbents,list(pool.values()),readiness,cfg.max_watchlist,
                           auto.fallback_source1,stamp.isoformat())
    selection.previous_watch=previous
    selection.demoted=[sym for sym in previous if sym not in selection.next_watch]
    selection.applied=bool(selection.promoted or selection.demoted)
    apply_focus_selection(engine,lookup,pool,selection)
    for sym in selection.next_watch:
        row=pool[sym];source1=row.get('source_count',0)==1
        from automation.entry_timing import watch_started
        first_watch=watch_started(row,lookup.get(sym),stamp)
        timeline=getattr(engine,'observation_timeline',None)
        if timeline is not None:first_watch=timeline.watch(sym,stamp,rejoined=sym not in previous)['first_watch_at']
        prior_entered = (lookup.get(sym) or {}).get('watch_entered_at')
        watch_entered = prior_entered if sym in previous and prior_entered else stamp.isoformat()
        lookup[sym]={**row,'status':'WATCH','fallback_source1':source1,
                     'watch_type':'FALLBACK_SOURCE1' if source1 else 'PRIMARY',
                     'first_watch_at':first_watch,'watch_entered_at':watch_entered}
        if sym in engine.source_qualification:engine.source_qualification[sym]['fallback_source1']=source1
    return selection


def _newest_market_status(original,*updates,clock=None):
    selected=dict(original) if isinstance(original,dict) else {}
    def stamp(value):
        try:
            parsed=datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else None
        except (TypeError,ValueError):return None
    prior=stamp(selected.get('checked_at'))
    barrier=stamp(selected.get('unknown_observed_at'))
    unknown_key=selected.get('unknown_status_key')
    unresolved=barrier is not None
    def unknown(candidate,reason='MARKET_STATUS_UNKNOWN'):
        nonlocal selected,barrier,unknown_key,unresolved
        key=hashlib.sha256(json.dumps(candidate,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
        if barrier is None or key!=unknown_key:barrier=clock
        unknown_key=key;unresolved=True
        selected={'pass':None,'reason_code':reason,'unknown_status_key':key}
        if prior is not None:selected['checked_at']=prior.isoformat()
        if barrier is not None:selected['unknown_observed_at']=barrier.isoformat()
    if selected.get('checked_at') and prior is None or selected.get('pass') is not True and 'pass' in selected and prior is None and not unresolved:
        unknown(selected)
    for candidate in updates:
        if not isinstance(candidate,dict):continue
        newer=stamp(candidate.get('checked_at'))
        if newer is None:
            unknown(candidate);continue
        if unresolved and (barrier is None or newer<=barrier):continue
        if newer==prior and selected.get('pass') is not True and candidate.get('pass') is True:continue
        if prior is None or newer>=prior:
            selected=dict(candidate);prior=newer
            barrier=stamp(selected.get('unknown_observed_at'));unknown_key=selected.get('unknown_status_key')
            unresolved=barrier is not None
    return selected


def latest_evaluation_watch(watch,radar,symbol,shadow=None,clock=None,max_age_seconds=None):
    """Use refreshed immutable metadata without REST work on the order thread."""
    view=dict(watch)
    cache=getattr(radar,'status_cache',None)
    cached=cache.get(symbol) if isinstance(cache,dict) else None
    metadata=getattr(shadow,'metadata',None)
    saved=metadata.get(symbol) if isinstance(metadata,dict) else None
    shadow_status=saved.get('status') if isinstance(saved,dict) else None
    selected=_newest_market_status(watch.get('market_status'),cached,shadow_status,clock=clock)
    if clock is not None and selected.get('checked_at'):
        try:
            checked=datetime.fromisoformat(selected['checked_at'])
            age=(clock-checked).total_seconds()
            ttl=180. if max_age_seconds is None else max_age_seconds
            if checked.tzinfo is None or checked.date()!=clock.date() or not 0<=age<=ttl:
                selected={**selected,'pass':None,'reason_code':'MARKET_STATUS_STALE'}
        except (TypeError,ValueError):selected={'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'}
    # No timestamp is invented. Legacy caller rows remain unchanged when no
    # worker/cache refresh exists; malformed refreshes become explicit UNKNOWN.
    view['market_status']=selected
    return view

def verified_radar_cache_available(engine,stamp,scan_seconds):
    """A failed refresh never renews or relaxes the existing qualification TTL."""
    verified=getattr(engine,'last_radar_at',None)
    if verified is None or verified.tzinfo is None or stamp.tzinfo is None:return False
    return (verified.date()==stamp.date() and supported(verified)
            and 0<=(stamp-verified).total_seconds()<=scan_seconds*3)

def handle_radar_failure(engine,result,stamp,scan_seconds):
    cached=verified_radar_cache_available(engine,stamp,scan_seconds)
    engine.event('RADAR_UNAVAILABLE',**{k:v for k,v in result.items() if k!='time'},
                 verified_cache_available=cached,
                 verified_cache_at=getattr(engine,'last_radar_at',None),
                 cached_candidate_checks_unchanged=True)
    return cached

def pending_broker_settlement(state):
    """A terminal sell still needs actual charges; modeled PnL is not settlement."""
    day=state.get('day','')
    return any(t.get('completed') and str(t.get('closed','')).startswith(day)
               and t.get('pnl_source')!='BROKER_QUERY' for t in state.get('trades',[]))


def ready_account_results(account,in_session):
    """One FIFO reconciliation per turn; a producer cannot starve market data.

    Never merge or discard fills. A later account result stays queued for the
    next turn, after controls, quote consumption and position protection run.
    """
    if not in_session:return
    try:result=account.q.get_nowait()
    except queue.Empty:return
    yield result


def record_observation_retention(engine,retained,previous,desired,grace_seconds,capacity):
    """Emit changes once, including release to empty, and own a detached key."""
    retained=set(retained)
    if retained!=previous:
        engine.event('OBSERVATION_RETENTION',symbols=sorted(retained),
            grace_seconds=grace_seconds,subscription_count=len(desired),
            capacity=capacity,actual_order_permission=False)
    return retained

def focus_readiness(detector,shadow_readiness,stamp):
    """WATCH ticks are routed to main; rank them by their own full evaluation."""
    from automation.focus_selection import calculate_tier,unviable_price_geometry
    result=dict(shadow_readiness)
    fast_strat=getattr(detector,'fast_strategy',None)
    with detector._state_lock:
        for sym,evaluation in detector.last_eval.items():
            tick_at=evaluation.get('time')
            if not isinstance(tick_at,datetime) or tick_at.tzinfo is None:continue
            readiness_at=tick_at
            age=(stamp-tick_at).total_seconds()
            if tick_at.astimezone(stamp.tzinfo).date()!=stamp.date():continue
            if age<0:
                # Match the receiver's bounded exchange-clock rule, but only
                # when the exact accepted source tick is still available.
                source_tick=getattr(detector,'last',{}).get(sym)
                if (source_tick is None or source_tick.time!=tick_at
                        or not tick_freshness(source_tick,stamp,15)['fresh']):continue
                age=0.
                # Timestamp readiness by the actual local observation, while
                # retaining the original exchange clock. Passing a future
                # exchange stamp to readiness_tier discarded this validated
                # FAST signal again at the next layer.
                readiness_at=stamp
            gates=evaluation.get('gates',{})
            price_blocked=unviable_price_geometry({'setup_trials':evaluation.get('trials',[])})
            # Positive readiness still expires at 15s. A same-day negative
            # geometry must reach retained_price_block even after that TTL;
            # dropping it here made the lower-level expiry fix unreachable.
            if age>15 and not price_blocked:continue
            # WATCH and SHADOW are two views of one causal observation history.
            # Never overwrite a newer real observation with an older live one.
            try:
                shadow_at=datetime.fromisoformat(result.get(sym,{}).get('timestamp',''))
                if shadow_at.tzinfo is not None and tick_at<shadow_at<=stamp:continue
            except (ValueError,TypeError):pass
            fast_ready=False
            if age<=15 and fast_strat is not None and gates.get('market_status',{}).get('result')=='PASS':
                active_fast=fast_strat.active_signals.get(sym)
                if active_fast is not None and 0<=(stamp-active_fast.created_at).total_seconds()<=active_fast.ttl_seconds:
                    fast_ready=True
            result[sym]={'timestamp':readiness_at.isoformat(),'exchange_time':tick_at.isoformat(),
                'tier':0 if fast_ready else calculate_tier(gates,evaluation.get('trials',[])),
                'price_blocked':False if fast_ready else price_blocked,
                'fast_signal_ready':fast_ready,
                'gates':{k:{'result':gates.get(k,{}).get('result')} for k in ('acceleration','liquidity')}}
    return result

def current_entry_quote(engine,t,book=None):
    """Pair the latest received 0D with the exact accepted trade; never renew it."""
    provider=getattr(engine,'entry_quote_provider',None)
    if not callable(provider):return t
    snapshot=provider(t.symbol)
    if not isinstance(snapshot,(tuple,list)) or len(snapshot)!=2:return t
    latest,current=snapshot
    if not isinstance(latest,Tick) or (latest.time,latest.cumulative,latest.price,
            latest.received_at,latest.received_monotonic,latest.venue)!=(t.time,t.cumulative,t.price,
            t.received_at,t.received_monotonic,t.venue):return t
    book=current if book is None else book
    return current_book_tick(t,book,engine.clock(),engine.c.stale_seconds)


def entry_history_pending(engine,t):
    """Defer entry calculation, never ingestion/protection, for a queued prefix.

    A strictly newer, same-session accepted receiver print must exist. An
    invalid/future/nonregular/reset counter cannot starve the current frame.
    No deferred frame is marked evaluated; the latest print follows normally.
    """
    provider=getattr(engine,'entry_quote_provider',None)
    if not callable(provider):return False
    snapshot=provider(t.symbol)
    if not isinstance(snapshot,(tuple,list)) or len(snapshot)!=2:return False
    latest=snapshot[0]
    if (not isinstance(latest,Tick) or latest.symbol!=t.symbol or not latest.regular
            or type(latest.cumulative) is not int or type(t.cumulative) is not int
            or latest.price<=0 or latest.volume<=0
            or latest.venue!=t.venue or latest.time.date()!=t.time.date()
            or latest.time<t.time or latest.cumulative<=t.cumulative
            or latest.cumulative-t.cumulative<latest.volume
            or not tick_freshness(latest,engine.clock(),engine.c.stale_seconds)['fresh']):return False
    counts=getattr(engine,'entry_delivery_counts',None)
    if counts is None:counts={};engine.entry_delivery_counts=counts
    counts['HISTORY_PREFIX_DEFERRED']=counts.get('HISTORY_PREFIX_DEFERRED',0)+1
    return True


def _quote_blocked(engine,detector,symbol):
    fast=getattr(getattr(engine,'fast_strategy',None),'last_evaluation',{}).get(symbol,{})
    if fast.get('reason') in {'BOOK_TIMESTAMP_UNKNOWN_OR_STALE','EXECUTABLE_DEPTH_UNKNOWN',
                            'EXECUTABLE_QUOTE_UNKNOWN','FULL_QUANTITY_DEPTH_INSUFFICIENT',
                            'EXECUTABLE_ASK_NOT_ABOVE_BREAKOUT'}:return True
    # AFTER ABC can be fully formed except for its existing depth gate.
    trials=getattr(detector,'last_eval',{}).get(symbol,{}).get('trials',[])
    if any(t.get('candidate_generated') and t.get('gates',{}).get('liquidity',{}).get('result')!='PASS'
        and t.get('gates',{}).get('liquidity',{}).get('values',{}).get('mode')=='depth'
        and all(v.get('result')=='PASS' for k,v in t.get('gates',{}).items() if k!='liquidity')
        for t in trials):return True
    if not getattr(getattr(engine,'a',None),'abc_quote_retry_enabled',True):return False
    quote_reasons={'breakout_structure':'INVALID_EXECUTABLE_QUOTE',
        'pullback_structure':'PULLBACK_QUOTE_UNAVAILABLE','box_structure':'BOX_QUOTE_UNAVAILABLE'}
    def quote_gate(key,value):
        return (key in {'spread','order_price'} or
            value.get('values',{}).get('reason')==quote_reasons.get(key) and key in quote_reasons)
    def retryable(trial):
        gates=trial.get('gates',{})
        quote_proof_missing=any(k in quote_reasons and quote_gate(k,v) and v.get('result')=='UNKNOWN' for k,v in gates.items())
        # Without a quote the independent structural proof cannot supply its
        # emergency line. RR/size-distance from that provisional line are not
        # permanent vetoes. Recompute them after a fresh quote; never waive
        # them at the entry or order boundary.
        derived={'invalidation','risk_reward','economic','continuation_cost_reward_risk'} if quote_proof_missing else set()
        return (trial.get('candidate_generated')
            and any(v.get('result')!='PASS' and quote_gate(k,v) for k,v in gates.items())
            and all(v.get('result')=='PASS' for k,v in gates.items() if k!='chase' and k not in derived and not quote_gate(k,v)))
    return any(retryable(t) for trial in trials for t in [trial]+trial.get('entry_alternatives',[])[:1])


def queue_current_quote_books(engine,detector,feed,symbols):
    """SHADOW books stay off the protected FIFO; poll only quote-blocked rows."""
    seen=getattr(engine,'_seen_entry_books',None)
    if seen is None:seen={};engine._seen_entry_books=seen
    pending=getattr(engine,'_quote_entry_pending',None)
    if pending is None:pending={};engine._quote_entry_pending=pending
    symbols=set(symbols)
    for sym in set(seen)-symbols:seen.pop(sym,None)
    for sym in set(pending)-symbols:pending.pop(sym,None)
    books=getattr(feed,'orderbooks',{})
    if not isinstance(books,dict):return
    for sym in symbols:
        if not _quote_blocked(engine,detector,sym):continue
        book=books.get(sym) or {}
        key=(book.get('time'),book.get('received_at'),book.get('received_monotonic'))
        if key==(None,None,None) or seen.get(sym)==key:continue
        seen[sym]=key;pending[sym]=True


def drain_quote_opportunities(engine,detector,feed,lookup,radar,prices,account,radar_ok,*,max_evaluations=2,by_symbol=None,subscriptions=None):
    """Retry quote-blocked WATCH frames after control/position FIFO work.

    Coalesce pending books per WATCH symbol. Never add a trade, bypass a
    subscription epoch, or evaluate an older prefix than the receiver holds.
    The regular strategy, sizing, INTENT and PRE_HTTP guards remain owners.
    """
    pending=getattr(engine,'_quote_entry_pending',{})
    if not pending or not radar_ok or not engine.connected:return 0
    counters=getattr(engine,'_quote_entry_counts',None)
    if counters is None:counters={};engine._quote_entry_counts=counters
    def counted(name):counters[name]=counters.get(name,0)+1
    deadline=time.monotonic()+.005;evaluated=0
    for sym in list(pending):
        if evaluated>=max_evaluations or time.monotonic()>=deadline:break
        critical=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
        critical.update(set(engine.s.get('automation',{}).get('existing',{}))&set(BREAKEVEN_EXITS))
        if getattr(feed,'transport_failed',False) or feed.overflow.is_set() or _protected_feed_work_pending(feed,critical):
            counted('PROTECTED_OR_TRANSPORT_DEFERRED');break
        if (sym not in lookup and sym not in (by_symbol or {})) or sym in critical or not _quote_blocked(engine,detector,sym):
            pending.pop(sym,None);continue
        t=detector.last.get(sym)
        if t is None or not t.regular or not tick_freshness(t,engine.clock(),engine.c.stale_seconds)['fresh']:
            pending.pop(sym,None);counted('STALE_TRADE');continue
        latest=getattr(feed,'latest_ticks',{}).get(sym)
        if latest is not None and (latest.time,latest.cumulative)!=(t.time,t.cumulative):
            counted('HISTORY_PENDING');continue
        pending.pop(sym,None)
        updated=current_entry_quote(engine,t)
        if updated==t:counted('BOOK_NOT_IMPROVED');continue
        # Freshness was verified above. A new receipt timestamp alone does not
        # change this immutable trade's executable price/depth or strategy.
        key=(t.time,t.cumulative,updated.bid,updated.ask,updated.ask_qty,updated.bid_qty)
        prior=getattr(engine,'_last_quote_entry_frame',None)
        if prior is None:prior={};engine._last_quote_entry_frame=prior
        if prior.get(sym)==key:counted('DUPLICATE');continue
        prior[sym]=key;evaluated+=1;counted('EVALUATED')
        if sym in lookup:
            order=evaluate_actual_entry(engine,detector,updated,lookup[sym],radar,prices,account,
                getattr(engine,'_last_eval_frame',None),quote_retry=True)
        else:
            received=datetime.fromisoformat(t.received_at) if t.received_at else t.time
            order=evaluate_observed_entry(engine,detector,updated,received,lookup,radar,prices,account,
                by_symbol,subscriptions,quote_retry=True)
        engine.event('ENTRY_QUOTE_RETRY',symbol=sym,trade_at=t.time.isoformat(),
            trade_received_at=t.received_at,book_at=updated.book_time.isoformat(),
            book_received_at=updated.book_received_at,order_created=bool(order),
            data_action='MATCHED_BOOK_ONLY_NO_TRADE_INGESTION')
    return evaluated


def evaluate_shadow_opportunity(engine,detector,t,watch,radar,prices,account,last_evaluated_frame=None,by_symbol=None,lookup=None,*,probe_only=False,quote_retry=False):
    """Pre-evaluation path for subscribed non-WATCH symbols.

    1. Evaluates existing LIVE strategies (A, B, C, FAST) and SHADOW redesign trials.
    2. If an existing LIVE strategy qualifies and the candidate is eligible, dynamically
       re-evaluates WATCH priority and promotes to WATCH with zero data loss.
    3. Redesign A/B strategies remain strictly SHADOW only; zero live orders without explicit approval.
    """
    if not watch or not engine.connected: return False
    if entry_history_pending(engine,t):return False
    if last_evaluated_frame is not None:
        key=(t.time,t.cumulative)
        if not quote_retry and last_evaluated_frame.get(t.symbol)==key:return False
        last_evaluated_frame[t.symbol]=key
    t=current_entry_quote(engine,t)
    engine.names[t.symbol]=watch.get('name')
    sig=detector.signal(t,watch)
    last_eval=detector.last_eval.get(t.symbol,{})
    trials=last_eval.get('trials',[])
    gates=last_eval.get('gates',{})

    # Evaluate FAST strategy in SHADOW mode if enabled and no A/B/C signal yet
    fast_sig=None
    fast_evaluation=None
    if not sig and getattr(engine.a,'fast_momentum_enabled',False) and getattr(engine,'fast_strategy',None) is not None:
        watch_eval=dict(watch)
        eval_now=engine.clock()
        if getattr(detector,'price_context',None) is not None and (detector.price_context.rows.get(t.symbol) or {}).get('peaks'):
            structure_now = engine.fast_strategy.get_tape(t.symbol).structure(eval_now)
            ctx_snapshot = detector.price_context.snapshot(
                t.symbol, t, t.ask or t.price,
                support=structure_now.get('support') if structure_now.get('pass') else None,
                breakout=structure_now.get('breakout') if structure_now.get('pass') else None,
            )
            peak = ctx_snapshot.get('nearest_confirmed_peak')
            if peak:
                watch_eval['nearest_confirmed_peak'] = peak
        raw_fast=engine.fast_strategy.evaluate(
            t, watch_eval,
            capital_limit=engine.watch_budget(),
            risk_budget=min(engine.risk_equity()*engine.c.risk_fraction,
                            engine.capital_context()['risk_remaining']),
            ingest=False,
            received=eval_now,
            live_authorized=False
        )
        fast_evaluation=engine.fast_strategy.last_evaluation.get(t.symbol)
        if raw_fast is not None:
            fast_sig=raw_fast.to_engine_signal(watch)
            sig=fast_sig

    live_signal_ready = (sig is not None and sig.get('pattern') in {'A', 'B', 'C', 'FAST'})
    live_trials_ready = any(tr.get('entry_ready') for tr in trials if tr.get('setup') in {'A', 'B', 'C'})
    observe_evaluation(engine,t,watch,detector,fast_evaluation,
        sig,'SHADOW_PROBE' if probe_only else 'SHADOW')
    if probe_only:return live_signal_ready or live_trials_ready
    ready_trials=[tr for tr in trials if tr.get('entry_ready')]
    if sig or ready_trials:
        pattern=sig.get('pattern') if sig else ready_trials[0].get('setup')
        engine.decisions.emit('SHADOW_ENTRY_EVAL',t.symbol,'SHADOW_SIGNAL_READY',pattern,
                              gates,price=t.price,
                              extra={'actual_order_permission':False,'shadow_only':True,
                                     'signal':sig,'ready_trials':ready_trials})
        engine.event('SHADOW_OPPORTUNITY_DETECTED',symbol=t.symbol,pattern=pattern,
                     price=t.price,time=t.time.isoformat(),
                     actual_order_permission=False,shadow_only=True)

    # LIVE Strategy Promotion Check:
    # Only existing LIVE strategies (A, B, C, FAST) can trigger dynamic WATCH promotion.
    # Redesign strategies (EarlyMomentumBreakout, PullbackReclaimBreakout) are strictly SHADOW only.
    live_signal_ready = (sig is not None and sig.get('pattern') in {'A', 'B', 'C', 'FAST'})
    live_trials_ready = any(tr.get('entry_ready') for tr in trials if tr.get('setup') in {'A', 'B', 'C'})

    if (live_signal_ready or live_trials_ready) and lookup is not None and by_symbol is not None:
        t_start=time.perf_counter()
        cands=list(by_symbol.values())
        if not any(r.get('code')==t.symbol for r in cands):
            cands.append(watch)
        decision_at=engine.clock()
        readiness=focus_readiness(detector,{},decision_at)
        selection=select_focus(
            current_watch=lookup,
            candidates=cands,
            readiness_map=readiness,
            max_watchlist=engine.c.max_watchlist,
            fallback_source1=engine.a.fallback_source1,
            clock_iso=decision_at.isoformat()
        )
        if selection.applied and t.symbol in selection.promoted:
            apply_focus_selection(engine,lookup,by_symbol,selection)
            latency_ms=(time.perf_counter()-t_start)*1000.0
            pattern=sig.get('pattern') if sig else 'LIVE_GEOMETRY'
            engine.event('WATCH_DYNAMIC_PROMOTION',symbol=t.symbol,pattern=pattern,
                         latency_ms=latency_ms,promoted_at=decision_at.isoformat(),
                         next_watch=selection.next_watch,previous_watch=selection.previous_watch)
            return True
        engine.event('WATCH_PROMOTION_BLOCKED',symbol=t.symbol,
                     pattern=sig.get('pattern') if sig else 'LIVE_GEOMETRY',
                     reason=selection.blocked_reasons.get(t.symbol,'WATCH_PRIORITY_RETAINED'),
                     signal_at=t.time.isoformat(),actual_order_permission=False)
    return False


def evaluate_observed_entry(engine,detector,t,received,lookup,radar,prices,account,by_symbol,subscriptions=None,*,quote_retry=False):
    """Bridge an actually received non-WATCH tick to existing order guards.

    Promotion is not buy authorization. Re-evaluate the LIVE strategy with the
    same causal tape, account, history, quote, sizing and PRE_HTTP checks.
    """
    if not engine.connected or not t.regular or not tick_freshness(t,engine.clock(),engine.c.stale_seconds)['fresh']:return None
    if subscriptions is not None:
        if not subscriptions.tick_ready(t,received):return None
        subscriptions.accepted(t.symbol)
    watch=lookup.get(t.symbol)
    candidate=by_symbol.get(t.symbol)
    if not watch and not candidate:return None
    prices[t.symbol]=t.bid or t.price
    record_regular_tick_progress(engine,t)
    timeline=getattr(engine,'observation_timeline',None)
    if timeline is not None:
        timeline.tick(t.symbol,t.time,detector.usable_bars(t.symbol),t.price,
                      received=received,processed=engine.clock(),execution_fresh=True)
    for name in ('_last_shadow_eval_frame','_last_eval_frame'):
        if getattr(engine,name,None) is None:setattr(engine,name,{})
    if not watch:
        promoted=evaluate_shadow_opportunity(engine,detector,t,{**candidate,'status':'SHADOW'},
            radar,prices,account,engine._last_shadow_eval_frame,by_symbol=by_symbol,lookup=lookup,quote_retry=quote_retry)
        if not promoted:return None
        watch=lookup.get(t.symbol)
    if not watch:return None
    context_provider=getattr(engine,'market_context_provider',None)
    if context_provider:
        indices=context_provider(t.time)
        market_name=(watch.get('market_status',{}).get('raw') or {}).get('marketName')
        benchmark='KOSDAQ' if market_name in {'코스닥','KOSDAQ'} else 'KOSPI' if market_name in {'코스피','KOSPI','거래소'} else None
        watch={**watch,'index_context':indices.get(benchmark) if benchmark else None,'market_environment':indices}
    return evaluate_actual_entry(engine,detector,t,watch,radar,prices,account,engine._last_eval_frame,quote_retry=quote_retry)


def evaluate_actual_entry(engine,detector,t,watch,radar,prices,account,last_evaluated_frame=None,*,quote_retry=False):
    if not watch or not engine.connected: return None
    if entry_history_pending(engine,t):return None
    if watch.get('status') and watch.get('status') != 'WATCH':
        from automation.decisions import gate
        engine.decisions.emit('ENTRY_EVAL', t.symbol, 'NO_BUY', 'CANDIDATE_STATUS_NOT_WATCH',
                              {'status': gate(False, status=watch.get('status'))}, price=t.price)
        return None
    if watch.get('vi_active') or watch.get('single_price_auction') or getattr(t, 'vi_active', False):
        from automation.decisions import gate
        engine.decisions.emit('ENTRY_EVAL', t.symbol, 'NO_BUY', 'VI_AUCTION_NO_BUY',
                              {'market_status': gate(False, vi_active=watch.get('vi_active'), single_price=watch.get('single_price_auction'))}, price=t.price)
        return None
    # A preserved WATCH slot's cash-uncertainty marker is a cached diagnostic.
    # Current account freshness and final sizing still control all orders.
    # Other observation-only qualifications never become order permission.
    cash_recovered=(watch.get('reason')=='ACCOUNT_CAPITAL_UNAVAILABLE'
                    and engine.capital_account_valid())
    if watch.get('observation_only') and not cash_recovered:
        from automation.decisions import gate
        engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','ACCOUNT_CAPITAL_UNAVAILABLE',
                              {'connection_account':gate(False,observation_only=True)},price=t.price)
        return None
    if last_evaluated_frame is not None:
        key=(t.time,t.cumulative)
        if not quote_retry and last_evaluated_frame.get(t.symbol)==key:return None
        last_evaluated_frame[t.symbol]=key
    t=current_entry_quote(engine,t)
    src_count=watch.get('source_count',len(watch.get('source_details',{})) if 'source_details' in watch else engine.a.min_source_count)
    if src_count<max(2,engine.a.min_source_count) and not watch.get('fallback_source1'):
        engine.event('NO_TRADE',symbol=t.symbol,reason='SOURCE_COUNT_BUY_PERMISSION_DENIED');return None
    prior_status=engine.market_status.get(t.symbol,{})
    if prior_status.get('unknown_observed_at'):
        watch={**watch,'market_status':_newest_market_status(prior_status,watch.get('market_status'),clock=engine.clock())}
    watch=latest_evaluation_watch(watch,radar,t.symbol,getattr(engine,'observation_worker',None),
                                 engine.clock(),engine.a.scan_seconds*3)
    engine.names[t.symbol]=watch.get('name')
    engine.market_status[t.symbol]=watch.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
    sig=detector.signal(t,watch)
    fast_evaluation=None
    if not sig and getattr(engine.a, 'fast_momentum_enabled', False) and getattr(engine, 'fast_strategy', None) is not None:
        is_live = (engine.policy.mode == 'LIVE' and
                   bool(engine.policy.state.get('live_authorized')) and
                   bool(engine.c.live_mode))
        watch_eval = dict(watch)
        eval_now = engine.clock()
        cur_day_high = None
        prior_day_high = None
        if hasattr(detector, 'day_high'):
            # Detector ingestion has already included this decision print.
            # FAST's accepted tape captured its high before that ingestion.
            tape=engine.fast_strategy.get_tape(t.symbol)
            prior_day_high=tape.metadata[-1].get('prior_observed_day_high') if tape.metadata else None
            cur_day_high = max(t.price, prior_day_high if prior_day_high is not None else t.price)
            cur_day_high=max(cur_day_high,detector.day_high.get(t.symbol,t.price))
        if getattr(detector, 'price_context', None) is not None:
            if (detector.price_context.rows.get(t.symbol) or {}).get('peaks'):
                structure_now = engine.fast_strategy.get_tape(t.symbol).structure(eval_now)
                ctx_snapshot = detector.price_context.snapshot(
                    t.symbol, t, t.ask,
                    support=structure_now.get('support') if structure_now.get('pass') else None,
                    breakout=structure_now.get('breakout') if structure_now.get('pass') else None,
                )
                peak = ctx_snapshot.get('nearest_confirmed_peak')
                if peak:
                    watch_eval['nearest_confirmed_peak'] = peak
            pc_day_high = detector.price_context.day_high(t.symbol)
            if pc_day_high:
                if cur_day_high is None or pc_day_high > cur_day_high:
                    cur_day_high = pc_day_high
                    if hasattr(detector, 'day_high'):
                        detector.day_high[t.symbol] = cur_day_high
                if pc_day_high != t.price and (prior_day_high is None or pc_day_high > prior_day_high):
                    prior_day_high = pc_day_high
        if cur_day_high is not None:
            watch_eval['day_high'] = cur_day_high
        if prior_day_high is not None:
            watch_eval['prior_day_high'] = prior_day_high
        watch_eval['daily_high_20d'] = watch.get('daily_high_20d') or watch.get('recent_high')
        fast_sig = engine.fast_strategy.evaluate(
            t, watch_eval,
            capital_limit=engine.watch_budget(),
            risk_budget=min(engine.risk_equity()*engine.c.risk_fraction,
                            engine.capital_context()['risk_remaining']),
            ingest=False,
            received=eval_now,
            live_authorized=is_live
        )
        fast_evaluation=engine.fast_strategy.last_evaluation.get(t.symbol)
        if fast_sig is not None:
            sig = fast_sig.to_engine_signal(watch)
    observe_evaluation(engine,t,watch,detector,fast_evaluation,sig,'WATCH')
    capture=getattr(engine,'trade_review',None)
    if capture is not None:
        try:capture.evaluation(t,watch,detector.last_eval.get(t.symbol,{}),fast_evaluation,engine.clock())
        except Exception:engine.trade_review_errors=getattr(engine,'trade_review_errors',0)+1
    if not sig:
        gates=detector.evaluation_gates(t,watch)
        failed=[k for k,v in gates.items() if v['result']=='FAIL']
        engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','PATTERN_OR_GATE_NOT_READY',
            gates,price=t.price,extra={'failed_gates':failed,'signal_detected':False,
                'receipt_evidence':tick_source_identity(t),
                'setup_trials':detector.last_eval.get(t.symbol,{}).get('trials',[]),
                'fast_evaluation':{k:fast_evaluation.get(k) for k in ('time','status','reason','regime','quote_evidence','geometry','details')} if isinstance(fast_evaluation,dict) else None})
        return None
    sig['name']=watch.get('name')
    sig['fallback_source1']=bool(watch.get('fallback_source1'))
    bars=radar.cache.get((t.time.date(),t.symbol))
    radar_root=getattr(radar,'root',None)
    if not bars and isinstance(radar_root,(str,os.PathLike)):
        from candidate_builder import bars_from_csv
        for folder in ('data/raw','data/observation'):
            csv_path=Path(radar_root)/folder/f'{t.symbol}_daily.csv'
            if csv_path.exists():
                loaded=bars_from_csv(csv_path,t.time.date())
                if len(loaded)>=engine.c.min_history_bars:
                    bars=loaded; radar.cache[(t.time.date(),t.symbol)]=bars; break
    if not bars:
        engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','HISTORY_UNAVAILABLE',
            detector.evaluation_gates(t,watch),signal=sig,price=t.price)
        return None
    if sig.get('pattern') == 'FAST':
        from automation.entry_timing import annotate_signal
        annotate_signal(engine,detector,watch,sig)
        from automation.fast_entry_context import attach_fast_entry_observation
        attach_fast_entry_observation(sig,detector,t,engine.clock(),engine.c.min_reward_risk)
        obs = sig.get('fast_context_observation', {})
        if obs.get('status') == 'REJECT_CANDIDATE':
            from automation.decisions import gate
            reason = obs.get('reason', 'FAST_ENTRY_CONTEXT_REJECTED')
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY',reason,
                {'fast_entry_context':gate(False,**obs)},signal=sig,price=t.price)
            engine.event('NO_TRADE',symbol=t.symbol,reason=reason); return None
        fresh=structure(bars,sig['limit'],replace(engine.c,max_recent_range=999.0,max_overhead_share=1.0,min_resistance_room=0.0),t.time.date())
        if fresh.get('status')!='ELIGIBLE':
            from automation.decisions import gate
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','STRUCTURE_REJECTED',
                {'structure':gate(False,**fresh)},signal=sig,price=t.price)
            engine.event('NO_TRADE',symbol=t.symbol,reason=fresh['reason']); return None
    else:
        from automation.entry_timing import annotate_signal
        annotate_signal(engine,detector,watch,sig)
        initial_sig = dict(sig)
        initial_pattern = sig.get('pattern')
        breakout_level = initial_sig.get('breakout')
        projected_target = initial_sig.get('projection') or initial_sig.get('target')

        fresh=structure(bars,sig['limit'],replace(engine.c,max_recent_range=.99,min_resistance_room=0 if sig['pattern'] in {'A','C'} else engine.c.min_resistance_room),t.time.date())
        if fresh.get('status')!='ELIGIBLE':
            from automation.decisions import gate
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','STRUCTURE_REJECTED',
                {'structure':gate(False,**fresh)},signal=sig,price=t.price)
            engine.event('NO_TRADE',symbol=t.symbol,reason=fresh['reason']); return None

        fresh_watch={**watch,**fresh}
        confirmed_overhead_resistance = fresh.get('resistance') or watch.get('resistance')
        if confirmed_overhead_resistance is not None:
            fresh_watch['resistance'] = confirmed_overhead_resistance

        sig=detector.signal(t,fresh_watch)
        if not sig:
            trials = detector.last_eval.get(t.symbol, {}).get('trials', [])
            matching_trial = next((tr for tr in trials if tr.get('setup') == initial_pattern), None)
            fail_reasons = (matching_trial.get('failed_reasons') or []) if matching_trial else []
            if 'RISK_REWARD_FAIL' in fail_reasons:
                primary_code = 'FRESH_RESISTANCE_REWARD_INSUFFICIENT'
            elif matching_trial and matching_trial.get('primary_reason_code'):
                primary_code = f"FRESH_STRUCTURE_{matching_trial['primary_reason_code']}"
            else:
                primary_code = 'FRESH_STRUCTURE_SIGNAL_NOT_READY'

            economic_reward_ref = (
                min(projected_target, confirmed_overhead_resistance)
                if (confirmed_overhead_resistance is not None and confirmed_overhead_resistance > t.price)
                else projected_target
            )
            semantic_evidence = {
                'breakout_level': breakout_level,
                'confirmed_overhead_resistance': confirmed_overhead_resistance,
                'projected_target': projected_target,
                'economic_reward_reference': economic_reward_ref,
                'signal_detected': False,
                'setup_trials': trials,
                'rejection_basis': 'OVERHEAD_CEILING_COMPRESSED_NET_RR' if 'RISK_REWARD_FAIL' in fail_reasons else primary_code
            }
            engine.decisions.emit('ENTRY_EVAL', t.symbol, 'NO_BUY', primary_code,
                detector.evaluation_gates(t, fresh_watch), price=t.price,
                signal=initial_sig,
                extra=semantic_evidence)
            engine.event('NO_TRADE', symbol=t.symbol, reason=primary_code)
            return None

        sig['name']=watch.get('name')
        annotate_signal(engine,detector,watch,sig)
        sig['fallback_source1']=bool(watch.get('fallback_source1'))
        sig['breakout_level'] = breakout_level
        sig['confirmed_overhead_resistance'] = confirmed_overhead_resistance
        sig['projected_target'] = projected_target
        sig['economic_reward_reference'] = sig.get('target')
    for sym,p in (list(engine.s['positions'].items()) if engine.a.replacement_enabled else []):
        if sym==t.symbol or p.get('exit_requested'): continue
        history=radar.cache.get((t.time.date(),sym)); price=prices.get(sym)
        if not history or not price: continue
        view=structure(history,price,replace(engine.c,max_recent_range=.99),t.time.date())
        held={'price':price,'support':p['stop'],'resistance':view.get('resistance'),
              'confidence':p['signal']['efficiency']['inputs']['confidence'],
              'planned_minutes':p['signal']['efficiency']['inputs']['planned_minutes'],
              'holding_minutes':(now()-datetime.fromisoformat(p['opened'])).total_seconds()/60,
              'trend':'UP' if price>p['avg'] else 'WEAK','pattern':p['pattern']}
        result=comparison(held,sig,engine.a,engine.costs,engine.market,engine.s['trades'])
        engine.event('OPPORTUNITY_COMPARISON',symbol=sym,candidate=t.symbol,comparison=result)
        if result['decision']=='REPLACE' and engine.policy.mode=='LIVE' and not engine.new_entry_reason(t.symbol):
            engine.request_exit(sym,'비용/안전마진 포함 상대효율 교체')
    context=review_entry_context(sig,engine.costs,engine.c.min_reward_risk,engine.a.min_efficiency_score)
    key=(sig.get('pattern'),sig.get('limit'),sig.get('stop'),sig.get('target'),
         (sig.get('decision_bar_snapshot') or [{}])[-1].get('time'))
    prior=getattr(engine,'_entry_context_keys',{})
    if prior.get(t.symbol)!=key:
        engine.event('ENTRY_CONTEXT_AUDIT',symbol=t.symbol,setup=sig.get('pattern'),review=context)
        prior[t.symbol]=key;engine._entry_context_keys=prior
    if sig.get('paper_only') and engine.policy.mode != 'PAPER':
        engine.event('SIGNAL_BLOCKED', symbol=t.symbol, reason='PAPER_STRATEGY_BLOCKED_IN_LIVE')
        return None
    o=engine.enter(sig,t)
    account.need_fills=bool(engine.active_orders())
    return o

def drain_feed(feed, consume, *, clock=None, monotonic=None,
               max_messages=64, max_seconds=.05, timeout=.25):
    """Bounded FIFO catch-up; never coalesce prices, fills or connection barriers.

    The outer loop reconciles accounts, refreshes subscriptions and writes UI
    snapshots. Doing all of that between every queued quote can make a healthy
    receiver unusable. Yield at the existing transport barriers so reconciliation
    and the watchdog still run before any later connection's data is consumed.
    """
    clock=clock or now;monotonic=monotonic or time.monotonic
    started=monotonic();count=0;lag=0.
    while count<max_messages and (not count or monotonic()-started<max_seconds):
        try:received,message=feed.q.get(timeout=timeout) if not count else feed.q.get(block=False)
        except queue.Empty:break
        lag=max(0.,(clock()-received).total_seconds())
        consume(received,message,lag);count+=1
        if message.get('trnm') in {'ERROR','DISCONNECTED','REG'}:break
    return count,lag


def process_message(engine,detector,message,received,symbols,lookup,radar,prices,account,radar_ok,subscriptions=None,by_symbol=None):
    tr=message.get('trnm')
    if tr in {'ERROR','DISCONNECTED'}:
        engine.event('REALTIME_FAILURE',transport_event=tr,reason=message.get('reason'),stage=message.get('stage'),
                     event_loop_lag=getattr(engine,'event_loop_lag',None),queue_depth=getattr(engine,'feed_queue_size',None))
        engine.disconnect(); engine.account_ok=False; engine.reset_detector=True; detector.reset()
        if subscriptions is not None:subscriptions.disconnected(received)
        return 0
    if tr=='REG':
        if message.get('return_code') not in (0,'0'):
            engine.halt('실시간 구독 실패'); return 0
        if not engine.connected:
            if getattr(engine,'reset_detector',True): detector.reset()
            engine.reset_detector=False; engine.account_ok=False; account.request.set()
        engine.connected=True
        if subscriptions is not None:subscriptions.acknowledge()
        timeline=getattr(engine,'observation_timeline',None)
        if timeline is not None and subscriptions is not None:
            timeline.subscription_ack(subscriptions.symbols,received)
        return 0
    accepted=0
    if tr!='REAL': return accepted
    for item in message.get('data',[]):
        kind=item.get('type')
        if kind=='00':
            engine.on_order_event(item['values']); account.need_fills=bool(engine.active_orders()); account.request.set()
        elif kind=='0D' and item.get('item') in symbols:
            book=item.get('_book') or {}
            health=getattr(engine,'position_health',None)
            if health is not None and book:
                health.accept_book(item['item'],book,engine.clock())
            sym=item['item']
            if book and radar_ok and engine.connected and (sym in lookup or (by_symbol and sym in by_symbol)) and _quote_blocked(engine,detector,sym):
                if not hasattr(engine,'_quote_entry_pending'):engine._quote_entry_pending={}
                engine._quote_entry_pending[sym]=True
            if not after_supported(received):continue
            sym=item['item']; last_log=getattr(engine,'_depth_log_at',{}).get(sym)
            if book and (last_log is None or (received-last_log).total_seconds()>=5):
                if not hasattr(engine,'_depth_log_at'):engine._depth_log_at={}
                engine._depth_log_at[sym]=received
                engine.event('BOOK_DEPTH_OBSERVED',symbol=sym,book=book,diagnostic_only=True)
        elif kind=='0s': update_market(engine,str(item['values']['215']))
        elif kind=='0B':
            parsed=item.get('_observation_tick')
            t=parsed[1] if isinstance(parsed,tuple) and len(parsed)==2 and parsed[0]==received and isinstance(parsed[1],Tick) else parse_tick(item,received,book_stale_seconds=engine.c.stale_seconds,received_monotonic=message.get('_received_monotonic'))
            if t.symbol not in symbols:continue
            if subscriptions is not None and not subscriptions.tick_ready(t,received):continue
            state=getattr(engine,'s',{})
            positions=state.get('positions',{}) if isinstance(state,dict) else {}
            clock_fn=getattr(engine,'clock',None)
            wall_clock=clock_fn() if callable(clock_fn) else None
            if not isinstance(wall_clock,datetime):wall_clock=now()
            timing=tick_freshness(t,wall_clock,engine.c.stale_seconds)
            protected=t.symbol in positions and t.regular and timing['fresh']
            # Preserve genuine receiver-time history even if the consumer was
            # delayed. Old frames can build past bars, never generate a current
            # order or exit. Current execution still uses wall-clock freshness.
            if not detector.ingest(t,received):
                if protected and t.price>0:
                    prices[t.symbol]=t.bid or t.price
                    engine.on_tick(t)  # no fabricated/old completed bar on a rejected observation
                    health=getattr(engine,'position_health',None)
                    if health is not None:health.trade_seen(t.symbol,t)
                continue
            capture=getattr(engine,'trade_review',None)
            history=detector.history.get(t.symbol,())
            if capture is not None and history:
                capture.completed_bar(t.symbol,history[-1],received,wall_clock)
            if getattr(engine.a, 'fast_momentum_enabled', False) and hasattr(engine, 'fast_strategy') and engine.fast_strategy is not None:
                engine.fast_strategy.ingest_tick(t, received)
            if subscriptions is not None:subscriptions.accepted(t.symbol)
            accepted+=1;record_regular_tick_progress(engine,t)
            timeline=getattr(engine,'observation_timeline',None)
            if timeline is not None:timeline.tick(t.symbol,t.time,detector.usable_bars(t.symbol),t.price,received=received,processed=wall_clock,
                execution_fresh=timing['fresh'])
            if not timing['fresh']:continue
            prices[t.symbol]=t.bid or t.price
            if engine.market=='UNKNOWN': engine.market='3'
            finished=list(detector.completed.get(t.symbol,[]))
            if protected:
                # Core's legacy completed buffer has high/low only. Structural
                # exits require the genuine completed OHLCV observation history.
                previous=positions[t.symbol].get('last_complete_bar_at','')
                for bar in detector.usable_bars(t.symbol)[-30:]:
                    if bar['time'].isoformat()>previous:engine.observe_protection_bar(t.symbol,bar)
            engine.on_tick(t,finished[-1]['low'] if finished else None)
            health=getattr(engine,'position_health',None)
            if protected and health is not None:health.trade_seen(t.symbol,t)
            watch=lookup.get(t.symbol)
            if not watch:
                if radar_ok and engine.connected:
                    cand = (by_symbol.get(t.symbol) if by_symbol else None)
                    if cand:
                        s_watch = {**cand, 'status': 'SHADOW'}
                    else:
                        s_watch = {'code': t.symbol, 'name': engine.names.get(t.symbol, t.symbol), 'status': 'SHADOW'}
                    promoted = evaluate_shadow_opportunity(
                        engine, detector, t, s_watch, radar, prices, account,
                        getattr(engine, '_last_shadow_eval_frame', None),
                        by_symbol=by_symbol, lookup=lookup
                    )
                    if promoted:
                        watch = lookup.get(t.symbol)
                if not watch:
                    continue
            if not radar_ok or not engine.connected: continue
            context_provider=getattr(engine,'market_context_provider',None)
            if context_provider:
                indices=context_provider(t.time)
                market_name=(watch.get('market_status',{}).get('raw') or {}).get('marketName')
                benchmark='KOSDAQ' if market_name in {'코스닥','KOSDAQ'} else 'KOSPI' if market_name in {'코스피','KOSPI','거래소'} else None
                watch={**watch,'index_context':indices.get(benchmark) if benchmark else None,'market_environment':indices}
            evaluate_actual_entry(engine,detector,t,watch,radar,prices,account,getattr(engine,'_last_eval_frame',None))
    return accepted

class ReadOnlyPilotPolicy:
    def __init__(self,policy):
        self.original=policy;self.path=policy.path;self.revision=policy.state['revision'];self.state=copy.deepcopy(policy.state)
        self.state.update(mode='ARMED',live_authorized=False,releases={},test_capital_release={})
    @property
    def mode(self):return self.state['mode']
    def locked(self,symbol):return self.original.locked(symbol)
    def refresh(self):
        state=self.original.refresh()
        if state['revision']!=self.revision:self.state['mode']='STOP'
        return self.state
    def command(self,command):
        if command not in {'STOP','KILL'}:raise ValueError('조회 실행에 권한을 부여할 수 없습니다.')
        self.state['mode']='STOP'

def order_ready_from_evidence(readiness,stale_seconds,connected):
    """One data-readiness rule; this never grants approval or symbol authority."""
    if not isinstance(readiness,dict) or not connected:return False
    if readiness.get('order_ready') is False:return False
    if readiness.get('regular_tick_timing_valid') is False:return False
    age=readiness.get('regular_tick_age')
    if type(age) not in (int,float) or not 0<=age:return False
    missing=readiness.get('missing',[])
    return bool((readiness.get('state')=='OPEN_READY' and not missing and age<=stale_seconds)
                or (missing==['FRESH_REGULAR_TICK_NOT_READY']
                    and readiness.get('tick_lag_source')=='WS_SOURCE_SILENT'
                    and age<=stale_seconds*2))

def heartbeat_order_readiness(heartbeat,cfg,stamp):
    """Age saved evidence for a read-only display, without inventing permission.

    Engine snapshots remain the authority. The GUI can revoke an expired
    heartbeat but cannot revive a snapshot that the engine marked OFF.
    """
    import math
    original=heartbeat.get('readiness')
    if not isinstance(original,dict) or not original:
        return {'order_ready':False,'state':'UNKNOWN','missing':['READINESS_UNKNOWN'],'off_reasons':['READINESS_UNKNOWN']}
    ready=copy.deepcopy(original)
    engine_off=ready.get('order_ready') is False
    missing=list(ready.get('missing',[]))
    gates=ready.get('gates')
    if not isinstance(gates,dict):gates={};ready['gates']=gates
    def revoke_gate(key):
        # Aging can revoke saved evidence, never grant a previously failed gate.
        gates[key]=False
        reason=key.upper()+'_NOT_READY'
        if reason not in missing:missing.append(reason)
        ready.update(state='WAITING',missing=missing)
    try:
        issued=datetime.fromisoformat(heartbeat['time']);elapsed=(stamp-issued).total_seconds()
        if issued.tzinfo is None or issued.date()!=stamp.date() or not 0<=elapsed<=10:raise ValueError('heartbeat expired')
    except (KeyError,ValueError,TypeError):
        for key in ('fresh_regular_tick','account_fresh','websocket_reg_ack'):
            if key in gates:revoke_gate(key)
        ready.update(order_ready=False,off_reasons=['HEARTBEAT_NOT_CURRENT']);return ready
    age=ready.get('regular_tick_age')
    if type(age) in (int,float) and math.isfinite(age):age+=elapsed
    else:age=None
    try:
        timing=market_freshness(ready['last_regular_tick_at'],stamp,cfg.stale_seconds,
            received_at=ready.get('last_regular_tick_received_at'))
        ready['regular_tick_timing_valid']=timing['valid']
        ready['regular_tick_exchange_age']=timing['exchange_age_seconds']
        if not timing['valid']:raise ValueError('invalid tick stamp')
        actual_age=timing['effective_age_seconds']
        age=max(age,actual_age) if age is not None else actual_age
    except KeyError:pass  # Older snapshots can carry an age relative to heartbeat time.
    except (TypeError,ValueError):age=None
    ready['regular_tick_age']=age
    tick_required=('fresh_regular_tick' in gates or 'FRESH_REGULAR_TICK_NOT_READY' in missing
                   or ready.get('state')=='OPEN_READY')
    if tick_required and (age is None or not 0<=age<=cfg.stale_seconds):
        revoke_gate('fresh_regular_tick')
        loop_lag=ready.get('event_loop_lag_seconds',heartbeat.get('event_loop_lag_seconds'))
        queue_size=ready.get('feed_queue_depth',heartbeat.get('feed_queue_depth'))
        if type(loop_lag) in (int,float) and loop_lag>1:ready['tick_lag_source']='EVENT_LOOP_LAG'
        elif type(queue_size) in (int,float) and queue_size>5:ready['tick_lag_source']='QUEUE_BUFFER'
        elif type(loop_lag) in (int,float) and type(queue_size) in (int,float):ready['tick_lag_source']='WS_SOURCE_SILENT'
        elif ready.get('tick_lag_source')!='WS_SOURCE_SILENT':ready['tick_lag_source']='READINESS_LAG_UNKNOWN'
    account_age=ready.get('account_age_seconds')
    if type(account_age) in (int,float) and account_age+elapsed>cfg.reconcile_seconds*3:
        revoke_gate('account_fresh')
    if heartbeat.get('connected') is not True:
        revoke_gate('websocket_reg_ack')
    if engine_off:
        ready['order_ready']=False
        prior=ready.get('off_reasons') or ['ENGINE_DATA_NOT_READY']
        ready['off_reasons']=list(dict.fromkeys(list(prior)+missing))
        return ready
    ready['order_ready']=order_ready_from_evidence(ready,cfg.stale_seconds,heartbeat.get('connected') is True)
    ready['off_reasons']=[] if ready['order_ready'] else list(ready.get('missing') or ['ENGINE_DATA_NOT_READY'])
    if not ready['order_ready'] and ready.get('tick_lag_source'):ready['off_reasons'].append(ready['tick_lag_source'])
    return ready

def record_order_readiness_transition(engine,ready):
    signature=(ready['order_ready'],tuple(ready.get('off_reasons',[])),ready.get('tick_lag_source'))
    if signature==getattr(engine,'last_order_readiness_signature',None):return False
    engine.last_order_readiness_signature=signature
    engine.event('ORDER_READINESS_CHANGED',order_ready=ready['order_ready'],
        reason_codes=ready.get('off_reasons',[]),readiness=ready,
        authority_scope='GLOBAL_DATA_ONLY_SYMBOL_QUOTE_RISK_APPROVAL_STILL_REQUIRED',actual_order_permission=False)
    return True

def readiness_snapshot(engine,counts,radar_ok,stamp,detector=None):
    """증거를 평가한다. 경과 초 수나 전략 완성봉 수를 readiness 조건으로 쓰지 않는다."""
    sess=getattr(engine,'current_session',lambda:current_session(stamp))()
    feed=getattr(engine,'readiness_feed',None)
    transport_failed=getattr(feed,'transport_failed',False) is True
    connected=engine.connected and not transport_failed
    is_after=sess=='AFTER'
    if is_after:
        in_control=getattr(engine.c,'after_enabled',False) and after_control_supported(stamp)
        in_market=getattr(engine.c,'after_enabled',False) and after_entry_supported(stamp)
    else:
        in_control=control_supported(stamp)
        in_market=supported(stamp)
    last=getattr(engine,'last_regular_tick_at',None)
    last_tick=getattr(engine,'last_regular_tick',None)
    if getattr(last_tick,'time',None)!=last:last_tick=None
    if detector is not None:
        # Capture immutable tick/receipt pairs under the observation lock.
        with (getattr(detector,'_state_lock',None) or nullcontext()):
            regular=[t for t in detector.last.values() if t.regular or is_after]
            if regular:
                candidate=max(regular,key=lambda t:t.time)
                if last is None or candidate.time>=last:last=candidate.time;last_tick=candidate
    timing=(tick_freshness(last_tick,stamp,engine.c.stale_seconds) if last_tick is not None
            else market_freshness(last,stamp,engine.c.stale_seconds))
    tick_age=timing['effective_age_seconds']
    fresh_tick=in_market and timing['fresh']
    tick_lag_source=None
    if not fresh_tick and in_market:
        loop_lag=getattr(engine,'event_loop_lag',0)
        q_size=getattr(engine,'feed_queue_size',0)
        if isinstance(loop_lag,(int,float)) and loop_lag>1.0:
            tick_lag_source='EVENT_LOOP_LAG'
        elif isinstance(q_size,(int,float)) and q_size>5:
            tick_lag_source='QUEUE_BUFFER'
        else:
            tick_lag_source='WS_SOURCE_SILENT'
    radar_at=getattr(engine,'last_radar_at',None)
    radar_current=radar_ok and radar_at is not None and supported(radar_at) and radar_at.date()==stamp.date()
    gates={'control_session':in_control,'websocket_reg_ack':bool(connected and counts['subscription_acks']>0),
           'account_fresh':engine.capital_account_valid(),'safe_state':not engine.s['halt'] and not any(o.get('status')=='UNKNOWN' for o in engine.active_orders())}
    if in_market:
        if is_after:
            gates.update(observation_session=True,regular_market=bool(engine.market),fresh_regular_tick=fresh_tick)
        else:
            gates.update(observation_session=True,regular_market=engine.market=='3',fresh_regular_tick=fresh_tick,radar_pipeline=radar_current)
    missing=[key.upper()+'_NOT_READY' for key,passed in gates.items() if not passed]
    state=('OPEN_READY' if in_market else 'PREOPEN_READY') if not missing else 'WAITING'
    ready={'state':state,'phase':assessment(stamp)['phase'],'gates':gates,'missing':missing,
            'regular_epoch':stamp.replace(hour=9,minute=0,second=0,microsecond=0).isoformat(),
            'last_regular_tick_at':last.isoformat() if last else None,'regular_tick_age':tick_age,
            'last_regular_tick_received_at':getattr(last_tick,'received_at',None),
            'regular_tick_exchange_age':timing['exchange_age_seconds'],
            'regular_tick_receive_age':timing['receive_age_seconds'],
            'regular_tick_receive_elapsed':timing['receive_elapsed_seconds'],
            'regular_tick_timing_valid':timing['valid'],
            'tick_lag_source':tick_lag_source,'market_pending':not in_market,'strategy_warmup_separate':True,
            'asof':stamp.isoformat(),'event_loop_lag_seconds':getattr(engine,'event_loop_lag',None),
            'feed_queue_depth':getattr(engine,'feed_queue_size',None),'transport_failure_seen':transport_failed}
    if type(getattr(engine,'last_account',None)) in (int,float):
        ready['account_age_seconds']=max(0,time.monotonic()-engine.last_account)
    ready['order_ready']=order_ready_from_evidence(ready,engine.c.stale_seconds,connected)
    ready['off_reasons']=[] if ready['order_ready'] else list(missing or ['MARKET_NOT_OPEN'])
    if not ready['order_ready'] and tick_lag_source:ready['off_reasons'].append(tick_lag_source)
    return ready

def run(cfg,root,seconds=0,recover_pilot=False,operational_pilot=False,readiness_check=False):
    root=Path(root); auto=load_automation(root/'automation.json'); policy=Policy(root/'runtime/automation_policy.json')
    if operational_pilot:
        if not recover_pilot or not (readiness_check or seconds>0) or policy.mode not in {'ARMED','STOP','LIVE'} or policy.state.get('releases') or policy.state.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
            raise ValueError('운영 재자격은 기존 보호정책·주문 금지 readiness 검사만 허용')
        policy=ReadOnlyPilotPolicy(policy);cfg=replace(cfg,live_mode=False)
    if recover_pilot:
        if not (readiness_check or seconds>0) or cfg.live_mode or policy.mode!="ARMED" or policy.state.get("live_authorized") or policy.state.get("releases") or policy.state.get("test_capital_release",{}).get("status") in {"APPROVED","SUBMITTING"}:
            raise ValueError("재자격은 ARMED·실주문 승인 없음·주문 금지 readiness 검사만 허용")
        # 파일의 승인한도는 보존하며 이 조회 실행에서만 주문자본을 0으로 제한한다.
        auto=replace(auto,AUTO_TRADING_CAP_KRW=0,daily_risk_override_day=None,daily_risk_override_budget_krw=None)
    if readiness_check:
        if policy.mode not in {'ARMED','STOP','LIVE'} or policy.state.get('releases') or policy.state.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
            raise ValueError('readiness 조회는 위험상태 및 시험매도 승인이 없는 때만 허용')
        if not isinstance(policy,ReadOnlyPilotPolicy):policy=ReadOnlyPilotPolicy(policy)
        cfg=replace(cfg,live_mode=False);auto=replace(auto,AUTO_TRADING_CAP_KRW=0,daily_risk_override_day=None,daily_risk_override_budget_krw=None)
    # 기존 Config의 비용 예약/총위험 계산에도 명시적 수수료를 반영한다.
    effective=replace(cfg,roundtrip_cost_fraction=auto.commission_buy+auto.commission_sell+auto.sell_tax)
    effective.validate()
    rest=Rest(effective); folder=root/'runtime'/('automation-smoke' if seconds or readiness_check else 'automation-'+cfg.broker_environment)
    engine=AutonomousEngine(effective,auto,policy,folder,rest,now)
    # This recorder never calls a broker or the experimental profit policies.
    # Recording failure must not interrupt existing position protection.
    try:
        from automation.trade_review_capture import TradeReviewCapture
        engine.trade_review=TradeReviewCapture(folder/'trade-review',clock=now)
    except OSError as error:
        engine.event('TRADE_REVIEW_CAPTURE_UNAVAILABLE',error_type=type(error).__name__)
    observation_settings=load_observation(root);engine.observation_settings=observation_settings
    engine.candidate_audit=CandidateAudit()
    from automation.entry_timing import ObservationTimeline
    engine.observation_timeline=ObservationTimeline(root/'runtime/observation_timeline.json')
    engine.candidate_audit.refresh_seconds=auto.scan_seconds
    atomic_json(folder/'session_settings.json',{'config':asdict(cfg),'effective_config':asdict(effective),'automation':asdict(auto)})
    detector=PatternDetector(effective,auto,engine.costs)
    detector.fast_strategy=engine.fast_strategy
    detector.continuous_observation=True
    capture=getattr(engine,'trade_review',None)
    if capture is not None:
        detector.accepted_tick_capture=lambda tick,received:capture.tick(tick,received,engine.clock())
    if auto.entry_context_enabled or auto.structural_breakout_enabled or auto.intraday_context_enabled or auto.fast_momentum_enabled:
        saved=[p.get('signal',{}) for p in engine.s['trades']]
        saved.extend(o.get('signal') or {} for o in engine.s['orders'].values() if o.get('side')=='BUY')
        references=detector.price_context.bootstrap(saved,now(),effective.stale_seconds)
        engine.event('ENTRY_REFERENCE_RECOVERY',**references)
        context_cache_path=root/'runtime'/'price_context_cache.json'
        if context_cache_path.exists():
            try:
                import json
                with open(context_cache_path,'r',encoding='utf-8') as f:cached=json.load(f)
                recovered=restore_reference_cache(detector.price_context,cached,now())
                if recovered:engine.event('PRICE_CONTEXT_CACHE_RESTORED',recovered_bars=recovered,symbols=len(detector.price_context.rows))
            except Exception:pass
    # 검사 시간 종료는 STOP과 같으며 LIVE의 시간 제한 검사는 금지한다.
    if seconds and (cfg.live_mode or auto.capital_limit!=0 or policy.mode!='ARMED' or
                    policy.state.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}):
        raise ValueError('조회 검사는 0원·ARMED·live_mode=false·시험매도 승인 비활성 상태에 한정')
    if policy.mode=='OFF' and not engine.s['positions'] and not engine.active_orders():
        engine.event('OFF'); return {'mode':'OFF','real_orders_sent':0}
    shadow=None
    def clients():
        rank_rest=Rest(effective,auth_session=rest.auth_session); account_rest=Rest(effective,auth_session=rest.auth_session)
        for client in (rest,rank_rest,account_rest): client.session_guard=lambda:control_supported(now()) or (getattr(effective,'after_enabled',False) and after_control_supported(now()))
        account_worker=AccountWorker(account_rest,effective.reconcile_seconds);account_worker.observation_settings=observation_settings
        account_worker.settlement_pending=pending_broker_settlement(engine.s)
        account_worker.settlement_interval=auto.scan_seconds
        radar_worker=RadarWorker(effective,auto,policy,rank_rest,root)
        bind_candidate_observation(radar_worker,lambda:shadow,engine)
        realtime_feed=Feed(rest,sorted(symbols));engine.readiness_feed=realtime_feed
        realtime_feed.trade_review=getattr(engine,'trade_review',None)
        return (radar_worker,account_worker,realtime_feed)
    symbols=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
    subscriptions=SubscriptionState(symbols); priority_symbols=set(symbols)
    observation_retention=ObservationRetention();retained_observation=set()
    radar,account,feed=clients()
    from automation.position_health import PositionDataHealth
    position_health=PositionDataHealth(engine,feed,account,subscriptions)
    radar.refresh_account=account.request.set
    account.need_fills=bool(engine.active_orders())
    reporter=Reporter(root)
    from automation.observation_worker import ObservationWorker
    observation_rest=Rest(replace(effective,live_mode=False),auth_session=rest.auth_session);observation_rest.session_guard=lambda:control_supported(now()) or (getattr(effective,'after_enabled',False) and after_control_supported(now()));observation_rest.diagnostic_only=True
    shadow=ObservationWorker(effective,auto,engine.costs,root,observation_rest)
    engine.observation_worker=shadow
    shadow.observer.timeline=engine.observation_timeline
    if auto.entry_context_enabled:shadow.observer.detector.price_context=copy.deepcopy(detector.price_context)
    shadow.start()
    bind_candidate_observation(radar,shadow,engine)
    context_worker=None;context_checkpoint=None
    if auto.intraday_context_enabled:
        from automation.intraday_context import IntradayContextWorker
        context_rest=Rest(replace(effective,live_mode=False),auth_session=rest.auth_session)
        context_rest.diagnostic_only=True;context_rest.session_guard=observation_rest.session_guard
        context_worker=IntradayContextWorker(context_rest);context_worker.start()
        context_checkpoint=PriceContextCheckpoint(root/'runtime'/'price_context_cache.json')
    shadow_state={};observation_drops=0
    lookup={}; rows=[]; by_symbol={}; prices={}; counts={'ticks':0,'radar_cycles':0,'account_checks':0,'subscription_acks':0}
    engine._last_eval_frame={}
    last_radar=0.; radar_ok=False; last_report=0.; last_heartbeat=0.;last_board=0.; started=now(); stop_at=time.monotonic()+(seconds or 120) if seconds or readiness_check else None
    smoke_ended=False; fatal=''; account_generation=0; kill_requested_at=None
    data_active=False; ever_started=False; blocked_day=None; session_transition_done=False; reporter.start()
    engine.readiness_reason=lambda:entry_readiness_reason()
    def entry_readiness_reason():
        ready=readiness_snapshot(engine,counts,radar_ok,now(),detector)
        if not ready.get('order_ready',ready['state']=='OPEN_READY'):return 'OPEN_READINESS_'+(ready['missing'][0] if ready['missing'] else 'MARKET_NOT_OPEN')
        if cfg.live_mode:
            from automation.readiness import authorization_valid
            valid,reasons=authorization_valid(root,cfg,auto,policy,now(),**engine.authorization_context())
            if not valid:return 'DAILY_AUTHORIZATION_'+(reasons[0] if reasons else 'INVALID')
        return ''
    from automation.decisions import BUILD_ID,STRATEGY_EXPERIMENT
    context=engine.capital_context()
    engine.event('AUTOMATION_STARTED',mode=policy.mode,approved_cap=auto.capital_limit,live_mode=cfg.live_mode,build_id=BUILD_ID,strategy_experiment=STRATEGY_EXPERIMENT,risk_fraction=cfg.risk_fraction,max_daily_loss_fraction=cfg.max_daily_loss_fraction,allowed_risk_krw=context['risk_equity']*cfg.risk_fraction,remaining_daily_risk_krw=context['risk_remaining'],
                 daily_risk_budget_krw=context['daily_risk_budget_krw'],daily_risk_override_day=context['daily_risk_override_day'],daily_risk_override_active=context['daily_risk_override_active'],
                 message='기존 보유 평가금액 제외; 기본 실제 신규 주문 불가')
    diagnostics=DiagnosticSnapshotWriter((root/'runtime/candidate_summary.json',folder/'heartbeat.json'),
        timeline_path=root/'runtime/observation_timeline.json' if auto.timeline_async_save_enabled else None)
    engine.observation_timeline.writer=diagnostics.submit if auto.timeline_async_save_enabled else None
    engine.observation_timeline.diagnostic_fsync=not auto.timeline_async_save_enabled
    from core.event_log import AsyncDiagnosticEventWriter
    diagnostic_events=AsyncDiagnosticEventWriter(folder/'events.jsonl')
    engine.diagnostic_event_writer=diagnostic_events
    try:
        while True:
            try:
                diagnostics.check_error()
                check_diagnostic_event_writer(engine,diagnostic_events)
                in_session=control_supported(now()) or (getattr(cfg,'after_enabled',False) and after_control_supported(now()))
                t_str=now().strftime('%H:%M:%S')
                if not session_transition_done and t_str>=getattr(auto,'after_prep_start','15:40:00') and engine.current_session()=='AFTER':
                    engine.transition_to_after()
                    session_transition_done=True
                if supported(now()) and getattr(engine,'observation_epoch',None)!=now().date():
                    engine.observation_epoch=now().date();radar.request.set();account.request.set()
                    engine.event('MARKET_OBSERVATION_STARTED',regular_epoch=now().replace(hour=9,minute=0,second=0,microsecond=0).isoformat(),pre_market_bars_used=False)
                if not in_session and blocked_day!=now().date():
                    blocked_day=now().date()
                    engine.event('SESSION_BLOCK',reason_code='OUT_OF_SUPPORTED_SESSION',decision='NO_BUY',strategy_version='V1.1-policy-20261001')
                    if engine.s['positions'] or engine.active_orders():
                        engine.event('POSITION_REMAINS_AFTER_SESSION',reason_code='OVERNIGHT_RISK',positions={s:p['qty'] for s,p in engine.s['positions'].items()},pending=len(engine.active_orders()))
                if in_session and not data_active:
                    if ever_started: radar,account,feed=clients(); radar.refresh_account=account.request.set
                    position_health.feed=feed;position_health.account=account
                    account.need_fills=bool(engine.active_orders()); radar.extra_symbols=set(engine.s['positions'])
                    radar.start(); account.start(); feed.start(); data_active=True; ever_started=True
                    engine.reset_detector=True
                elif not in_session and data_active:
                    feed.close(); radar.close(); account.close(); data_active=False
                    detector.reset(); engine.disconnect(); engine.account_ok=False; engine.market='UNKNOWN'; radar_ok=False;engine.last_regular_tick_at=None;engine.last_regular_tick=None;engine.last_radar_at=None
                    engine.event('OUT_OF_SUPPORTED_SESSION',message='연결 검사/조회와 신규 주문을 다음 정규장까지 대기')
                    if engine.s['positions'] or engine.active_orders():
                        engine.event('POSITION_REMAINS_AFTER_SESSION',reason_code='OVERNIGHT_RISK',allow_overnight=auto.ALLOW_OVERNIGHT,
                            positions={s:p['qty'] for s,p in engine.s['positions'].items()},pending=len(engine.active_orders()),
                            message='실제 미청산 상태 보존. 다음 정규장 계좌 대조 후 청산 의도 복구')
                if engine.roll_day():
                    detector.reset(); engine.reset_detector=True; radar.request.set(); account.request.set(); feed.logged_in=False
                    if feed.socket: feed.socket.close()
                getattr(policy,'refresh_if_changed',policy.refresh)()
                engine.recover_io_halt()
                if policy.mode=='KILL' and policy.state['revision']!=engine.control_revision:
                    kill_requested_at=time.monotonic(); account.request.set()
                done=engine.controls(prices)
                for result in ready_account_results(account,in_session):
                    if result['error']:
                        engine.account_ok=False; engine.event('ACCOUNT_UNAVAILABLE',**{k:v for k,v in result.items() if k!='started'})
                    elif time.monotonic()-result['started']>effective.reconcile_seconds*3:
                        engine.account_ok=False; engine.event('ACCOUNT_UNAVAILABLE',error='조회 지연')
                    else:
                        for sym,error in result['snapshot'].get('book_errors',{}).items(): engine.event('EXISTING_BOOK_UNAVAILABLE',symbol=sym,**error)
                        position_health.probe_results(result['snapshot'],now())
                        reconcile_started=time.perf_counter()
                        reconciled=engine.reconcile(result['snapshot'],result['fills']); counts['account_checks']+=1; account_generation+=1
                        reconcile_ms=(time.perf_counter()-reconcile_started)*1000
                        if reconciled is False and engine.s.get('automation',{}).get('reconcile_deferred_pending'):
                            engine.s['automation']['reconcile_deferred_pending']=False
                            account.request.set()
                        for sym,book in result['snapshot'].get('books',{}).items():
                            position_health.accept_book(sym,book,now(),source='ka10004')
                        engine.event('ACCOUNT_LATENCY',**result['snapshot'].get('performance',{}),
                            reconcile_ms=reconcile_ms,pending_results=account.q.qsize(),
                            reconcile_checkpoint=dict(getattr(engine,'reconcile_checkpoint_stats',{})),
                            result_age_seconds=max(0,time.monotonic()-result['started']))
                        external=set(engine.s['automation']['existing'])&(set(BREAKEVEN_EXITS)|({TEST_RELEASE_SYMBOL} if legacy_test_release_enabled() else set()))
                        account.external_symbols=external
                        radar.extra_symbols=set(engine.s['positions'])
                        if kill_requested_at is not None and result['started']<kill_requested_at: engine.kill_checked=False
                        if engine.kill_checked: kill_requested_at=None
                    account.need_fills=bool(engine.active_orders())
                    account.settlement_pending=pending_broker_settlement(engine.s)
                    account.settlement_interval=auto.scan_seconds
                focus_promotions=[]
                while in_session and not radar.q.empty():
                    result=radar.q.get_nowait()
                    if result['error']:
                        radar_ok=handle_radar_failure(engine,result,now(),auto.scan_seconds)
                    else:
                        if result.get('phase')=='QUALIFIED_PROGRESS':
                            rows=merge_verified_shadow_candidates(rows,shadow,lookup,now(),auto.scan_seconds*3)
                            rows,selection=apply_qualified_progress(engine,lookup,rows,result,
                                focus_readiness(detector,shadow.runtime_view()['evaluations'],now()),effective,auto,now())
                            if selection is not None:
                                radar_ok=True;last_radar=time.monotonic()
                                engine.last_radar_at=datetime.fromisoformat(result['time'])
                                by_symbol={r['code']:r for r in rows}
                                focus_promotions.extend(selection.promoted)
                                engine.candidate_audit.begin(rows,result['time'],result.get('capital_context',{}))
                                submit_candidate_observation(engine,shadow,rows,result['time'],engine.capital_context())
                                record_opening_focus_change(engine,selection)
                            continue
                        if result.get('phase')=='RAW':
                            engine.candidate_audit.record_rows(result['rows'],result['time'],'RAW')
                            engine.observation_timeline.raw(result['rows'],datetime.fromisoformat(result['time']))
                            qualify_raw(engine,result['rows'],result['time'],is_raw_phase=True)
                            engine.event('RAW_CANDIDATES',observed_at=result['time'],raw_count=len(result['rows']),
                                candidates=[{k:row.get(k) for k in ('code','name','first_raw_at','first_raw_price','reference_price','source_count','affordability')} for row in result['rows']])
                            continue
                        if 'source_observed_at' in result or any('source_count' in row for row in result['rows']):qualify_raw(engine,result['rows'],result.get('source_observed_at',result['time']),is_raw_phase=False)
                        if engine.candidate_audit.started:
                            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
                            diagnostics.submit(root/'runtime/candidate_summary.json',summary)
                            compact_summary={**summary,'candidates':[{'symbol':c.get('symbol'),'primary_reason_code':c.get('primary_reason_code'),
                                'discovery_status':c.get('discovery_status'),'source_count':c.get('source_count'),'current_price':c.get('current_price')}
                                for c in summary.get('candidates',[])]}
                            engine.event('CANDIDATE_CYCLE_SUMMARY',**compact_summary)
                        prior_watch=set(lookup)
                        rows=merge_verified_shadow_candidates(result['rows'],shadow,lookup,now(),auto.scan_seconds*3);radar_ok=True;engine.last_radar_at=datetime.fromisoformat(result['time'])
                        selection=resolve_radar_focus(engine,lookup,rows,result['watch'],
                            focus_readiness(detector,shadow.runtime_view().get('evaluations',{}),now()),effective,auto,now())
                        focus_promotions.extend(selection.promoted)
                        if selection.applied:
                            engine.event('FOCUS_SELECTION_APPLIED',promoted=selection.promoted,demoted=selection.demoted,
                                next_watch=selection.next_watch,previous_watch=selection.previous_watch,
                                primary_reason=selection.primary_reason,applied_at=selection.focus_epoch,basis='RADAR_FINAL_FOCUS',
                                tier_map=selection.tier_map,priority_tuples=selection.priority_tuples)
                        # Both observation and subsequent notifications see the committed roles.
                        for row in rows:
                            if row['code'] in lookup:row.update(status='WATCH',fallback_source1=lookup[row['code']]['fallback_source1'])
                            elif row.get('status')=='WATCH':row.update(status='RESERVE',fallback_source1=False)
                        by_symbol={row['code']:row for row in rows}
                        for sym in prior_watch^set(lookup):
                            row=by_symbol.get(sym,{})
                            engine.event('WATCH_TRANSITION',symbol=sym,name=row.get('name'),source_count=row.get('source_count'),
                                data_action='RETAIN_CONTINUOUS_IF_SUBSCRIBED' if sym in detector.last else 'NO_DATA',
                                stored_completed_bars=len(detector.history.get(sym,[])),usable_completed_bars=len(detector.usable_bars(sym)))
                        engine.candidate_audit.begin(rows,result['time'],result.get('capital_context',engine.capital_context()))
                        for row in rows:
                            engine.names[row['code']]=row.get('name')
                            engine.market_status[row['code']]=row.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
                            if row['code'] in lookup and row.get('status') in {'REJECT','UNKNOWN','RESERVE'}:
                                from automation.decisions import gate
                                engine.decisions.emit('ENTRY_EVAL',row['code'],'NO_BUY',row.get('reason') or 'WATCH_CAPACITY_RESERVE',
                                    {'candidate_compression':gate(False,status=row['status'],source_count=row.get('source_count'),
                                        reference_price=row.get('reference_price'),affordability=row.get('affordability'),
                                        market_status=row.get('market_status'),overhead_share=row.get('overhead_share_estimate'),
                                        resistance=row.get('resistance'))},price=row.get('reference_price'))
                        context=engine.capital_context()
                        submit_candidate_observation(engine,shadow,rows,result['time'],context)
                        engine.event('CANDIDATE_LATENCY',**result.get('performance',{}))
                        last_radar=time.monotonic(); counts['radar_cycles']+=1
                        engine.event('RADAR_UPDATED',watch=list(lookup),changes={'added':sorted(set(lookup)-prior_watch),'removed':sorted(prior_watch-set(lookup))},rank_changes=result['changes'])
                focus_dirty=False
                highest_notif_tier=99
                while in_session and hasattr(shadow,'focus_notifications') and not shadow.focus_notifications.empty():
                    notif=shadow.focus_notifications.get_nowait()
                    try:
                        notif_dt=datetime.fromisoformat(notif.detected_at)
                        notif_age=(now()-notif_dt).total_seconds()
                    except Exception:notif_age=float('inf')
                    if not -1<=notif_age<=5.0:
                        engine.event('FOCUS_SELECTION_BLOCKED',symbol=notif.symbol,reason='NOTIFICATION_STALE')
                        continue
                    focus_dirty=True
                    highest_notif_tier=min(highest_notif_tier,getattr(notif,'tier',3))
                if focus_dirty:
                    last_focus_mono=getattr(engine,'_last_focus_eval_mono',None)
                    now_mono=time.monotonic()
                    if highest_notif_tier>=2 and last_focus_mono and (now_mono-last_focus_mono)<1.0:
                        focus_dirty=False
                if focus_dirty:
                    engine._last_focus_eval_mono=time.monotonic()
                    rows=merge_verified_shadow_candidates(rows,shadow,lookup,now(),auto.scan_seconds*3)
                    by_symbol={row['code']:row for row in rows}
                    readiness=focus_readiness(detector,shadow.runtime_view().get('evaluations',{}),now())
                    selection=select_focus(
                        current_watch=lookup,
                        candidates=rows,
                        readiness_map=readiness,
                        max_watchlist=effective.max_watchlist,
                        fallback_source1=auto.fallback_source1,
                        clock_iso=now().isoformat()
                    )
                    if selection.applied:
                        apply_focus_selection(engine,lookup,by_symbol,selection)
                        for row in rows:
                            if row['code'] in lookup:row.update(status='WATCH',fallback_source1=lookup[row['code']]['fallback_source1'])
                            elif row.get('status')=='WATCH':row.update(status='RESERVE',fallback_source1=False)
                        if not radar_ok:
                            radar_ok=True; last_radar=last_radar or time.monotonic()
                            if getattr(engine,'last_radar_at',None) is None:engine.last_radar_at=now()
                        focus_promotions.extend(selection.promoted)
                        engine.event('FOCUS_SELECTION_APPLIED',promoted=selection.promoted,demoted=selection.demoted,
                            next_watch=selection.next_watch,previous_watch=selection.previous_watch,
                            primary_reason=selection.primary_reason,applied_at=selection.focus_epoch,
                            tier_map=selection.tier_map,priority_tuples=selection.priority_tuples)
                    elif notif.symbol in selection.blocked_reasons:
                        engine.event('FOCUS_SELECTION_BLOCKED',symbol=notif.symbol,reason=selection.blocked_reasons[notif.symbol])
                if last_radar and time.monotonic()-last_radar>auto.scan_seconds*3: radar_ok=False
                # 실제 수량 변동 뒤에도 다음 후보 주기에서 같은 현금 상한을 사용한다.
                context=engine.capital_context(); prior_context=radar.capital_context
                if context['account_fresh'] and (not prior_context.get('account_fresh') or context['entry_budget']!=prior_context.get('entry_budget')):
                    radar.request.set()
                radar.capital_context=context; radar.available_capital=context['entry_budget']
                shadow.set_capital(context)
                radar.status_priority_symbols=frozenset(lookup)
                if context_worker:
                    from automation.intraday_context import context_targets
                    venue=effective.after_default_venue if after_supported(now()) else 'KRX'
                    reference_targets=context_targets(lookup,shadow.runtime_view().get('evaluations',{}),venue,now()) if in_session else {}
                    context_worker.set_targets(reference_targets)
                    drain_intraday_context(engine,detector,shadow,context_worker,reference_targets,
                        venue,now(),context_checkpoint)
                    checkpoint_state=context_checkpoint.snapshot()
                    if checkpoint_state['errors']>getattr(engine,'_reference_cache_errors',0):
                        engine._reference_cache_errors=checkpoint_state['errors']
                        engine.event('REFERENCE_CACHE_CHECKPOINT_PENDING',**checkpoint_state,
                            scope='REFERENCE_CACHE_ONLY',order_ledger_affected=False)
                shadow_state=shadow.runtime_view();radar.radar.momentum=focus_readiness(detector,shadow_state.get('evaluations',{}),now())
                desired=set(lookup)|set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
                desired.update(set(engine.s['automation']['existing'])&(set(BREAKEVEN_EXITS)|({TEST_RELEASE_SYMBOL} if legacy_test_release_enabled() else set())))
                if (readiness_check or (in_session and not supported(now()))) and not desired:desired.add('005930')  # 조회 증거용; HARD_LOCK 종목은 WATCH/주문 대상이 아니다.
                real_symbols=set(desired)
                observed=shadow_state.get('symbols',[])
                engine.candidate_audit.shadow_count=len(observed)
                from automation.observation_retention import observation_residence_seconds
                observation_grace=observation_residence_seconds(auto)
                desired,observed,retained=observation_retention.select(lookup,real_symbols,observed,now(),
                    grace_seconds=observation_grace,capacity=observation_settings.max_symbols)
                retained_observation=record_observation_retention(
                    engine,retained,retained_observation,desired,observation_grace,
                    observation_settings.max_symbols)
                opportunity_callback=(lambda tick,received:evaluate_observed_entry(
                    engine,detector,tick,received,lookup,radar,prices,account,by_symbol,subscriptions)) \
                    if in_session and radar_ok and not smoke_ended else None
                observation_drops,priority_symbols=prewarm_handoff(
                    engine,detector,feed,observed,real_symbols,priority_symbols,observation_drops,
                    shadow.deliver,
                    shadow_detector=getattr(getattr(shadow, 'observer', None), 'detector', None),
                    # No per-frame strategy probe in the operating loop.
                    # The latest frame is evaluated only after this symbol's
                    # already-received history has caught up.
                    on_opportunity=opportunity_callback
                )
                if in_session:
                    subscriptions.refresh(engine,detector,feed,desired,now(),grace_seconds=observation_grace); symbols=set(subscriptions.symbols)
                    # Rejoined symbols must first deliver a fresh post-subscription regular tick.
                    for prom in set(focus_promotions)&set(lookup):
                        prom_tick=detector.last.get(prom)
                        if prom not in subscriptions.waiting and prom_tick and 0<=(now()-prom_tick.time).total_seconds()<=effective.stale_seconds:
                            evaluate_actual_entry(engine,detector,prom_tick,lookup[prom],radar,prices,account,getattr(engine,'_last_eval_frame',None))
                if feed.overflow.is_set():
                    feed.overflow.clear(); engine.halt('실시간 수신 큐 초과'); detector.reset()
                    for sym in list(engine.s['positions']): engine.request_exit(sym,'수신 큐 초과')
                try:
                    if not in_session: raise queue.Empty
                    try:
                        raw_qs = feed.q.qsize() if hasattr(feed, 'q') and hasattr(feed.q, 'qsize') else 0
                        engine.feed_queue_size = raw_qs if isinstance(raw_qs, (int, float)) else 0
                    except Exception:
                        engine.feed_queue_size = 0
                    def consume_frame(received,message,lag):
                        engine.event_loop_lag=lag
                        if message.get('trnm')=='REG' and message.get('return_code') in (0,'0'):counts['subscription_acks']+=1
                        counts['ticks']+=process_message(engine,detector,message,received,real_symbols,lookup,radar,prices,account,radar_ok and not smoke_ended,subscriptions,by_symbol=by_symbol)
                    drain_feed(feed,consume_frame,timeout=.02,max_messages=512,max_seconds=.25)
                    # Catch receiver arrivals during the main FIFO drain before
                    # evaluating them. Never replace a prefix with an uningested
                    # latest tick; prewarm retains the existing bounded budget.
                    observation_drops,priority_symbols=prewarm_handoff(
                        engine,detector,feed,observed,real_symbols,priority_symbols,observation_drops,
                        shadow.deliver,shadow_detector=getattr(getattr(shadow,'observer',None),'detector',None),
                        on_opportunity=opportunity_callback)
                    drain_observed_opportunities(engine,feed,opportunity_callback)
                    queue_current_quote_books(engine,detector,feed,real_symbols|set(observed))
                    drain_quote_opportunities(engine,detector,feed,lookup,radar,prices,account,
                        radar_ok and not smoke_ended,by_symbol=by_symbol,subscriptions=subscriptions)
                except queue.Empty:
                    if not in_session: time.sleep(.25)
                position_health.prune()
                engine.timer(prices)
                if isinstance(getattr(account,'external_prices',None),dict):
                    prior=account.external_prices;account.external_prices=dict(prices)
                    if any(prices.get(sym,0)>=threshold*(1-observation_settings.near_threshold_fraction) and prior.get(sym,0)<threshold*(1-observation_settings.near_threshold_fraction) for sym,threshold in BREAKEVEN_EXITS.items()):account.request.set()
                request_capital_refresh(engine,account,in_session)
                if engine.candidate_audit.started and time.monotonic()-last_board>=5:
                    summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context());summary['shadow']=len(observed)
                    summary['raw_definition']='5개 ranking API의 일반주 고유종목 합집합; HTS 0168과 동일 목록은 아님'
                    summary['refresh_seconds']=auto.scan_seconds
                    diagnostics.submit(root/'runtime/candidate_summary.json',summary);last_board=time.monotonic()
                ready=readiness_snapshot(engine,counts,radar_ok,now(),detector)
                record_order_readiness_transition(engine,ready)
                if ready['state']!=getattr(engine,'last_readiness_state',None):
                    engine.event('READINESS_CHANGED',readiness=ready);engine.last_readiness_state=ready['state']
                if readiness_check and ready['state'] in {'PREOPEN_READY','OPEN_READY'}:smoke_ended=True
                if stop_at and time.monotonic()>=stop_at: smoke_ended=True
                if smoke_ended:
                    for o in engine.active_orders(side='BUY'): engine.cancel(o)
                    for sym in list(engine.s['positions']): engine.request_exit(sym,'조회 전용 검사 종료')
                    engine.drive_exits(prices)
                    done=not engine.s['positions'] and not engine.active_orders()
                if time.monotonic()-last_report>=60:
                    summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
                    diagnostics.submit(root/'runtime/candidate_summary.json',summary)
                    engine.save(); reporter.submit(rows,engine.s); last_report=time.monotonic()
                if time.monotonic()-last_heartbeat>=1:
                    capture=getattr(engine,'trade_review',None)
                    if capture is not None:capture.set_scope(lookup,engine.s['positions'],now())
                    drained_stages=engine.observation_timeline.drain_events()
                    for stage in drained_stages[:8]:
                        engine.event('OPENING_PIPELINE_STAGE',**stage)
                    engine.observation_timeline.save(now())
                    if in_session:
                        from automation.opening_diagnostics import check_opening_funnel_diagnostic
                        check_opening_funnel_diagnostic(engine,detector,shadow,radar,lookup,rows,now(),root)
                    sess_name=engine.current_session()
                    phase_str=market_phase(now(),live_approved=getattr(engine,'after_live_authorized',False))
                    if sess_name=='AFTER':
                        if phase_str=='AFTER_PREP': hb_session='AFTER_PREP'
                        elif phase_str=='AFTER_EXIT_ONLY': hb_session='AFTER_EXIT_ONLY'
                        elif phase_str=='FORCE_FLATTEN': hb_session='AFTER_FLATTEN'
                        elif getattr(engine,'after_live_authorized',False) and cfg.live_mode: hb_session='AFTER_LIVE'
                        else: hb_session='AFTER_READY'
                    elif sess_name=='REGULAR':
                        hb_session='REGULAR'
                    else:
                        t_now=now().strftime('%H:%M:%S')
                        if '15:40:00'<=t_now<'16:00:00': hb_session='AFTER_PREP'
                        elif '08:00:00'<=t_now<'09:00:00': hb_session='PREOPEN'
                        else: hb_session='CLOSED'
                    diagnostics.submit(folder/'heartbeat.json',{'time':now().isoformat(),'mode':policy.mode,'market':engine.market,
                        'market_environment':position_health.market_context(now()),
                        'session':hb_session,'market_phase':phase_str,'venue':getattr(engine,'after_default_venue','SOR') if sess_name=='AFTER' else 'KRX',
                        'session_classification':assessment(now())['classification'],'readiness':ready,'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT,
                        'pid':os.getpid(),'identity':process_identity(os.getpid()),
                        'account_ok':engine.account_ok,'radar_ok':radar_ok,'connected':ready['gates']['websocket_reg_ack'],
                        'capital_snapshot':dict(engine.s.get('automation',{}).get('capital_snapshot',{})),
                        'reconcile_checkpoint':dict(getattr(engine,'reconcile_checkpoint_stats',{})),
                        'subscription_refresh_pending':subscriptions.refresh_pending,'subscription_tick_waiting':sorted(subscriptions.waiting),
                        'watch_observation':{s:dict(engine.observation_timeline.rows.get(s,{})) for s in lookup},
                        'feed_queue_depth':getattr(engine,'feed_queue_size',None),'event_loop_lag_seconds':getattr(engine,'event_loop_lag',None),
                        'diagnostic_event_writer':diagnostic_events.stats(),
                        'available_capital':min(engine.available_cash,auto.capital_limit),'approved_cap':auto.capital_limit,
                        'bot_cash':engine.s['cash'],'positions':len(engine.s['positions']),'pending':len(engine.active_orders()),
                        'strategy':'MANAGE' if engine.s['positions'] else 'SCAN' if lookup else 'QUIET','counts':counts,
                        'entry_delivery':dict(getattr(engine,'entry_delivery_counts',{})),
                        'entry_quote_retry':{'pending':len(getattr(engine,'_quote_entry_pending',{})),
                            'counts':dict(getattr(engine,'_quote_entry_counts',{}))},
                        'entry_funnel':engine.candidate_audit.funnel_snapshot(now(),compact=True),
                        'entry_funnel_errors':getattr(engine,'entry_funnel_errors',0)+engine.candidate_audit.funnel_errors,
                        'trade_review_capture':capture.stats() if capture is not None else {'available':False}})
                    last_heartbeat=time.monotonic()
                if done: break
            except KeyboardInterrupt:
                user_command(policy,'STOP'); engine.event('STOP_REQUESTED',reason='키보드 중단; 봇 포지션 보호 유지')
            except (ValueError,KeyError,TypeError,OSError) as e:
                # An error may surface during a diagnostic event after the
                # loop's check. Detach before HALT and subsequent protection.
                try:check_diagnostic_event_writer(engine,diagnostic_events)
                except OSError:pass
                engine.account_ok=False; engine.halt('데이터/상태 오류: '+type(e).__name__)
                if engine.s.get('halt_info',{}).get('classification')=='RECOVERABLE_IO':
                    engine.s['halt_info']['exception']=error_record(e); engine.save()
                engine.event('RUNTIME_ERROR',**error_record(e))
                # 보호를 끊지 않고 다음 정상 이벤트/계좌 대조를 기다린다.
                time.sleep(.25)
    except Exception as e:
        fatal=type(e).__name__; engine.event('WORKER_FATAL',**error_record(e)); raise
    finally:
        # Critical shutdown records always retain their direct synchronous path.
        engine.diagnostic_event_writer=None
        try:diagnostic_events.close(timeout=5.)
        except OSError as error:
            fatal=fatal or 'DIAGNOSTIC_EVENT_CLOSE_ERROR';engine.account_ok=False
            engine.event('DIAGNOSTIC_EVENT_WRITER_ERROR',**error_record(error),
                writer=diagnostic_events.stats(),order_ledger_affected=False)
        engine.event('DIAGNOSTIC_EVENT_WRITER_CLOSED',writer=diagnostic_events.stats(),
            order_ledger_affected=False)
        diagnostic_complete=diagnostics.close(timeout=5.)
        if not diagnostic_complete:fatal=fatal or 'DIAGNOSTIC_SNAPSHOT_CLOSE_TIMEOUT'
        try:diagnostics.check_error()
        except OSError as error:
            fatal=fatal or type(error).__name__;engine.account_ok=False
            engine.event('DIAGNOSTIC_SNAPSHOT_ERROR',**error_record(error))
        engine.event('DIAGNOSTIC_SNAPSHOT_WRITER_CLOSED',complete=diagnostic_complete,
            **diagnostics.snapshot(),order_ledger_affected=False)
        shadow.close()
        if context_worker:context_worker.close()
        if context_checkpoint:
            complete=context_checkpoint.close(timeout=1.)
            engine.event('REFERENCE_CACHE_CHECKPOINT_CLOSED',complete=complete,
                **context_checkpoint.snapshot(),scope='REFERENCE_CACHE_ONLY',order_ledger_affected=False)
        # A successful read-only probe intentionally closes its transport.
        # Observe while it is still open: on_close marks transport_failed even
        # for our own shutdown. Earlier real disconnects/stale evidence still
        # fail here, and cleanup failures still veto passed below. This is a
        # completed probe result, never a live heartbeat or order permission.
        check_readiness=(readiness_snapshot(engine,counts,radar_ok,now(),detector)
                         if readiness_check else None)
        if data_active: feed.close(); radar.close(); account.close()
        capture=getattr(engine,'trade_review',None)
        if capture is not None:
            capture.close()
            engine.event('TRADE_REVIEW_CAPTURE_CLOSED',capture=capture.stats())
        if engine.candidate_audit.started:
            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
            if diagnostic_complete:atomic_json(root/'runtime/candidate_summary.json',summary,fsync=False)
            engine.event('CANDIDATE_CYCLE_SUMMARY',**summary)
        engine.save(); reporter.submit(rows,engine.s); reporter.close()
        try:
            if not diagnostic_complete:raise RuntimeError('DIAGNOSTIC_WRITER_STILL_ACTIVE_NO_TERMINAL_OVERWRITE')
            atomic_json(folder/'heartbeat.json',{'time':now().isoformat(),'mode':policy.mode,'market':getattr(engine,'market','3'),
                'session':'CLOSED' if not in_session else 'WORKER_TERMINATED',
                'market_phase':'CLOSED',
                'venue':'KRX',
                'readiness':{'state':'STOPPED','gates':{},'missing':['WORKER_TERMINATED']},
                'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT,
                'pid':os.getpid(),'identity':process_identity(os.getpid()),
                'account_ok':False,'radar_ok':False,'connected':False,'fatal':fatal,
                'subscription_refresh_pending':False,'subscription_tick_waiting':[],
                'available_capital':0,'approved_cap':auto.capital_limit,
                'bot_cash':engine.s.get('cash',0.),'positions':len(engine.s['positions']),'pending':len(engine.active_orders()),
                'strategy':'STOPPED','counts':counts,
                'entry_funnel':engine.candidate_audit.funnel_snapshot(now()),
                'entry_funnel_errors':getattr(engine,'entry_funnel_errors',0)+engine.candidate_audit.funnel_errors},fsync=False)
        except Exception: pass
        result={'started':started.isoformat(),'finished':now().isoformat(),'mode':policy.mode,'approved_cap':auto.capital_limit,
                'counts':counts,'remaining_positions':len(engine.s['positions']),'remaining_orders':len(engine.active_orders()),
                'readiness':check_readiness if check_readiness is not None else readiness_snapshot(engine,counts,radar_ok,now(),detector),'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT,
                'fatal':fatal,'real_orders_sent':sum(1 for o in engine.s['orders'].values() if not o['id'].startswith('PAPER-') and o['id']),
                'passed':not fatal and counts['account_checks']>0 and counts['radar_cycles']>0 and counts['subscription_acks']>0 and not engine.s['positions'] and not engine.active_orders()}
        if readiness_check:
            result['readiness_evidence_scope']='BEFORE_INTENTIONAL_TRANSPORT_SHUTDOWN'
            result['passed']=not fatal and result['readiness']['state'] in {'PREOPEN_READY','OPEN_READY'} and not engine.s['positions'] and not engine.active_orders()
            result['classification']=result['readiness']['state'] if result['passed'] else 'INCONCLUSIVE_READINESS'
        if not (control_supported(started) or (getattr(cfg,'after_enabled',False) and after_control_supported(started))):
            result.update(classification='OUT_OF_SUPPORTED_SESSION',passed=None,regression_failure=False)
        elif seconds and not readiness_check and counts['ticks']==0:
            result.update(classification='INCONCLUSIVE_NO_REGULAR_TICKS',passed=None,regression_failure=False)
        atomic_json(root/('automation_check.json' if seconds or readiness_check else 'automation_last_run.json'),result)
    return result

