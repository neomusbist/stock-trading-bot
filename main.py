"""장전 수집 → 감시 → 실시간 진입후보 → 체결/보호 관리의 단일 진입점."""
import argparse
import os
import json
import queue
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
from core.settings import Config, load_config, now, KST
from core.util import ProcessLock, atomic_json, read_csv, write_csv
from core.broker import Rest
from core.strategy import Detector, Tick
from core.engine import Engine
from core.report import export
from candidate_builder import build, structure, bars_from_csv

def prerequisites():
    import openpyxl
    import websocket

def load_credentials(path=None):
    # 기존 설치의 .env는 읽기만 한다. 키/토큰을 복사하거나 출력하지 않는다.
    path=Path(path) if path else ROOT/'.env'
    if not path.exists() and path==ROOT/'.env' and (ROOT/'legacy_credentials_enabled.local').exists():
        path=Path.home()/'Desktop/kiwoom_data_collector/.env'
    if not path.exists(): return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if '=' not in line or line.lstrip().startswith('#'): continue
        k,v=line.split('=',1); k=k.strip()
        if k in ('KIWOOM_APP_KEY','KIWOOM_APP_SECRET'):
            os.environ.setdefault(k,v.strip().strip('\"').strip("'"))

def replay_demo(cfg):
    # 별도 폴더·별도 설정·합성 데이터. 실계좌/실제 수익성과 무관한 배선 시험.
    cfg=replace(cfg,live_mode=False)
    day=date(2026,9,30); root=ROOT/'demo_output'
    root.mkdir(exist_ok=True)
    folder=root/f'run-{time.time_ns()}'
    clock=[datetime(2026,9,30,9,0,0,tzinfo=KST)]
    engine=Engine(cfg,folder,clock=lambda:clock[0]); engine.connected=True; detector=Detector(cfg)
    bars=[]; d=day-timedelta(days=1100)
    while d<day:
        if d.weekday()<5: bars.append({'datetime':d.isoformat(),'open':9900.,'high':9950.,'low':9850.,'close':9900.,'volume':2000000.})
        d+=timedelta(days=1)
    watch={'code':'999990','name':'합성_배선시험',**structure(bars,10000,cfg,day)}
    watch['status']='WATCH'
    atomic_json(folder/'watchlist.json',watch)
    ticks=[]; cumulative=0
    for i in range(160):
        px=10000 if i<125 else (10000+(i-124)*15 if i<140 else (10300 if i<148 else 10080))
        volume=100 if i<125 else 3000
        cumulative+=volume; clock[0]=datetime(2026,9,30,9,0,0,tzinfo=KST)+timedelta(seconds=i)
        tick=Tick('999990',clock[0],px,volume,cumulative,px+10,px-10)
        ticks.append({**asdict(tick),'time':tick.time.isoformat()})
        if detector.ingest(tick,clock[0]):
            closed=list(detector.completed.get(tick.symbol,[]))
            engine.on_tick(tick,closed[-1]['low'] if closed else None)
            sig=detector.signal(tick,watch)
            if sig: engine.enter(sig,tick)
            engine.timer({tick.symbol:tick.bid})
    write_csv(folder/'replay_ticks.csv',ticks)
    report=export(root,[watch],engine.s)
    atomic_json(root/'latest_run.json',{'folder':str(folder),'entries':engine.s['entries'],'trades':engine.s['trades'],
        'open_positions':len(engine.s['positions']),'report':str(report)})
    print(f'합성 dry-run 완료: {report}')
    if not engine.s['trades'] or engine.s['positions']:
        raise RuntimeError('합성 배선 시험에서 진입/청산 완결 실패')

def refresh_account(engine,rest):
    fills=rest.fills() if engine.active_orders() else []
    snapshot=rest.snapshot()
    engine.reconcile(snapshot,fills)

def safe_report(rows,engine):
    try: export(ROOT,rows,engine.s)
    except (OSError,ValueError) as e:
        print(f'보고서 저장 보류 ({type(e).__name__}). 포지션 관리는 계속 실행됩니다.')

def check_connection(cfg):
    """인증/실시간 스키마만 30초 확인. Engine/주문 모듈을 실행하지 않는다."""
    from core.realtime import Feed,parse_tick
    w=json.loads((ROOT/'watchlist.json').read_text(encoding='utf-8'))
    symbols=[r['code'] for r in w['items']]
    if not symbols: raise ValueError('연결 검사할 감시종목 없음')
    from automation.session import supported
    rest=Rest(cfg); rest.session_guard=lambda:supported(now())
    feed=Feed(rest,symbols); counts={'REG':0,'REAL_0B':0,'valid_regular_ticks':0,'invalid_ticks':0,'ERROR':0}
    started=now(); deadline=time.monotonic()+30; feed.start()
    try:
        while time.monotonic()<deadline:
            if not supported(now()): break
            try: received,m=feed.q.get(timeout=0.5)
            except queue.Empty: continue
            if m.get('trnm')=='REG' and m.get('return_code') in (0,'0'): counts['REG']+=1
            if m.get('trnm')=='ERROR': counts['ERROR']+=1
            if m.get('trnm')=='REAL':
                for item in m.get('data',[]):
                    if item.get('type')!='0B': continue
                    counts['REAL_0B']+=1
                    try:
                        t=parse_tick(item,received)
                        if t.regular and 0<=((received-t.time).total_seconds())<=cfg.stale_seconds:
                            counts['valid_regular_ticks']+=1
                    except (ValueError,KeyError,TypeError): counts['invalid_ticks']+=1
    finally: feed.close()
    result={'started':started.isoformat(),'finished':now().isoformat(),'orders_sent':0,'counts':counts,
      'passed':counts['REG']>0 and counts['valid_regular_ticks']>0 and counts['invalid_ticks']==0}
    atomic_json(ROOT/'connection_check.json',result); print(json.dumps(result,ensure_ascii=False))
    if not result['passed']: raise RuntimeError('30초 실시간 연결 검사 미통과: 휴장/비거래시간 또는 접속 확인 필요')

def check_account(cfg):
    """잔고·미체결·체결 조회 필드 검사. 잔고액/종목/계좌번호는 보고서에 저장하지 않는다."""
    from automation.session import supported
    rest=Rest(cfg); rest.session_guard=lambda:supported(now())
    snapshot=rest.snapshot(); fills=rest.fills()
    result={'checked_at':now().isoformat(),'orders_sent':0,'holding_count':len(snapshot['holdings']),
        'open_order_count':len(snapshot['orders']),'fill_row_count':len(fills),
        'cash_field_valid':isinstance(snapshot['cash'],(int,float)),
        'equity_field_valid':isinstance(snapshot['equity'],(int,float)),'passed':True}
    atomic_json(ROOT/'account_check.json',result); print(json.dumps(result,ensure_ascii=False))

def monitor(cfg,rows,watch,paper_seconds=0):
    from core.realtime import Feed, parse_tick
    rest=Rest(cfg)
    from automation.session import supported
    rest.session_guard=lambda:supported(now())
    folder=ROOT/'runtime'/('paper-smoke' if paper_seconds else ('live-'+cfg.broker_environment if cfg.live_mode else 'paper'))
    engine=Engine(cfg,folder,rest if cfg.live_mode else None)
    if cfg.live_mode:
        try: refresh_account(engine,rest)
        except Exception as e: engine.halt('시작 계좌 조회 실패: '+type(e).__name__)
    atomic_json(folder/'session_config.json',asdict(cfg))
    symbols=sorted(set(x['code'] for x in watch)|set(engine.s['positions'])|{o['symbol'] for o in engine.active_orders()})
    if not symbols:
        print('감시 가능한 종목 없음. 기준을 자동 완화하지 않습니다.'); export(ROOT,rows,engine.s); return
    lookup={x['code']:x for x in watch}; detector=Detector(cfg)
    histories={symbol:bars_from_csv(ROOT/'data/raw'/f'{symbol}_daily.csv',now().date()) for symbol in lookup}
    feed=Feed(rest,symbols); prices={}; feed.start(); last_account=0.; last_report=0.; end_requested=False
    started=now(); deadline=time.monotonic()+paper_seconds if paper_seconds else None; accepted_ticks=0
    print('LIVE 주문 활성화' if cfg.live_mode else 'DRY-RUN: 실제 주문 API 차단')
    try:
        while True:
            current=now()
            if not supported(current):
                engine.event('OUT_OF_SUPPORTED_SESSION',reason_code='OUT_OF_SUPPORTED_SESSION'); safe_report(rows,engine); return
            if feed.overflow.is_set():
                engine.halt('실시간 수신 큐 초과'); detector.reset(); feed.overflow.clear()
                for sym in list(engine.s['positions']): engine.request_exit(sym,'수신 큐 초과')
            try:
                received,m=feed.q.get(timeout=0.5)
                tr=m.get('trnm')
                if tr in ('ERROR','DISCONNECTED'):
                    engine.disconnect(); detector.reset()
                elif tr=='REG':
                    if m.get('return_code') not in (0,'0'):
                        engine.halt('실시간 구독 실패')
                    else:
                        if cfg.live_mode:
                            try: refresh_account(engine,rest)
                            except Exception as e:
                                engine.account_ok=False; engine.halt('재연결 계좌 조회 실패: '+type(e).__name__)
                        engine.connected=True; detector.reset(); engine.event('CONNECTED',symbols=symbols)
                elif tr=='REAL':
                    for item in m.get('data',[]):
                        if item.get('type')=='00' and cfg.live_mode:
                            engine.on_order_event(item['values'])
                        elif item.get('type')=='0B':
                            t=parse_tick(item,received)
                            if t.symbol not in symbols: continue
                            if not detector.ingest(t,now()): continue
                            accepted_ticks+=1
                            prices[t.symbol]=t.bid if t.bid>0 else t.price
                            completed=list(detector.completed.get(t.symbol,[]))
                            engine.on_tick(t,completed[-1]['low'] if completed else None)
                            if t.symbol in lookup and engine.connected and not end_requested:
                                sig=detector.signal(t,lookup[t.symbol])
                                if sig:
                                    # 시가 갭/돌파로 가격대가 바뀌면 같은 일봉을 현재 가격으로 다시 평가한다.
                                    fresh=structure(histories[t.symbol],sig['limit'],cfg,current.date())
                                    if fresh.get('status')=='ELIGIBLE':
                                        r=fresh.get('resistance')
                                        if r is None or (r-sig['limit'])/(sig['limit']-sig['stop'])>=cfg.min_reward_risk:
                                            engine.enter(sig,t)
                # 시세 부재도 timer를 통과한다. 거래량이 멈추어도 청산시각이 실행된다.
            except queue.Empty: pass
            except (ValueError,KeyError,TypeError) as e:
                engine.halt('실시간/계좌 데이터 형식 오류: '+type(e).__name__)
            engine.timer(prices)
            if cfg.live_mode and time.monotonic()-last_account>=cfg.reconcile_seconds:
                try: refresh_account(engine,rest)
                except Exception as e:
                    engine.account_ok=False; engine.halt('계좌 조회 실패: '+type(e).__name__)
                last_account=time.monotonic()
            if time.monotonic()-last_report>60:
                safe_report(rows,engine); last_report=time.monotonic()
            # 파일은 신규진입 중단+청산 요청이다. 미체결 상태에서는 프로세스를 계속 유지한다.
            if (ROOT/'STOP_NEW_ENTRIES').exists():
                end_requested=True; engine.halt('사용자 종료 파일')
                for sym in list(engine.s['positions']): engine.request_exit(sym,'사용자 종료 요청')
                for o in engine.active_orders(side='BUY'): engine.cancel(o)
            if deadline is not None and time.monotonic()>=deadline:
                end_requested=True; engine.halt('dry-run 검증 시간 종료')
                for sym in list(engine.s['positions']): engine.request_exit(sym,'dry-run 검증 시간 종료')
                for o in engine.active_orders(side='BUY'): engine.cancel(o)
                engine.drive_exits(prices)
            if (end_requested or current.strftime('%H:%M:%S')>='15:20:00') and not engine.s['positions'] and not engine.active_orders():
                break
    except KeyboardInterrupt:
        engine.halt('키보드 종료: 로컬 보호 감시 중단, HTS 확인 필요')
        print('프로그램 종료 시 로컬 손절 감시는 멈춥니다. 남은 포지션/미체결은 HTS에서 확인하세요.')
    finally:
        feed.close(); engine.save(); safe_report(rows,engine)
        if paper_seconds:
            result={'started':started.isoformat(),'finished':now().isoformat(),'accepted_ticks':accepted_ticks,
              'real_orders_sent':0,'paper_entry_attempts':sum(engine.s['entries'].values()),
              'remaining_positions':len(engine.s['positions']),'remaining_orders':len(engine.active_orders()),
              'halt':engine.s['halt'],'passed':accepted_ticks>0 and not engine.s['positions'] and not engine.active_orders()}
            atomic_json(ROOT/'monitor_check.json',result); print(json.dumps(result,ensure_ascii=False))

def main():
    p=argparse.ArgumentParser(description='Kiwoom 실전 프로토콜 V1 (기본 dry-run)')
    p.add_argument('--config',default=str(ROOT/'config.json'))
    p.add_argument('--credentials',help='인증정보 .env 파일 위치 (기본: V1 .env 또는 기존 Desktop 설치)')
    p.add_argument('--demo',action='store_true'); p.add_argument('--offline',action='store_true')
    p.add_argument('--collect-only',action='store_true'); p.add_argument('--monitor-only',action='store_true')
    p.add_argument('--self-test',action='store_true')
    p.add_argument('--diagnostics',action='store_true',help='시간과 무관한 로컬 상태/설정/파일 I/O 진단 (네트워크 없음)')
    p.add_argument('--diagnostic-account',action='store_true',help='명시 선택한 조회 전용 계좌 진단. 운영시간 밖에도 API가 허용하면 조회; 주문 불가')
    p.add_argument('--check-connection',action='store_true',help='30초 조회 전용 실시간 연결 검사')
    p.add_argument('--check-account',action='store_true',help='조회 전용 잔고/미체결/체결 필드 검사')
    p.add_argument('--paper-seconds',type=int,default=0,help='별도 paper-smoke 상태로 지정 초 동안 dry-run 감시 후 청산/종료')
    p.add_argument('--legacy-monitor',action='store_true',help='V1 원형의 합성/조회용 감시. 실제 주문 불가')
    args=p.parse_args()
    if args.self_test:
        import unittest
        result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
        atomic_json(ROOT/'tests/latest_result.json',{'checked_at':now().isoformat(),'tests':result.testsRun,
          'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful()})
        return 0 if result.wasSuccessful() else 1
    cfg=load_config(args.config)
    from automation.session import supported
    if args.diagnostics or args.diagnostic_account or (not supported(now()) and not any((args.demo,args.offline,args.check_connection,args.check_account,args.paper_seconds,args.collect_only,args.legacy_monitor))):
        from local_diagnostics import diagnose,account_diagnostic
        result=diagnose(ROOT,now())
        if args.diagnostic_account:
            load_credentials(args.credentials)
            result['account_diagnostic']=account_diagnostic(cfg,ROOT,now())
        print(json.dumps(result,ensure_ascii=False)); return 0
    prerequisites()
    if args.check_connection or args.check_account or args.paper_seconds or args.collect_only or args.legacy_monitor:
        from automation.session import supported,assessment
        if not supported(now()):
            result={**assessment(now()),'test':'실제 연결 검사','passed':None,'reason':'INCONCLUSIVE_AFTER_HOURS',
                    'next_step':'다음 실제 개장일 V1.1 운영시간 09:00 이상 15:20 미만에 다시 검사'}
            atomic_json(ROOT/'diagnostic_session_guard.json',result)
            print(json.dumps(result,ensure_ascii=False)); return 0
    if (args.demo or args.offline) and cfg.live_mode:
        raise ValueError('오프라인/데모는 live_mode=false 설정만 허용')
    if args.paper_seconds and (cfg.live_mode or not 1<=args.paper_seconds<=600):
        raise ValueError('paper-seconds는 dry-run에서 1~600초만 허용')
    if not args.demo and not args.offline: load_credentials(args.credentials)
    with ProcessLock(ROOT/'runtime/process.lock'):
        if args.demo: replay_demo(cfg); return 0
        if args.check_connection: check_connection(cfg); return 0
        if args.check_account: check_account(cfg); return 0
        if not args.demo and not args.offline and not args.collect_only and not args.legacy_monitor:
            from automation.runner import run
            result=run(cfg,ROOT,args.paper_seconds)
            print(json.dumps(result,ensure_ascii=False)); return 0 if not args.paper_seconds or result['passed'] is not False else 1
        if args.legacy_monitor and cfg.live_mode: raise ValueError('원형 감시 경로의 실제 주문은 금지. 기본 자동운용 경로 사용')
        folder=ROOT/'runtime'/('live-'+cfg.broker_environment if cfg.live_mode else 'paper')
        state_path=folder/'state.json'
        existing=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
        # 보유/미체결이 있으면 느린 장전 수집을 먼저 실행하지 않는다.
        recovery=existing and (existing['positions'] or any(o['status'] not in ('FILLED','CANCELED','REJECTED') for o in existing['orders'].values()))
        if args.monitor_only or recovery:
            w=json.loads((ROOT/'watchlist.json').read_text(encoding='utf-8'))
            if not recovery and (w['asof']!=now().date().isoformat() or w['offline'] or w['config_hash']!=cfg.fingerprint()):
                raise ValueError('당일 온라인 수집/설정 일치 감시목록 필요')
            rows=read_csv(ROOT/'candidate_audit.csv'); watch=w['items'] if w['asof']==now().date().isoformat() and not w['offline'] else []
        else:
            rest=None if args.offline else Rest(cfg)
            if rest is not None:
                from automation.session import supported
                rest.session_guard=lambda:supported(now())
            rows,watch=build(cfg,rest,offline=args.offline)
        report=export(ROOT,rows)
        print(f'후보 {len(rows)} → 감시 {len(watch)}: {report}')
        if args.offline or args.collect_only: return 0
        monitor(cfg,rows,watch,args.paper_seconds)
    return 0

if __name__=='__main__':
    try: sys.exit(main())
    except Exception as e:
        print(f'실행 중단: {type(e).__name__}: {e}',file=sys.stderr)
        sys.exit(1)
