"""단일 writer 주문/체결 상태기계. 접수는 포지션이 아니며 실제 체결만 수량에 반영한다."""
import json
import time
from pathlib import Path
from core.settings import now
from core.util import atomic_json, number, code,read_json_retry,exception_details
from core.strategy import size, protective_stop
from core.broker import Rejected, AmbiguousOrder

TERMINAL={'FILLED','CANCELED','REJECTED'}

class Engine:
    def __init__(self,cfg,folder,broker=None,clock=now):
        self.c=cfg; self.folder=Path(folder); self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'state.json'; self.broker=broker; self.clock=clock
        identity=broker.identity if broker else 'paper'
        if cfg.live_mode and broker is None:
            raise ValueError('live_mode에서 실제 브로커 누락')
        if self.path.exists():
            self.s=read_json_retry(self.path)
            if self.s['identity']!=identity:
                raise RuntimeError('기존 세션 계좌 불일치. 기존 계좌 설정으로 복원하세요.')
            if self.s['config_hash']!=cfg.fingerprint():
                if self.s['day']==clock().date().isoformat() or self.s['positions'] or self.active_orders():
                    raise RuntimeError('기존 세션 설정 불일치. 설정을 복원해 포지션을 먼저 관리하세요.')
                self.s['config_hash']=cfg.fingerprint()
            if self.s['day']!=clock().date().isoformat():
                if self.active_orders():
                    raise RuntimeError('전일 미해결 주문 존재: HTS/주문내역 대조 필요')
                self.s['day']=clock().date().isoformat(); self.s['entries']={}; self.s['daily_loss']=0
                self.s['orders']={}; self.s['excluded']=[]  # HALT requires explicit safe recovery, even across dates
        else:
            self.s={'version':1,'day':clock().date().isoformat(),'config_hash':cfg.fingerprint(),'identity':identity,
                'positions':{},'orders':{},'entries':{},'daily_loss':0.,'realized':0.,'cash':cfg.paper_cash,
                'equity':cfg.account_equity,'halt':'','excluded':[],'trades':[],'sequence':0}
        self.connected=False
        self.account_ok=not cfg.live_mode
        self.last_account=0.
        self.last_checkpoint=0.
        self.save()

    def save(self):
        atomic_json(self.path,self.s)

    def event(self,kind,**data):
        row={'time':self.clock().isoformat(),'event':kind,**data}
        with (self.folder/'events.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        if kind not in ('STOP_RAISED',):
            print(json.dumps(row,ensure_ascii=False))

    def halt(self,reason):
        if (self.s.get('halt') and self.s['halt']!='데이터/상태 오류: PermissionError'
            and reason=='데이터/상태 오류: PermissionError'):
            return  # a later I/O fault must never downgrade a dangerous latch
        if self.s['halt']!=reason or reason=='데이터/상태 오류: PermissionError':
            prior=self.s.get('halt_info')
            if prior: self.s.setdefault('halt_history',[]).append(prior)
            self.s['halt']=reason
            self.s['halt_info']={'reason':reason,'occurred_at':self.clock().isoformat(),'active':True,
                'classification':'RECOVERABLE_IO' if reason=='데이터/상태 오류: PermissionError' else 'MANUAL_REVIEW'}
            self.save(); self.event('ENTRY_HALTED',reason=reason,halt_info=self.s['halt_info'])

    def active_orders(self,symbol=None,side=None):
        return [o for o in self.s['orders'].values() if o['status'] not in TERMINAL and
            (symbol is None or o['symbol']==symbol) and (side is None or o['side']==side)]

    def disconnect(self):
        self.connected=False
        self.event('DISCONNECTED',message='신규진입 중단; 재연결 후 잔고/미체결 대조')

    # 확장 정책용 작은 훅. 기본 동작은 V1과 동일하다.
    def entry_window(self):
        return self.c.entry_start<=self.clock().strftime('%H:%M:%S')<self.c.entry_end

    def session_ending(self):
        return self.clock().strftime('%H:%M:%S')>=self.c.flatten_time

    def risk_equity(self): return min(self.c.account_equity,self.s['equity'])

    def quantity(self,signal,equity,cash,remaining):
        return size(self.c,signal['limit'],signal['stop'],equity,cash,remaining)

    def buy_fee(self,amount): return amount*self.c.roundtrip_cost_fraction/2

    def sell_fee(self,amount): return amount*self.c.roundtrip_cost_fraction/2

    def position_created(self,p,o): pass

    def fill_applied(self,o,delta,price): pass

    def decorate_trade(self,trade,p): return trade

    def protection(self,p,price,completed_low): return protective_stop(self.c,p,price,completed_low)

    def can_exit(self,symbol,p): return True

    def live_order_allowed(self,side,symbol,qty,limit,signal):
        return self.c.live_mode

    def submit(self,side,symbol,qty,limit=0,signal=None):
        self.s['sequence']+=1; local=f'{self.s["day"]}-{self.s["sequence"]}'
        o={'local':local,'id':'','symbol':symbol,'side':side,'qty':qty,'limit':limit,'filled':0,'amount':0.,
           'status':'INTENT','created':self.clock().isoformat(),'signal':signal,'cancel_id':''}
        self.s['orders'][local]=o
        self.save()  # 전송 전에 의도를 저장해 응답 유실 시 중복 주문을 막는다.
        try:
            if self.live_order_allowed(side,symbol,qty,limit,signal):
                o['id']=self.broker.submit(side,symbol,qty,limit)
                if not o['id']: raise AmbiguousOrder('실제 주문번호 없음: 자동 재전송 금지')
            else:
                if self.c.live_mode or (signal or {}).get('policy')=='TEST_CAPITAL_RELEASE':
                    raise Rejected('ORDER_BOUNDARY_BLOCKED: 실제 주문 허가 없음')
                o['id']='PAPER-'+local
            o['execution']='PAPER' if o['id'].startswith('PAPER-') else 'REAL'
            o['status']='ACCEPTED'; self.save(); self.event('ORDER_ACCEPTED',local=local,side=side,symbol=symbol,qty=qty,execution=o['execution'],order_id=o['id'])
        except Rejected as e:
            o['status']='REJECTED'; o['execution']='BLOCKED' if str(e).startswith('ORDER_BOUNDARY_BLOCKED') else 'BROKER_REJECTED'
            o['failure_reason']=str(e); self.save(); self.event('ORDER_REJECTED',local=local,reason=str(e),execution=o['execution'])
        except Exception as e:
            o['status']='UNKNOWN'; o['execution']='UNKNOWN'; self.save(); self.halt('주문결과 불명확: 자동 재전송 금지, HTS 확인 필요')
            self.event('ORDER_UNKNOWN',local=local,**exception_details(e))
        return o

    def enter(self,signal,tick):
        c=self.c; sym=signal['symbol']
        if self.clock().date().isoformat()!=self.s['day']:
            self.halt('거래일 변경: 신규 실행 필요'); return None
        if not self.connected or not self.account_ok or self.s['halt'] or sym in self.s['excluded'] or sym in self.s['positions']:
            return None
        if c.live_mode and time.monotonic()-self.last_account>c.reconcile_seconds*3:
            return None
        if not self.entry_window():
            return None
        if not tick.regular or abs((self.clock()-tick.time).total_seconds())>c.stale_seconds:
            return None
        if self.active_orders(sym) or self.s['entries'].get(sym,0)>=c.max_entries_per_symbol or sum(self.s['entries'].values())>=c.max_entries_per_day:
            return None
        # 매수 접수만 있어도 자금/위험을 예약한다. 거절도 당일 시도 횟수에 포함한다.
        buys=self.active_orders(side='BUY')
        if len(set(self.s['positions'])|{o['symbol'] for o in buys})>=c.max_positions:
            return None
        reserved=sum(o['qty']*o['limit']*(1+c.roundtrip_cost_fraction) for o in buys)
        used_risk=sum(max(0,p['avg']-p['stop']+p['avg']*(c.roundtrip_cost_fraction+c.exit_slippage_fraction))*p['qty'] for p in self.s['positions'].values())
        used_risk+=sum((o['limit']-o['signal']['stop']+o['limit']*(c.roundtrip_cost_fraction+c.exit_slippage_fraction))*(o['qty']-o['filled']) for o in buys)
        equity=self.risk_equity()
        remaining=equity*c.max_daily_loss_fraction-self.s['daily_loss']-used_risk
        qty=self.quantity(signal,equity,self.s['cash']-reserved,remaining)
        if qty<=0:
            self.event('SIGNAL_BLOCKED',symbol=sym,reason='수량 0 / 계좌 위험 한도'); return None
        self.s['entries'][sym]=self.s['entries'].get(sym,0)+1
        self.event('ENTRY_CANDIDATE',**signal,qty=qty)
        o=self.submit('BUY',sym,qty,signal['limit'],signal)
        if o and not c.live_mode and o['status']=='ACCEPTED':
            # 가격 상한 안에서 불리한 체결을 가정한다. 실제 체결 보장은 아니다.
            self.cumulative_fill(o,qty,qty*signal['limit'])
        return o

    def cumulative_fill(self,o,total,amount):
        if total<o['filled'] or (total==o['filled'] and abs(amount-o['amount'])<0.01):
            return
        if total>o['qty'] or total<0 or amount<o['amount'] or (total==o['filled'] and amount!=o['amount']):
            self.halt('체결 누계 불일치'); return
        delta=total-o['filled']
        if not delta: return
        price=(amount-o['amount'])/delta
        if price<=0: self.halt('체결가격 오류'); return
        sym=o['symbol']; positions=self.s['positions']; c=self.c
        if o['side']=='BUY':
            sig=o['signal']; p=positions.get(sym)
            if not p:
                p={'symbol':sym,'qty':0,'avg':price,'initial_stop':sig['stop'],'stop':sig['stop'],
                    'initial_r':max(1,price-sig['stop']),'high':price,'low':price,'opened':self.clock().isoformat(),
                    'exit_requested':False,'exit_reason':'','exit_attempts':0,'qty_blocked':False,'last_tick':self.clock().isoformat()}
                positions[sym]=p
                self.position_created(p,o)
            p['avg']=(p['qty']*p['avg']+delta*price)/(p['qty']+delta); p['qty']+=delta
            p['initial_r']=max(1,p['avg']-p['initial_stop'])
            p['high']=max(p['high'],price); p['low']=min(p['low'],price)
            self.s['cash']-=delta*price+self.buy_fee(delta*price)
            if price>o['limit'] or price<=p['stop']:
                p['exit_requested']=True; p['exit_reason']='진입 체결가격 범위 이탈'
        else:
            p=positions.get(sym)
            if not p or delta>p['qty']:
                self.halt('매도 체결수량이 관리 잔고 초과'); return
            pnl=(price-p['avg'])*delta-self.buy_fee(p['avg']*delta)-self.sell_fee(price*delta)
            self.s['realized']+=pnl; self.s['daily_loss']+=max(0,-pnl)
            self.s['cash']+=delta*price-self.sell_fee(delta*price)
            self.s['trades'].append(self.decorate_trade({'symbol':sym,'opened':p['opened'],'closed':self.clock().isoformat(),'qty':delta,
                'entry':p['avg'],'exit':price,'pnl_estimate':pnl,'MFE':p['high']-p['avg'],'MAE':p['avg']-p['low'],
                'initial_stop':p['initial_stop'],'final_stop':p['stop'],'reason':p['exit_reason']},p))
            p['qty']-=delta
            if not p['qty']: del positions[sym]
        self.fill_applied(o,delta,price)
        o['filled']=total; o['amount']=amount
        if total==o['qty']: o['status']='FILLED'
        self.save(); self.event('FILL',side=o['side'],symbol=sym,qty=delta,price=price)

    def on_order_event(self,v):
        order_id=str(v.get('9203','')); original=str(v.get('904',''))
        known=next((o for o in self.s['orders'].values() if o['id']==order_id and o['id']),None)
        if not known:
            cancel=next((o for o in self.s['orders'].values() if o['id']==original and o.get('cancel_id')==order_id),None)
            if cancel and v.get('913')=='확인' and '취소' in v.get('905',''):
                # 취소 확인 전후의 체결 경합은 다음 REST 대조로 확정한다.
                self.event('CANCEL_CONFIRMED',local=cancel['local'])
            else:
                sym=code(v['9001'])
                self.s['excluded']=sorted(set(self.s['excluded'])|{sym}); self.save()
                if sym in self.s['positions']:
                    self.s['positions'][sym]['qty_blocked']=True
                    self.halt('관리 종목에 외부 주문 감지: 계좌 대조 필요')
            return
        if code(v['9001'])!=known['symbol'] or v.get('907')!=('2' if known['side']=='BUY' else '1'):
            self.halt('주문 이벤트 종목/방향 불일치'); return
        if v.get('913')=='체결' and '취소' not in v.get('905','') and '정정' not in v.get('905',''):
            if int(number(v['900']))!=known['qty']:
                self.halt('체결 이벤트 주문수량 불일치'); return
            total=int(number(v['900']))-int(number(v['902']))
            self.cumulative_fill(known,total,number(v['903']))
        elif v.get('919') not in (None,'','0') or v.get('913')=='거부':
            known['status']='REJECTED'; self.save()
            self.event('ORDER_REJECTED',local=known['local'],reason='실시간 거부')

    def cancel(self,o):
        if o['status'] in TERMINAL or o['status'] in ('CANCEL_SENT','UNKNOWN','INTENT'):
            return
        o['status']='CANCEL_SENT'; self.save()
        try:
            if self.c.live_mode:
                o['cancel_id']=self.broker.cancel(o['symbol'],o['id'])
            else: o['status']='CANCELED'
            self.save(); self.event('CANCEL_SENT',local=o['local'])
        except Exception:
            o['status']='UNKNOWN'; self.save(); self.halt('취소 결과 불명확: 자동 재전송 금지')

    def request_exit(self,symbol,reason):
        p=self.s['positions'].get(symbol)
        if p:
            p['exit_requested']=True; p['exit_reason']=p['exit_reason'] or reason; self.save()
        for o in self.active_orders(symbol,'BUY'): self.cancel(o)

    def drive_exits(self,prices=None):
        prices=prices or {}
        for sym,p in list(self.s['positions'].items()):
            if not self.can_exit(sym,p): continue
            if not p['exit_requested'] or self.active_orders(sym) or p.get('qty_blocked'): continue
            if self.c.live_mode and (not self.account_ok or time.monotonic()-self.last_account>self.c.reconcile_seconds*3):
                continue
            if not self.c.live_mode and sym not in prices:
                continue
            if p['exit_attempts']>=self.c.max_exit_attempts:
                self.halt('청산 주문 반복 거절/미체결: HTS 즉시 확인'); continue
            p['exit_attempts']+=1
            o=self.submit('SELL',sym,p['qty'])
            if o and not self.c.live_mode and o['status']=='ACCEPTED':
                px=prices.get(sym)
                if px is None:
                    o['status']='CANCELED'; self.save(); continue
                self.cumulative_fill(o,o['qty'],o['qty']*px*(1-self.c.exit_slippage_fraction))

    def on_tick(self,t,completed_low=None):
        p=self.s['positions'].get(t.symbol)
        if not p: return
        previous=p['stop']; p['last_tick']=self.clock().isoformat()
        p['low']=min(p['low'],t.price)
        p['high'],p['stop']=self.protection(p,t.price,completed_low)
        # 보호선은 변경 즉시 저장. 나머지 틱 통계는 초당 한 번 체크포인트한다.
        if p['stop']>previous or time.monotonic()-self.last_checkpoint>=1:
            self.save(); self.last_checkpoint=time.monotonic()
        if p['stop']>previous: self.event('STOP_RAISED',symbol=t.symbol,stop=p['stop'])
        if min(t.price,t.bid if t.bid>0 else t.price)<=p['stop']:
            self.request_exit(t.symbol,'구조/이익보호선 이탈')
        self.drive_exits({t.symbol:t.bid or t.price})

    def timer(self,prices=None):
        from datetime import datetime
        current=self.clock()
        if self.session_ending():
            for o in self.active_orders(side='BUY'): self.cancel(o)
            for sym in list(self.s['positions']): self.request_exit(sym,'당일 청산 시각')
        for o in list(self.active_orders()):
            if (current-datetime.fromisoformat(o['created'])).total_seconds()>self.c.order_timeout_seconds:
                self.cancel(o)
        for sym,p in list(self.s['positions'].items()):
            if (current-datetime.fromisoformat(p['last_tick'])).total_seconds()>self.c.position_stale_seconds:
                self.halt('보유종목 시세 지연: 긴급 청산 요청')
                self.request_exit(sym,'시세 지연/연결 단절')
        self.drive_exits(prices)

    def reconcile(self,snapshot,fills=()):
        """잔고와 미체결 조회가 불일치하면 신규 진입을 금지하고 기존 보호는 유지한다."""
        self.account_ok=False
        balances=snapshot['holdings']; open_orders={str(r['ord_no']):r for r in snapshot['orders']}
        for p in self.s['positions'].values(): p['qty_blocked']=False
        for o in self.active_orders():
            if not o['id']:
                self.halt('주문번호 없는 전송 의도: HTS 수동 대조 필요'); continue
            # 체결내역은 주문별로 합산. 중복/초과 또는 잔고 불일치는 성공으로 간주하지 않는다.
            rows=[r for r in fills if str(r['ord_no'])==o['id'] and r.get('ord_stt')=='체결']
            if rows:
                total=sum(int(number(r['cntr_qty'])) for r in rows)
                amount=sum(number(r['cntr_qty'])*number(r['cntr_pric']) for r in rows)
                self.cumulative_fill(o,total,amount)
            if o['id'] in open_orders:
                r=open_orders[o['id']]
                if code(r['stk_cd'])!=o['symbol']:
                    self.halt('미체결 종목 불일치'); continue
                filled=o['qty']-int(number(r['oso_qty']))
                if number(r['cntr_tot_amt'])>0:
                    self.cumulative_fill(o,filled,number(r['cntr_tot_amt']))
            elif o['status']=='CANCEL_SENT':
                o['status']='CANCELED'
            elif o['status'] not in TERMINAL:
                self.halt('주문 접수 후 체결/취소 상태 미확인')
        for sym,p in self.s['positions'].items():
            b=balances.get(sym)
            if not b or b['qty']!=p['qty']:
                self.halt('관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
                # 수량 불명 상태에서는 전체 주문을 보내지 않는다.
                p['qty_blocked']=True
        external=set(balances)-set(self.s['positions'])
        own_ids={o['id'] for o in self.s['orders'].values()}|{o.get('cancel_id') for o in self.s['orders'].values()}
        for oid,r in open_orders.items():
            if oid not in own_ids:
                sym=code(r['stk_cd']); external.add(sym)
                if sym in self.s['positions']:
                    self.halt('관리 종목 외부 미체결 주문')
                    self.s['positions'][sym]['qty_blocked']=True
        self.s['excluded']=sorted(set(self.s['excluded'])|external)
        self.s['cash']=max(0,snapshot['cash']); self.s['equity']=min(self.c.account_equity,snapshot['equity'])
        self.account_ok=True; self.last_account=time.monotonic()
        self.save()
