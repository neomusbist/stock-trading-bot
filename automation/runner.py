"""일봉/계좌 조회는 별도 스레드, 주문/체결/보호 상태는 주 스레드 하나에서 변경한다."""
import copy
from contextlib import nullcontext
import json
import queue
import threading
import time
from dataclasses import replace,asdict
from datetime import datetime
from pathlib import Path
from core.settings import now
from core.util import atomic_json,ProcessLock,exception_details
from core.broker import Rest,Rejected
from core.realtime import Feed,parse_tick
from core.report import export
from candidate_builder import structure
from automation.policy import Policy,load_automation,LEGACY_SYMBOL,BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,legacy_test_release_enabled
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import RadarWorker,observation_structure
from automation.allocation import comparison
from automation.performance import report
from automation.session import supported,assessment
from automation.candidate_audit import CandidateAudit
from automation.observation_settings import ObservationSettings,load_observation

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
        self.observation_settings=ObservationSettings()
        self.thread=threading.Thread(target=self.run,name='account-reader',daemon=True)
    def book_due(self,symbol,stamp=None):
        price=self.external_prices.get(symbol);stamp=time.monotonic() if stamp is None else stamp
        settings=self.observation_settings
        far=symbol in BREAKEVEN_EXITS and price and price<BREAKEVEN_EXITS[symbol]*(1-settings.near_threshold_fraction)
        return stamp-self.book_queries.get(symbol,float('-inf'))>=(settings.far_book_seconds if far else self.interval)
    def run(self):
        while not self.stop.is_set():
            started=time.monotonic(); query_started_at=now().isoformat()
            try:
                fills=self.rest.fills() if self.need_fills else []
                snapshot=self.rest.snapshot()
                snapshot['query_started_at']=query_started_at
                snapshot['books']={}
                snapshot['book_errors']={}
                book_started=time.perf_counter();book_calls=0
                for sym in sorted(set(self.external_symbols)&set(snapshot['holdings'])):
                    due=self.book_due(sym)
                    if not due:
                        if sym in self.last_book:snapshot['books'][sym]=dict(self.last_book[sym])
                        continue
                    try:
                        snapshot['books'][sym]=self.rest.existing_book(sym);self.last_book[sym]=dict(snapshot['books'][sym])
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
    if in_session and engine.s['automation'].get('capital_refresh_pending'):
        account.request.set()
        return True
    return False

def qualify_raw(engine,rows,stamp):
    engine.source_qualification_seen=True
    engine.source_qualification={row['code']:{'source_count':row.get('source_count',0),'timestamp':stamp,
        'economic_reason':row.get('affordability',{}).get('reason_code')} for row in rows}

class SubscriptionState:
    """구독 변경은 소켓/계좌 연결과 별개이며 추가 종목만 새 정규장 틱을 기다린다."""
    def __init__(self,symbols=()):
        self.symbols=set(symbols); self.waiting={}; self.detached=set(); self.refresh_pending=False

    def disconnected(self,stamp):
        self.waiting={sym:stamp for sym in self.symbols}
        self.detached.clear(); self.refresh_pending=False

    def acknowledge(self):
        self.refresh_pending=False

    def refresh(self,engine,detector,feed,desired,stamp):
        desired=set(desired)
        for sym in list(self.detached-desired):
            if not detector.observation_fresh(sym,stamp):
                detector.forget(sym); self.detached.discard(sym)
                engine.event('OBSERVATION_RELEASED',symbol=sym,data_action='RESET_STALE_GAP')
        if desired==self.symbols:return False
        added=desired-self.symbols; removed=self.symbols-desired
        for sym in removed:
            self.waiting.pop(sym,None)
            self.detached.add(sym)
            engine.event('OBSERVATION_RELEASED',symbol=sym,data_action='RETAIN_UNTIL_STALE_GAP',
                stored_completed_bars=len(detector.history.get(sym,[])))
        for sym in added:
            had_data=sym in detector.last
            fresh=detector.observation_fresh(sym,stamp)
            if had_data and not fresh:detector.forget(sym)
            self.detached.discard(sym); self.waiting[sym]=stamp
            engine.event('OBSERVATION_REJOINED',symbol=sym,
                data_action='RETAIN_FRESH' if fresh else 'RESET_STALE_GAP' if had_data else 'NO_DATA',
                fresh_regular_tick_required=True)
        feed.update_symbols(sorted(desired))
        self.symbols=desired; self.refresh_pending=True
        engine.event('SUBSCRIPTION_REFRESH',added=sorted(added),removed=sorted(removed),
            connected=engine.connected,account_ok=engine.account_ok)
        return True

    def tick_ready(self,t,received):
        since=self.waiting.get(t.symbol)
        return since is None or (t.regular and t.time.date()==since.date() and t.time>=since and received>=since)

    def accepted(self,symbol):
        self.waiting.pop(symbol,None)

def prewarm_handoff(engine,detector,feed,observed,real_symbols,prior_priority,drops,sink=None):
    """구독 라우팅 경계에서 승격 종목의 기존 예열 큐만 인계한다. 주문은 하지 않는다."""
    priority=set(real_symbols)
    if not isinstance(getattr(feed,'observation_q',None),queue.Queue):
        if hasattr(feed,'configure_observation'):feed.configure_observation(priority,observed,sink)
        return drops,priority
    critical=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
    critical.update(set(engine.s['automation']['existing'])&set(BREAKEVEN_EXITS))
    promotions=(priority-set(prior_priority))&observed-critical
    promoted=[]
    # emit도 같은 lock을 쓰므로 인계 경계 이후의 새 WATCH 틱은 REAL 큐로 간다.
    with getattr(feed,'subscription_lock',nullcontext()):
        if feed.observation_drops!=drops:
            drops=feed.observation_drops
            for sym in observed-set(prior_priority)-critical:detector.forget(sym)
            engine.event('OBSERVATION_DATA_GAP',SHADOW_ONLY=True,dropped=drops,data_action='RESET_PREWARM',actual_order_permission=False)
        if promotions:
            remaining=[]
            for _ in range(feed.observation_q.qsize()):
                try:item=feed.observation_q.get_nowait()
                except queue.Empty:break
                feed.observation_q.task_done()
                if item[1].symbol in promotions:promoted.append(item)
                else:remaining.append(item)
            for item in remaining:feed.observation_q.put_nowait(item)
        feed.configure_observation(priority,observed,sink)
    for index,(_,tick) in enumerate(promoted):
        detector.ingest(tick,now())
        if critical and (index+1)%64==0:engine.timer(engine.prices)
    if promoted:
        engine.event('OBSERVATION_PREWARM_HANDOFF',symbols=sorted(promotions),queued_ticks=len(promoted),
            SHADOW_ONLY=True,actual_order_permission=False)
    for _ in range(64):
        try:observed_at,tick=feed.observation_q.get_nowait()
        except queue.Empty:break
        if tick.symbol in observed and tick.symbol not in priority and tick.symbol not in critical:detector.ingest(tick,now())
    return drops,priority

def process_message(engine,detector,message,received,symbols,lookup,radar,prices,account,radar_ok,subscriptions=None):
    tr=message.get('trnm')
    if tr in {'ERROR','DISCONNECTED'}:
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
        return 0
    accepted=0
    if tr!='REAL': return accepted
    for item in message.get('data',[]):
        kind=item.get('type')
        if kind=='00':
            engine.on_order_event(item['values']); account.need_fills=bool(engine.active_orders()); account.request.set()
        elif kind=='0s': update_market(engine,str(item['values']['215']))
        elif kind=='0B':
            t=parse_tick(item,received)
            if t.symbol not in symbols:continue
            if subscriptions is not None and not subscriptions.tick_ready(t,received):continue
            if not detector.ingest(t,now()):continue
            if subscriptions is not None:subscriptions.accepted(t.symbol)
            accepted+=1; prices[t.symbol]=t.bid or t.price
            # 중간에 시작했을 때 장시작 이벤트를 못 받은 경우, 신선한 정규장 체결의 공식 FID 290으로 확인.
            if engine.market=='UNKNOWN': engine.market='3'
            finished=list(detector.completed.get(t.symbol,[]))
            engine.on_tick(t,finished[-1]['low'] if finished else None)
            watch=lookup.get(t.symbol)
            if not watch or not radar_ok or not engine.connected: continue
            if watch.get('source_count',len(watch.get('source_details',{})) if 'source_details' in watch else engine.a.min_source_count)<max(2,engine.a.min_source_count):
                engine.event('NO_TRADE',symbol=t.symbol,reason='SOURCE_COUNT_BUY_PERMISSION_DENIED');continue
            engine.names[t.symbol]=watch.get('name')
            engine.market_status[t.symbol]=watch.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
            sig=detector.signal(t,watch)
            if not sig:
                gates=detector.evaluation_gates(t,watch)
                failed=[k for k,v in gates.items() if v['result']=='FAIL']
                engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','PATTERN_OR_GATE_NOT_READY',
                    gates,price=t.price,extra={'failed_gates':failed,'signal_detected':False,'setup_trials':detector.last_eval.get(t.symbol,{}).get('trials',[])})
                continue
            sig['name']=watch.get('name')
            sig['candidate_detected_at']=watch.get('observed_at')
            bars=radar.cache.get((t.time.date(),t.symbol))
            if not bars:
                engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','HISTORY_UNAVAILABLE',
                    detector.evaluation_gates(t,watch),signal=sig,price=t.price)
                continue
            fresh=structure(bars,sig['limit'],replace(engine.c,max_recent_range=.99,min_resistance_room=0 if sig['pattern'] in {'A','C'} else engine.c.min_resistance_room),t.time.date())
            if fresh.get('status')!='ELIGIBLE':
                from automation.decisions import gate
                engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','STRUCTURE_REJECTED',
                    {'structure':gate(False,**fresh)},signal=sig,price=t.price)
                engine.event('NO_TRADE',symbol=t.symbol,reason=fresh['reason']); continue
            sig=detector.signal(t,{**watch,**fresh})
            if not sig:
                engine.decisions.emit('ENTRY_EVAL',t.symbol,'NO_BUY','FRESH_STRUCTURE_SIGNAL_NOT_READY',
                    detector.evaluation_gates(t,{**watch,**fresh}),price=t.price)
                continue
            sig['name']=watch.get('name')
            sig['candidate_detected_at']=watch.get('observed_at')
            # 현재 보유의 기회비용 비교. 교체 청산 뒤에도 새 틱으로 다시 진입 검증한다.
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
            engine.enter(sig,t)
            account.need_fills=bool(engine.active_orders())
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

def run(cfg,root,seconds=0,recover_pilot=False,operational_pilot=False):
    root=Path(root); auto=load_automation(root/'automation.json'); policy=Policy(root/'runtime/automation_policy.json')
    if operational_pilot:
        if not recover_pilot or seconds!=150 or policy.mode not in {'ARMED','STOP','LIVE'} or policy.state.get('releases') or policy.state.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
            raise ValueError('운영 재자격은 기존 보호정책·150초 조회 실행만 허용')
        policy=ReadOnlyPilotPolicy(policy);cfg=replace(cfg,live_mode=False)
    if recover_pilot:
        if seconds!=150 or cfg.live_mode or policy.mode!="ARMED" or policy.state.get("live_authorized") or policy.state.get("releases") or policy.state.get("test_capital_release",{}).get("status") in {"APPROVED","SUBMITTING"}:
            raise ValueError("재자격은 ARMED·실주문 승인 없음·150초 조회만 허용")
        # 파일의 승인한도는 보존하며 이 조회 실행에서만 주문자본을 0으로 제한한다.
        auto=replace(auto,AUTO_TRADING_CAP_KRW=0)
    # 기존 Config의 비용 예약/총위험 계산에도 명시적 수수료를 반영한다.
    effective=replace(cfg,roundtrip_cost_fraction=auto.commission_buy+auto.commission_sell+auto.sell_tax)
    effective.validate()
    rest=Rest(effective); folder=root/'runtime'/('automation-smoke' if seconds else 'automation-'+cfg.broker_environment)
    engine=AutonomousEngine(effective,auto,policy,folder,rest,now)
    observation_settings=load_observation(root);engine.observation_settings=observation_settings
    engine.candidate_audit=CandidateAudit()
    engine.candidate_audit.refresh_seconds=auto.scan_seconds
    atomic_json(folder/'session_settings.json',{'config':asdict(cfg),'effective_config':asdict(effective),'automation':asdict(auto)})
    detector=PatternDetector(effective,auto,engine.costs)
    detector.continuous_observation=True
    # 검사 시간 종료는 STOP과 같으며 LIVE의 시간 제한 검사는 금지한다.
    if seconds and (cfg.live_mode or auto.capital_limit!=0 or policy.mode!='ARMED' or
                    policy.state.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}):
        raise ValueError('조회 검사는 0원·ARMED·live_mode=false·시험매도 승인 비활성 상태에 한정')
    if policy.mode=='OFF' and not engine.s['positions'] and not engine.active_orders():
        engine.event('OFF'); return {'mode':'OFF','real_orders_sent':0}
    def clients():
        rank_rest=Rest(effective,auth_session=rest.auth_session); account_rest=Rest(effective,auth_session=rest.auth_session)
        for client in (rest,rank_rest,account_rest): client.session_guard=lambda:supported(now())
        account_worker=AccountWorker(account_rest,effective.reconcile_seconds);account_worker.observation_settings=observation_settings
        radar_worker=RadarWorker(effective,auto,policy,rank_rest,root)
        radar_worker.observation_sink=lambda rows,stamp,capital:shadow.submit(rows,stamp,capital)
        radar_worker.observation_allowed=not bool(engine.s['positions'] or engine.active_orders())
        return (radar_worker,account_worker,Feed(rest,sorted(symbols)))
    symbols=set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
    subscriptions=SubscriptionState(symbols); priority_symbols=set(symbols)
    radar,account,feed=clients()
    radar.refresh_account=account.request.set
    account.need_fills=bool(engine.active_orders())
    reporter=Reporter(root)
    from automation.observation_worker import ObservationWorker
    observation_rest=Rest(replace(effective,live_mode=False),auth_session=rest.auth_session);observation_rest.session_guard=lambda:supported(now());observation_rest.diagnostic_only=True
    shadow=ObservationWorker(effective,auto,engine.costs,root,observation_rest);shadow.start()
    shadow_state={};observation_drops=0
    lookup={}; rows=[]; prices={}; counts={'ticks':0,'radar_cycles':0,'account_checks':0,'subscription_acks':0}
    last_radar=0.; radar_ok=False; last_report=0.; last_heartbeat=0.;last_board=0.; started=now(); stop_at=time.monotonic()+seconds if seconds else None
    smoke_ended=False; fatal=''; account_generation=0; kill_requested_at=None
    data_active=False; ever_started=False; blocked_day=None; reporter.start()
    engine.event('AUTOMATION_STARTED',mode=policy.mode,approved_cap=auto.capital_limit,live_mode=cfg.live_mode,
                 message='기존 보유 평가금액 제외; 기본 실제 신규 주문 불가')
    try:
        while True:
            try:
                in_session=supported(now())
                if not in_session and blocked_day!=now().date():
                    blocked_day=now().date()
                    engine.event('SESSION_BLOCK',reason_code='OUT_OF_SUPPORTED_SESSION',decision='NO_BUY',strategy_version='V1.1-policy-20261001')
                    if engine.s['positions'] or engine.active_orders():
                        engine.event('POSITION_REMAINS_AFTER_SESSION',reason_code='OVERNIGHT_RISK',positions={s:p['qty'] for s,p in engine.s['positions'].items()},pending=len(engine.active_orders()))
                if in_session and not data_active:
                    if ever_started: radar,account,feed=clients(); radar.refresh_account=account.request.set
                    account.need_fills=bool(engine.active_orders()); radar.extra_symbols=set(engine.s['positions'])
                    radar.start(); account.start(); feed.start(); data_active=True; ever_started=True
                    engine.reset_detector=True
                elif not in_session and data_active:
                    feed.close(); radar.close(); account.close(); data_active=False
                    detector.reset(); engine.disconnect(); engine.account_ok=False; engine.market='UNKNOWN'; radar_ok=False
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
                        engine.reconcile(result['snapshot'],result['fills']); counts['account_checks']+=1; account_generation+=1
                        engine.event('ACCOUNT_LATENCY',**result['snapshot'].get('performance',{}))
                        external=set(engine.s['automation']['existing'])&(set(BREAKEVEN_EXITS)|({TEST_RELEASE_SYMBOL} if legacy_test_release_enabled() else set()))
                        account.external_symbols=external
                        radar.extra_symbols=set(engine.s['positions'])
                        if kill_requested_at is not None and result['started']<kill_requested_at: engine.kill_checked=False
                        if engine.kill_checked: kill_requested_at=None
                    account.need_fills=bool(engine.active_orders())
                while in_session and not radar.q.empty():
                    result=radar.q.get_nowait()
                    if result['error']:
                        radar_ok=False; engine.event('RADAR_UNAVAILABLE',**{k:v for k,v in result.items() if k!='time'})
                    else:
                        if result.get('phase')=='RAW':
                            qualify_raw(engine,result['rows'],result['time'])
                            engine.event('RAW_CANDIDATES',observed_at=result['time'],raw_count=len(result['rows']),
                                candidates=[{k:row.get(k) for k in ('code','name','first_raw_at','first_raw_price','reference_price','source_count','affordability')} for row in result['rows']])
                            continue
                        if 'source_observed_at' in result or any('source_count' in row for row in result['rows']):qualify_raw(engine,result['rows'],result.get('source_observed_at',result['time']))
                        if engine.candidate_audit.started:
                            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
                            atomic_json(root/'runtime/candidate_summary.json',summary)
                            engine.event('CANDIDATE_CYCLE_SUMMARY',**summary)
                        prior_watch=set(lookup)
                        rows=result['rows']; lookup={x['code']:x for x in result['watch']}; radar_ok=True
                        by_symbol={row['code']:row for row in rows}
                        for sym in prior_watch^set(lookup):
                            row=by_symbol.get(sym,{})
                            engine.event('WATCH_TRANSITION',symbol=sym,name=row.get('name'),source_count=row.get('source_count'),
                                from_status='WATCH' if sym in prior_watch else 'OBSERVE',to_status='WATCH' if sym in lookup else 'SHADOW_OR_UNSUBSCRIBED',
                                reason=row.get('reason','RAW_SOURCE_MISSING'),observed_at=result['time'],
                                data_action='RETAIN_CONTINUOUS_IF_SUBSCRIBED' if sym in detector.last else 'NO_DATA',
                                stored_completed_bars=len(detector.history.get(sym,[])),usable_completed_bars=len(detector.usable_bars(sym)))
                        engine.candidate_audit.begin(rows,result['time'],result.get('capital_context',engine.capital_context()))
                        for row in rows:
                            engine.names[row['code']]=row.get('name')
                            engine.market_status[row['code']]=row.get('market_status',{'pass':None,'reason_code':'MARKET_STATUS_UNKNOWN'})
                            if row.get('status') in {'REJECT','UNKNOWN','RESERVE'}:
                                from automation.decisions import gate
                                engine.decisions.emit('ENTRY_EVAL',row['code'],'NO_BUY',row.get('reason') or 'WATCH_CAPACITY_RESERVE',
                                    {'candidate_compression':gate(False,status=row['status'],source_count=row.get('source_count'),
                                        reference_price=row.get('reference_price'),affordability=row.get('affordability'),
                                        market_status=row.get('market_status'),overhead_share=row.get('overhead_share_estimate'),
                                        resistance=row.get('resistance'))},price=row.get('reference_price'))
                        context=engine.capital_context()
                        shadow.submit(rows,result['time'],context,allow_metadata=not bool(engine.s['positions'] or engine.active_orders()))
                        engine.event('CANDIDATE_LATENCY',**result.get('performance',{}))
                        last_radar=time.monotonic(); counts['radar_cycles']+=1
                        engine.event('RADAR_UPDATED',watch=list(lookup),changes=result['changes'])
                if last_radar and time.monotonic()-last_radar>auto.scan_seconds*3: radar_ok=False
                # 실제 수량 변동 뒤에도 다음 후보 주기에서 같은 현금 상한을 사용한다.
                context=engine.capital_context(); prior_context=radar.capital_context
                if context['account_fresh'] and (not prior_context.get('account_fresh') or context['entry_budget']!=prior_context.get('entry_budget')):
                    radar.request.set()
                radar.capital_context=context; radar.available_capital=context['entry_budget']
                shadow.set_capital(context)
                radar.observation_allowed=not bool(engine.s['positions'] or engine.active_orders())
                shadow_state=shadow.runtime_view();radar.radar.momentum=shadow_state.get('evaluations',{})
                desired=set(lookup)|set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()}
                desired.update(set(engine.s['automation']['existing'])&(set(BREAKEVEN_EXITS)|({TEST_RELEASE_SYMBOL} if legacy_test_release_enabled() else set())))
                real_symbols=set(desired)
                observed=set(shadow_state.get('symbols',[]))
                engine.candidate_audit.shadow_count=len(observed)
                desired.update(observed)
                observation_drops,priority_symbols=prewarm_handoff(engine,detector,feed,observed,real_symbols,priority_symbols,observation_drops,shadow.deliver)
                if in_session:
                    subscriptions.refresh(engine,detector,feed,desired,now()); symbols=set(subscriptions.symbols)
                if feed.overflow.is_set():
                    feed.overflow.clear(); engine.halt('실시간 수신 큐 초과'); detector.reset()
                    for sym in list(engine.s['positions']): engine.request_exit(sym,'수신 큐 초과')
                try:
                    if not in_session: raise queue.Empty
                    received,message=feed.q.get(timeout=.25)
                    if message.get('trnm')=='REG' and message.get('return_code') in (0,'0'): counts['subscription_acks']+=1
                    counts['ticks']+=process_message(engine,detector,message,received,real_symbols,lookup,radar,prices,account,radar_ok and not smoke_ended,subscriptions)
                except queue.Empty:
                    if not in_session: time.sleep(.25)
                engine.timer(prices)
                if isinstance(getattr(account,'external_prices',None),dict):
                    prior=account.external_prices;account.external_prices=dict(prices)
                    if any(prices.get(sym,0)>=threshold*(1-observation_settings.near_threshold_fraction) and prior.get(sym,0)<threshold*(1-observation_settings.near_threshold_fraction) for sym,threshold in BREAKEVEN_EXITS.items()):account.request.set()
                request_capital_refresh(engine,account,in_session)
                if engine.candidate_audit.started and time.monotonic()-last_board>=5:
                    summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context());summary['shadow']=len(observed)
                    summary['raw_definition']='5개 ranking API의 일반주 고유종목 합집합; HTS 0168과 동일 목록은 아님'
                    summary['refresh_seconds']=auto.scan_seconds
                    atomic_json(root/'runtime/candidate_summary.json',summary);last_board=time.monotonic()
                if stop_at and time.monotonic()>=stop_at: smoke_ended=True
                if smoke_ended:
                    for o in engine.active_orders(side='BUY'): engine.cancel(o)
                    for sym in list(engine.s['positions']): engine.request_exit(sym,'조회 전용 검사 종료')
                    engine.drive_exits(prices)
                    done=not engine.s['positions'] and not engine.active_orders()
                if time.monotonic()-last_report>=60:
                    summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
                    atomic_json(root/'runtime/candidate_summary.json',summary)
                    engine.save(); reporter.submit(rows,engine.s); last_report=time.monotonic()
                if time.monotonic()-last_heartbeat>=1:
                    atomic_json(folder/'heartbeat.json',{'time':now().isoformat(),'mode':policy.mode,'market':engine.market,
                        'session':assessment(now())['classification'],
                        'account_ok':engine.account_ok,'radar_ok':radar_ok,'connected':engine.connected,
                        'subscription_refresh_pending':subscriptions.refresh_pending,'subscription_tick_waiting':sorted(subscriptions.waiting),
                        'available_capital':min(engine.available_cash,auto.capital_limit),'approved_cap':auto.capital_limit,
                        'bot_cash':engine.s['cash'],'positions':len(engine.s['positions']),'pending':len(engine.active_orders()),
                        'strategy':'MANAGE' if engine.s['positions'] else 'SCAN' if lookup else 'QUIET','counts':counts})
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
        fatal=type(e).__name__; engine.event('WORKER_FATAL',error=fatal); raise
    finally:
        shadow.close()
        if data_active: feed.close(); radar.close(); account.close()
        if engine.candidate_audit.started:
            summary=engine.candidate_audit.summary(now().isoformat(),engine.capital_context())
            atomic_json(root/'runtime/candidate_summary.json',summary); engine.event('CANDIDATE_CYCLE_SUMMARY',**summary)
        engine.save(); reporter.submit(rows,engine.s); reporter.close()
        result={'started':started.isoformat(),'finished':now().isoformat(),'mode':policy.mode,'approved_cap':auto.capital_limit,
                'counts':counts,'remaining_positions':len(engine.s['positions']),'remaining_orders':len(engine.active_orders()),
                'fatal':fatal,'real_orders_sent':sum(1 for o in engine.s['orders'].values() if not o['id'].startswith('PAPER-') and o['id']),
                'passed':not fatal and counts['account_checks']>0 and counts['radar_cycles']>0 and counts['subscription_acks']>0 and not engine.s['positions'] and not engine.active_orders()}
        if not supported(started):
            result.update(classification='OUT_OF_SUPPORTED_SESSION',passed=None,regression_failure=False)
        elif seconds and counts['ticks']==0:
            result.update(classification='INCONCLUSIVE_NO_REGULAR_TICKS',passed=None,regression_failure=False)
        atomic_json(root/('automation_check.json' if seconds else 'automation_last_run.json'),result)
    return result

