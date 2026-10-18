"""일봉/계좌 조회는 별도 스레드, 주문/체결/보호 상태는 주 스레드 하나에서 변경한다."""
import copy
import os
from contextlib import nullcontext
from operator_process import process_identity
import json
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
from core.realtime import Feed,parse_tick
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
from automation.observation_settings import ObservationSettings,load_observation
from automation.focus_selection import select_focus

def error_record(error):
    record=exception_details(error)
    if isinstance(error,Rejected):
        record.update(error=str(error),api=error.api,raw_response=error.response,
            return_code=error.response.get('return_code') if error.response else None,
            return_msg=error.response.get('return_msg') if error.response else None)
    return record

def user_command(policy,command):
    with ProcessLock(policy.path.parent/'control.lock'): policy.command(command)

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
                             subscription_gap_seconds=gap)
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

def prewarm_handoff(engine,detector,feed,observed,real_symbols,prior_priority,drops,sink=None,shadow_detector=None):
    """구독 라우팅 경계에서 승격 종목의 기존 예열 큐와 관찰 상태를 인계한다. 주문은 하지 않는다."""
    priority=set(real_symbols)
    if shadow_detector is None:
        if hasattr(sink,'__self__') and not str(type(sink)).startswith("<class 'unittest.mock."):
            worker = getattr(sink, '__self__', None)
            shadow_detector = getattr(getattr(worker, 'observer', None), 'detector', None)
        elif hasattr(sink, 'shadow_detector') and not str(type(sink)).startswith("<class 'unittest.mock."):
            shadow_detector = sink.shadow_detector
    critical=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
    critical.update(set(engine.s['automation']['existing'])&set(BREAKEVEN_EXITS))
    promotions=(priority-set(prior_priority))&observed-critical
    demotions=(set(prior_priority)-priority)&observed-critical
    if shadow_detector:
        for sym in promotions:
            detector.handoff_from(shadow_detector, sym)
        for sym in demotions:
            shadow_detector.handoff_from(detector, sym)
    if not isinstance(getattr(feed,'observation_q',None),queue.Queue):
        if hasattr(feed,'configure_observation'):feed.configure_observation(priority,observed,sink)
        return drops,priority
    promoted=[]
    with getattr(feed,'subscription_lock',nullcontext()):
        if feed.observation_drops!=drops:
            drops=feed.observation_drops
            for sym in observed-set(prior_priority)-critical:detector.forget(sym)
            engine.event('OBSERVATION_DATA_GAP',SHADOW_ONLY=True,dropped=drops,data_action='RESET_PREWARM',actual_order_permission=False)
        feed.configure_observation(priority,observed,sink)
    if promotions and hasattr(feed, 'observation_q'):
        remaining=[]
        for _ in range(feed.observation_q.qsize()):
            try:item=feed.observation_q.get_nowait()
            except queue.Empty:break
            feed.observation_q.task_done()
            if item[1].symbol in promotions:promoted.append(item)
            else:remaining.append(item)
        for item in remaining:
            try: feed.observation_q.put_nowait(item)
            except queue.Full: break
    for index,(received,tick) in enumerate(promoted):
        detector.ingest(tick,received)
        if getattr(engine,'fast_strategy',None) is not None:
            engine.fast_strategy.ingest_tick(tick,received)
        if critical and (index+1)%64==0:engine.timer(engine.prices)
    if promoted:
        engine.event('OBSERVATION_PREWARM_HANDOFF',symbols=sorted(promotions),queued_ticks=len(promoted),
            SHADOW_ONLY=True,actual_order_permission=False)
    drained = 0
    drain_deadline = time.monotonic() + 0.01
    while drained < 512 and time.monotonic() < drain_deadline:
        try: observed_at, tick = feed.observation_q.get_nowait()
        except queue.Empty: break
        drained += 1
        if tick.symbol in observed and tick.symbol not in priority and tick.symbol not in critical:
            detector.ingest(tick, observed_at)
            if getattr(engine,'fast_strategy',None) is not None:
                engine.fast_strategy.ingest_tick(tick, observed_at)
    return drops, priority

def apply_focus_selection(engine,lookup,by_symbol,selection):
    """main thread에서 WATCH 자격 표시만 동기화한다. detector/주문 상태는 바꾸지 않는다."""
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
                     'watch_type':'FALLBACK_SOURCE1' if source1 else 'PRIMARY','first_watch_at':first_watch}
        if sym in engine.source_qualification:
            engine.source_qualification[sym]['fallback_source1']=source1

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
        lookup[sym]={**row,'status':'WATCH','fallback_source1':source1,
                     'watch_type':'FALLBACK_SOURCE1' if source1 else 'PRIMARY','first_watch_at':first_watch}
        if sym in engine.source_qualification:engine.source_qualification[sym]['fallback_source1']=source1
    return selection


def latest_evaluation_watch(watch,radar,symbol):
    """Use refreshed immutable metadata without REST work on the order thread."""
    view=dict(watch)
    cache=getattr(radar,'status_cache',None)
    cached=cache.get(symbol) if isinstance(cache,dict) else None
    if not isinstance(cached,dict):return view
    old=watch.get('market_status',{})
    try:
        newer=datetime.fromisoformat(cached['checked_at'])
        prior=datetime.fromisoformat(old['checked_at']) if old.get('checked_at') else None
        if prior is None or newer>=prior:view['market_status']=dict(cached)
    except (KeyError,TypeError,ValueError):
        # Malformed status is not fresh normal-status evidence.
        view['market_status']={'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'}
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

def focus_readiness(detector,shadow_readiness,stamp):
    """WATCH ticks are routed to main; rank them by their own full evaluation."""
    from automation.focus_selection import calculate_tier,unviable_price_geometry
    result=dict(shadow_readiness)
    fast_strat=getattr(detector,'fast_strategy',None)
    with detector._state_lock:
        for sym,evaluation in detector.last_eval.items():
            tick_at=evaluation.get('time')
            if not isinstance(tick_at,datetime) or not 0<=(stamp-tick_at).total_seconds()<=15:continue
            gates=evaluation.get('gates',{})
            fast_ready=False
            if fast_strat is not None and gates.get('market_status',{}).get('result')=='PASS':
                active_fast=fast_strat.active_signals.get(sym)
                if active_fast is not None and 0<=(stamp-active_fast.created_at).total_seconds()<=active_fast.ttl_seconds:
                    fast_ready=True
            result[sym]={'timestamp':tick_at.isoformat(),
                'tier':0 if fast_ready else calculate_tier(gates,evaluation.get('trials',[])),
                'price_blocked':False if fast_ready else unviable_price_geometry({'setup_trials':evaluation.get('trials',[])}),
                'fast_signal_ready':fast_ready,
                'gates':{k:{'result':gates.get(k,{}).get('result')} for k in ('acceleration','liquidity')}}
    return result

def evaluate_actual_entry(engine,detector,t,watch,radar,prices,account,last_evaluated_frame=None):
    if not watch or not engine.connected: return None
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
    if watch.get('observation_only'):
        from automation.decisions import gate
        engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','ACCOUNT_CAPITAL_UNAVAILABLE',
                              {'connection_account':gate(False,observation_only=True)},price=t.price)
        return None
    if last_evaluated_frame is not None:
        key=(t.time,t.cumulative)
        if last_evaluated_frame.get(t.symbol)==key:return None
        last_evaluated_frame[t.symbol]=key
    src_count=watch.get('source_count',len(watch.get('source_details',{})) if 'source_details' in watch else engine.a.min_source_count)
    if src_count<max(2,engine.a.min_source_count) and not watch.get('fallback_source1'):
        engine.event('NO_TRADE',symbol=t.symbol,reason='SOURCE_COUNT_BUY_PERMISSION_DENIED');return None
    watch=latest_evaluation_watch(watch,radar,t.symbol)
    engine.names[t.symbol]=watch.get('name')
    engine.market_status[t.symbol]=watch.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
    sig=detector.signal(t,watch)
    if not sig and getattr(engine.a, 'fast_momentum_enabled', False) and getattr(engine, 'fast_strategy', None) is not None:
        is_live = (engine.policy.mode == 'LIVE' and
                   bool(engine.policy.state.get('live_authorized')) and
                   bool(engine.c.live_mode))
        watch_eval = dict(watch)
        if getattr(detector, 'price_context', None) is not None:
            structure_now = engine.fast_strategy.get_tape(t.symbol).structure(t.time)
            ctx_snapshot = detector.price_context.snapshot(
                t.symbol, t, t.ask,
                support=structure_now.get('support') if structure_now.get('pass') else None,
                breakout=structure_now.get('breakout') if structure_now.get('pass') else None,
            )
            peak = ctx_snapshot.get('nearest_confirmed_peak')
            if peak:
                watch_eval['nearest_confirmed_peak'] = peak
        fast_sig = engine.fast_strategy.evaluate(
            t, watch_eval,
            capital_limit=engine.auto_available_capital(),
            risk_budget=engine.risk_equity() * engine.c.risk_fraction,
            ingest=False,
            received=engine.clock(),
            live_authorized=is_live
        )
        if fast_sig is not None:
            sig = fast_sig.to_engine_signal(watch)
    if not sig:
        gates=detector.evaluation_gates(t,watch)
        failed=[k for k,v in gates.items() if v['result']=='FAIL']
        engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','PATTERN_OR_GATE_NOT_READY',
            gates,price=t.price,extra={'failed_gates':failed,'signal_detected':False,'setup_trials':detector.last_eval.get(t.symbol,{}).get('trials',[])})
        return None
    sig['name']=watch.get('name')
    sig['fallback_source1']=bool(watch.get('fallback_source1'))
    bars=radar.cache.get((t.time.date(),t.symbol))
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
        fresh=structure(bars,sig['limit'],replace(engine.c,max_recent_range=.99,min_resistance_room=0 if sig['pattern'] in {'A','C'} else engine.c.min_resistance_room),t.time.date())
        if fresh.get('status')!='ELIGIBLE':
            from automation.decisions import gate
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','STRUCTURE_REJECTED',
                {'structure':gate(False,**fresh)},signal=sig,price=t.price)
            engine.event('NO_TRADE',symbol=t.symbol,reason=fresh['reason']); return None
        sig=detector.signal(t,{**watch,**fresh})
        if not sig:
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','FRESH_STRUCTURE_SIGNAL_NOT_READY',
                detector.evaluation_gates(t,{**watch,**fresh}),price=t.price,
                extra={'signal_detected':False,'setup_trials':detector.last_eval.get(t.symbol,{}).get('trials',[])})
            return None
        sig['name']=watch.get('name')
        annotate_signal(engine,detector,watch,sig)
        sig['fallback_source1']=bool(watch.get('fallback_source1'))
        sig=detector.signal(t,{**watch,**fresh})
        if not sig:
            engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','FRESH_STRUCTURE_SIGNAL_NOT_READY',
                detector.evaluation_gates(t,{**watch,**fresh}),price=t.price,
                extra={'signal_detected':False,'setup_trials':detector.last_eval.get(t.symbol,{}).get('trials',[])})
            return None
        sig['name']=watch.get('name')
        annotate_signal(engine,detector,watch,sig)
        sig['fallback_source1']=bool(watch.get('fallback_source1'))
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


def process_message(engine,detector,message,received,symbols,lookup,radar,prices,account,radar_ok,subscriptions=None):
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
            accepted+=1;engine.last_regular_tick_at=t.time;engine.last_regular_tick=t
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
            if not watch or not radar_ok or not engine.connected: continue
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
    try:
        issued=datetime.fromisoformat(heartbeat['time']);elapsed=(stamp-issued).total_seconds()
        if issued.tzinfo is None or issued.date()!=stamp.date() or not 0<=elapsed<=10:raise ValueError('heartbeat expired')
    except (KeyError,ValueError,TypeError):
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
    if ready.get('order_ready') is False:
        ready['off_reasons']=list(ready.get('off_reasons') or ready.get('missing') or ['ENGINE_DATA_NOT_READY'])
        return ready
    missing=list(ready.get('missing',[]))
    if age is None or not 0<=age<=cfg.stale_seconds:
        if 'FRESH_REGULAR_TICK_NOT_READY' not in missing:missing.append('FRESH_REGULAR_TICK_NOT_READY')
        ready['state']='WAITING';ready['missing']=missing
        loop_lag=ready.get('event_loop_lag_seconds',heartbeat.get('event_loop_lag_seconds'))
        queue_size=ready.get('feed_queue_depth',heartbeat.get('feed_queue_depth'))
        if type(loop_lag) in (int,float) and loop_lag>1:ready['tick_lag_source']='EVENT_LOOP_LAG'
        elif type(queue_size) in (int,float) and queue_size>5:ready['tick_lag_source']='QUEUE_BUFFER'
        elif type(loop_lag) in (int,float) and type(queue_size) in (int,float):ready['tick_lag_source']='WS_SOURCE_SILENT'
        elif ready.get('tick_lag_source')!='WS_SOURCE_SILENT':ready['tick_lag_source']='READINESS_LAG_UNKNOWN'
    account_age=ready.get('account_age_seconds')
    if type(account_age) in (int,float) and account_age+elapsed>cfg.reconcile_seconds*3:
        if 'ACCOUNT_FRESH_NOT_READY' not in missing:missing.append('ACCOUNT_FRESH_NOT_READY')
        ready.update(state='WAITING',missing=missing)
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
                recovered=detector.price_context.load_cache(cached,now())
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
    shadow.observer.timeline=engine.observation_timeline
    if auto.entry_context_enabled:shadow.observer.detector.price_context=copy.deepcopy(detector.price_context)
    shadow.start()
    bind_candidate_observation(radar,shadow,engine)
    context_worker=None
    if auto.intraday_context_enabled:
        from automation.intraday_context import IntradayContextWorker
        context_rest=Rest(replace(effective,live_mode=False),auth_session=rest.auth_session)
        context_rest.diagnostic_only=True;context_rest.session_guard=observation_rest.session_guard
        context_worker=IntradayContextWorker(context_rest);context_worker.start()
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
    try:
        while True:
            try:
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
                policy.refresh()
                engine.recover_io_halt()
                if policy.mode=='KILL' and policy.state['revision']!=engine.control_revision:
                    kill_requested_at=time.monotonic(); account.request.set()
                done=engine.controls(prices)
                while in_session and not account.q.empty():
                    result=account.q.get_nowait()
                    if result['error']:
                        engine.account_ok=False; engine.event('ACCOUNT_UNAVAILABLE',**{k:v for k,v in result.items() if k!='started'})
                    elif time.monotonic()-result['started']>effective.reconcile_seconds*3:
                        engine.account_ok=False; engine.event('ACCOUNT_UNAVAILABLE',error='조회 지연')
                    else:
                        for sym,error in result['snapshot'].get('book_errors',{}).items(): engine.event('EXISTING_BOOK_UNAVAILABLE',symbol=sym,**error)
                        position_health.probe_results(result['snapshot'],now())
                        reconciled=engine.reconcile(result['snapshot'],result['fills']); counts['account_checks']+=1; account_generation+=1
                        if reconciled is False and engine.s.get('automation',{}).get('reconcile_deferred_pending'):
                            engine.s['automation']['reconcile_deferred_pending']=False
                            account.request.set()
                        for sym,book in result['snapshot'].get('books',{}).items():
                            position_health.accept_book(sym,book,now(),source='ka10004')
                        engine.event('ACCOUNT_LATENCY',**result['snapshot'].get('performance',{}))
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
                            rows,selection=apply_qualified_progress(engine,lookup,rows,result,
                                focus_readiness(detector,shadow.runtime_view()['evaluations'],now()),effective,auto,now())
                            if selection is not None:
                                radar_ok=True;last_radar=time.monotonic()
                                engine.last_radar_at=datetime.fromisoformat(result['time'])
                                by_symbol={r['code']:r for r in rows}
                                focus_promotions.extend(selection.promoted)
                                engine.candidate_audit.begin(rows,result['time'],result.get('capital_context',{}))
                                submit_candidate_observation(engine,shadow,rows,result['time'],engine.capital_context())
                                engine.event('FOCUS_SELECTION_APPLIED',basis='OPENING_VERIFIED_PROGRESS',
                                    promoted=selection.promoted,demoted=selection.demoted,
                                    next_watch=selection.next_watch,previous_watch=selection.previous_watch,
                                    primary_reason=selection.primary_reason,applied_at=selection.focus_epoch,
                                    tier_map=selection.tier_map,priority_tuples=selection.priority_tuples,
                                    compression_batch_complete=False)
                            continue
                        if result.get('phase')=='RAW':
                            engine.observation_timeline.raw(result['rows'],datetime.fromisoformat(result['time']))
                            qualify_raw(engine,result['rows'],result['time'],is_raw_phase=True)
                            engine.event('RAW_CANDIDATES',observed_at=result['time'],raw_count=len(result['rows']),
                                candidates=[{k:row.get(k) for k in ('code','name','first_raw_at','first_raw_price','reference_price','source_count','affordability')} for row in result['rows']])
                            continue
                        if 'source_observed_at' in result or any('source_count' in row for row in result['rows']):qualify_raw(engine,result['rows'],result.get('source_observed_at',result['time']),is_raw_phase=False)
                        if engine.candidate_audit.started:
                            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
                            atomic_json(root/'runtime/candidate_summary.json',summary,fsync=False)
                            engine.event('CANDIDATE_CYCLE_SUMMARY',**summary)
                        prior_watch=set(lookup)
                        rows=result['rows'];radar_ok=True;engine.last_radar_at=datetime.fromisoformat(result['time'])
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
                if focus_dirty:
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
                    while not context_worker.q.empty():
                        packet=context_worker.q.get_nowait()
                        sym=packet['symbol'];received=datetime.fromisoformat(packet['received_at'])
                        if sym not in reference_targets or packet['venue']!=venue or received.date()!=now().date():continue
                        if not packet.get('error'):
                            if sym in lookup:
                                with detector._state_lock:detector.price_context.merge_history(sym,packet['bars'],received,venue)
                            with shadow.observer.detector._state_lock:shadow.observer.detector.price_context.merge_history(sym,packet['bars'],received,venue)
                            try:
                                atomic_json(root/'runtime'/'price_context_cache.json',detector.price_context.dump_cache())
                            except Exception:pass
                        engine.event('INTRADAY_REFERENCE_READY' if not packet.get('error') else 'INTRADAY_REFERENCE_PENDING',
                            symbol=sym,venue=venue,received_at=packet['received_at'],known_bars=len(packet['bars']),
                            source='REST_REFERENCE',restored_signal_bars=0,restored_warmup=False,error=packet.get('error'))
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
                if retained!=retained_observation:
                    engine.event('OBSERVATION_RETENTION',symbols=sorted(retained),
                        grace_seconds=observation_grace,subscription_count=len(desired),
                        capacity=observation_settings.max_symbols,actual_order_permission=False)
                observation_drops,priority_symbols=prewarm_handoff(
                    engine,detector,feed,observed,real_symbols,priority_symbols,observation_drops,
                    shadow.deliver,
                    shadow_detector=getattr(getattr(shadow, 'observer', None), 'detector', None)
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
                        counts['ticks']+=process_message(engine,detector,message,received,real_symbols,lookup,radar,prices,account,radar_ok and not smoke_ended,subscriptions)
                    drain_feed(feed,consume_frame,timeout=.02)
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
                    atomic_json(root/'runtime/candidate_summary.json',summary,fsync=False);last_board=time.monotonic()
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
                    atomic_json(root/'runtime/candidate_summary.json',summary,fsync=False)
                    engine.save(); reporter.submit(rows,engine.s); last_report=time.monotonic()
                if time.monotonic()-last_heartbeat>=1:
                    capture=getattr(engine,'trade_review',None)
                    if capture is not None:capture.set_scope(lookup,engine.s['positions'],now())
                    for stage in engine.observation_timeline.drain_events():
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
                    atomic_json(folder/'heartbeat.json',{'time':now().isoformat(),'mode':policy.mode,'market':engine.market,
                        'market_environment':position_health.market_context(now()),
                        'session':hb_session,'market_phase':phase_str,'venue':getattr(engine,'after_default_venue','SOR') if sess_name=='AFTER' else 'KRX',
                        'session_classification':assessment(now())['classification'],'readiness':ready,'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT,
                        'pid':os.getpid(),'identity':process_identity(os.getpid()),
                        'account_ok':engine.account_ok,'radar_ok':radar_ok,'connected':ready['gates']['websocket_reg_ack'],
                        'subscription_refresh_pending':subscriptions.refresh_pending,'subscription_tick_waiting':sorted(subscriptions.waiting),
                        'watch_observation':{s:dict(engine.observation_timeline.rows.get(s,{})) for s in lookup},
                        'feed_queue_depth':getattr(engine,'feed_queue_size',None),'event_loop_lag_seconds':getattr(engine,'event_loop_lag',None),
                        'available_capital':min(engine.available_cash,auto.capital_limit),'approved_cap':auto.capital_limit,
                        'bot_cash':engine.s['cash'],'positions':len(engine.s['positions']),'pending':len(engine.active_orders()),
                        'strategy':'MANAGE' if engine.s['positions'] else 'SCAN' if lookup else 'QUIET','counts':counts,
                        'trade_review_capture':capture.stats() if capture is not None else {'available':False}},fsync=False)
                    last_heartbeat=time.monotonic()
                if done: break
            except KeyboardInterrupt:
                user_command(policy,'STOP'); engine.event('STOP_REQUESTED',reason='키보드 중단; 봇 포지션 보호 유지')
            except (ValueError,KeyError,TypeError,OSError) as e:
                engine.account_ok=False; engine.halt('데이터/상태 오류: '+type(e).__name__)
                if engine.s.get('halt_info',{}).get('classification')=='RECOVERABLE_IO':
                    engine.s['halt_info']['exception']=error_record(e); engine.save()
                engine.event('RUNTIME_ERROR',**error_record(e))
                # 보호를 끊지 않고 다음 정상 이벤트/계좌 대조를 기다린다.
                time.sleep(.25)
    except Exception as e:
        fatal=type(e).__name__; engine.event('WORKER_FATAL',**error_record(e)); raise
    finally:
        shadow.close()
        if context_worker:context_worker.close()
        if data_active: feed.close(); radar.close(); account.close()
        capture=getattr(engine,'trade_review',None)
        if capture is not None:
            capture.close()
            engine.event('TRADE_REVIEW_CAPTURE_CLOSED',capture=capture.stats())
        if engine.candidate_audit.started:
            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
            atomic_json(root/'runtime/candidate_summary.json',summary,fsync=False); engine.event('CANDIDATE_CYCLE_SUMMARY',**summary)
        engine.save(); reporter.submit(rows,engine.s); reporter.close()
        try:
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
                'strategy':'STOPPED','counts':counts},fsync=False)
        except Exception: pass
        result={'started':started.isoformat(),'finished':now().isoformat(),'mode':policy.mode,'approved_cap':auto.capital_limit,
                'counts':counts,'remaining_positions':len(engine.s['positions']),'remaining_orders':len(engine.active_orders()),
                'readiness':readiness_snapshot(engine,counts,radar_ok,now(),detector),'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT,
                'fatal':fatal,'real_orders_sent':sum(1 for o in engine.s['orders'].values() if not o['id'].startswith('PAPER-') and o['id']),
                'passed':not fatal and counts['account_checks']>0 and counts['radar_cycles']>0 and counts['subscription_acks']>0 and not engine.s['positions'] and not engine.active_orders()}
        if readiness_check:
            result['passed']=not fatal and result['readiness']['state'] in {'PREOPEN_READY','OPEN_READY'} and not engine.s['positions'] and not engine.active_orders()
            result['classification']=result['readiness']['state'] if result['passed'] else 'INCONCLUSIVE_READINESS'
        if not (control_supported(started) or (getattr(cfg,'after_enabled',False) and after_control_supported(started))):
            result.update(classification='OUT_OF_SUPPORTED_SESSION',passed=None,regression_failure=False)
        elif seconds and not readiness_check and counts['ticks']==0:
            result.update(classification='INCONCLUSIVE_NO_REGULAR_TICKS',passed=None,regression_failure=False)
        atomic_json(root/('automation_check.json' if seconds or readiness_check else 'automation_last_run.json'),result)
    return result

