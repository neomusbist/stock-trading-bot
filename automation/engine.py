"""V1 체결 상태기계를 보존하고 승인 자본/운영 정책을 주문 경계에 추가한다."""
import json
import time
import math
from datetime import datetime,timedelta
from pathlib import Path
from core.engine import Engine, TERMINAL
from core.broker import Rejected
from core.settings import KST
from core.market_time import market_freshness,tick_freshness,book_freshness
from core.util import code, floor_tick,tick_size,read_json_retry,atomic_json
from automation.capital import Costs, initial_allocation, owned_equity
from automation.policy import LEGACY_SYMBOL,BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,HARD_LOCKS,legacy_test_release_enabled
from automation.session import supported, SessionPlan, AfterSessionPlan, after_supported, after_entry_supported, current_session
from automation.decisions import DecisionLog,gate,STRATEGY_VERSION,BUILD_ID,STRATEGY_EXPERIMENT,safe_values
from automation.existing import ExistingExits
from automation.protection_safety import ProtectionSafety


class AutonomousEngine(ProtectionSafety,ExistingExits,Engine):
    def __init__(self,cfg,auto,policy,folder,broker,clock,shadow=False):
        source_clock=clock
        def clock():
            value=source_clock()
            return value.astimezone(KST) if value.tzinfo is not None else value
        self.a=auto.validate(); self.policy=policy; self.shadow=shadow
        self.costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,
                         cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        self.market='UNKNOWN'; self.prices={}; self.available_cash=0.; self.actual={}
        self.preview_at={}; self.kill_checked=False
        self.source_qualification={}
        self.source_qualification_seen=False
        self.control_revision=-1
        self.entry_intent=None
        self.books={}; self.external_open_orders=[]; self.external_qty_mismatch=set()
        self.names={'005930':'삼성전자','017900':'광전자','052260':'현대바이오랜드','267320':'나인테크'}; self.market_status={}
        self.decisions=DecisionLog(self)
        self.active_limit_lifecycles={}
        from automation.fast_momentum import FastMomentumStrategy
        self.fast_strategy=FastMomentumStrategy(cfg,auto,self.costs)
        # 설정 변경은 잔고/미체결이 없는 때만 허용. 변경 이력을 보존한다.
        path=Path(folder)/'state.json'
        boot_clock=clock
        carried=set()
        day_changed=False
        if path.exists():
            old=read_json_retry(path)
            if old['day']!=clock().date().isoformat():
                day_changed=True
                carried=set(old['positions'])
            if old['day']!=clock().date().isoformat() and any(o['status'] not in TERMINAL for o in old['orders'].values()):
                boot_time=datetime.fromisoformat(old['day']+'T'+clock().strftime('%H:%M:%S')).replace(tzinfo=clock().tzinfo)
                boot_clock=lambda:boot_time
            if not old['positions'] and all(o['status'] in TERMINAL for o in old['orders'].values()):
                if old['config_hash']!=cfg.fingerprint():
                    old.setdefault('settings_changes',[]).append({'time':clock().isoformat(),'from':old['config_hash'],'to':cfg.fingerprint()})
                    old['config_hash']=cfg.fingerprint()
                    from core.util import atomic_json
                    atomic_json(path,old)
        super().__init__(cfg,folder,broker,boot_clock)
        self.clock=clock
        self.account_ok=False
        self.s.setdefault('automation',{'approved_cap':auto.capital_limit,'allocated':False,'cash':0.,
            'existing':{},'consecutive_losses':0,'last_exit':{},'mode':'ARMED','strategy':'QUIET'})
        if day_changed and self.s['day']==clock().date().isoformat():
            self.s['automation']['consecutive_losses']=0
        prior=self.s['automation']['approved_cap']
        if prior!=auto.capital_limit:
            if self.s['positions'] or self.active_orders():
                raise RuntimeError('보유/미체결 중 승인 한도 변경: 기존 설정으로 복구 후 정리 필요')
            self.s['automation'].update(approved_cap=auto.capital_limit,allocated=False,cash=0.)
        self.s['cash']=self.s['automation']['cash']; self.s['equity']=0.
        prior_hash=self.s['automation'].get('config_hash')
        if prior_hash and prior_hash!=auto.fingerprint() and (self.s['positions'] or self.active_orders()):
            raise RuntimeError('관리 중 자동운용 설정 변경: 저장된 설정으로 복구 필요')
        self.s['automation']['config_hash']=auto.fingerprint()
        min_rem=getattr(auto,'minimum_remaining_minutes',15)
        self.session_plan=SessionPlan(hard_new_entry_end=auto.new_entry_cutoff,force_flatten_start=auto.force_flatten_start,minimum_remaining_minutes=min_rem,allow_overnight=auto.ALLOW_OVERNIGHT)
        self.after_session_plan=AfterSessionPlan(prep_start=getattr(auto,'after_prep_start','15:40:00'),market_start=getattr(auto,'after_market_start','16:00:00'),entry_cutoff=getattr(auto,'after_entry_cutoff','19:45:00'),force_flatten_start=getattr(auto,'after_force_flatten_start','19:55:00'),market_close=getattr(auto,'after_market_close','20:00:00'),allow_overnight=auto.ALLOW_OVERNIGHT)
        self.after_enabled=getattr(self.c,'after_enabled',False)
        self.after_default_venue=getattr(self.c,'after_default_venue','SOR')
        self.broker.order_guard=self.order_guard
        self.broker.test_release_authorizer=self.test_release_authorized
        self.recover_test_release()
        if not auto.ALLOW_OVERNIGHT:
            for sym in carried:
                p=self.s['positions'][sym]; p['exit_requested']=True
                p['exit_reason']=p['exit_reason'] or '전일 미청산 봇 잔고'; p.setdefault('exit_signal_at',clock().isoformat())
            if carried: self.event('POSITION_REMAINS_AFTER_SESSION',reason_code='OVERNIGHT_RISK',symbols=sorted(carried),message='새 계좌 대조 전 주문 보류')
        self.save()

    def disconnect(self):
        self.invalidate_protection_orders('TRANSPORT_DISCONNECTED',disconnect=True)
        super().disconnect()
        if hasattr(self, 'fast_strategy') and self.fast_strategy is not None:
            self.fast_strategy.reset('DISCONNECT')

    @property
    def after_live_authorized(self):
        if hasattr(self, '_after_live_authorized') and self._after_live_authorized is not None:
            return self._after_live_authorized
        if hasattr(self, 'policy') and hasattr(self.policy, 'state'):
            return bool(self.policy.state.get('after_live_authorized', False))
        return False

    @after_live_authorized.setter
    def after_live_authorized(self, value):
        self._after_live_authorized = bool(value) if value is not None else None
        if hasattr(self, 'policy') and hasattr(self.policy, 'state'):
            self.policy.state['after_live_authorized'] = bool(value)

    def current_session(self):
        t=self.clock()
        if supported(t): return 'REGULAR'
        if self.after_enabled and after_supported(t): return 'AFTER'
        return 'CLOSED'

    def is_supported_now(self):
        t=self.clock()
        return supported(t) or (self.after_enabled and after_supported(t))

    def entry_window(self):
        t=self.clock()
        sess=self.current_session()
        if sess=='REGULAR':
            cutoff=getattr(self,'session_plan',None).effective_new_entry_end if hasattr(self,'session_plan') else self.a.new_entry_cutoff
            return self.session_plan.is_entry_window(t) and self.market=='3' and t.strftime('%H:%M:%S')<cutoff
        elif sess=='AFTER':
            if not self.after_enabled: return False
            if not self.after_session_plan.is_entry_window(t): return False
            if self.c.live_mode and not self.after_live_authorized: return False
            return t.strftime('%H:%M:%S')<getattr(self.a,'after_entry_cutoff','19:45:00')
        return False

    def event(self,kind,**data):
        # 기존 이벤트명/체결 순서는 유지하고 공통 식별정보만 보강한다.
        order=self.s.get('orders',{}).get(data.get('local'),{})
        sym=data.get('symbol') or order.get('symbol'); p=self.s.get('positions',{}).get(sym,{})
        order_signal=p.get('signal') or order.get('signal') or {}
        if order and order_signal.get('pattern') not in {'A','B','C','FAST'}:
            order_signal=next((t.get('signal') or {} for t in reversed(self.s.get('trades',[]))
                               if t.get('exit_order_local')==order.get('local')), {})
        info={'timestamp':self.clock().isoformat(timespec='milliseconds'),'strategy_version':STRATEGY_VERSION,
              'name':self.names.get(sym),'symbol':sym,'setup':order_signal.get('pattern'),
              'build_id':BUILD_ID,'strategy_experiment':STRATEGY_EXPERIMENT}
        info.update(data)
        result=super().event(kind,**safe_values(info))
        audit=getattr(self,'candidate_audit',None)
        if audit is not None and kind in {'ORDER_ACCEPTED','FILL','ORDER_REJECTED','ORDER_UNKNOWN'}:
            try:audit.funnel.execution(kind,sym,self.clock(),side=data.get('side') or order.get('side'),
                                      order={**order,'signal':order_signal} if order else None)
            except Exception:self.entry_funnel_errors=getattr(self,'entry_funnel_errors',0)+1
        capture=getattr(self,'trade_review',None)
        if capture is not None:
            try:
                captured_at=self.clock()
                capture.event_ref(kind,data,captured_at,self.s.get('positions',{}))
                if hasattr(capture,'order_evidence'):capture.order_evidence(kind,order,data,captured_at)
                if hasattr(capture,'account_protection_evidence'):capture.account_protection_evidence(self,kind,captured_at)
                if kind=='ENTRY_CANDIDATE' and data.get('pattern')=='FAST':
                    decision_at=data.get('signal_detected_at') or data.get('time')
                    if decision_at and hasattr(capture,'decision_tape'):
                        snapshot=self.fast_strategy.snapshot_decision_tape(sym,decision_at)
                        capture.decision_tape(sym,captured_at,snapshot)
            except Exception:self.trade_review_errors=getattr(self,'trade_review_errors',0)+1
        return result
    def session_ending(self):
        sess=self.current_session()
        t=self.clock().strftime('%H:%M:%S')
        if sess=='REGULAR':
            return self.market in {'2','4','8','9'} or (not self.a.ALLOW_OVERNIGHT and t>=self.a.force_flatten_start)
        elif sess=='AFTER':
            return not self.a.ALLOW_OVERNIGHT and t>=getattr(self.a,'after_force_flatten_start','19:55:00')
        return False
    def risk_equity(self):
        return min(self.a.capital_limit,owned_equity(self.s['cash'],self.s['positions'],self.prices))
    def daily_risk_budget(self,equity=None):
        # An explicit absolute ceiling expires by KST date, including within a running process.
        if self.a.daily_risk_override_day==self.clock().date().isoformat():
            return self.a.daily_risk_override_budget_krw
        return super().daily_risk_budget(equity)
    def buy_fee(self,amount): return self.costs.buy_fee(amount)
    def sell_fee(self,amount): return self.costs.sell_fee(amount)

    def pending_buy_reservation(self,buys=None):
        buys=self.active_orders(side='BUY') if buys is None else buys
        return sum(max(0,o['qty']-o['filled'])*o['limit']*(1+self.a.commission_buy) for o in buys)

    def quantity(self,signal,equity,cash,remaining):
        return self.quantity_details(signal,equity,cash,remaining)['quantity']

    def quantity_details(self,signal,equity,cash,remaining):
        entry=signal['limit']
        policy=signal.get('invalidation_policy') or {}
        stop=(policy.get('emergency_stop')
              if self.a.confirmed_invalidation_enabled and isinstance(policy.get('emergency_stop'),(int,float)) and 0<policy['emergency_stop']<entry
              else signal.get('emergency') if isinstance(signal.get('emergency'),(int,float)) and 0<signal['emergency']<entry
              else signal['stop'])
        valid=0<stop<entry
        unit=entry-stop+self.costs.roundtrip(entry,stop)+stop*self.c.exit_slippage_fraction if valid else 0
        risk_budget=max(0,min(equity*self.c.risk_fraction,remaining))
        exposure=sum(p['qty']*p['avg'] for p in self.s['positions'].values())
        # 호출자가 미체결 예약을 뺀 cash를 전달한다. 최종 guard의 자기 INTENT를 이중 예약하지 않는다.
        amount=max(0,min(cash,self.available_cash,self.a.capital_limit-exposure,equity*self.c.max_position_fraction))
        cash_qty=int(amount//(entry*(1+self.a.commission_buy))) if entry>0 else 0
        risk_qty=int(risk_budget//unit) if unit>0 else 0
        fresh=self.capital_account_valid()
        economics=None
        if signal.get('pattern') in {'A','B','C','D','E','E2'}:
            from automation.capital import signal_economics,entry_quantity
            economics=signal_economics(self.c,self.a,signal)
            sized=entry_quantity(economics,amount,risk_budget)
            cash_qty=sized['capital_quantity'];risk_qty=sized['risk_quantity']
            valid=economics.get('status')=='KNOWN'
            unit=economics.get('risk_per_share') or 0
        qty=max(0,min(cash_qty,risk_qty)) if fresh and valid else 0
        planned=signal.get('planned_qty')
        planned_valid=planned is None or (type(planned) is int and planned>=0)
        if not planned_valid:
            qty=0
        elif planned is not None:
            qty=min(qty,planned)
        reason=('ACCOUNT_CAPITAL_UNAVAILABLE' if not fresh else 'INVALIDATION_UNKNOWN_OR_INVALID' if not valid else
                'INVALID_PLANNED_QUANTITY' if not planned_valid else 'STRATEGY_ZERO_QUANTITY' if planned==0 else
                'CAP_EXHAUSTED' if amount<=0 else 'ONE_SHARE_UNAFFORDABLE' if cash_qty==0 else
                'RISK_BUDGET_ZERO_QTY' if risk_qty==0 else 'QUANTITY_AVAILABLE')
        one_share_cost=economics.get('one_share_cash') if economics else entry*(1+self.a.commission_buy)
        structural_stop=economics.get('structural_stop') if economics else (policy.get('structural_stop') or signal.get('stop'))
        emergency_stop=economics.get('emergency_stop') if economics else stop
        zero_detail=(None if qty>0 else
                     f"ONE_SHARE_UNAFFORDABLE: one_share_cost={one_share_cost:.2f} > entry_budget={amount:.2f}" if reason=='ONE_SHARE_UNAFFORDABLE' else
                     f"RISK_BUDGET_ZERO_QTY: risk_per_share={unit:.2f} > risk_budget={risk_budget:.2f} (emergency_stop={emergency_stop})" if reason=='RISK_BUDGET_ZERO_QTY' else
                     reason)
        return {'quantity':qty,'reason_code':reason,'capital_quantity':cash_qty,'risk_quantity':risk_qty,
                'one_share_cash_affordable':bool(cash_qty>=1),'one_share_risk_affordable':bool(risk_qty>=1),
                'zero_quantity_detail':zero_detail,'structural_stop':structural_stop,'emergency_stop':emergency_stop,
                'effective_auto_cap':self.auto_available_capital(),'entry_budget':amount,
                'risk_budget':risk_budget,'risk_per_share':unit,'one_share_cost':one_share_cost,
                **({'entry_economics':economics} if economics else {}),
                'account_fresh':fresh,'strategy_quantity':planned if planned_valid else None}

    def risk_used(self,buys=None,*,execution=False):
        """Same cash-flow reserve at preview and immediately before transport."""
        from automation.capital import loss_reserve
        used=0.
        for p in self.s['positions'].values():
            if p.get('ownership','BOT')!='BOT':continue
            if (p.get('signal') or {}).get('pattern',p.get('pattern')) in {'A','B','C','D','E','E2'}:
                from automation.exit_plan import invalidation_lines
                emergency=invalidation_lines(self.c,self.a,p)['emergency']
                unit=loss_reserve(p['avg'],emergency,self.costs,filled=True)
            else:
                stop=p.get('stop',p['avg'])
                unit=max(0,p['avg']-stop+(self.costs.roundtrip(p['avg'],stop)+stop*self.c.exit_slippage_fraction if execution else p['avg']*(self.c.roundtrip_cost_fraction+self.c.exit_slippage_fraction)))
            if not math.isfinite(unit):return self.daily_risk_budget()
            used+=unit*p['qty']
        for o in self.active_orders(side='BUY') if buys is None else buys:
            sig=o.get('signal') or {};stop=sig.get('stop',o['limit'])
            if sig.get('pattern') in {'A','B','C','D','E','E2'}:
                from automation.capital import signal_economics
                econ=signal_economics(self.c,self.a,{**sig,'limit':o['limit']})
                if econ.get('status')!='KNOWN':return self.daily_risk_budget()
                unit=econ['risk_per_share']
            else:unit=o['limit']-stop+(self.costs.roundtrip(o['limit'],stop)+stop*self.c.exit_slippage_fraction if execution else o['limit']*(self.c.roundtrip_cost_fraction+self.c.exit_slippage_fraction))
            used+=unit*(o['qty']-o['filled'])
        return used

    def new_entry_reason(self,symbol):
        if self.s.get('protection_safety',{}).get('active_failures'):return 'PROTECTION_UNAVAILABLE_NEW_BUY_BLOCKED'
        if getattr(self,'balance_refresh_uncertain',False):return 'KT00018_BALANCE_REFRESH_UNCERTAIN'
        try:
            from automation.storage_governor import check_disk_safety_gate
            gate = check_disk_safety_gate(getattr(self, 'folder', Path('.')))
            if gate and gate.get('order_blocked'):
                return 'CRITICAL_LOW_DISK_SPACE'
        except Exception:
            return 'CRITICAL_LOW_DISK_SPACE'
        self.policy.refresh()
        health=getattr(self,'position_health',None)
        if health is not None:health.prune()
        if health is not None and health.waiting:return 'POSITION_DATA_RECHECK_PENDING'
        if not self.is_supported_now(): return 'OUT_OF_SUPPORTED_SESSION'
        if symbol in HARD_LOCKS or self.policy.locked(symbol): return 'HARD_LOCK'
        if symbol in BREAKEVEN_EXITS or (symbol==TEST_RELEASE_SYMBOL and legacy_test_release_enabled()): return 'EXISTING_POLICY_NO_BUY'
        st=self.market_status.get(symbol,{})
        if st.get('vi_active') or st.get('single_price_auction'): return 'VI_AUCTION_NO_BUY'
        sess=self.current_session()
        if sess=='AFTER':
            t_str=self.clock().strftime('%H:%M:%S')
            if t_str<getattr(self.a,'after_market_start','16:00:00'): return 'AFTER_PREWARM_NO_BUY'
            if t_str>=getattr(self.a,'after_entry_cutoff','19:45:00'): return 'AFTER_ENTRY_CUTOFF'
            if self.c.live_mode and not self.after_live_authorized: return 'AFTER_LIVE_NOT_AUTHORIZED'
            status=st
            if status.get('is_etf_etn') or status.get('after_ineligible'): return 'AFTER_INELIGIBLE_SYMBOL'
            raw_text='|'.join(str((status.get('raw') or {}).get(k) or '') for k in ('state','auditInfo'))
            for bad_kw, r_code in (
                ('관리종목', 'AFTER_INELIGIBLE_MANAGED'),
                ('투자경고', 'AFTER_INELIGIBLE_INVESTMENT_WARNING'),
                ('투자위험', 'AFTER_INELIGIBLE_INVESTMENT_RISK'),
                ('이상급등', 'AFTER_INELIGIBLE_ABNORMAL_SURGE'),
                ('초저유동', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
                ('투자주의환기', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION_ALERT'),
                ('투자유의', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION'),
            ):
                if bad_kw in raw_text: return r_code
        else:
            cutoff=min(self.a.new_entry_cutoff,getattr(self,'session_plan',SessionPlan()).effective_new_entry_end)
            if self.clock().strftime('%H:%M:%S')>=cutoff: return 'NEW_ENTRY_CUTOFF'
        readiness_reason=getattr(self,'readiness_reason',None)
        if readiness_reason:
            reason=readiness_reason()
            if reason:return reason
        status=self.market_status.get(symbol)
        if not status or status.get('pass') is not True: return (status or {}).get('reason_code','MARKET_STATUS_UNKNOWN')
        if status.get('checked_at'):
            age=(self.clock()-datetime.fromisoformat(status['checked_at'])).total_seconds()
            if not -1<=age<=self.a.scan_seconds*3: return 'MARKET_STATUS_STALE'
        if symbol in self.s['automation']['existing']: return '기존 보유: 추가매수 금지'
        if self.source_qualification_seen or self.source_qualification:
            qualification=self.source_qualification.get(symbol,{})
            if qualification.get('source_count',0)<max(2,self.a.min_source_count) and not qualification.get('fallback_source1'):return 'SOURCE_COUNT_BUY_PERMISSION_DENIED'
            if qualification.get('economic_reason')=='ONE_SHARE_UNAFFORDABLE':return 'ECONOMIC_UNIVERSE_EXCLUDED'
            try:age=(self.clock()-datetime.fromisoformat(qualification['timestamp'])).total_seconds()
            except (KeyError,ValueError,TypeError):return 'SOURCE_QUALIFICATION_UNKNOWN'
            if not -1<=age<=self.a.scan_seconds*3:return 'SOURCE_QUALIFICATION_STALE'
        if self.a.capital_limit<=0: return '승인 자동매매 자본 0원'
        if self.policy.mode!='LIVE': return self.policy.mode+' 신규 실주문 금지'
        if not self.policy.state.get('live_authorized') and sess=='REGULAR': return 'LIVE 명시 승인 누락'
        if not self.c.live_mode and not self.shadow: return 'live_mode=false'
        if getattr(self.a,'setup_a_paper_rr_alignment_enabled',False) and self.c.live_mode and not self.shadow:
            return 'SETUP_A_PAPER_RR_ALIGNMENT_LIVE_FORBIDDEN'
        if not self.a.costs_confirmed: return '거래비용 설정 미확인'
        if self.a.max_consecutive_losses is not None and self.s['automation']['consecutive_losses']>=self.a.max_consecutive_losses: return '연속손실 한도'
        last=self.s['automation']['last_exit'].get(symbol)
        if last and (self.clock()-datetime.fromisoformat(last)).total_seconds()<self.a.cooldown_seconds: return 'COOLDOWN'
        return ''

    def auto_available_capital(self):
        return max(0,min(self.available_cash,self.a.capital_limit))

    def capital_account_valid(self):
        return (not getattr(self,'balance_refresh_uncertain',False) and self.account_ok
                and time.monotonic()-self.last_account<=self.c.reconcile_seconds*3)

    def capital_context(self):
        equity=self.risk_equity()
        used=self.risk_used()
        return {'actual_orderable_cash':self.available_cash,'approved_cap':self.a.capital_limit,
                'risk_equity':equity,'allowed_risk_krw':equity*self.c.risk_fraction,
                'risk_remaining':max(0,self.daily_risk_budget(equity)-self.s['daily_loss']-used),
                'daily_risk_budget_krw':self.daily_risk_budget(equity),
                'daily_risk_override_day':self.a.daily_risk_override_day,
                'daily_risk_override_active':self.a.daily_risk_override_day==self.clock().date().isoformat(),
                'effective_auto_cap':self.auto_available_capital(),'entry_budget':self.watch_budget(),
                'account_fresh':self.capital_account_valid(),'last_account':self.last_account,
                'reconcile_seconds':self.c.reconcile_seconds,'snapshot_at':self.s['automation'].get('capital_snapshot',{}).get('timestamp')}

    def watch_budget(self):
        if not self.capital_account_valid(): return 0
        exposure=sum(p['qty']*p['avg'] for p in self.s['positions'].values() if p.get('ownership','BOT')=='BOT')
        reserved=self.pending_buy_reservation()
        return max(0,min(self.available_cash,self.s['cash'],self.a.capital_limit-exposure,
                         self.risk_equity()*self.c.max_position_fraction)-reserved)

    def effective_max_positions(self):
        if getattr(self.a, 'max_positions', None) is not None:
            return self.a.max_positions
        return self.c.max_positions

    def entry_gates(self,signal,tick):
        sym=tick.symbol; buys=self.active_orders(side='BUY'); equity=self.risk_equity()
        reserved=self.pending_buy_reservation(buys)
        used=self.risk_used(buys)
        remaining=self.daily_risk_budget(equity)-self.s['daily_loss']-used
        sizing=self.quantity_details(signal,equity,self.s['cash']-reserved,remaining); qty=sizing['quantity']
        effective_max_pos = self.effective_max_positions()
        reason=self.new_entry_reason(sym)
        breakout=signal.get('evaluation_gates',{}).get('breakout',{}).get('values',{}).get('breakout')
        timing=tick_freshness(tick,self.clock(),self.c.stale_seconds)
        return qty,{**signal.get('evaluation_gates',{}),
            'breakout_quote':gate(breakout is None or (tick.price>breakout and tick.ask>breakout and signal['limit']>breakout),
                breakout=breakout,price=tick.price,ask=tick.ask,entry=signal['limit']),
            'policy':gate(not reason,reason=reason,mode=self.policy.mode,live_mode=self.c.live_mode,approved_cap=self.a.capital_limit,costs_confirmed=self.a.costs_confirmed),
            'market_status':gate(self.market_status.get(sym,{}).get('pass'),status=self.market_status.get(sym)),
            'session':gate(self.entry_window(),time=self.clock().strftime('%H:%M:%S'),cutoff=getattr(self.a,'after_entry_cutoff','19:45:00') if self.current_session()=='AFTER' else self.a.new_entry_cutoff,market=self.market),
            'connection_account':gate(self.connected and self.account_ok and not self.s['halt'] and (not self.c.live_mode or time.monotonic()-self.last_account<=self.c.reconcile_seconds*3),connected=self.connected,account_ok=self.account_ok,halt=self.s['halt'],account_age=time.monotonic()-self.last_account),
            'tick':gate(tick.regular and timing['fresh'],lag=timing['exchange_age_seconds'],regular=tick.regular,stale_limit=self.c.stale_seconds,timing=timing),
            'repeat_slots':gate(not self.entry_failure(sym) and not self.active_orders(sym) and sym not in self.s['positions'] and sym not in self.s['excluded'] and (self.c.max_entries_per_symbol is None or self.s['entries'].get(sym,0)<self.c.max_entries_per_symbol) and self.entry_slots_available() and len(set(self.s['positions'])|{o['symbol'] for o in buys})<effective_max_pos,entries=self.s['entries'].get(sym,0),symbol_entry_limit=self.c.max_entries_per_symbol,consecutive_losses=self.s['automation']['consecutive_losses'],consecutive_loss_limit=self.a.max_consecutive_losses,daily_entries=sum(self.s['entries'].values()),daily_entry_limit=self.c.max_entries_per_day,daily_entry_limit_enabled=self.c.max_entries_per_day is not None,daily_entry_limit_pass=self.entry_slots_available(),reserved_entry_slots=self.pending_entry_lifecycles(),entry_count_basis=self.s.get('entry_count_basis'),failure_latch=self.entry_failure(sym),slots=len(set(self.s['positions'])|{o['symbol'] for o in buys})),
            'risk_quantity':gate(qty>0,**sizing,equity=equity,remaining_risk=remaining,reserved_cash=reserved,book_cash=self.s['cash'],actual_cash=self.available_cash,entry=signal['limit'],stop=signal['stop'],risk_fraction=self.c.risk_fraction,max_daily_loss_fraction=self.c.max_daily_loss_fraction,allowed_risk_krw=equity*self.c.risk_fraction,remaining_daily_risk_krw=max(0,remaining)),
            'efficiency':gate(signal.get('efficiency',{}).get('decision')=='TRADE' and (signal.get('net_reward_risk') or 0) >= getattr(self.c, 'min_reward_risk', 1.5),efficiency=signal.get('efficiency'),net_rr=signal.get('net_reward_risk'),min_required=getattr(self.c, 'min_reward_risk', 1.5))}

    def entry_market_reason(self,signal,quantity,fallback=None,phase='PRE_INTENT'):
        c2_verified=False
        if signal.get('research_strategy')=='C2':
            if not self.research_paper_allowed(signal):return 'C2_SEALED_PAPER_REQUIRED'
            from research.c2_proof import verify_c2_proof
            proof_tick=fallback or getattr(self,'entry_tick',None)
            if proof_tick is None or not verify_c2_proof(signal,proof_tick,self.c,self.a):return 'C2_STRUCTURE_PROOF_INVALID'
            c2_verified=True
        if signal.get('entry_basis') in {'CONFIRMED_BREAKOUT_RETEST','CONFIRMED_SUPPORT_RECLAIM'}:
            pair=signal.get('evaluation_gates',{}).get('reclaim_tick',{});v=pair.get('values',{})
            try:
                previous=datetime.fromisoformat(v['prior_time']);current=datetime.fromisoformat(v['current_time'])
                tick=fallback or getattr(self,'entry_tick',None)
                if (pair.get('result')!='PASS' or tick is None or not 0<=(current-previous).total_seconds()<=self.c.stale_seconds
                        or current!=tick.time or v['current_cumulative']!=tick.cumulative or v['current_price']!=tick.price
                        or not v['prior_cumulative']<tick.cumulative or not v['prior_price']<tick.price):
                    return 'RECLAIM_ADVANCING_TICK_PROOF_INVALID'
            except (KeyError,TypeError,ValueError):return 'RECLAIM_ADVANCING_TICK_PROOF_INVALID'
        try:
            from automation.storage_governor import check_disk_safety_gate
            gate = check_disk_safety_gate(getattr(self, 'folder', Path('.')))
            if gate and gate.get('order_blocked'):
                return 'CRITICAL_LOW_DISK_SPACE'
        except Exception:
            return 'CRITICAL_LOW_DISK_SPACE'
        if (self.a.exit_plan_reward_guard_enabled or signal.get('pattern') in {'A','B','C','D','E','E2','FAST'}) and hasattr(self, 'c') and all(signal.get(k) is not None for k in ('limit', 'stop', 'target')):
            from automation.exit_plan import build_plan
            signal['exit_plan']=build_plan(self.c,self.a,signal,quantity=quantity)
            if not signal['exit_plan']['reward_guard_pass']:return 'EXIT_PLAN_REWARD_UNKNOWN_OR_LOW'
        if self.a.structural_breakout_enabled and signal.get('pattern')=='A':
            from automation.structural_breakout import structural_breakout
            try:
                stamp=datetime.fromisoformat(signal['time'])
                received=signal.get('reference_received_at')
                if received and datetime.fromisoformat(received)>stamp:return 'BREAKOUT_REFERENCE_NOT_KNOWN_AT_SIGNAL'
                quote=signal['evaluation_gates']['spread']['values']
                passed,evidence=structural_breakout(signal.get('reference_bar_snapshot'),stamp,signal,self.c,signal['limit'],quote['bid'],quote['ask'],auto=self.a,decision_bars=signal.get('decision_bar_snapshot'))
                proof=signal['evaluation_gates'].get('breakout_structure')
                paper_align=bool(getattr(self.a,'setup_a_paper_rr_alignment_enabled',False) and not self.c.live_mode)
                allowed_proj=max(signal['projection'],evidence['target_ceiling']) if paper_align else signal['projection']
                rr_stop=(evidence['structural_stop'] if (paper_align and getattr(self.a,'setup_a_rr_stop_basis','EMERGENCY_STOP')=='STRUCTURAL_STOP') else signal['stop'])
                if (passed is not True or proof!={'result':'PASS','values':{'enabled':True,**evidence}}
                        or signal.get('invalidation_policy')!={'enabled':True,**evidence}
                        or signal['stop']!=evidence['emergency_stop'] or signal['target']>evidence['target_ceiling']
                        or signal['target']>allowed_proj
                        or (signal['target']-signal['limit'])/(signal['limit']-rr_stop)<self.c.min_reward_risk):
                    return 'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID'
                acceleration=signal['evaluation_gates'].get('acceleration',{})
                if signal.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST' and (acceleration.get('result')!='PASS'
                        or acceleration.get('values',{}).get('mode')!='CONFIRMED_BREAKOUT_RETEST'):
                    return 'BREAKOUT_RETEST_MOMENTUM_PROOF_INVALID'
                if acceleration.get('values',{}).get('mode')=='CONFIRMED_CONTINUATION':
                    from automation.structural_breakout import confirmed_continuation
                    # Revalidate the exact source used by the detector. Local A
                    # uses received signal candles; references still prove its
                    # support/ceiling above and cannot substitute for those bars.
                    bars=(signal.get('decision_bar_snapshot') if signal.get('entry_basis')=='LOCAL_CONFIRMED_CONTINUATION'
                          else signal.get('reference_bar_snapshot'))
                    sustained,continuation=confirmed_continuation(bars,stamp,self.c.min_price_gain)
                    if sustained is not True or acceleration.get('result')!='PASS' or acceleration['values'].get('continuation')!=continuation:
                        return 'CONTINUATION_PROOF_MISSING_OR_INVALID'
            except (KeyError,TypeError,ValueError,ZeroDivisionError):return 'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID'
        """No I/O: recheck latest receiver state immediately before a BUY leaves the process."""
        if self.a.structural_pullback_enabled and signal.get('pattern')=='B':
            from automation.structural_pullback import structural_pullback
            try:
                proof=signal.get('evaluation_gates',{}).get('pullback_structure',{})
                values=proof.get('values',{})
                passed,evidence=structural_pullback(signal.get('decision_bar_snapshot'),datetime.fromisoformat(signal['time']),
                    signal,self.c,signal['limit'],values.get('bid'),values.get('ask'),close_policy=self.a.confirmed_invalidation_enabled,auto=self.a)
                if (passed is not True or proof!={'result':'PASS','values':{'enabled':True,**evidence}}
                        or signal['stop']!=evidence.get('emergency_stop',evidence['buffered_stop'])
                        or self.a.confirmed_invalidation_enabled and signal.get('invalidation_policy')!={'enabled':True,**evidence}
                        or evidence['target_ceiling'] is not None and signal['target']>evidence['target_ceiling']
                        or signal['target']>signal['projection']
                        or (signal['target']-signal['limit'])/(signal['limit']-signal['stop'])<self.c.min_reward_risk):
                    return 'PULLBACK_STRUCTURE_PROOF_MISSING_OR_INVALID'
            except (KeyError,TypeError,ValueError,ZeroDivisionError):return 'PULLBACK_STRUCTURE_PROOF_MISSING_OR_INVALID'
        if self.a.fresh_box_basis_enabled and signal.get('pattern')=='C' and not c2_verified:
            if self.a.confirmed_invalidation_enabled:
                from automation.structural_box import structural_box
                try:
                    values=signal['evaluation_gates']['spread']['values']
                    proof=signal['evaluation_gates']['box_structure']
                    # Carry the exact known session ceiling used by the detector.
                    setup={**signal,'prior_session_high':proof['values'].get('known_prior_session_high')}
                    passed,evidence=structural_box(signal.get('decision_bar_snapshot'),datetime.fromisoformat(signal['time']),
                        setup,self.c,self.a,signal['limit'],values['bid'],values['ask'])
                    if (passed is not True or proof!={'result':'PASS','values':{'enabled':True,**evidence}}
                            or signal.get('invalidation_policy')!={'enabled':True,**evidence}
                            or signal['stop']!=evidence['emergency_stop'] or signal['target']>evidence['target_ceiling']
                            or (signal['target']-signal['limit'])/(signal['limit']-signal['stop'])<self.c.min_reward_risk):
                        return 'BOX_STRUCTURE_PROOF_MISSING_OR_INVALID'
                except (KeyError,TypeError,ValueError,ZeroDivisionError):return 'BOX_STRUCTURE_PROOF_MISSING_OR_INVALID'
            from automation.trade_quality import fresh_box_basis
            try:
                passed,evidence=fresh_box_basis(signal.get('decision_bar_snapshot'),datetime.fromisoformat(signal['time']),self.a.box_min_bars,self.a.box_max_range)
                proof=signal.get('evaluation_gates',{}).get('box_origin',{})
                if passed is not True or proof!={'result':'PASS','values':{'enabled':True,**evidence}}:
                    return 'BOX_ORIGIN_PROOF_MISSING_OR_INVALID'
            except (KeyError,TypeError,ValueError):return 'BOX_ORIGIN_PROOF_MISSING_OR_INVALID'
        if self.a.confirmed_box_direction and signal.get('pattern')=='C' and not c2_verified:
            from automation.trade_quality import box_direction
            try:
                passed,evidence=box_direction(signal.get('decision_bar_snapshot'),datetime.fromisoformat(signal['time']),self.a.box_min_bars)
                proof=signal.get('evaluation_gates',{}).get('box_direction',{})
                if passed is not True or proof!={'result':'PASS','values':{'enabled':True,**evidence}}:
                    return 'BOX_DIRECTION_PROOF_MISSING_OR_INVALID'
            except (KeyError,TypeError,ValueError):return 'BOX_DIRECTION_PROOF_MISSING_OR_INVALID'
        if self.a.entry_context_enabled and signal.get('pattern') in {'A','B','C'}:
            proof=signal.get('evaluation_gates',{}).get('entry_context',{})
            values=proof.get('values',{})
            if (proof.get('result')!='PASS' or values.get('enabled') is not True
                    or values.get('asof')!=signal.get('time') or values.get('target')!=signal.get('target')):
                return 'ENTRY_CONTEXT_PROOF_MISSING_OR_INVALID'
            try:
                paper_align=bool(signal.get('pattern')=='A' and getattr(self.a,'setup_a_paper_rr_alignment_enabled',False) and not self.c.live_mode)
                s_stop=(signal.get('invalidation_policy') or {}).get('structural_stop',signal['stop'])
                rr_stop=(s_stop if (paper_align and getattr(self.a,'setup_a_rr_stop_basis','EMERGENCY_STOP')=='STRUCTURAL_STOP') else signal['stop'])
                rr=(signal['target']-signal['limit'])/(signal['limit']-rr_stop)
                if not math.isfinite(rr) or rr_stop>=signal['limit'] or rr<self.c.min_reward_risk:
                    return 'ENTRY_CONTEXT_RR_CHANGED'
                peak=values.get('nearest_confirmed_peak')
                if peak:
                    from automation.entry_timing import known_stamp
                    price=peak.get('price')
                    if (type(price) not in (int,float) or not math.isfinite(price) or price<=signal['limit']
                            or not known_stamp(peak.get('confirmed_at'),datetime.fromisoformat(signal['time']))
                            or signal['target']>price):
                        return 'ENTRY_CONTEXT_TARGET_ABOVE_CONFIRMED_RESISTANCE'
            except (KeyError,TypeError,ValueError,ZeroDivisionError):return 'ENTRY_CONTEXT_PROOF_MISSING_OR_INVALID'
        provider=getattr(self,'entry_quote_provider',None)
        if provider and not signal.get('symbol'):return 'ENTRY_MARKET_DATA_UNAVAILABLE'
        tick,book=provider(signal['symbol']) if provider else (fallback,{})
        checked_at=self.clock()
        if tick is not None and provider:
            from core.realtime import current_book_tick
            tick=current_book_tick(tick,book,checked_at,self.c.stale_seconds)
        evidence={'checked_at':checked_at.isoformat(),'phase':phase,
            'receiver_provider':bool(provider),'tick_available':tick is not None,'quantity':quantity,
            'limit':signal.get('limit'),'book_time':book.get('time'),
            'book_bid':book.get('best_bid'),'book_ask':book.get('best_ask'),
            'book_bid_qty':book.get('bid_qty'),'book_ask_qty':book.get('ask_qty')}
        self.latest_entry_execution_evidence=evidence
        # The saved INTENT must remain exactly equal to the in-memory record
        # during authorization. Final boundary evidence belongs in an event.
        if phase=='PRE_INTENT':signal['entry_execution_evidence']=evidence
        if tick is None:return 'ENTRY_MARKET_DATA_UNAVAILABLE'
        timing=tick_freshness(tick,checked_at,self.c.stale_seconds)
        evidence.update(tick_time=tick.time.isoformat(),tick_age_seconds=timing['exchange_age_seconds'],
            tick_received_at=getattr(tick,'received_at',None),tick_timing=timing,
            price=tick.price,bid=tick.bid,ask=tick.ask,regular=tick.regular,
            tick_venue=getattr(tick,'venue',None),tick_depth_source=getattr(tick,'depth_source',None),
            spread=(tick.ask-tick.bid)/tick.price if tick.price>0 else None)
        if not tick.regular or not timing['fresh']:return 'ENTRY_MARKET_DATA_STALE'
        if signal.get('pattern') in {'A','B','C'}:
            # A new quote does not renew the breakout tick that generated an
            # INTENT. Preserve its immutable receipt counter across PRE_HTTP.
            source=signal.get('entry_source_timing') or {}
            if source and source.get('exchange_at')!=signal.get('time'):
                return 'ENTRY_SIGNAL_TICK_MISMATCH'
            source_timing=market_freshness(signal.get('time'),checked_at,self.c.stale_seconds,
                received_at=source.get('received_at'),received_monotonic=source.get('received_monotonic'))
            evidence['signal_timing']=source_timing
            if not source_timing['fresh']:return 'ENTRY_SIGNAL_DATA_STALE'
        if signal.get('pattern')=='FAST':
            # Preserve the signal's own lifetime/chase contract at the final
            # boundary; no new momentum threshold or aggressive price is used.
            try:
                detected=datetime.fromisoformat(signal['signal_detected_at'])
                age=(checked_at-detected).total_seconds()
                ttl=signal['ttl_seconds'];ceiling=signal['max_chase_price']
                if (type(ttl) not in (int,float) or not math.isfinite(ttl) or ttl<=0
                        or type(ceiling) not in (int,float) or not math.isfinite(ceiling) or ceiling<=0):
                    return 'FAST_ENTRY_PROOF_MISSING'
                if age<0 or detected.date()!=checked_at.date():return 'FAST_SIGNAL_CLOCK_INVALID'
                if age>ttl:return 'FAST_SIGNAL_TTL_EXPIRED'
                if tick.ask>ceiling:return 'FAST_MAX_CHASE_EXCEEDED'
                obs = signal.get('fast_context_observation', {})
                if obs.get('status') == 'REJECT_CANDIDATE':
                    return obs.get('reason', 'KNOWN_CEILING_NET_REWARD_RISK_LOW')
            except (KeyError,TypeError,ValueError):return 'FAST_ENTRY_PROOF_MISSING'
        b_structure=self.a.structural_pullback_enabled and signal.get('pattern')=='B'
        a_structure=self.a.structural_breakout_enabled and signal.get('pattern')=='A'
        if b_structure or a_structure:
            key='pullback_structure' if b_structure else 'breakout_structure'
            proof=signal.get('evaluation_gates',{}).get(key,{}).get('values',{})
            if not isinstance(proof,dict):return 'ENTRY_STRUCTURE_BUFFER_UNKNOWN'
            planned_buffer=proof.get('buffer_amount') if b_structure else proof.get('buffer',proof.get('buffer_amount'))
            if type(planned_buffer) not in (int,float) or not math.isfinite(planned_buffer) or planned_buffer<0:
                return 'ENTRY_STRUCTURE_BUFFER_UNKNOWN'
            if tick.ask-tick.bid>planned_buffer:
                return 'PULLBACK_STRUCTURE_SPREAD_CHANGED'
            if provider:
                try:
                    book_timing=market_freshness(book['time'],checked_at,self.c.stale_seconds,
                        received_at=book.get('received_at'),received_monotonic=book.get('received_monotonic'))
                    bid,ask=book['best_bid'],book['best_ask']
                    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (bid,ask)) or bid>ask:
                        return 'PULLBACK_ENTRY_BOOK_UNAVAILABLE'
                except (KeyError,TypeError,ValueError):return 'PULLBACK_ENTRY_BOOK_UNAVAILABLE'
                evidence['book_timing']=book_timing
                if not book_timing['fresh']:return 'PULLBACK_ENTRY_BOOK_STALE'
                if ask>signal['limit']:return 'ENTRY_QUOTE_ABOVE_LIMIT'
                if ask-bid>planned_buffer:
                    return 'PULLBACK_STRUCTURE_SPREAD_CHANGED'
        breakout=signal.get('evaluation_gates',{}).get('breakout',{}).get('values',{}).get('breakout') or signal.get('breakout')
        # Legacy core synthetic signals have no geometry. The real runner always
        # supplies a provider; all production A/B/C signals also carry breakout.
        if provider or breakout is not None:
            if not 0<tick.bid<=tick.ask or tick.price<=0 or (tick.ask-tick.bid)/tick.price>self.c.max_spread:return 'ENTRY_SPREAD_CHANGED'
            # Authorize the exact persisted order price. A tolerance applied
            # only to this check leaves a resting BUY below the current ask,
            # which can fill later after its momentum premise has changed.
            if signal['limit']<tick.ask:return 'ENTRY_QUOTE_ABOVE_LIMIT'
        if breakout is not None:
            if tick.price<=breakout or tick.ask<=breakout or signal['limit']<=breakout:return 'BREAKOUT_QUOTE_NOT_CONFIRMED'
            if max(tick.price,signal['limit'])/breakout-1>self.c.max_chase:return 'ENTRY_CHASE_CHANGED'
        # FAST's executable-depth requirement also applies in the regular
        # session. A fresh signal is not proof of liquidity at PRE_HTTP.
        if self.current_session()=='AFTER' or signal.get('pattern')=='FAST':
            if book:
                try:book_timing=market_freshness(book['time'],checked_at,self.c.stale_seconds,
                        received_at=book.get('received_at'),received_monotonic=book.get('received_monotonic'))
                except (KeyError,ValueError,TypeError):return 'ENTRY_DEPTH_UNAVAILABLE'
                evidence['book_timing']=book_timing
                if not book_timing['fresh'] or not book.get('depth_available'):return 'ENTRY_DEPTH_UNAVAILABLE'
                if book.get('best_bid')!=tick.bid or book.get('best_ask')!=tick.ask:return 'ENTRY_DEPTH_PRICE_MISMATCH'
                ask_qty=book.get('ask_qty',0);bid_qty=book.get('bid_qty',0)
            else:
                if getattr(tick,'depth_available',None) is False:return 'ENTRY_DEPTH_UNAVAILABLE'
                stamp=getattr(tick,'book_time',None)
                if stamp is not None and not book_freshness(tick,checked_at,self.c.stale_seconds)['fresh']:return 'ENTRY_DEPTH_UNAVAILABLE'
                ask_qty=tick.ask_qty;bid_qty=tick.bid_qty
            if any(type(q) not in (int,float) or not math.isfinite(q) or q<0 for q in (ask_qty,bid_qty)):
                return 'ENTRY_DEPTH_UNAVAILABLE'
            if min(ask_qty,bid_qty)<max(1,quantity):return 'ENTRY_DEPTH_BELOW_ACTUAL_QUANTITY'
        return None

    def research_paper_allowed(self,signal):
        """Research entries may reach only the explicitly sealed offline broker."""
        if not isinstance(signal,dict):return False
        research=(signal.get('paper_only') is True or signal.get('pattern') in {'D','E','E2'}
                  or signal.get('research_strategy') in {'D','E','E2','C2'})
        if not research:return True
        broker=type(self.broker)
        return (getattr(self,'paper_mode',False) is True
                and broker.__module__=='research.causal_account'
                and broker.__name__=='QuoteReplayBroker'
                and self.policy.state.get('PAPER_FIXTURE_ONLY') is True)

    def enter(self,signal,tick):
        if not self.research_paper_allowed(signal):
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','RESEARCH_PAPER_ONLY',signal=signal,price=tick.price)
            return None
        if signal.get('pattern') in ('EarlyMomentumBreakout', 'PullbackReclaimBreakout'):
            if not (getattr(self.a, 'enable_redesign_live', False) and getattr(self.c, 'enable_redesign_live', False)):
                self.decisions.emit('ENTRY_EVAL', tick.symbol, 'NO_BUY', 'REDESIGN_STRATEGY_LIVE_NOT_AUTHORIZED', signal=signal, price=tick.price)
                return None
        if signal.get('SHADOW_ONLY') is True or signal.get('actual_order_permission') is False or signal.get('diagnostic_only') is True or ('source_count' in signal and signal['source_count']<max(2,self.a.min_source_count) and not signal.get('fallback_source1')):
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','OBSERVATION_NOT_BUY_PERMISSION',signal=signal,price=tick.price);return None
        self.diagnostic('signal_count')
        from automation.allocation import apply_empirical
        try:
            if code(signal.get('symbol'))!=tick.symbol: raise ValueError('signal/tick symbol mismatch')
        except (TypeError,ValueError):
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','INVALID_SYMBOL',{'symbol':gate(False,signal_symbol=signal.get('symbol'),tick_symbol=tick.symbol)},price=tick.price)
            self.diagnostic('internal_reject_count'); return None
        if not self.is_supported_now():
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','NO_BUY_OUTSIDE_SESSION',
                {'session':gate(False,clock=self.clock().isoformat(),timezone_known=self.clock().tzinfo is not None)},signal=signal,price=tick.price)
            self.diagnostic('internal_reject_count'); return None
        if any(not isinstance(signal.get(k),(int,float)) or not math.isfinite(signal[k]) or signal[k]<=0 for k in ('limit','stop')) or signal['stop']>=signal['limit']:
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','INVALIDATION_UNKNOWN_OR_INVALID',
                {'invalidation':gate(False,entry=signal.get('limit'),stop=signal.get('stop'))},signal=signal,price=tick.price,extra={'quantity':0})
            self.diagnostic('internal_reject_count'); return None
        signal=apply_empirical(signal,self.s['trades'],self.a)
        if signal.get('pattern') in {'A','B','C'} and not signal.get('entry_source_timing'):
            try:source_matches=datetime.fromisoformat(signal['time'])==tick.time
            except (KeyError,TypeError,ValueError):source_matches=False
            if not source_matches:
                self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','ENTRY_SIGNAL_TICK_MISMATCH',signal=signal,price=tick.price)
                return None
            signal['entry_source_timing']={'exchange_at':signal['time'],
                'received_at':getattr(tick,'received_at',None),
                'received_monotonic':getattr(tick,'received_monotonic',None)}
        qty,gates=self.entry_gates(signal,tick)
        reason=self.new_entry_reason(signal['symbol'])
        market_reason=self.entry_market_reason(signal,qty,tick)
        gates['latest_market']=gate(not market_reason,reason=market_reason,quantity=qty,quantity_scope='ACTUAL_RISK_SIZED',evidence=signal.get('entry_execution_evidence'))
        reason=reason or market_reason
        if gates['breakout_quote']['result']=='FAIL': reason=reason or 'BREAKOUT_QUOTE_NOT_CONFIRMED'
        if signal.get('efficiency',{}).get('decision')!='TRADE': reason=reason or '비용 포함 효율 부족/UNKNOWN'
        if reason:
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY',reason,gates,signal=signal,price=tick.price,extra={'quantity':qty})
            stamp=self.clock().timestamp()
            if stamp-self.preview_at.get(tick.symbol,0)>=30:
                self.preview_at[tick.symbol]=stamp
                self.event('NO_TRADE',symbol=tick.symbol,reason=reason,signal=signal,
                           approved_cap=self.a.capital_limit,available_capital=min(self.available_cash,self.a.capital_limit))
            self.diagnostic('internal_reject_count'); return None
        sess=self.current_session()
        venue=signal.get('venue') or (self.after_default_venue if sess=='AFTER' else 'KRX')
        from automation.entry_timing import mark_risk_eligible
        eligibility=('policy','market_status','session','connection_account','tick',
            'repeat_slots','risk_quantity','efficiency','breakout_quote','latest_market')
        if qty>0 and all(gates[k]['result']=='PASS' for k in eligibility):
            mark_risk_eligible(self,signal,self.clock())
        signal={**signal,'account_risk_budget_krw':self.risk_equity()*self.c.risk_fraction,
                'daily_risk_budget_krw':self.daily_risk_budget(),
                'approved_cap_krw':self.a.capital_limit,'bot_equity_krw':self.risk_equity(),
                'reentry':self.s['entries'].get(signal['symbol'],0)>0,'market_state':self.market,
                'daily_attempt':sum(self.s['entries'].values())+1,'symbol_attempt':self.s['entries'].get(signal['symbol'],0)+1,
                'order_sent_at':self.clock().isoformat(),
                'session':sess,'venue':venue}
        signal.update(entry_build_id=BUILD_ID,entry_strategy_version=STRATEGY_VERSION,
                      entry_strategy_experiment=STRATEGY_EXPERIMENT)
        signal.setdefault('strategy_planned_qty',signal.get('planned_qty'))
        signal['planned_qty']=qty
        fast_signal = signal.get('pattern') == 'FAST'
        sig_time_raw = signal.get('signal_detected_at') or signal.get('time')
        if fast_signal and sig_time_raw:
            try:
                sig_dt = datetime.fromisoformat(sig_time_raw)
                latency = max(0.0, (self.clock() - sig_dt).total_seconds())
                signal['entry_latency_seconds'] = latency
                ttl = signal.get('ttl_seconds', 15.0)
                if latency > ttl:
                    gates['signal_latency'] = gate(False, latency=latency, ttl=ttl)
                    self.decisions.emit('ENTRY_EVAL', tick.symbol, 'NO_BUY', f'SIGNAL_TTL_EXPIRED ({latency:.1f}s > {ttl}s)',
                                        gates, signal=signal, price=tick.price, extra={'quantity': qty})
                    self.event('NO_TRADE', symbol=tick.symbol, reason=f'SIGNAL_TTL_EXPIRED ({latency:.1f}s > {ttl}s)', latency=latency, ttl=ttl)
                    return None
            except (ValueError, TypeError):
                pass
        max_chase = signal.get('max_chase_price')
        if max_chase and tick.ask > max_chase:
            gates['max_chase'] = gate(False, ask=tick.ask, max_chase_price=max_chase)
            self.decisions.emit('ENTRY_EVAL', tick.symbol, 'NO_BUY', f'MAX_CHASE_EXCEEDED (ask {tick.ask} > max {max_chase})',
                                gates, signal=signal, price=tick.price, extra={'quantity': qty})
            self.event('NO_TRADE', symbol=tick.symbol, reason='MAX_CHASE_EXCEEDED', ask=tick.ask, max_chase=max_chase)
            return None
        if 'entry_latency_seconds' in signal:
            self.event('ENTRY_LATENCY_RECORDED', symbol=tick.symbol, latency_seconds=signal['entry_latency_seconds'],
                       ttl_seconds=signal.get('ttl_seconds', 15.0), ask=tick.ask, limit=signal['limit'])
        from automation.exit_plan import build_plan
        signal['exit_plan']=build_plan(self.c,self.a,signal,quantity=qty)
        if (self.a.exit_plan_reward_guard_enabled or signal.get('pattern') in {'B','C','D','E','E2'}) and not signal['exit_plan']['reward_guard_pass']:
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'NO_BUY','EXIT_PLAN_REWARD_UNKNOWN_OR_LOW',
                {'exit_plan_reward':gate(False,plan=signal['exit_plan'])},signal=signal,price=tick.price)
            return None
        self.entry_tick=tick
        self.s['automation']['strategy']='ENTER'
        self.entry_intent=signal
        if (signal.get('setup_variant') in ('A2', 'MOD_B', 'B2', 'MTF', 'MTF_3M_1M')
                or signal.get('pattern') in ('MTF',)
                or signal.get('use_limit_lifecycle')
                or signal.get('shadow_only')):
            signal['use_limit_lifecycle'] = True
        try:
            o=super().enter(signal,tick,count_signal=False)
            if o and o.get('status') == 'ACCEPTED' and signal.get('use_limit_lifecycle'):
                from automation.entry_lifecycle import LimitBuyOrder
                s_stop = (signal.get('invalidation_policy') or {}).get('structural_stop', signal.get('stop', 0.0))
                l_order = LimitBuyOrder(
                    symbol=tick.symbol,
                    limit=signal['limit'],
                    qty=qty,
                    submitted_at=self.clock(),
                    timeout_seconds=getattr(self.a, 'limit_order_timeout_seconds', 120),
                    structural_stop=s_stop
                )
                if not hasattr(self, 'active_limit_lifecycles'):
                    self.active_limit_lifecycles = {}
                self.active_limit_lifecycles[o['local']] = l_order
            failed=[k for k,v in gates.items() if v['result']=='FAIL' and v.get('required',True)]
            self.decisions.emit('ENTRY_EVAL',tick.symbol,'BUY' if o and o['status'] in {'ACCEPTED','FILLED'} else 'NO_BUY',
                'ENTRY_INTENT_ACCEPTED' if o and o['status'] in {'ACCEPTED','FILLED'} else (gates['risk_quantity']['values']['reason_code'] if failed and failed[0]=='risk_quantity' else failed[0] if failed else 'ORDER_NOT_ACCEPTED'),
                gates,signal=signal,price=tick.price,extra={'quantity':qty},force=bool(o))
            return o
        finally: self.entry_intent=None;self.entry_tick=None

    def submit(self,side,symbol,qty,limit=0,signal=None):
        # 강제로 생성한 주문도 같은 경계를 통과하며 ARMED에는 모의체결도 만들지 않는다.
        if not self.is_supported_now() or symbol in HARD_LOCKS or self.policy.locked(symbol): return None
        if side=='BUY' and signal and not self.research_paper_allowed(signal): return None
        if side=='BUY' and signal and (signal.get('SHADOW_ONLY') is True or signal.get('actual_order_permission') is False or signal.get('diagnostic_only') is True): return None
        if side=='BUY' and self.new_entry_reason(symbol): return None
        if side=='BUY' and (signal is not self.entry_intent or signal is None): return None
        sess=self.current_session()
        venue=(signal or {}).get('venue') or (self.after_default_venue if sess=='AFTER' else 'KRX')
        if side=='SELL' and sess=='AFTER' and (limit<=0 or limit is None):
            px=self.prices.get(symbol) or (self.s['positions'].get(symbol,{}).get('avg',0))
            limit=floor_tick(px) if px>0 else 0
        if signal is None:
            signal={'session':sess,'venue':venue}
        else:
            signal.setdefault('session',sess)
            signal.setdefault('venue',venue)
        if side=='SELL':
            p=self.s['positions'].get(symbol)
            external=signal and signal.get('ownership')=='EXISTING_POLICY'
            legacy=signal and signal.get('ownership')=='LEGACY_REPLACEMENT'
            if external and self.active_orders(symbol): return None
            if external and symbol==TEST_RELEASE_SYMBOL and self.policy.state.get('test_capital_release',{}).get('order_local')!=f'{self.s["day"]}-{self.s["sequence"]+1}': return None
            if external and not self.external_guard(symbol,qty,limit,signal): return None
            if not external and not legacy and (not p or not self.can_exit(symbol,p) or qty>p['qty']): return None
            if legacy and not self.legacy_exit_allowed(symbol,qty,signal): return None
            if not self.c.live_mode and not self.shadow and not (external and symbol==TEST_RELEASE_SYMBOL and self.external_guard(symbol,qty,limit,signal)): return None
        o=super().submit(side,symbol,qty,limit,signal)
        self.decisions.emit('BUY_ORDER' if side=='BUY' else 'SELL_ORDER',symbol,
            'BUY' if side=='BUY' and o['status']=='ACCEPTED' else 'SELL' if side=='SELL' and o['status']=='ACCEPTED' else 'NO_BUY' if side=='BUY' else 'HOLD',
            'ORDER_'+o['status'],{'order':gate(o['status']=='ACCEPTED',local=o['local'],status=o['status'],qty=qty,limit=limit)},
            signal=signal,position=self.s['positions'].get(symbol),extra={'quantity':qty},force=True)
        return o

    def live_order_allowed(self,side,symbol,qty,limit,signal):
        if symbol in HARD_LOCKS: return False
        if side=='BUY' and signal and not self.research_paper_allowed(signal): return False
        if side=='BUY' and signal and (
            signal.get('setup_variant') in ('A2', 'MOD_B', 'B2', 'MTF', 'MTF_3M_1M')
            or signal.get('pattern') in ('MTF',)
            or signal.get('shadow_only')
            or signal.get('use_limit_lifecycle')
        ):
            if signal.get('shadow_only'):
                return False
            # Setup A2 / modified B / MTF live trading requires explicit authorization switches on both policy and engine config
            if signal.get('setup_variant') in ('A2',):
                if not (getattr(self.a, 'enable_a2_live', False) and getattr(self.c, 'enable_a2_live', False)):
                    return False
            if signal.get('setup_variant') in ('MOD_B', 'B2') or (signal.get('pattern') == 'B' and signal.get('setup_variant') in ('MOD_B', 'B2')):
                if not (getattr(self.a, 'enable_modb_live', False) and getattr(self.c, 'enable_modb_live', False)):
                    return False
            if signal.get('pattern') in ('MTF', 'A3') or signal.get('setup_variant') in ('MTF', 'MTF_3M_1M', 'A3'):
                if not (getattr(self.a, 'enable_mtf_live', False) and getattr(self.c, 'enable_mtf_live', False)):
                    return False
        if side=='BUY':
            try:
                from automation.storage_governor import check_disk_safety_gate
                gate=check_disk_safety_gate(getattr(self,'folder',Path('.')))
                if gate and gate.get('order_blocked'):
                    return False
            except Exception:
                return False
        if self.c.live_mode: return True
        return self.test_release_authorized('kt10001' if side=='SELL' else 'kt10000',
            {'stk_cd':symbol,'ord_qty':str(qty),'ord_uv':str(limit)})

    def test_release_authorized(self,api,body):
        if not legacy_test_release_enabled(): return False
        # cfg.live_mode를 바꾸지 않는다. 저장된 267320 SELL 1주 intent만 허용한다.
        if api!='kt10001' or body.get('stk_cd')!=TEST_RELEASE_SYMBOL: return False
        try: qty=int(body['ord_qty']); limit=float(body.get('ord_uv') or 0)
        except (KeyError,TypeError,ValueError): return False
        if str(body['ord_qty'])!='1' or qty!=1: return False
        intents=[o for o in self.active_orders(TEST_RELEASE_SYMBOL,'SELL')
                 if o['status']=='INTENT' and o['qty']==1 and o['limit']==limit]
        if len(intents)!=1: return False
        o=intents[0]; sig=o.get('signal') or {}
        if sig.get('policy')!='TEST_CAPITAL_RELEASE' or sig.get('ownership')!='EXISTING_POLICY': return False
        self.policy.refresh()
        if self.policy.state.get('test_capital_release',{}).get('order_local')!=o['local']: return False
        return self.external_guard(TEST_RELEASE_SYMBOL,1,limit,sig)

    def authorization_context(self):
        """현재 enter()가 만든 정확한 자기 INTENT만 승인 재검사에서 구분한다."""
        intent=getattr(self,'_guard_intent',None)
        if intent is None or self.entry_intent is None or intent.get('signal') is not self.entry_intent:
            return {}
        return {'own_intent':(self.path,intent)}

    def order_guard(self,api,body):
        self.policy.refresh(); sym=code(body['stk_cd'])
        if api=='kt10003':
            if not self.is_supported_now(): raise Rejected('OUT_OF_SUPPORTED_SESSION')
            if not any(o['symbol']==sym and o['id']==str(body['orig_ord_no']) and o['status']=='CANCEL_SENT' for o in self.active_orders()):
                raise Rejected('봇 취소 의도와 불일치')
            return
        if not self.is_supported_now(): raise Rejected('OUT_OF_SUPPORTED_SESSION')
        if sym in HARD_LOCKS or self.policy.locked(sym): raise Rejected('HARD_LOCK')
        side='BUY' if api=='kt10000' else 'SELL' if api=='kt10001' else None
        qty=int(body['ord_qty']); limit=float(body.get('ord_uv') or 0)
        sess=self.current_session()
        if sess=='AFTER':
            if str(body.get('trde_tp'))=='3':
                raise Rejected('AFTER 시장에서는 시장가 주문(trde_tp=3) 금지: 지정가만 허용')
            if side=='BUY':
                if self.c.live_mode and not self.after_live_authorized:
                    raise Rejected('AFTER_LIVE_NOT_AUTHORIZED')
                t_str=self.clock().strftime('%H:%M:%S')
                if t_str<getattr(self.a,'after_market_start','16:00:00'):
                    raise Rejected('AFTER_PREWARM_NO_BUY')
                if t_str>=getattr(self.a,'after_entry_cutoff','19:45:00'):
                    raise Rejected('AFTER_ENTRY_CUTOFF')
                st=self.market_status.get(sym,{})
                if st.get('vi_active') or st.get('single_price_auction'):
                    raise Rejected('VI_AUCTION_NO_BUY')
                if st.get('is_etf_etn') or st.get('after_ineligible'):
                    raise Rejected('AFTER_INELIGIBLE_SYMBOL')
                raw_text='|'.join(str((st.get('raw') or {}).get(k) or '') for k in ('state','auditInfo'))
                for bad_kw, r_code in (
                    ('관리종목', 'AFTER_INELIGIBLE_MANAGED'),
                    ('투자경고', 'AFTER_INELIGIBLE_INVESTMENT_WARNING'),
                    ('투자위험', 'AFTER_INELIGIBLE_INVESTMENT_RISK'),
                    ('이상급등', 'AFTER_INELIGIBLE_ABNORMAL_SURGE'),
                    ('초저유동', 'AFTER_INELIGIBLE_ULTRA_LOW_LIQUIDITY'),
                    ('투자주의환기', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION_ALERT'),
                    ('투자유의', 'AFTER_INELIGIBLE_INVESTMENT_CAUTION'),
                ):
                    if bad_kw in raw_text: raise Rejected(r_code)
        intents=[o for o in self.active_orders(sym,side) if o['status']=='INTENT' and o['qty']==qty and o['limit']==limit]
        if len(intents)!=1: raise Rejected('저장된 봇 주문 의도 없는 직접 주문 차단')
        if side=='BUY':
            st=self.market_status.get(sym,{})
            if st.get('vi_active') or st.get('single_price_auction'):
                raise Rejected('VI_AUCTION_NO_BUY')
            for o in self.s['orders'].values():
                if o is not intents[0] and o.get('status') in {'UNKNOWN', 'INTENT'}:
                    raise Rejected(f'다른 미해결/INTENT 주문 존재 ({o.get("symbol")}): 신규 매수 차단')
            if self.s.get('halt'):
                raise Rejected(f'HALT 상태: 신규 매수 차단 ({self.s["halt"]})')
            signal=intents[0].get('signal') or {}
            if not self.research_paper_allowed(signal):raise Rejected('RESEARCH_PAPER_ONLY')
            if signal.get('SHADOW_ONLY') is True or signal.get('actual_order_permission') is False or signal.get('diagnostic_only') is True or ('source_count' in signal and signal['source_count']<max(2,self.a.min_source_count) and not signal.get('fallback_source1')):raise Rejected('OBSERVATION_NOT_BUY_PERMISSION')
            breakout=signal.get('evaluation_gates',{}).get('breakout',{}).get('values',{}).get('breakout')
            if breakout is not None and limit<=breakout:raise Rejected('BREAKOUT_QUOTE_NOT_CONFIRMED')
            market_reason=self.entry_market_reason(signal,qty,getattr(self,'entry_tick',None),phase='PRE_HTTP')
            self.event('ENTRY_EXECUTION_RECHECK',symbol=sym,local=intents[0]['local'],
                reason=market_reason,evidence=dict(self.latest_entry_execution_evidence))
            if market_reason:
                error=Rejected(market_reason);error.retryable_market_recheck=True
                raise error
            self._guard_intent=intents[0]
            try:reason=self.new_entry_reason(sym)
            finally:self._guard_intent=None
            if reason or not self.entry_window() or not self.connected or not self.account_ok or self.s['halt']:
                raise Rejected(reason or '시장/계좌/연결 조건 미충족')
            other=self.pending_buy_reservation([o for o in self.active_orders(side='BUY') if o is not intents[0]])
            exposure=sum(p['qty']*p['avg'] for p in self.s['positions'].values() if p.get('ownership','BOT')=='BOT')
            available=min(self.available_cash,self.s['cash'],max(0,self.a.capital_limit-exposure))-other
            if qty*limit*(1+self.a.commission_buy)>available: raise Rejected('승인 자본/현금 초과')
            signal=intents[0]['signal']
            used=self.risk_used([o for o in self.active_orders(side='BUY') if o is not intents[0]],execution=True)
            remaining=self.daily_risk_budget()-self.s['daily_loss']-used
            if qty>self.quantity(signal,self.risk_equity(),available,remaining): raise Rejected('주문 경계에서 위험 수량 재검증 실패')
            bot_positions={s for s,p in self.s['positions'].items() if p.get('ownership','BOT')=='BOT'}
            max_pos = self.effective_max_positions()
            if len(bot_positions|{o['symbol'] for o in self.active_orders(side='BUY')})>max_pos: raise Rejected('동시 포지션 초과')
        else:
            p=self.s['positions'].get(sym)
            if (intents[0].get('signal') or {}).get('ownership')=='EXISTING_POLICY':
                if any(o is not intents[0] for o in self.active_orders(sym)): raise Rejected('기존 보유 정책 중복 주문')
                if sym==TEST_RELEASE_SYMBOL and self.policy.state.get('test_capital_release',{}).get('order_local')!=intents[0]['local']: raise Rejected('1회 승인 주문 의도 불일치')
                if not self.external_guard(sym,qty,limit,intents[0]['signal']): raise Rejected('기존 보유 정책 주문 경계 실패')
            elif intents[0].get('signal',{} ) and intents[0]['signal'].get('ownership')=='LEGACY_REPLACEMENT':
                if not self.legacy_exit_allowed(sym,qty,intents[0]['signal']): raise Rejected('기존 포지션 교체 조건 실패')
            elif not p or not self.can_exit(sym,p) or qty>p['qty']: raise Rejected('봇 소유 수량 초과')
            if self.protection_account_fresh():
                actual=self.actual.get(sym)
                if not actual or actual['available']<qty: raise Rejected('실제 매도 가능 수량 부족')
            else:
                proof=self.protection_certificate(sym,qty,intents[0])
                if not proof['allowed']:
                    self.record_protection_failure(sym,proof['reason'])
                    raise Rejected('PROTECTION_UNVERIFIED: '+proof['reason'])
                intents[0]['protection_authority']=proof
                self.save()  # exact reduced authority must be durable before transport
        if side=='BUY' and time.monotonic()-self.last_account>self.c.reconcile_seconds*3: raise Rejected('계좌 대조 지연')

    def can_exit(self,symbol,p):
        sess=self.current_session()
        sess_ok = (supported(self.clock()) and self.market in {'3','2'}) if sess=='REGULAR' else (after_supported(self.clock()) if sess=='AFTER' else False)
        return (symbol not in HARD_LOCKS and symbol not in BREAKEVEN_EXITS and (symbol!=TEST_RELEASE_SYMBOL or not legacy_test_release_enabled()) and sess_ok and not self.policy.locked(symbol) and p.get('ownership')=='BOT' and
                not p.get('qty_blocked') and (self.policy.mode!='KILL' or self.kill_checked))

    def position_created(self,p,o):
        sig=o['signal']; p.update(ownership='BOT',trade_id=o['local'],pattern=sig.get('pattern','A'),
            entry_hour=self.clock().strftime('%H'),market_state=self.market,signal=sig,
            entry_qty=0,stop_history=[{'time':self.clock().isoformat(),'stop':p['stop']}])
        if sig.get('pattern') == 'FAST':
            p.update(emergency=sig.get('emergency', p['stop']),
                     risk_budget=sig.get('account_risk_budget_krw') or sig.get('risk_budget'),
                     target=sig.get('target'),
                     opened=self.clock().isoformat())
        p.update(first_fill_at=self.clock().isoformat(),last_entry_fill_at=self.clock().isoformat())

    def fill_applied(self,o,delta,price):
        self.s['automation']['cash']=self.s['cash']
        if o['side']=='BUY':
            self.s['positions'][o['symbol']]['entry_qty']+=delta
            self.s['positions'][o['symbol']]['last_entry_fill_at']=self.clock().isoformat()
            p=self.s['positions'][o['symbol']]
            from automation.exit_plan import build_plan
            p['exit_plan']=build_plan(self.c,self.a,p['signal'],p['avg'],p['qty'],True)
            self.refresh_exit_state(o['symbol'])
        elif o['symbol'] not in self.s['positions']:
            self.s['automation']['last_exit'][o['symbol']]=self.clock().isoformat()
            if hasattr(self, 'fast_strategy') and self.fast_strategy is not None:
                try: self.fast_strategy.record_exit(o['symbol'], self.clock())
                except Exception: pass
            fragments=[t for t in self.s['trades'] if t['trade_id']==self.s['trades'][-1]['trade_id']]
            pnl=sum(t['pnl_estimate'] for t in fragments)
            self.s['automation']['consecutive_losses']=self.s['automation']['consecutive_losses']+1 if pnl<0 else 0
        self.s['automation']['strategy']='MANAGE' if self.s['positions'] else 'QUIET'
        if o['side']=='SELL':
            t=self.s['trades'][-1]; completed=o['symbol'] not in self.s['positions']
            pnl=sum(x['pnl_estimate'] for x in self.s['trades'] if x['trade_id']==t['trade_id']) if completed else None
            self.decisions.emit('EXIT_EVAL',o['symbol'],'SELL','BOT_FILL_COMPLETE' if completed else 'BOT_PARTIAL_FILL',
                {'fill':gate(True,delta=delta,price=price,filled_before=o['filled'])},
                position={'avg':t['entry'],'qty':delta,'high':t['entry']+t['MFE'],'low':t['entry']-t['MAE'],
                          'initial_stop':t['initial_stop'],'stop':t['final_stop'],'signal':t['signal']},price=price,
                extra={'net_pnl_completed':pnl},force=True)

    def decorate_trade(self,trade,p):
        trade.update(trade_id=p['trade_id'],pattern=p['pattern'],entry_hour=p['entry_hour'],
            market_state=p['market_state'],ownership=p['ownership'],completed=trade['qty']==p['qty'],
            high=p['high'],holding_seconds=(self.clock()-datetime.fromisoformat(p['opened'])).total_seconds(),
            fee_estimate=self.buy_fee(trade['entry']*trade['qty'])+self.sell_fee(trade['exit']*trade['qty']),
            stop_history=list(p['stop_history']),signal=p['signal'])
        price=p['avg']
        band=next((label for upper,label in ((500,'0~500'),(1000,'500~1000'),(3000,'1000~3000'),
                   (10000,'3000~10000'),(30000,'10000~30000')) if price<upper),'30000 이상')
        buy_commission=self.buy_fee(trade['entry']*trade['qty']); sell_commission=trade['exit']*trade['qty']*self.a.commission_sell
        trade.update(strategy_version=p['signal'].get('entry_strategy_version','UNKNOWN_LEGACY_ENTRY'),
            entry_build_id=p['signal'].get('entry_build_id'),exit_build_id=BUILD_ID,
            exit_strategy_version=STRATEGY_VERSION,entry_strategy_experiment=p['signal'].get('entry_strategy_experiment'),
            protection_evidence=p.get('protection_evidence'),data_health=p.get('data_health'),
            exit_plan=p.get('exit_plan'),exit_state=p.get('exit_state'),
            profit_policy_observation=p.get('profit_policy_observation'),
            fast_exit_evidence=p.get('fast_exit_evidence'),
            exit_trigger_evidence=p.get('exit_trigger_evidence'),
            opportunity_timing=p['signal'].get('opportunity_timing'),
            name=p['signal'].get('name') or self.names.get(trade['symbol']),setup=p['pattern'],
            candidate_detected_at=p['signal'].get('candidate_detected_at'),signal_at=p['signal'].get('time'),
            order_sent_at=p['signal'].get('order_sent_at'),first_fill_at=p.get('first_fill_at'),last_fill_at=self.clock().isoformat(),
            entry_price=trade['entry'],entry_value=trade['entry']*trade['qty'],initial_invalidation=p['initial_stop'],
            initial_risk=p['initial_r'],highest_price=p['high'],lowest_price=p['low'],exit_signal_at=p.get('exit_signal_at'),
            exit_fill_at=self.clock().isoformat(),exit_price=trade['exit'],exit_reason=trade['reason'],
            gross_pnl=(trade['exit']-trade['entry'])*trade['qty'],commission=buy_commission+sell_commission,
            tax=trade['exit']*trade['qty']*self.a.sell_tax,net_pnl=trade['pnl_estimate'],
            price_band=band,actual_slippage=None,slippage_basis='체결가격 반영; 관측 불가 슬리피지는 null',
            intraday_trade_sequence=p['signal'].get('daily_attempt'),same_symbol_attempt=p['signal'].get('symbol_attempt'))
        return trade

    def observe_protection_bar(self,symbol,bar):
        """Retain real post-entry bars; AFTER burst failure needs bounded recent history."""
        p=self.s['positions'].get(symbol)
        if not p:return
        from automation.trade_quality import complete_sequence
        valid=complete_sequence([bar],self.clock(),1)
        if not valid:return
        try:opened=datetime.fromisoformat(p['opened'])
        except (KeyError,TypeError,ValueError):return
        b=valid[0]
        if b['time']<opened:return
        stamp=b['time'].isoformat()
        previous=p.get('protection_bars',[])
        if previous and stamp<=previous[-1]['time']:return
        p['protection_bars']=(previous+[{**b,'time':stamp}])[-30:]
        p['last_complete_bar_at']=stamp
        policy=p.get('signal',{}).get('invalidation_policy')
        from automation.exit_plan import invalidation_lines
        if invalidation_lines(self.c,self.a,p)['close_based']:
            soft=max(policy['structural_stop'],p.get('confirmed_soft_stop',policy['structural_stop']))
            p['last_invalidation_bar']={'time':stamp,'close':b['close'],'structural_stop':soft}
            if b['time']==self.clock().replace(second=0,microsecond=0)-timedelta(minutes=1) and b['close']<=soft:
                self.request_exit(symbol,'확정봉 종가의 구조적 무효화')
            if self.a.after_momentum_failure_enabled:
                from automation.after_momentum import after_momentum_failure
                failed,evidence=after_momentum_failure(p,self.clock())
                p['after_momentum_evidence']=evidence
                if failed is True:self.request_exit(symbol,'AFTER 급등 후 반등 실패 확인')
        self.refresh_exit_state(symbol)
        self.save()

    def refresh_exit_state(self,symbol):
        p=self.s['positions'].get(symbol)
        if not p or p.get('ownership','BOT')!='BOT':return
        from automation.exit_plan import state_for_position
        from automation.exit_candidates import observe_candidates
        allowed=self.can_exit(symbol,p) and self.exit_account_ready(symbol,p['qty'])
        orders=self.active_orders(symbol)
        p['exit_state']=state_for_position(self.c,self.a,p,self.clock(),len(orders),allowed,orders=orders)
        key=[p.get('last_complete_bar_at'),p.get('target_touch',{}).get('time'),bool(p.get('exit_requested')),p['qty']]
        previous=p.get('profit_policy_observation',{})
        if previous.get('source_key')!=key:
            p['profit_policy_observation']=observe_candidates(self.c,self.a,p,self.clock(),previous)
            p['profit_policy_observation']['source_key']=key

    def position_price_updated(self,p,t):
        if not t.regular or not tick_freshness(t,self.clock(),self.c.stale_seconds)['fresh']:return
        p['last_trade_time']=t.time.isoformat();p['last_trade_price']=t.price
        target=p.get('exit_plan',{}).get('target')
        if target is not None and t.price>=target and not p.get('target_touch'):
            p['target_touch']={'time':t.time.isoformat(),'price':t.price,'basis':'RECEIVED_TRADE'}
        self.refresh_exit_state(t.symbol)

    def protection(self,p,price,completed_low):
        from automation.exit_plan import invalidation_lines
        policy=p.get('signal',{}).get('invalidation_policy')
        if self.a.confirmed_structure_protection or policy and invalidation_lines(self.c,self.a,p)['close_based']:
            from automation.trade_quality import confirmed_protection
            high,stop,evidence=confirmed_protection(self.c,self.a,p,price,self.clock())
            evidence['effective_stop']=stop
            p['protection_evidence']=evidence
            if stop>p['stop']:
                p['confirmed_protection_low']=evidence['pivot_low']
                p.setdefault('stop_history',[]).append({'time':self.clock().isoformat(),'stop':stop,'evidence':dict(evidence)})
            return high,stop
        high,stop=super().protection(p,price,completed_low)
        core_stop=stop;active=high-p['avg']>=self.c.protect_at_r*p['initial_r']
        giveback=floor_tick(p['avg']+(high-p['avg'])*(1-self.a.unrealized_giveback_fraction)) if active else None
        if active:stop=max(stop,giveback)
        stop=max(p['stop'],stop)
        p['protection_evidence']={'checked_at':self.clock().isoformat(),'initial_invalidation':p.get('initial_stop'),
            'initial_price_risk':p['initial_r'],'price':price,'high':high,'activation_r':self.c.protect_at_r,
            'profit_protection_active':active,'core_stop':core_stop,
            'unrealized_giveback_fraction':self.a.unrealized_giveback_fraction,'giveback_stop':giveback,
            'prior_stop':p['stop'],'effective_stop':stop,'completed_low':completed_low,
            'emergency_bid_line':min(stop-tick_size(stop),floor_tick(stop*(1-self.c.structural_buffer)))}
        if stop>p['stop']:p.setdefault('stop_history',[]).append({'time':self.clock().isoformat(),'stop':stop,'evidence':dict(p['protection_evidence'])})
        return high,stop

    def request_exit(self,symbol,reason):
        p=self.s['positions'].get(symbol)
        if p and not p.get('exit_requested'):
            from copy import deepcopy
            p['exit_signal_at']=self.clock().isoformat()
            # Receiver/account updates after the exit latch must not rewrite why
            # an already submitted exit was requested in the first place.
            p['exit_trigger_evidence']=deepcopy({'requested_at':p['exit_signal_at'],'reason':reason,
                'initial_invalidation':p.get('initial_stop'),'effective_stop':p.get('stop'),
                'last_quote':p.get('last_quote'),'data_health':p.get('data_health'),
                'protection':p.get('protection_evidence'),
                'invalidation':p.get('invalidation_evidence'),
                'fast_exit':p.get('fast_exit_evidence'),
                'fast_protection_history':p.get('fast_protection_history'),
                'fast_protection_history_omitted':p.get('fast_protection_history_omitted',0),
                'fast_protection_evidence_error':p.get('fast_protection_evidence_error',False),
                'after_momentum':p.get('after_momentum_evidence')})
            p['exit_trigger_evidence']['exit_state']=deepcopy(p.get('exit_state'))
        return super().request_exit(symbol,reason)

    def reconcile(self,snapshot,fills=()):
        if type(snapshot.get('cash')) not in (int,float) or not math.isfinite(snapshot['cash']) or snapshot['cash']<0:
            self.account_ok=False
            raise ValueError('ACCOUNT_CASH_INVALID: 주문가능현금은 유한한 0 이상 숫자여야 함')
        query_age=0.
        if snapshot.get('query_started_at'):
            try: query_age=(self.clock()-datetime.fromisoformat(snapshot['query_started_at'])).total_seconds()
            except (TypeError,ValueError):
                self.account_ok=False; raise ValueError('ACCOUNT_QUERY_TIME_INVALID')
        self.books=snapshot.get('books',{})
        self.external_qty_mismatch=set()
        latest={}
        for o in self.s['orders'].values():
            if (o.get('signal') or {}).get('ownership')=='EXISTING_POLICY': latest[o['symbol']]=o
        for sym,o in latest.items():
            expected=o['signal']['starting_qty']-o['filled']
            if snapshot['holdings'].get(sym,{}).get('qty',0)!=expected: self.external_qty_mismatch.add(sym)
        self.external_open_orders=[{'symbol':code(r['stk_cd']),'id':str(r['ord_no'])} for r in snapshot['orders']
                                   if str(r['ord_no']) not in {o['id'] for o in self.s['orders'].values()}]
        ledger=self.s['automation']; book=ledger['cash']
        pending_refresh=ledger.get('capital_refresh_pending',False)
        if not ledger['allocated']:
            book=initial_allocation(snapshot,self.a.capital_limit); ledger['cash']=book; ledger['allocated']=True
            ledger['existing']={s:dict(h) for s,h in snapshot['holdings'].items() if s not in self.s['positions']}
        self.s['cash']=book
        if super().reconcile(snapshot,fills,_final_checkpoint=False) is False:
            self.account_ok=False
            self.s['cash']=ledger['cash']
            self.save()
            return False
        self.last_account=time.monotonic()-max(0,query_age)
        if not -1<=query_age<=self.c.reconcile_seconds*3: self.account_ok=False
        # REST에서 새 체결 누계가 반영된 뒤 다시 검사한다. 과거 잔고 스냅샷으로 추가 매도하지 않는다.
        self.external_qty_mismatch={sym for sym,o in latest.items()
            if snapshot['holdings'].get(sym,{}).get('qty',0)!=o['signal']['starting_qty']-o['filled']}
        # superは実際の現金を反映するが、運用資金は会計簿と承認上限だけから復元する。
        self.s['cash']=ledger['cash']; self.available_cash=max(0,snapshot['cash']); self.actual=snapshot['holdings']
        for sym,h in snapshot['holdings'].items():
            if sym not in self.s['positions']: ledger['existing'][sym]=dict(h)
        for sym in list(ledger['existing']):
            if sym not in snapshot['holdings']: del ledger['existing'][sym]
        ledger['capital_snapshot']={'timestamp':self.clock().isoformat(),'actual_orderable_cash':self.available_cash,
            'approved_cap':self.a.capital_limit,'auto_available_capital':self.auto_available_capital()}
        if ledger.get('capital_refresh_pending'):
            # 체결을 발견한 조회와 체결 전에 시작된 결과는 현금 재조회 완료로 인정하지 않는다.
            started=snapshot.get('query_started_at')
            fresh=pending_refresh and started and datetime.fromisoformat(started)>=datetime.fromisoformat(ledger['capital_refresh_requested_at'])
            if fresh:
                ledger['capital_refresh_pending']=False
                self.event('ACCOUNT_CAPITAL_REFRESHED',**ledger['capital_snapshot'])
            else: self.account_ok=False
        from automation.broker_pnl import settle_broker_pnl
        fee_report=settle_broker_pnl(self.s,fills,self.costs,self.clock(),getattr(self.broker,'identity','paper'))
        if fee_report['cash_adjustment'] or fee_report['pnl_adjustment']:
            self.event('BROKER_PNL_RECONCILED',**fee_report)
        self.s['equity']=self.risk_equity()
        # A healthy, flat account refresh can change only its observation time.
        # Do not fsync the same financial state every five seconds. Every cash,
        # fee, order, position, risk or HALT change still checkpoints eagerly;
        # holdings/active orders never use this flat-account fast path.
        from copy import deepcopy
        compare_started=time.perf_counter()
        # Compare the full financial state, but do not clone large immutable
        # closed-trade archives on every healthy flat refresh. Only the two
        # dictionaries whose timestamp is normalized need a shallow view.
        projection=dict(self.s)
        if 'automation' in self.s:
            projection['automation']=dict(self.s['automation'])
            if 'capital_snapshot' in self.s['automation']:
                capital=dict(self.s['automation']['capital_snapshot'])
                capital.pop('timestamp',None)
                projection['automation']['capital_snapshot']=capital
        unchanged=(self.account_ok and not self.s['positions'] and not self.active_orders()
                   and not self.s.get('halt')
                   and projection==getattr(self,'_durable_reconcile_projection',None))
        commit_started=time.perf_counter()
        try:
            if not unchanged:
                self.save(fsync=True)
                # Never retain aliases to mutable ledger objects. A later
                # nested fee/order/risk change must force another checkpoint.
                self._durable_reconcile_projection=deepcopy(projection)
        except Exception:
            self.account_ok=False
            raise
        stats=getattr(self,'reconcile_checkpoint_stats',{'written':0,'unchanged_flat':0})
        stats['unchanged_flat' if unchanged else 'written']+=1
        stats['last_commit_seconds']=time.perf_counter()-commit_started
        stats['last_compare_seconds']=commit_started-compare_started
        stats['last_action']='UNCHANGED_FLAT' if unchanged else 'DURABLE_CHECKPOINT'
        self.reconcile_checkpoint_stats=stats
        self.kill_checked=True
        self.reconciled_at=self.clock().isoformat()
        self.reconcile_query_started=snapshot.get('query_started_at',self.reconciled_at)
        self.reconcile_has_orders=bool(snapshot['orders'])
        if self.account_ok:
            self.balance_refresh_uncertain=False
            self.anchor_protection_account()

    def recover_io_halt(self):
        info=self.s.get('halt_info',{})
        if self.s.get('halt')!='데이터/상태 오류: PermissionError' or info.get('classification')!='RECOVERABLE_IO': return False
        occurred=info.get('occurred_at')
        if not occurred: return False  # historical reason-only latches need human review
        fault=info.get('exception',{}); target=fault.get('filename2') or fault.get('filename')
        if not target or Path(target).resolve() not in {self.path.resolve(),(self.folder/'heartbeat.json').resolve()}:
            info['classification']='MANUAL_REVIEW'; self.save(); return False
        if (not supported(self.clock()) or self.market!='3' or not self.connected or not self.account_ok
            or self.s['positions'] or self.active_orders() or self.external_qty_mismatch
            or getattr(self,'reconcile_has_orders',True)
            or time.monotonic()-self.last_account>self.c.reconcile_seconds*3
            or getattr(self,'reconcile_query_started','')<occurred
            or (self.clock()-datetime.fromisoformat(occurred)).total_seconds()<1): return False
        if self.policy.refresh().get('test_capital_release',{}).get('status')=='SUBMITTING': return False
        # Verify the failed storage path has recovered before changing the latch.
        self.save(); check=read_json_retry(self.path)
        if check.get('halt')!=self.s['halt'] or check.get('orders')!=self.s['orders']: return False
        if Path(target).resolve()!=self.path.resolve():
            heartbeat=read_json_retry(target); atomic_json(target,heartbeat); read_json_retry(target)
        old=dict(info); old.update(active=False,recovered_at=self.clock().isoformat(),recovery='FRESH_RECONCILE_FLAT_STORAGE_OK')
        self.s.setdefault('halt_history',[]).append(old)
        self.s['halt']=''; self.s['halt_info']=old
        try: self.save()
        except Exception:
            self.s['halt']=info['reason']; self.s['halt_info']=info; raise
        self.event('HALT_RECOVERED',halt_info=old)
        return True

    def legacy_exit_allowed(self,symbol,qty,signal):
        # 계산 도구는 allocation.py에 보존. 현재 실계좌의 기존 보유 매도에는 연결하지 않음.
        return False

    def replace_legacy(self,comparison):
        h=self.s['automation']['existing'].get(LEGACY_SYMBOL)
        sig={'ownership':'LEGACY_REPLACEMENT','comparison':comparison}
        if h and self.legacy_exit_allowed(LEGACY_SYMBOL,h['available'],sig) and not self.active_orders(LEGACY_SYMBOL):
            return self.submit('SELL',LEGACY_SYMBOL,h['available'],signal=sig)

    def cumulative_fill(self,o,total,amount):
        if (o.get('signal') or {}).get('ownership')=='EXISTING_POLICY':
            return self.external_fill(o,total,amount)
        if o.get('signal') and o['signal'].get('ownership')=='LEGACY_REPLACEMENT':
            if total<o['filled']: return
            if not o['filled']<=total<=o['qty'] or amount<o['amount'] or (total==o['filled'] and amount!=o['amount']):
                self.halt('기존 포지션 교체 체결 누계 불일치'); return
            delta=total-o['filled']
            if delta:
                px=(amount-o['amount'])/delta
                if px<=0: self.halt('교체 체결가격 오류'); return
                h=self.s['automation']['existing'].get(o['symbol'])
                if not h or delta>h['qty']: self.halt('교체 기존 잔고 불일치'); return
                h['qty']-=delta; h['available']=max(0,h['available']-delta)
                self.event('LEGACY_REPLACEMENT_FILL',symbol=o['symbol'],qty=delta,price=px,
                           proceeds_allocated=0,comparison=o['signal']['comparison'])
                o['filled']=total; o['amount']=amount
                if total==o['qty']: o['status']='FILLED'
                self.save()
            return
        return super().cumulative_fill(o,total,amount)

    def controls(self,prices=None):
        getattr(self.policy,'refresh_if_changed',self.policy.refresh)(); mode=self.policy.mode
        revision=self.policy.state['revision']
        if revision!=self.control_revision:
            self.control_revision=revision
            if mode=='KILL': self.kill_checked=False
            self.event('MODE',mode=mode,revision=revision)
        self.s['automation']['mode']=mode
        bot_work=self.s['positions'] or any(not (o['symbol']==TEST_RELEASE_SYMBOL and (o.get('signal') or {}).get('policy')=='TEST_CAPITAL_RELEASE' and (o.get('signal') or {}).get('ownership')=='EXISTING_POLICY') for o in self.active_orders())
        if mode in {'OFF','ARMED'} and (bot_work or (mode=='OFF' and self.active_orders())):
            # CLI는 이 요청을 막는다. 외부 파일 변경 때도 보호가 사라지지 않게 STOP으로 전환한다.
            from core.util import ProcessLock
            with ProcessLock(self.policy.path.parent/'control.lock'): self.policy.command('STOP')
            mode='STOP'; self.s['automation']['mode']=mode
            self.event('MODE_DEFERRED',requested='OFF/ARMED',effective='STOP',reason='기존 봇 보호 유지 후 종료')
        if mode in {'OFF','ARMED','STOP','STOP_FLAT','KILL'}:
            for o in list(self.active_orders(side='BUY')): self.cancel(o)
        if mode in {'STOP_FLAT','KILL'}:
            if mode=='KILL' and not self.kill_checked:
                self.account_ok=False
                return False
            for sym in list(self.s['positions']): self.request_exit(sym,'사용자 '+mode)
            self.drive_exits(prices)
        return mode in {'OFF','STOP','STOP_FLAT','KILL'} and not self.s['positions'] and not self.active_orders()

    def drive_exits(self,prices=None):
        self.audit_protection_health()
        sess=self.current_session()
        t_str=self.clock().strftime('%H:%M:%S')
        is_after_flatten=(sess=='AFTER' and not self.a.ALLOW_OVERNIGHT and
                          t_str>=getattr(self.a,'after_force_flatten_start','19:55:00'))
        if not is_after_flatten:
            return super().drive_exits(prices)

        # 20:00 이후 신규 주문 생성 절대 금지 및 미청산 잔고 감지 시 HALT
        if t_str>=getattr(self.a,'after_market_close','20:00:00'):
            bot_positions={sym:p['qty'] for sym,p in self.s['positions'].items() if p.get('ownership')=='BOT' and p.get('qty',0)>0}
            if bot_positions and not self.s.get('halt'):
                self.event('AFTER_CLOSE_UNFLATTENED_POSITION',symbols=list(bot_positions.keys()),holdings=bot_positions)
                self.halt(f'AFTER 마감 시점 미청산 봇 잔고 감지: {bot_positions}')
            return

        prices=prices or self.prices or {}
        for sym,p in list(self.s['positions'].items()):
            if p.get('ownership')!='BOT': continue # 외부/HARD_LOCK 보유는 절대 강제청산 대상 아님
            if not self.can_exit(sym,p): continue
            if not p.get('exit_requested'):
                p['exit_requested']=True; p['exit_reason']='AFTER 강제 청산'; self.save()
            if self.active_orders(sym): continue # 중복 SELL 금지: 기존 주문 취소/체결 대기
            if any(o.get('status') in {'UNKNOWN','INTENT'} for o in self.s['orders'].values() if o.get('symbol')==sym):
                continue # UNKNOWN/INTENT 발생 시 fail-closed (reconcile 후 재개)
            if p.get('qty_blocked') or p['qty']<=0: continue
            if not self.exit_account_ready(sym,p['qty']):
                continue
            if not self.circuit_exit_ready(sym,p):continue

            # 일반 정상 청산의 max_exit_attempts=3 제한을 우회하여 19:55~19:59:50 지속 재시도
            p['exit_attempts']=p.get('exit_attempts',0)+1

            # 현재 허용된 가장 공격적인 지정가 재산출 (시장가 생성 금지)
            px=prices.get(sym) or self.prices.get(sym) or p.get('avg',0)
            st=self.market_status.get(sym,{})
            bid=prices.get(sym) or getattr(st,'bid',None) or px
            limit_px=floor_tick(bid*(1-self.c.exit_slippage_fraction)) if bid>0 else floor_tick(px)
            if limit_px<=0: limit_px=floor_tick(px) if px>0 else 0

            o=self.submit('SELL',sym,p['qty'],limit=limit_px)
            if o and not self.c.live_mode and o['status']=='ACCEPTED':
                fill_px=prices.get(sym,limit_px)
                self.cumulative_fill(o,o['qty'],o['qty']*fill_px*(1-self.c.exit_slippage_fraction))

    def timer(self,prices=None):
        self.prices=prices or self.prices
        t_str=self.clock().strftime('%H:%M:%S')
        if hasattr(self, 'active_limit_lifecycles') and self.active_limit_lifecycles:
            now_dt = self.clock()
            for local_id, l_order in list(self.active_limit_lifecycles.items()):
                if (now_dt - l_order.submitted_at).total_seconds() > l_order.timeout_seconds:
                    l_order.expire(now_dt)
                    o = self.s['orders'].get(local_id)
                    if o and o['status'] in ('ACCEPTED', 'PARTIAL'):
                        self.cancel(o)
                    self.active_limit_lifecycles.pop(local_id, None)
        if not self.is_supported_now():
            if t_str>='20:00:00' and not self.a.ALLOW_OVERNIGHT:
                bot_positions={sym:p['qty'] for sym,p in self.s['positions'].items() if p.get('ownership')=='BOT' and p.get('qty',0)>0}
                if bot_positions and not self.s.get('halt'):
                    self.event('AFTER_CLOSE_UNFLATTENED_POSITION',symbols=list(bot_positions.keys()),holdings=bot_positions)
                    self.halt(f'AFTER 마감 시점 미청산 봇 잔고 감지: {bot_positions}')
            return
        sess=self.current_session()
        cutoff=getattr(self.a,'after_entry_cutoff','19:45:00') if sess=='AFTER' else self.a.new_entry_cutoff
        if t_str>=cutoff:
            for o in list(self.active_orders(side='BUY')): self.cancel(o)
        if sess=='AFTER' and not self.a.ALLOW_OVERNIGHT and t_str>=getattr(self.a,'after_force_flatten_start','19:55:00'):
            for sym,p in list(self.s['positions'].items()):
                if p.get('ownership')=='BOT' and not p.get('exit_requested'):
                    self.request_exit(sym,'AFTER 강제 청산')
        super().timer(prices)
        self.evaluate_existing()
        for sym,p in self.s['positions'].items(): self.position_eval(sym,p)

    def on_tick(self,t,completed_low=None):
        if hasattr(self, 'active_limit_lifecycles') and self.active_limit_lifecycles:
            for local_id, l_order in list(self.active_limit_lifecycles.items()):
                if l_order.symbol != t.symbol:
                    continue
                o = self.s['orders'].get(local_id)
                if not o or o['status'] not in ('ACCEPTED', 'PARTIAL'):
                    self.active_limit_lifecycles.pop(local_id, None)
                    continue
                if t.bid and t.ask:
                    l_order.on_quote(t.time, t.bid, t.ask)
                if l_order.state == 'CANCELLED_INVALIDATED_SUPPORT_LOST':
                    self.cancel(o)
                    self.active_limit_lifecycles.pop(local_id, None)
                    continue
                prev_filled = l_order.filled
                l_order.on_trade(t.time, t.price, t.volume)
                delta_fill = l_order.filled - prev_filled
                if delta_fill > 0:
                    self.cumulative_fill(o, l_order.filled, l_order.notional)
                if l_order.state in ('FILLED', 'CANCELLED_INVALIDATED_SUPPORT_LOST', 'CANCELLED_TIMEOUT', 'PARTIAL_TIMEOUT'):
                    if l_order.state in ('CANCELLED_TIMEOUT', 'PARTIAL_TIMEOUT') and o['status'] in ('ACCEPTED', 'PARTIAL'):
                        self.cancel(o)
                    self.active_limit_lifecycles.pop(local_id, None)
        self.observe_protection_tick(t)
        self.names.setdefault(t.symbol,None)
        previous=self.s['positions'].get(t.symbol,{}).get('stop')
        p=self.s['positions'].get(t.symbol)
        policy=p.get('signal',{}).get('invalidation_policy') if p else None
        if p and (p.get('pattern') == 'FAST' or p.get('signal', {}).get('pattern') == 'FAST'):
            if hasattr(self, 'fast_strategy') and self.fast_strategy is not None:
                self.fast_strategy.ingest_tick(t, received=self.clock())
                should_exit, exit_reason, exit_evidence = self.fast_strategy.evaluate_exit(p, t, self.clock())
                # FAST's micro-pivot line requires two independent trade
                # breaches. It is not a confirmed-bar structural stop: copying
                # it into p['stop'] also raises the quote-only emergency line
                # and lets one book update override a FAST HOLD.
                p['fast_exit_evidence']={'checked_at':self.clock().isoformat(),
                    'decision':'EXIT' if should_exit else 'HOLD' if exit_reason=='HOLD' else 'UNKNOWN','reason':exit_reason,
                    'micro_protection_stop':p.get('protection_stop'),
                    'structural_stop':p.get('stop'),'emergency_stop':p.get('emergency'),
                    'protection_authority':'FAST_TWO_INDEPENDENT_TRADE_BREACHES',
                    'evidence':dict(exit_evidence)}
                if should_exit:
                    self.request_exit(t.symbol, f'FAST_{exit_reason}')
        from automation.exit_plan import invalidation_lines
        if p and invalidation_lines(self.c,self.a,p)['close_based']:
            trade_fresh=t.regular and tick_freshness(t,self.clock(),self.c.stale_seconds)['fresh']
            if trade_fresh:
                if self.a.after_momentum_failure_enabled and t.price>p['high']:
                    p['after_entry_peak']={'price':t.price,'time':t.time.isoformat()}
                p['last_tick']=self.clock().isoformat();p['low']=min(p['low'],t.price)
                p['high'],p['stop']=self.protection(p,t.price,completed_low)
                self.position_price_updated(p,t)
            lines=invalidation_lines(self.c,self.a,p)
            soft=lines['active_structure']
            p['confirmed_soft_stop']=soft
            hard=lines['emergency']
            quote=p.get('last_quote',{})
            try:quote_fresh=market_freshness(quote['time'],self.clock(),self.c.stale_seconds,
                received_at=quote.get('received_at'),received_monotonic=quote.get('received_monotonic'))['fresh']
            except (KeyError,TypeError,ValueError):quote_fresh=False
            bid=quote.get('bid',0) if quote_fresh else 0
            p['invalidation_evidence']={'checked_at':self.clock().isoformat(),'structural_stop':soft,
                'emergency_stop':hard,'fresh_price':t.price if trade_fresh else None,'fresh_bid':bid,'trigger':'COMPLETE_CLOSE_OR_EMERGENCY'}
            if trade_fresh and t.price<=hard or bid>0 and bid<=hard:self.request_exit(t.symbol,'계좌 위험으로 수량 계산한 비상선 이탈')
            if p['stop']>previous or time.monotonic()-self.last_checkpoint>=1:
                self.save();self.last_checkpoint=time.monotonic()
            self.drive_exits({t.symbol:t.bid or t.price})
        else:super().on_tick(t,completed_low)
        p=self.s['positions'].get(t.symbol)
        if p and previous is not None and p.get('stop',0)>previous:
            self.decisions.emit('PROTECTION_UPDATE',t.symbol,'HOLD','PROTECTION_RAISED',
                {'protection':gate(True,previous=previous,new=p.get('stop',0),completed_low=completed_low)},position=p,price=t.price,force=True)
        if p:
            self.position_eval(t.symbol,p,t.price)
            capture=getattr(self,'trade_review',None)
            if capture is not None:
                try:capture.position(t.symbol,p,self.clock())
                except Exception:self.trade_review_errors=getattr(self,'trade_review_errors',0)+1

    def position_eval(self,sym,p,price=None):
        self.refresh_exit_state(sym)
        allowed=self.can_exit(sym,p) and self.exit_account_ready(sym,p['qty'])
        requested=p.get('exit_requested',False)
        gates={'exit_requested':gate(requested,reason=p.get('exit_reason'),time=self.clock().strftime('%H:%M:%S'),flatten=self.a.force_flatten_start,allow_overnight=self.a.ALLOW_OVERNIGHT),
               'exit_safe':gate(allowed,qty=p['qty'],qty_blocked=p.get('qty_blocked'),market=self.market,account_ok=self.account_ok),
               'pending':gate(not self.active_orders(sym),count=len(self.active_orders(sym))),
               'market_data_health':gate(not p.get('data_health',{}).get('emergency_exit',False),**p.get('data_health',{}))}
        self.decisions.emit('EXIT_EVAL' if requested else 'POSITION_EVAL',sym,
            'SELL' if requested and allowed else 'HOLD',p.get('exit_reason') if requested else 'PROTECTION_INTACT',gates,position=p,price=price)

    def cancel(self,o):
        is_shadow = not o.get('id') or str(o.get('id','')).startswith('SHADOW-') or (o.get('signal') or {}).get('shadow_only')
        if is_shadow:
            if o['status'] in ('FILLED', 'CANCELED', 'REJECTED'):
                return
            o['status']='CANCELED'; self.canceled_entry(o); self.save()
            self.event('CANCEL_SENT',local=o['local'],shadow=True)
            return
        if not self.c.live_mode and (o.get('signal') or {}).get('policy')=='TEST_CAPITAL_RELEASE':
            if not o.get('manual_cancel_required'):
                o['manual_cancel_required']=True; self.save()
                self.event('TEST_RELEASE_CANCEL_REQUIRES_MANUAL',symbol=o['symbol'],local=o['local'])
            return
        if self.is_supported_now(): return super().cancel(o)
        # 지원 시간 밖에서는 새 API 연결을 만들지 않고 취소 의도를 다음 정규장까지 보존한다.
        o['cancel_requested']=True; self.save()

    def transition_to_after(self):
        """정규장 종료 후 AFTER 진입 시 미체결 정규장 주문 취소 및 세션 상태 전이"""
        for o in list(self.active_orders()):
            if o.get('session')!='AFTER':
                self.cancel(o)
        for k,o in list(self.s['orders'].items()):
            if o.get('status')=='INTENT' and o.get('session')!='AFTER':
                o['status']='CANCELED'
        self.save()
        self.event('SESSION_TRANSITION',from_session='REGULAR',to_session='AFTER',time=self.clock().isoformat())

    def roll_day(self):
        day=self.clock().date().isoformat()
        if day==self.s['day']: return False
        if self.active_orders():
            self.halt('전일 미해결 주문: 실제 계좌 대조 필요'); return False
        self.s.update(day=day,entries={},daily_loss=0.,orders={},entry_lifecycles={},entry_failures={},entry_diagnostics={})
        self.s['automation']['consecutive_losses']=0
        self.market='UNKNOWN'; self.connected=False; self.account_ok=False; self.save()
        return True


AutomationEngine = AutonomousEngine

