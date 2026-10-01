"""기존 조회/감시/운영명령의 사용자용 어댑터. 매매 규칙/주문 상태기계는 수정하지 않는다."""
import json
import math
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict,replace
from datetime import datetime
from pathlib import Path
from core.settings import ROOT,now,load_config
from core.util import ProcessLock,atomic_json,read_json_retry,exception_details
from automation.policy import Policy,load_automation,HARD_LOCKS,BREAKEVEN_EXITS,EXISTING_POLICIES,legacy_test_release_enabled
from automation.session import supported
from automation.decisions import STRATEGY_VERSION
from operator_process import process_identity,busy

TERMINAL={'FILLED','CANCELED','REJECTED'}
MENUS={'1':'현재 상태 / 최근 판단','2':'150초 안전 시운전','3':'계속 감시 및 거래 시작',
       '4':'감시 및 거래 중단 STOP',
       '6':'3,000원 LIVE 준비 / 전환','7':'봇 포지션 청산 후 중단 STOP_FLAT',
       '8':'비상정지 KILL','9':'최근 결과 / 상세 로그','0':'프로그램 메뉴 종료'}
REASONS={'AUTO_CAP_ZERO':'승인한도 0원','HARD_LOCK':'자동매매 금지 종목','MANUAL_HOLD':'자동매매 금지 종목',
 'MODE_ARMED_NO_NEW_ENTRY':'조회 대기 상태','PATTERN_OR_GATE_NOT_READY':'패턴 또는 조건 미완성',
 'FRESH_STRUCTURE_SIGNAL_NOT_READY':'현재 가격구조의 신호 미완성','STRUCTURE_REJECTED':'가격구조 조건 부족',
 'MARKET_STATUS_UNKNOWN':'종목상태 미확인','MARKET_STATUS_STALE':'종목상태 오래됨',
 'NO_BUY_EFFICIENCY':'비용 대비 조건 부족','PROTECTION_INTACT':'보호 조건 유지',
 'SELL_INVALIDATION_HIT':'매수 논리 무효화','SELL_PROFIT_PROTECTION_HIT':'이익보호선 이탈',
 'SELL_SESSION_FLATTEN':'V1.1 운영 종료 전 봇 청산','SELL_STOP_FLAT':'사용자 청산 요청',
 'SELL_EMERGENCY':'사용자 비상정지','EXTERNAL_FILL_COMPLETE':'기존 보유 매도 체결 완료',
 'ACCOUNT_CAPITAL_REFRESHED':'체결 후 주문가능현금 재조회 완료','ACCOUNT_OR_CONNECTION_UNSAFE':'계좌 또는 통신 확인 필요',
 'TEST_RELEASE_NOT_APPROVED':'나인테크 시험매도 미승인','BELOW_BREAKEVEN':'현대 손익분기 가격 미달',
 'ACCOUNT_NOT_OK':'계좌 대조 미완료','CONNECTION_NOT_OK':'실시간 연결 미확인','HALT_ACTIVE':'안전중단 사유 유지',
 'ACCOUNT_RECONCILE_STALE':'계좌 대조 기록 오래됨','ONE_SHARE_UNAFFORDABLE':'승인자본·현금으로 비용 포함 1주 매수 불가'}

def read_json(path,default=None):
    if not path.exists(): return default
    return read_json_retry(path,encoding='utf-8-sig')

def age(stamp,clock):
    try:
        value=datetime.fromisoformat(stamp)
        if value.tzinfo is None: return math.inf
        seconds=(clock-value).total_seconds()
        return seconds if seconds>=0 else math.inf
    except (ValueError,TypeError): return math.inf

def in_window(stamp,start,end):
    try:
        value=datetime.fromisoformat(stamp)
        return value.tzinfo is not None and start<=value<=end
    except (ValueError,TypeError): return False

def tail_events(path,limit=1000):
    if not path.exists(): return []
    # 대형 로그 전체를 메모리에 올리지 않는다. 원본은 읽기만 한다.
    with path.open('rb') as f:
        f.seek(0,2); end=f.tell(); f.seek(max(0,end-512000)); data=f.read()
    lines=data.decode('utf-8',errors='replace').splitlines()
    rows=[]
    for line in lines[-limit:]:
        try: rows.append(json.loads(line))
        except ValueError: continue
    return rows

def decision_line(row):
    stamp=(row.get('timestamp') or row.get('time') or '')[11:19]
    name=row.get('name') or row.get('symbol') or '운영'
    decision={'BUY':'매수 후보','NO_BUY':'매수 안 함','HOLD':'보유/관찰 유지','SELL':'매도 판단'}.get(row.get('decision'),'상태 기록')
    reason=row.get('primary_reason_code') or row.get('reason_code') or row.get('reason') or row.get('event')
    text=REASONS.get(reason,reason or '근거 미확인')
    setup=(row.get('setup')+'패턴 → ') if row.get('setup') else ''
    labels={'warmup':'관찰시간 부족','acceleration':'급가속 부족','liquidity':'거래대금 부족','spread':'호가 간격',
            'invalidation':'무효화가격','risk_reward':'상단 여유/위험비','connection_account':'계좌/연결',
            'market_status':'종목상태','risk_quantity':'허용수량 0','chase':'추격 범위','economic':'거래비용',
            'structure':'가격구조','session':'시간 조건','repeat_slots':'재진입/보유한도'}
    gates=row.get('gates',{}); failed=[labels[k] for k,v in gates.items() if k in labels and v.get('result')!='PASS']
    detail=' ('+', '.join(failed[:3])+')' if failed else ''
    return f'{stamp} {name} → {setup}{text}{detail} → {decision}'

class Gateway:
    """실제 연결은 사용자가 선택한 지원시간 내 작업에서만 한다."""
    def __init__(self,service): self.service=service
    def snapshot(self):
        if not supported(self.service.clock()): raise ValueError('지원시간 밖: 계좌 연결을 시작하지 않습니다.')
        import main
        from core.broker import Rest
        main.load_credentials()
        cfg=load_config(self.service.root/'config.json'); rest=Rest(cfg)
        rest.session_guard=lambda:supported(self.service.clock())
        snapshot=rest.snapshot(); rest.fills()
        return snapshot
    def command(self,args,timeout=60):
        return self.service.run_command(args,timeout)
    def start(self,args): return self.service.start_command(args)

class OperatorService:
    def __init__(self,root=ROOT,clock=now,gateway=None,sleep=time.sleep,monotonic=time.monotonic):
        self.root=Path(root); self.clock=clock; self.sleep=sleep; self.monotonic=monotonic; self.child=None
        self.gateway=gateway or Gateway(self)
    def settings(self):
        return load_config(self.root/'config.json'),load_automation(self.root/'automation.json'),read_json(self.root/'runtime/automation_policy.json',
            {'mode':'ARMED','live_authorized':False,'releases':{},'revision':0,'commands':[]})
    def folders(self):
        return list((self.root/'runtime').glob('automation-*'))
    def states(self):
        return [(folder,read_json(folder/'state.json')) for folder in self.folders() if (folder/'state.json').exists()]
    def state_counts(self):
        rows=self.states()
        return {'positions':sum(sum(p['qty'] for p in s['positions'].values()) for _,s in rows),
                'pending':sum(o['status'] not in TERMINAL for _,s in rows for o in s['orders'].values()),
                'uncertain':sum(o['status'] in {'UNKNOWN','INTENT'} for _,s in rows for o in s['orders'].values())}
    def latest(self):
        cfg,_,_=self.settings(); primary=self.root/'runtime'/('automation-'+cfg.broker_environment)
        if self.process_info()['managed']: return primary,read_json(primary/'state.json',{}),read_json(primary/'heartbeat.json',{})
        choices=self.folders()
        if not choices: return primary,{},{}
        folder=max(choices,key=lambda p:(p/'heartbeat.json').stat().st_mtime if (p/'heartbeat.json').exists() else 0)
        return folder,read_json(folder/'state.json',{}),read_json(folder/'heartbeat.json',{})
    @contextmanager
    def idle(self):
        with ProcessLock(self.root/'runtime/supervisor.lock'),ProcessLock(self.root/'runtime/process.lock'),ProcessLock(self.root/'runtime/operator_runner.lock'):
            yield
    def running(self):
        return self.process_info()['running']
    def process_info(self):
        record=read_json(self.root/'runtime/operator_runner.json',{})
        identity=process_identity(record.get('pid'))
        alive=bool(identity and identity==record.get('identity'))
        locked=busy(self.root)
        active=alive and record.get('status') not in {'STOPPED','CRASHED'}
        return {**record,'running':bool(active or locked),'managed':active,
            'state':'실행 중' if active else '별도 실행/종료 확인 중' if locked else '중지',
            'process_heartbeat_fresh':age(record.get('heartbeat'),self.clock())<=5 if active else False,
            'stale':bool(record and not active and not locked)}
    def hard_locks_ok(self):
        return {'005930','017900'}<=HARD_LOCKS and '017900' not in BREAKEVEN_EXITS and EXISTING_POLICIES.get('017900')=='HARD_LOCK'
    def safe_reasons(self,require_zero=True,require_disabled=True,require_cost_false=False):
        cfg,auto,policy=self.settings(); reasons=[]; counts=self.state_counts()
        if not supported(self.clock()): reasons.append('지원시간 밖입니다. 실제 개장일 09:00 이상 15:20 미만에 선택하세요.')
        if not policy or policy['mode']!='ARMED': reasons.append('운영모드가 ARMED가 아닙니다.')
        if policy and policy.get('live_authorized'): reasons.append('기존 LIVE 승인 기록이 남아 있습니다. 임의로 지우지 않고 점검을 중단합니다.')
        if cfg.live_mode: reasons.append('실제 자동매매 설정이 켜져 있습니다.')
        if require_zero and auto.capital_limit!=0: reasons.append('승인한도가 0원이 아닙니다.')
        if require_cost_false and auto.costs_confirmed: reasons.append('첫 조회 검사 조건과 다릅니다: 비용 확인 설정이 켜져 있습니다.')
        approval=(policy or {}).get('test_capital_release',{})
        if require_disabled and approval.get('status') in {'APPROVED','SUBMITTING'}: reasons.append('나인테크 시험매도 승인이 활성 상태입니다.')
        if not self.hard_locks_ok(): reasons.append('삼성전자 또는 광전자 잠금 정책이 예상과 다릅니다.')
        if counts['positions'] or counts['pending']: reasons.append('봇 보유/미체결 주문이 있습니다. 먼저 기존 관리를 확인하세요.')
        return reasons
    def remember_account(self,snapshot):
        cash=snapshot['cash']
        if type(cash) not in (float,int) or not math.isfinite(cash) or cash<0: raise ValueError('주문가능현금 응답이 유효하지 않습니다.')
        atomic_json(self.root/'runtime/operator_account.json',{'timestamp':self.clock().isoformat(),'cash':cash,
            'holding_count':len(snapshot['holdings']),'open_order_count':len(snapshot['orders'])})
    def public_status(self):
        cfg,auto,policy=self.settings(); folder,state,hb=self.latest(); counts=self.state_counts()
        ledger=state.get('automation',{}); saved=ledger.get('capital_snapshot',{})
        other=read_json(self.root/'runtime/operator_account.json',{})
        if age(other.get('timestamp'),self.clock())<age(saved.get('timestamp'),self.clock()):
            saved={'timestamp':other['timestamp'],'actual_orderable_cash':other['cash']}
        cash=saved.get('actual_orderable_cash'); process=self.process_info()
        fresh=age(hb.get('time'),self.clock())<=10 and process['running']
        if process.get('managed'): fresh=fresh and in_window(hb.get('time'),datetime.fromisoformat(process['started']),self.clock())
        events=tail_events(folder/'events.jsonl'); decisions=[x for x in events if x.get('decision')][-10:]
        errors=[x for x in events if x.get('error') or x.get('event') in {'ENTRY_HALTED','ORDER_UNKNOWN','ORDER_REJECTED','RUNTIME_ERROR'}]
        if state.get('halt'): last_error=state['halt']
        elif errors: last_error=str(errors[-1].get('return_msg') or errors[-1].get('reason') or errors[-1].get('error') or errors[-1]['event'])
        else: last_error='기록 없음 (정상 연결을 뜻하지 않음)'
        if errors:
            record=errors[-1]
            detail=''
            if record.get('return_code') is not None: detail+=' / 오류코드 '+str(record['return_code'])
            if record.get('api'): detail+=' / API '+str(record['api'])
            if last_error=='Rejected': last_error='조회 거절 (원문: Rejected, 세부 응답은 이 기록에 없음)'
            last_error+=' / 기록시각 '+str(record.get('timestamp') or record.get('time') or '미확인')+detail
        ui_error=read_json(self.root/'runtime/operator_error.json',{})
        if ui_error and age(ui_error.get('timestamp'),self.clock())<age((errors[-1].get('timestamp') or errors[-1].get('time')) if errors else None,self.clock()):
            last_error=(ui_error.get('return_msg') or ui_error.get('error') or '운영 메뉴 오류')+' / 기록시각 '+str(ui_error.get('timestamp','미확인'))
        orders=list(state.get('orders',{}).values()); last_order=orders[-1] if orders else None
        armed=policy['mode']=='ARMED'; configured=policy['mode']=='LIVE' and policy.get('live_authorized') and cfg.live_mode and auto.capital_limit>0 and auto.costs_confirmed
        watch=read_json(self.root/'watchlist.json',{})
        watch_text=', '.join((x.get('name') or x['code'])+' '+x['code'] for x in watch.get('items',[])) or '후보 없음'
        activity={'SCAN':'후보와 A/B/C 조건 관찰','MANAGE':'봇 보유 보호/청산 관리','QUIET':'후보 대기'}.get(hb.get('strategy'),'대기')
        if not fresh: activity='감시 미실행/현재 관측 미확인 — 마지막 동작: '+activity
        approval=policy.get('test_capital_release',{})
        return {'time':self.clock().isoformat(),'supported':supported(self.clock()),'mode':policy['mode'],'live_mode':cfg.live_mode,
            'candidate_summary':read_json(self.root/'runtime/candidate_summary.json',{}),
            'live_approved':bool(policy.get('live_authorized') and cfg.live_mode),
            'halt':state.get('halt') or '', 'halt_info':state.get('halt_info',{}),
            'order_loop':'RUNNING' if process['running'] else 'NOT RUNNING',
            'process':process,'live_configured':bool(configured),
            'trading_on':bool(configured and fresh and hb.get('connected') and hb.get('account_ok') and not state.get('halt') and not counts['uncertain'] and supported(self.clock())),
            'candidate_count':len(watch.get('items',[])) if fresh and watch.get('asof')==self.clock().date().isoformat() else 0,
            'position_count':sum(len(s['positions']) for _,s in self.states()),
            'last_decision':(('최근 기록: ' if fresh else '과거 기록: ')+str((decisions[-1].get('timestamp') or decisions[-1].get('time') or '')[:10])+' '+decision_line(decisions[-1])) if decisions else '아직 판단 기록 없음',
            'current_error':state['halt'] if state.get('halt') else last_error if age(ui_error.get('timestamp'),self.clock())<=60 or (fresh and errors and age(errors[-1].get('timestamp') or errors[-1].get('time'),self.clock())<=60) else '현재 오류 미확인 / 과거 기록은 상세보기',
            'cap':auto.capital_limit,'cash':cash,'cash_time':saved.get('timestamp'),'available':min(cash,auto.capital_limit) if cash is not None else None,
            'costs_confirmed':auto.costs_confirmed,'websocket':'현재 연결 정상' if fresh and hb.get('connected') else '미연결/오래된 기록 — 현재 정상 미확인',
            'account':'현재 조회 정상' if fresh and hb.get('account_ok') else '최근 조회 기록만 있음' if cash is not None else '미확인',
            'positions':counts['positions'],'pending':counts['pending'],'uncertain':counts['uncertain'],'hard_ok':self.hard_locks_ok(),
            'test_release':approval.get('status','DISABLED'),'error':last_error,'strategy_version':STRATEGY_VERSION,
            'watch':activity+' / 최근 감시목록: '+watch_text+' (목록 기준일 '+watch.get('asof','미확인')+')','decisions':[decision_line(x) for x in decisions],
            'last_order':f"{'매수' if last_order['side']=='BUY' else '매도'} {last_order['symbol']} {last_order['qty']}주 / "+{'ACCEPTED':'접수(체결과 다름)','FILLED':'체결 완료','CANCELED':'취소 완료','REJECTED':'거부','UNKNOWN':'결과 불명확','INTENT':'전송 의도','CANCEL_SENT':'취소 확인 대기'}.get(last_order['status'],last_order['status'])+' / '+str(last_order.get('execution') or ('PAPER' if str(last_order.get('id','')).startswith('PAPER-') else 'REAL' if last_order.get('id') else 'NOT_SUBMITTED')) if last_order else '기록 없음',
            'order_permission':'OUTSIDE_TRADING_WINDOW — V1.1 운영시간 밖, 실주문 불가' if not supported(self.clock()) else 'LIVE 설정 승인됨 — 실제 계좌·시세·위험 조건도 통과해야 주문' if configured else
                '나인테크 1주 SELL 전용 승인 — 일반 매수 금지' if legacy_test_release_enabled() and armed and approval.get('status')=='APPROVED' else '일반 신규매수 불가',
            'observation_time':hb.get('time')}
    def save_result(self,kind,result):
        result={**result,'kind':kind,'timestamp':self.clock().isoformat(),'strategy_version':STRATEGY_VERSION}
        atomic_json(self.root/f'runtime/operator_{kind}.json',result)
        atomic_json(self.root/'runtime/operator_last_result.json',result)
        lines=['주식자동매매 최근 사용자 결과','검사 시각: '+result['timestamp'],'판정: '+result.get('verdict','미확인')]
        for key,label in {'account_ok':'계좌조회','websocket_ok':'WebSocket','regular_ticks':'정규장 틱 수',
             'radar_ok':'후보검색','candidate_count':'후보 수','signals':'기록된 신호 수','orders':'실제 주문',
             'samsung_orders':'삼성전자 주문','gwang_orders':'광전자 주문','cash':'주문가능현금',
             'cap':'승인한도','available':'가용자본','filled_qty':'나인테크 체결 수량','remaining_qty':'잔여 수량'}.items():
            if key in result:
                value=result[key]; text='정상' if value is True else '미확인/부족' if value is False else '미확인' if value is None else str(value)
                lines.append(label+': '+text)
        lines+=result.get('reasons',[])
        if result.get('note'): lines.append(result['note'])
        (self.root/'사용자_최근결과.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
        return result
    def save_error(self,error):
        response=getattr(error,'response',None)
        record={**exception_details(error),'timestamp':self.clock().isoformat(),'error':str(error),'type':type(error).__name__,
                'api':getattr(error,'api',None),'raw_response':response}
        if isinstance(response,dict): record.update(return_code=response.get('return_code'),return_msg=response.get('return_msg'))
        atomic_json(self.root/'runtime/operator_error.json',record)
        return self.save_result('error',{'verdict':'FAIL','reasons':[str(error)]})
    def run_command(self,args,timeout=60):
        if args[:1]==['--paper-seconds']:
            child=self.start_command(args)
            try: return child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop('STOP')
                raise ValueError('조회 시운전이 제한 시간 안에 끝나지 않았습니다. STOP을 요청했으며 감시를 강제로 종료하지 않았습니다.') from None
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        log=self.root/'runtime/operator_console.txt'; log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a',encoding='utf-8') as output:
            p=subprocess.run([sys.executable,'-X','utf8',str(self.root/'main.py'),*args],cwd=self.root,stdin=subprocess.DEVNULL,
                stdout=output,stderr=subprocess.STDOUT,timeout=timeout,creationflags=flags)
        return p.returncode
    def start_command(self,args):
        if not args: return self.launch_continuous()
        with self.idle(): pass
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        log=self.root/'runtime/operator_console.txt'; log.parent.mkdir(parents=True,exist_ok=True)
        entry='main.py' if args else 'supervisor.py'
        with log.open('a',encoding='utf-8') as output:
            self.child=subprocess.Popen([sys.executable,'-X','utf8',str(self.root/entry),*args],cwd=self.root,
                stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,creationflags=flags)
        return self.child
    def launch_continuous(self):
        # 모든 사용자 실행 경로가 동일한 시작 잠금과 프로세스 식별정보를 쓴다.
        with ProcessLock(self.root/'runtime/operator_launch.lock'):
            if self.running(): raise ValueError('이미 감시 중입니다. 새 인스턴스를 시작하지 않습니다.')
            with self.idle(): pass
            if not supported(self.clock()): raise ValueError('지원시간 밖: 계속 감시를 시작하지 않습니다.')
            cfg,auto,policy=self.settings()
            if not legacy_test_release_enabled() and policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
                raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 미정리 과거 승인/주문은 수동 확인하세요.')
            token=uuid.uuid4().hex
            purpose='TEST_CAPITAL_RELEASE' if legacy_test_release_enabled() and policy.get('test_capital_release',{}).get('status')=='APPROVED' else 'CONTINUOUS'
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            log=self.root/'runtime/operator_console.txt'; log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a',encoding='utf-8') as output:
                self.child=subprocess.Popen([sys.executable,'-X','utf8',str(self.root/'operator_runner.py'),'--token',token,'--purpose',purpose],
                    cwd=self.root,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,creationflags=flags)
            deadline=self.monotonic()+8
            while self.monotonic()<deadline:
                if self.child.poll() is not None: raise ValueError('감시 시작 실패. 상세 오류를 확인하세요. 자동 재전송하지 않습니다.')
                info=self.process_info()
                if info.get('token')==token and info['managed']: return self.child
                self.sleep(.05)
            self.stop('STOP')
            raise ValueError('감시 시작 확인이 지연되어 STOP을 요청했습니다. 실제 프로세스 상태를 확인하세요.')
    def start_continuous(self,confirm_live=False):
        if self.running(): return {'verdict':'ALREADY_RUNNING','note':'이미 감시 중입니다. 새 인스턴스를 시작하지 않습니다.'}
        cfg,auto,policy=self.settings(); counts=self.state_counts(); reasons=[]
        if not supported(self.clock()): reasons.append('OUTSIDE_TRADING_WINDOW: V1.1 감시/실주문 운영은 09:00 이상 15:20 미만입니다. 메뉴·로컬 진단은 계속 사용할 수 있습니다.')
        if not self.hard_locks_ok(): reasons.append('삼성/광전자 HARD_LOCK 확인 실패')
        mode=policy['mode']
        if not legacy_test_release_enabled() and policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
            reasons.append('종료된 시험매도에 미정리 승인/주문이 있습니다. 자동 재개하지 않고 장부 확인이 필요합니다.')
        recovery=mode in {'STOP','STOP_FLAT','KILL'} and bool(counts['positions'] or counts['pending'])
        proof=read_json(self.root/'runtime/operator_pilot.json',{})
        if not recovery and (proof.get('verdict')!='PASS' or proof.get('strategy_version')!=STRATEGY_VERSION):
            reasons.append('먼저 최초 150초 안전 시운전 PASS가 필요합니다. 이미 PASS이면 반복할 필요 없습니다.')
        if mode=='ARMED':
            if counts['positions'] or counts['pending']: reasons.append('기존 봇 보유/미체결 관리부터 확인하세요.')
            if policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}: reasons.append('나인테크 전용 시험 경로로 확인하세요. 일반 감시에서 승인하지 않습니다.')
        elif mode=='LIVE':
            if not (cfg.live_mode and auto.capital_limit>0 and auto.costs_confirmed and policy.get('live_authorized')): reasons.append('LIVE 안전 설정/비용/승인한도를 확인하세요.')
            if confirm_live is not True: reasons.append('실제 자동매매 시작 확인이 필요합니다.')
        elif not recovery: reasons.append('ARMED 또는 명시 승인한 LIVE 상태에서 시작하세요. STOP 뒤에는 상태 메뉴에서 안전 전환을 확인하세요.')
        if reasons: return self.save_result('continuous',{'verdict':'FAIL','reasons':reasons})
        self.gateway.start([])
        return self.save_result('continuous',{'verdict':'STARTED','mode':mode,'cap':auto.capital_limit,
            'note':'기존 봇 보호/복구 관리 재개. 신규진입 금지 상태 유지' if recovery else 'ARMED 지속 관찰 시작. 일반 실제 주문과 가상 포지션 없음' if mode=='ARMED' else '승인된 LIVE 계속 감시 시작. 계좌·시장·위험 조건은 기존 코어가 재검증합니다.'})
    def pilot(self,progress=lambda x:None):
        reasons=self.safe_reasons(require_cost_false=True)
        if reasons: return self.save_result('pilot',{'verdict':'FAIL','reasons':reasons,'orders':None})
        with self.idle(): pass
        started=self.clock(); progress('계좌를 조회합니다. 실제 주문은 하지 않습니다.')
        snapshot=self.gateway.snapshot(); self.remember_account(snapshot)
        reasons=self.safe_reasons(require_cost_false=True)
        if reasons: return self.save_result('pilot',{'verdict':'FAIL','reasons':reasons,'orders':None})
        progress('약 150초 동안 WebSocket·정규장 틱·후보·A/B/C 판단을 관찰합니다. 비상정지 파일은 언제든 사용할 수 있습니다.')
        rc=self.gateway.command(['--paper-seconds','150'],timeout=230)
        result=read_json(self.root/'automation_check.json',{}); folder=self.root/'runtime/automation-smoke'
        hb=read_json(folder/'heartbeat.json',{}); state=read_json(folder/'state.json',{})
        if not in_window(result.get('started'),started,self.clock()) or not result.get('finished'):
            return self.save_result('pilot',{'verdict':'FAIL','reasons':['이번 실행 결과를 확인하지 못했습니다. 이전 결과를 통과로 사용하지 않습니다.'],'orders':None})
        orders=list(state.get('orders',{}).values()); actual=sum(bool(o.get('id')) and not o['id'].startswith('PAPER-') for o in orders)
        if any(not o.get('id') and o.get('status') in {'UNKNOWN','INTENT'} for o in orders): actual=None
        samsung=sum(o['symbol']=='005930' for o in orders); gwang=sum(o['symbol']=='017900' for o in orders)
        events=tail_events(folder/'events.jsonl')
        evaluations=[x for x in events if x.get('event')=='ENTRY_EVAL' and in_window(x.get('timestamp'),started,self.clock())]
        signals=[x for x in evaluations if x.get('signal_detected')]
        watch=read_json(self.root/'watchlist.json',{})
        items=watch.get('items',[]) if watch.get('asof')==self.clock().date().isoformat() else []
        counts=result.get('counts',{}); reasons=[]
        if rc or result.get('fatal'): reasons.append('감시 실행 오류가 있습니다. 최근 오류를 확인하세요.')
        if orders or state.get('positions') or actual or samsung or gwang: reasons.append('조회 시운전 중 주문 또는 포지션이 발견됐습니다.')
        if not supported(started) or not supported(self.clock()): verdict='INCONCLUSIVE'; reasons.append('지원시간 밖이거나 종료 경계를 지났습니다.')
        elif reasons: verdict='FAIL'
        elif (counts.get('account_checks',0)>0 and counts.get('subscription_acks',0)>0 and counts.get('radar_cycles',0)>0
              and counts.get('ticks',0)>0 and hb.get('market')=='3' and hb.get('connected') and hb.get('account_ok')
              and items and evaluations): verdict='PASS'
        else: verdict='INCONCLUSIVE'; reasons.append('정규장 틱·계좌/연결·오늘 후보·A/B/C 판단로그 중 관측이 부족합니다. 신호 발생 자체는 필수가 아닙니다.')
        cash=state.get('automation',{}).get('capital_snapshot',{}).get('actual_orderable_cash',snapshot['cash'])
        return self.save_result('pilot',{'verdict':verdict,'reasons':reasons,'cash':cash,'cap':0,'available':0,'orders':actual,
            'samsung_orders':samsung,'gwang_orders':gwang,'account_ok':counts.get('account_checks',0)>0,
            'websocket_ok':counts.get('subscription_acks',0)>0,'regular_ticks':counts.get('ticks',0),
            'radar_ok':counts.get('radar_cycles',0)>0,'candidate_count':len(items),'signals':len(signals),'entry_evaluations':len(evaluations),
            'note':'신호 0건은 조건 미충족/워밍업일 수 있습니다. 거래를 강제로 만들지 않습니다.'})
    def resume_live_continuous(self,confirmed):
        if confirmed is not True: raise ValueError('기존 승인 LIVE 재개 확인이 필요합니다.')
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            cfg,auto,data=self.settings(); counts=self.state_counts(); previous=read_json(self.root/'runtime/operator_runner.json',{})
            if not (supported(self.clock()) and data['mode']=='STOP' and data.get('live_authorized') and cfg.live_mode
                    and auto.capital_limit>0 and auto.costs_confirmed and not counts['positions'] and not counts['pending']
                    and previous.get('initial_mode')=='LIVE' and self.resume_config_matches(cfg,auto,previous)
                    and previous.get('auto_hash')==auto.fingerprint() and self.hard_locks_ok()):
                raise ValueError('기존 LIVE 승인/설정/장부가 달라 재개하지 않습니다. 상세 상태를 확인하세요.')
            Policy(self.root/'runtime/automation_policy.json').command('LIVE',confirmation='LIVE')
        try: return self.start_continuous(confirm_live=True)
        except Exception:
            self.stop('STOP'); raise
    def resume_config_matches(self,cfg,auto,previous):
        if previous.get('config_hash')==cfg.fingerprint(): return True
        change=read_json(self.root/'runtime/operator_config_change.json',{})
        from dataclasses import replace
        return (change.get('scope')=='USER_APPROVED_SMALL_CAP_SINGLE_POSITION' and
            auto.capital_limit==3000 and cfg.max_position_fraction==1.0 and cfg.max_positions==1 and
            change.get('to_hash')==cfg.fingerprint() and change.get('auto_hash')==auto.fingerprint() and
            change.get('from_hash')==previous.get('config_hash') and
            replace(cfg,max_position_fraction=0.2,max_positions=2).fingerprint()==change.get('from_hash'))
    def stop(self,command):
        if command not in {'STOP','STOP_FLAT','KILL'}: raise ValueError('허용되지 않은 중단 명령')
        with ProcessLock(self.root/'runtime/control.lock'):
            if read_json(self.root/'runtime/automation_policy.json') is None: raise ValueError('운영정책 파일이 없어 중단 요청을 저장하지 못했습니다. 임의 재생성하지 않습니다. HTS를 확인하세요.')
            policy=Policy(self.root/'runtime/automation_policy.json'); policy.command(command)
        return {'verdict':'REQUESTED','command':command,'note':'중단 요청을 저장했습니다. 체결/청산 완료를 의미하지 않습니다. 기존 보유 HARD_LOCK은 유지합니다.'}
    def arm_safe(self,confirmed):
        if confirmed is not True: raise ValueError('조회 대기 전환 확인이 필요합니다.')
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            cfg,auto,policy=self.settings(); counts=self.state_counts()
            if cfg.live_mode or auto.capital_limit!=0 or policy.get('live_authorized') or counts['positions'] or counts['pending']:
                raise ValueError('0원·실주문 비활성·봇 보유/미체결 없음 조건이 필요합니다. 기존 보호를 해제하지 않습니다.')
            if policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}:
                raise ValueError('시험매도 승인이 처리 중입니다.')
            Policy(self.root/'runtime/automation_policy.json').command('ARMED')
        return {'verdict':'SAVED','note':'0원 조회 대기 ARMED로 전환했습니다. 실제 주문 권한을 켜지 않았습니다.'}

    def cancel_test_release(self,confirmed):
        if not legacy_test_release_enabled(): raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 시험기능이 종료됐습니다.')
        if confirmed is not True:
            raise ValueError('시험매도 승인 취소 확인이 필요합니다.')

        if self.running():
            raise ValueError('감시 프로세스가 실행 중입니다. 먼저 STOP으로 중단하세요.')

        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            policy=Policy(self.root/'runtime/automation_policy.json')
            policy.refresh()

            approval=policy.state.get('test_capital_release')
            if not approval:
                return self.save_result('cancel_test_release',{
                    'verdict':'PASS',
                    'reasons':[],
                    'note':'활성화된 시험매도 승인이 없습니다.'
                })

            if approval.get('status')!='APPROVED':
                raise ValueError('주문 제출 전 APPROVED 상태만 취소할 수 있습니다.')

            if approval.get('order_local'):
                raise ValueError('주문 기록이 연결되어 있어 자동 취소하지 않습니다.')

            policy.command(
                'CANCEL_TEST_CAPITAL_RELEASE',
                confirmation='CANCEL_TEST_CAPITAL_RELEASE'
            )

        return self.save_result('cancel_test_release',{
            'verdict':'SAVED',
            'reasons':[],
            'note':'미제출 나인테크 시험매도 승인을 취소했습니다. 실제 주문은 발생하지 않았습니다.'
        })
    def release(self,confirmed,progress=lambda x:None):
        if not legacy_test_release_enabled(): raise ValueError('TEST_CAPITAL_RELEASE_RETIRED: 시험기능이 종료됐습니다.')
        if confirmed is not True: raise ValueError('나인테크 1주 실제 매도 확인이 필요합니다.')
        reasons=self.safe_reasons()
        proof=read_json(self.root/'runtime/operator_pilot.json',{})
        if proof.get('verdict')!='PASS' or proof.get('timestamp','')[:10]!=self.clock().date().isoformat() or proof.get('strategy_version')!=STRATEGY_VERSION:
            reasons.append('오늘의 0원 조회 시운전 PASS가 먼저 필요합니다.')
        if reasons: return self.save_result('release',{'verdict':'FAIL','reasons':reasons})
        with self.idle(): pass
        snapshot=self.gateway.snapshot(); self.remember_account(snapshot); h=snapshot['holdings'].get('267320',{})
        if h.get('qty')!=1 or h.get('available')!=1 or snapshot['orders']:
            return self.save_result('release',{'verdict':'FAIL','reasons':['실제 나인테크 보유/매도가능수량이 정확히 1주가 아니거나 미체결 주문이 있습니다.']})
        # 조회 중 상태가 바뀌어도 승인하지 않는다.
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            reasons=self.safe_reasons()
            if reasons: return self.save_result('release',{'verdict':'FAIL','reasons':reasons})
            policy=Policy(self.root/'runtime/automation_policy.json')
            policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        progress('나인테크 기존 1주 매도만 승인했습니다. 전체 LIVE는 켜지지 않았습니다. 실제 체결과 현금 재조회를 기다립니다.')
        submission_started=self.clock()
        try: self.gateway.start([])
        except Exception:
            self.stop('STOP')  # 프로세스 시작 결과가 불명확해도 승인 삭제/재승인하지 않는다.
            raise
        submission_deadline=self.monotonic()+15
        deadline=self.monotonic()+180; reported=set()
        cfg,_,_=self.settings(); folder=self.root/'runtime'/('automation-'+cfg.broker_environment)
        while self.monotonic()<deadline:
            state=read_json(folder/'state.json',{}); ledger=state.get('automation',{}); policy=read_json(self.root/'runtime/automation_policy.json')
            approval=policy.get('test_capital_release',{}); order=state.get('orders',{}).get(approval.get('order_local'),{})
            if order and order.get('status') not in reported:
                reported.add(order.get('status'))
                progress('시험매도 주문: '+str(order.get('execution','미확인'))+' / 주문번호 '+str(order.get('id') or '없음')+' / '+str(order.get('status')))
            if order.get('status') in {'UNKNOWN','REJECTED'}:
                self.stop('STOP')
                return self.save_result('release',{'verdict':'FAIL','reasons':[order.get('failure_reason') or '시험매도 결과 불명확 — HTS 확인, 자동 재전송 금지'],
                    'order_id':order.get('id'),'order_status':order.get('status'),'execution':order.get('execution')})
            if approval.get('status')=='APPROVED':
                evaluations=[e for e in tail_events(folder/'events.jsonl') if e.get('event')=='EXIT_EVAL' and e.get('symbol')=='267320'
                    and in_window(e.get('timestamp') or e.get('time'),submission_started,self.clock())]
                blocking=evaluations[-1].get('primary_reason_code') if evaluations and evaluations[-1].get('decision')=='HOLD' else None
                if blocking or self.monotonic()>=submission_deadline:
                    self.stop('STOP')
                    reason=blocking or 'SUBMISSION_NOT_OBSERVED_WITHIN_15_SECONDS'
                    return self.save_result('release',{'verdict':'FAIL','reasons':[reason+' / '+REASONS.get(reason,'주문 미제출; 승인 기록 유지. 상태 확인 후 메뉴 10에서 미제출 승인만 취소 가능')],
                        'blocking_reason':reason,'order_status':'NOT_SUBMITTED','execution':'NOT_SUBMITTED'})
            if policy.get('test_capital_release',{}).get('status')=='COMPLETED' and ledger.get('capital_refresh_pending') is False:
                cash=ledger.get('capital_snapshot',{}).get('actual_orderable_cash')
                approval=policy['test_capital_release']; order=state.get('orders',{}).get(approval.get('order_local'),{})
                if cash is not None and age(ledger.get('capital_snapshot',{}).get('timestamp'),self.clock())<=15 and not ledger.get('existing',{}).get('267320') and order.get('status')=='FILLED' and order.get('filled')==1:
                    self.stop('STOP')
                    for _ in range(15):
                        if not self.running(): break
                        self.sleep(1)
                    if not self.running(): self.arm_safe(True)
                    return self.save_result('release',{'verdict':'PASS','reasons':[],'filled_qty':1,'remaining_qty':0,
                        'cash':cash,'cap':policy_cap(self.root),'available':min(cash,policy_cap(self.root)),
                        'note':'실제 체결 장부와 새 현금 조회를 확인했습니다. 승인한도는 변경하지 않았습니다. 정상 중단 요청을 보냈습니다.'})
            if self.child is not None and self.child.poll() is not None: break
            if not supported(self.clock()): break
            self.sleep(1)
        self.stop('STOP')
        return self.save_result('release',{'verdict':'INCONCLUSIVE','reasons':['전량 체결과 새 현금 조회를 아직 함께 확인하지 못했습니다. HTS에서 미체결/체결을 확인하세요. 재승인·재전송하지 않습니다.']})
    def confirm_costs(self,buy_pct,sell_pct,tax_pct,confirmed):
        if confirmed is not True: raise ValueError('실제 비용 확인이 필요합니다.')
        rates=[]
        for x in (buy_pct,sell_pct,tax_pct):
            try: value=float(x)/100
            except (TypeError,ValueError): raise ValueError('비용은 실제 확인한 백분율 숫자로 입력하세요.') from None
            if not math.isfinite(value) or not 0<=value<1: raise ValueError('비용 숫자 범위를 확인하세요.')
            rates.append(value)
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            cfg,auto,policy=self.settings(); counts=self.state_counts()
            if cfg.live_mode or policy['mode'] not in {'ARMED','OFF','STOP'} or counts['positions'] or counts['pending']:
                raise ValueError('감시를 정상 중단하고 봇 보유/미체결을 먼저 확인하세요.')
            updated=replace(auto,commission_buy=rates[0],commission_sell=rates[1],sell_tax=rates[2],costs_confirmed=True).validate()
            atomic_json(self.root/'automation.json',asdict(updated))
        return {'verdict':'SAVED','note':'사용자가 입력·확인한 비용만 저장했습니다. cap/LIVE는 변경하지 않았습니다.'}
    def prepare_live(self,progress=lambda x:None):
        reasons=self.safe_reasons(); cfg,auto,policy=self.settings()
        if not auto.costs_confirmed: reasons.append('비용 설정 미확인 — LIVE 시작 차단')
        if reasons: return self.save_result('live_ready',{'verdict':'FAIL','reasons':reasons,'costs_confirmed':auto.costs_confirmed})
        with self.idle(): pass
        progress('준비 점검: 계좌와 정규장 연결만 확인합니다. LIVE를 켜거나 주문하지 않습니다.')
        started=self.clock(); snapshot=self.gateway.snapshot(); self.remember_account(snapshot)
        if snapshot['orders']: return self.save_result('live_ready',{'verdict':'FAIL','reasons':['실제 계좌 미체결 주문이 있습니다.']})
        rc=self.gateway.command(['--check-connection'],timeout=65)
        connection=read_json(self.root/'connection_check.json',{}); counts=connection.get('counts',{})
        if rc or not connection.get('passed') or not in_window(connection.get('started'),started,self.clock()) or not counts.get('valid_regular_ticks'):
            return self.save_result('live_ready',{'verdict':'INCONCLUSIVE','reasons':['이번 정규장 연결/신선한 틱 검사가 미완료입니다. LIVE를 켜지 않습니다.']})
        reasons=self.safe_reasons()
        if reasons: return self.save_result('live_ready',{'verdict':'FAIL','reasons':reasons})
        return self.save_result('live_ready',{'verdict':'PASS','reasons':[],'cash':snapshot['cash'],'cap':auto.capital_limit,
            'available':min(snapshot['cash'],auto.capital_limit),'proposed_3000_max':min(snapshot['cash'],3000),
            'costs_confirmed':True,'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),'policy_revision':policy['revision'],
            'note':'준비 검사만 통과했습니다. 아직 LIVE가 아닙니다. 명시 금액과 최종 확인이 별도로 필요합니다.'})
    def activate_live(self,amount,confirmation,start=True):
        if amount!='3000' or confirmation!='실제매매시작': raise ValueError('금액 3000과 최종 확인이 모두 정확해야 합니다. LIVE를 켜지 않았습니다.')
        proof=read_json(self.root/'runtime/operator_live_ready.json',{})
        cfg,auto,policy=self.settings(); reasons=self.safe_reasons()
        pilot=read_json(self.root/'runtime/operator_pilot.json',{})
        if pilot.get('verdict')!='PASS' or pilot.get('timestamp','')[:10]!=self.clock().date().isoformat() or pilot.get('strategy_version')!=STRATEGY_VERSION:
            reasons.append('오늘의 0원 조회 시운전 PASS가 먼저 필요합니다.')
        if not auto.costs_confirmed: reasons.append('비용 설정 미확인')
        if proof.get('verdict')!='PASS' or age(proof.get('timestamp'),self.clock())>60: reasons.append('최신 준비 검사 PASS가 필요합니다.')
        if proof.get('config_hash')!=cfg.fingerprint() or proof.get('auto_hash')!=auto.fingerprint() or proof.get('policy_revision')!=policy['revision']:
            reasons.append('준비 검사 후 설정/승인이 변경되었습니다. 다시 검사하세요.')
        if reasons: raise ValueError('\n'.join(reasons))
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            # 입력 대기/락 대기 중 변경도 다시 확인.
            current_cfg,current_auto,current_policy=self.settings()
            if self.safe_reasons() or current_cfg!=cfg or current_auto!=auto or current_policy!=policy: raise ValueError('승인 직전 상태가 변경되었습니다.')
            old_policy=json.loads(json.dumps(policy)); old_cfg=read_json(self.root/'config.json'); old_auto=read_json(self.root/'automation.json')
            try:
                atomic_json(self.root/'automation.json',asdict(replace(auto,AUTO_TRADING_CAP_KRW=3000).validate()))
                atomic_json(self.root/'config.json',asdict(replace(cfg,live_mode=True).validate()))
                p=Policy(self.root/'runtime/automation_policy.json'); p.command('LIVE',confirmation='LIVE')
            except Exception:
                atomic_json(self.root/'config.json',old_cfg); atomic_json(self.root/'automation.json',old_auto)
                atomic_json(self.root/'runtime/automation_policy.json',old_policy); raise
        try:
            if start: self.gateway.start([])
        except Exception:
            with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
                atomic_json(self.root/'config.json',old_cfg); atomic_json(self.root/'automation.json',old_auto)
                atomic_json(self.root/'runtime/automation_policy.json',old_policy)
            raise
        return self.save_result('live_start',{'verdict':'STARTED' if start else 'SAVED','cap':3000,'cash':proof['cash'],'available':min(proof['cash'],3000),
            'note':'사용자 금액·최종 확인으로 LIVE를 승인했습니다. 감시가 중지 상태면 3번에서 별도로 시작하세요. 조건이 없으면 NO_TRADE가 정상입니다.'})

    def live_ready_seconds(self):
        proof=read_json(self.root/'runtime/operator_live_ready.json',{})
        cfg,auto,policy=self.settings()
        if (proof.get('verdict')!='PASS' or proof.get('config_hash')!=cfg.fingerprint()
            or proof.get('auto_hash')!=auto.fingerprint() or proof.get('policy_revision')!=policy['revision']): return 0
        return max(0,60-age(proof.get('timestamp'),self.clock()))
    def select(self,choice):
        if legacy_test_release_enabled() and choice in {'5','10'}: return 'historical test replay'
        if choice not in MENUS: raise ValueError('0~9 중 표시된 번호만 선택하세요. 설정은 바꾸지 않았습니다.')
        return MENUS[choice]

def policy_cap(root): return load_automation(root/'automation.json').capital_limit
