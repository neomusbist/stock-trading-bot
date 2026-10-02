"""기존 보유분만 대상으로 하는 제한된 매도. 봇 장부/승인 자본에는 편입하지 않는다."""
import time
from datetime import datetime
from core.util import floor_tick,tick_size,ProcessLock,atomic_json
from automation.policy import BREAKEVEN_EXITS,BREAKEVEN_MAX_INVENTORY,TEST_RELEASE_SYMBOL,legacy_test_release_enabled
from automation.session import supported
from automation.decisions import gate

def ceil_tick(price):
    down=floor_tick(price)
    return down if down>=price else down+tick_size(price)

class ExistingExits:
    def external_reason(self,symbol,qty,limit,signal,transmitting=False):
        if symbol==TEST_RELEASE_SYMBOL and not legacy_test_release_enabled(): return 'TEST_CAPITAL_RELEASE_RETIRED'
        self.policy.refresh(); h=self.s['automation']['existing'].get(symbol); actual=self.actual.get(symbol)
        if symbol not in BREAKEVEN_EXITS and symbol!=TEST_RELEASE_SYMBOL: return 'EXTERNAL_HOLD'
        if signal.get('ownership')!='EXISTING_POLICY': return 'EXTERNAL_INTENT_REQUIRED'
        if signal.get('policy')!=('TEST_CAPITAL_RELEASE' if symbol==TEST_RELEASE_SYMBOL else 'BREAKEVEN_EXIT'): return 'EXTERNAL_POLICY_MISMATCH'
        if not supported(self.clock()): return 'OUT_OF_SUPPORTED_SESSION'
        if self.market!='3': return 'MARKET_NOT_REGULAR'
        if symbol==TEST_RELEASE_SYMBOL:
            if self.policy.mode not in {'ARMED','LIVE'}: return 'TEST_RELEASE_MODE_BLOCKED'
        else:
            if self.policy.mode!='LIVE' or not self.policy.state.get('live_authorized') or not self.c.live_mode: return 'LIVE_NOT_AUTHORIZED'
            if self.a.capital_limit<=0: return 'AUTO_CAP_ZERO'
            if not self.a.costs_confirmed: return 'COSTS_UNCONFIRMED'
        if not self.account_ok: return 'ACCOUNT_NOT_OK'
        if not self.connected: return 'CONNECTION_NOT_OK'
        if self.s['halt']: return 'HALT_ACTIVE'
        if time.monotonic()-self.last_account>self.c.reconcile_seconds*3: return 'ACCOUNT_RECONCILE_STALE'
        if not h or not actual or h['qty']!=actual['qty'] or qty<=0 or actual['available']<qty: return 'EXTERNAL_QTY_UNCONFIRMED'
        if symbol in self.external_qty_mismatch: return 'EXTERNAL_QTY_UNCONFIRMED'
        if any(o['symbol']==symbol for o in self.external_open_orders): return 'EXTERNAL_OPEN_ORDER'
        book=self.books.get(symbol); minimum=ceil_tick(BREAKEVEN_EXITS[symbol]) if symbol in BREAKEVEN_EXITS else None
        if not book or not book.get('regular') or not book.get('status',{}).get('pass'): return 'EXTERNAL_BOOK_OR_STATUS_UNKNOWN'
        age=(self.clock()-datetime.fromisoformat(book['time'])).total_seconds()
        max_age=self.c.stale_seconds
        if not 0<=age<=max_age: return 'EXTERNAL_BOOK_STALE'
        if symbol in BREAKEVEN_EXITS:
            if h['qty']>BREAKEVEN_MAX_INVENTORY[symbol]: return 'EXTERNAL_INVENTORY_EXCEEDS_POLICY'
            if qty!=h['qty'] or actual['available']!=actual['qty']: return 'BREAKEVEN_FULL_QTY_REQUIRED'
            if limit!=minimum or book['bid']<minimum: return 'BELOW_BREAKEVEN'
        else:
            approval=self.policy.state.get('test_capital_release',{})
            expected='SUBMITTING' if transmitting else 'APPROVED'
            if approval.get('symbol')!=TEST_RELEASE_SYMBOL or approval.get('quantity')!=1 or approval.get('side')!='SELL' or approval.get('max_qty')!=1 or approval.get('one_shot') is not True or approval.get('status')!=expected: return 'TEST_RELEASE_NOT_APPROVED'
            if h['qty']!=1 or actual['qty']!=1 or qty!=1 or limit!=book['bid']: return 'TEST_RELEASE_ONE_EXISTING_SHARE_ONLY'
        if limit<=0 or book['bid_qty']<qty: return 'INSUFFICIENT_FULL_SIZE_BID'
        return ''

    def external_guard(self,symbol,qty,limit,signal):
        return not self.external_reason(symbol,qty,limit,signal,transmitting=symbol==TEST_RELEASE_SYMBOL)

    def evaluate_existing(self):
        from automation.observation_settings import ObservationSettings
        settings=getattr(self,'observation_settings',ObservationSettings())
        if not hasattr(self,'external_eval_cache'):self.external_eval_cache={}
        for sym,h in list(self.s['automation']['existing'].items()):
            if sym==TEST_RELEASE_SYMBOL and not legacy_test_release_enabled(): continue
            if sym not in BREAKEVEN_EXITS and sym!=TEST_RELEASE_SYMBOL: continue
            if self.active_orders(sym): continue
            b=self.books.get(sym,{})
            price=max(self.prices.get(sym,0),b.get('bid') or 0)
            far=sym in BREAKEVEN_EXITS and 0<price<BREAKEVEN_EXITS[sym]*(1-settings.near_threshold_fraction)
            signature=(self.policy.mode,self.policy.state.get('revision'),self.account_ok,self.connected,self.market,self.s['halt'],h['qty'],h['available'])
            previous=self.external_eval_cache.get(sym)
            if far and previous and previous[0]==signature and self.clock().timestamp()-previous[1]<settings.far_evaluation_seconds:continue
            self.external_eval_cache[sym]=(signature,self.clock().timestamp())
            limit=ceil_tick(BREAKEVEN_EXITS[sym]) if sym in BREAKEVEN_EXITS else b.get('bid',0)
            sig={'ownership':'EXISTING_POLICY','policy':'BREAKEVEN_EXIT' if sym in BREAKEVEN_EXITS else 'TEST_CAPITAL_RELEASE',
                 'book':b,'name':self.names.get(sym),'entry_price':h.get('avg'),'starting_qty':h['qty']}
            qty=h['qty'] if sym in BREAKEVEN_EXITS else 1
            reason=self.external_reason(sym,qty,limit,sig)
            gates={'external_policy':gate(not reason,policy=sig['policy'],holding_qty=h['qty'],available=h['available'],
                     requested_qty=qty,threshold=BREAKEVEN_EXITS.get(sym),limit=limit,bid=b.get('bid'),bid_qty=b.get('bid_qty'),
                     book_time=b.get('time'),market=self.market,status=b.get('status'),approved=self.policy.state.get('test_capital_release'))}
            if b.get('time'):
                gates['external_policy']['values'].update(book_age_seconds=(self.clock()-datetime.fromisoformat(b['time'])).total_seconds(),
                    max_book_age_seconds=self.c.stale_seconds)
            self.decisions.emit('EXIT_EVAL',sym,'HOLD' if reason else 'SELL',reason or sig['policy'],gates,
                signal={**sig,'limit':h.get('avg')},price=b.get('bid'),extra={'quantity':qty,'ownership':'EXTERNAL_POLICY'})
            if reason: continue
            if sym==TEST_RELEASE_SYMBOL:
                # 전송 전 승인을 사용 중으로 영속화. 중단/응답 유실 후 승인 재사용이나 재전송 금지.
                with ProcessLock(self.policy.path.parent/'control.lock'):
                    self.policy.refresh(); a=self.policy.state.get('test_capital_release',{})
                    if a.get('status')!='APPROVED': continue
                    a.update(status='SUBMITTING',order_local=f'{self.s["day"]}-{self.s["sequence"]+1}')
                    atomic_json(self.policy.path,self.policy.state)
            self.submit('SELL',sym,qty,limit,sig)

    def external_fill(self,o,total,amount):
        if total<o['filled'] or (total==o['filled'] and amount==o['amount']): return
        if type(total) is not int:
            self.halt('기존 정책 체결수량은 정수여야 함'); return
        if not o['filled']<total<=o['qty'] or amount<=o['amount']:
            self.halt('기존 정책 체결 누계 불일치'); return
        delta=total-o['filled']; price=(amount-o['amount'])/delta
        h=self.s['automation']['existing'].get(o['symbol'])
        if not h or delta>h['qty'] or price<o['limit']:
            self.halt('기존 정책 수량/지정가 체결 불일치'); return
        fee=self.sell_fee(delta*price)
        row={'symbol':o['symbol'],'qty':delta,'price':price,'fee_estimate':fee,'net_proceeds':delta*price-fee,
             'pnl_estimate':(price-h['avg'])*delta-self.buy_fee(h['avg']*delta)-fee,
             'order_local':o['local'],'policy':o['signal']['policy'],'time':self.clock().isoformat()}
        self.s['automation'].setdefault('external_exit_fills',[]).append(row)
        h['qty']-=delta; h['available']=max(0,h['available']-delta)
        o['filled']=total; o['amount']=amount
        completed=total==o['qty']
        if completed: o['status']='FILLED'
        self.save()  # 완료 장부를 먼저 저장. 정책 완료 기록은 재시작 때도 복구할 수 있다.
        if completed and o['symbol']==TEST_RELEASE_SYMBOL:
            self.complete_test_release(o)
            self.mark_capital_refresh(o)
        pnl=sum(r['pnl_estimate'] for r in self.s['automation']['external_exit_fills'] if r['order_local']==o['local'])
        self.decisions.emit('EXIT_EVAL',o['symbol'],'SELL','EXTERNAL_FILL_COMPLETE' if completed else 'EXTERNAL_PARTIAL_FILL',
            {'fill':gate(True,total=total,amount=amount,price=price,limit=o['limit'],remaining=o['qty']-total)},
            signal={**o['signal'],'limit':h['avg']},price=price,
            extra={'quantity':delta,'net_pnl_completed':pnl if completed else None,'proceeds_allocated':0},force=True)
        self.event('EXISTING_POLICY_FILL',**row,completed=completed,proceeds_allocated=0)

    def complete_test_release(self,o):
        with ProcessLock(self.policy.path.parent/'control.lock'):
            self.policy.refresh(); a=self.policy.state.get('test_capital_release',{})
            if a.get('order_local')==o['local']:
                if a.get('status')=='COMPLETED' and a.get('filled_qty')==o['filled']: return
                a.update(status='COMPLETED',completed_at=self.clock().isoformat(),filled_qty=o['filled'])
                atomic_json(self.policy.path,self.policy.state)

    def mark_capital_refresh(self,o):
        ledger=self.s['automation']
        ledger.update(capital_refresh_pending=True,capital_refresh_requested_at=self.clock().isoformat())
        self.account_ok=False; self.save()
        self.event('ACCOUNT_CAPITAL_REFRESH_REQUIRED',symbol=o['symbol'],local=o['local'],approved_cap=self.a.capital_limit)

    def recover_test_release(self):
        for o in self.s['orders'].values():
            if o['symbol']==TEST_RELEASE_SYMBOL and (o.get('signal') or {}).get('ownership')=='EXISTING_POLICY' and o['status']=='FILLED':
                self.complete_test_release(o)
                ledger=self.s['automation']
                if 'capital_refresh_pending' not in ledger: self.mark_capital_refresh(o)
