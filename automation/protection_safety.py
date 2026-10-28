"""Protection authority during BALANCE-only outages; never BUY authority.

A fresh complete order/fill view and an in-process full-account anchor are
both required. A persisted quantity alone is not an execution permission.
Unknown orders, external activity, restart and disconnected transport fail
closed. Alerts are in the durable financial state, independent of capture.
"""
from copy import deepcopy
from decimal import Decimal,InvalidOperation
from datetime import datetime
import math,sys,time
from core.util import code
from core.market_time import market_freshness


def integer(value):
    if isinstance(value,bool):raise ValueError('BOOL_QUANTITY')
    try:d=Decimal(str(value).strip().replace(',',''))
    except InvalidOperation:raise ValueError('INVALID_QUANTITY') from None
    if not d.is_finite() or d<0 or d!=d.to_integral_value():raise ValueError('INVALID_QUANTITY')
    return int(d)


def aware(value):
    try:t=datetime.fromisoformat(value) if isinstance(value,str) else value
    except ValueError:return None
    return t if isinstance(t,datetime) and t.tzinfo else None


class ProtectionSafety:
    def protection_account_fresh(self):
        return (self.account_ok and 0<=time.monotonic()-self.last_account<=self.c.reconcile_seconds*3)

    def anchor_protection_account(self):
        # Never restore the volatile anchor from a previous process's ledger.
        if not self.protection_account_fresh():return
        if self.s.get('halt') or any(p.get('qty_blocked') or self.actual.get(s,{}).get('qty')!=p['qty']
            for s,p in self.s['positions'].items() if p.get('ownership')=='BOT'):
            self.invalidate_protection_orders('FULL_RECONCILIATION_CONFLICT',disconnect=True);return
        self._protection_anchor=dict(known_at=self.clock().isoformat(),
            session=self.current_session(),
            holdings=deepcopy(self.actual),fills={k:o.get('filled',0) for k,o in self.s['orders'].items()},
            reserved={s:sum(o['qty']-o.get('filled',0) for o in self.active_orders(s,'SELL')) for s in self.actual})
        self._protection_anchor_conflict=None
        self._protection_order_view=None
        state=self.s.get('protection_safety',{})
        if state.get('active_failures'):
            state['last_full_reconcile_at']=self.clock().isoformat()
            state['active_failures']={}
            self.save()

    def invalidate_protection_orders(self,reason,*,disconnect=False):
        self._protection_order_view=None
        self._protection_view_error=reason
        if disconnect:self._protection_anchor=None;self._protection_anchor_conflict=None

    def accept_protection_orders(self,view):
        self.invalidate_protection_orders('ORDER_STATUS_UNVERIFIED')
        try:
            now=self.clock();start=aware(view.get('query_started_at'));end=aware(view.get('known_at'))
            mono=view.get('known_monotonic')
            if (view.get('complete') is not True or not start or not end
                or not 0<=(now-end).total_seconds()<=self.c.stale_seconds
                or not 0<=(end-start).total_seconds()<=self.c.stale_seconds
                or type(mono) not in (int,float) or not math.isfinite(mono)
                or not 0<=time.monotonic()-mono<=self.c.stale_seconds):raise ValueError('ORDER_STATUS_NOT_FRESH_COMPLETE')
            orders=view['orders'];fills=view['fills']
            if not isinstance(orders,list) or not isinstance(fills,list):raise ValueError('ORDER_STATUS_SCHEMA')
            owned=[o for o in self.s['orders'].values() if o.get('id')]
            byid={str(o['id']):o for o in owned}
            if len(byid)!=len(owned):raise ValueError('LOCAL_BROKER_ID_COLLISION')
            opened={};totals={};seen=set()
            for r in orders:
                oid=str(r['ord_no']);sym=code(r['stk_cd']);qty=integer(r['ord_qty']);left=integer(r['oso_qty'])
                if not oid or oid in opened or left>qty or not qty:raise ValueError('OPEN_ORDER_INCONSISTENT')
                if oid in byid and byid[oid]['symbol']!=sym:raise ValueError('OPEN_IDENTITY_MISMATCH')
                if sym in self.s['positions'] and oid not in byid:raise ValueError('EXTERNAL_ORDER_ON_BOT_SYMBOL')
                opened[oid]=(sym,qty,left)
            for r in fills:
                if r.get('ord_stt')!='체결':continue
                signature=tuple(sorted((k,str(v)) for k,v in r.items()))
                if signature in seen:raise ValueError('DUPLICATE_FILL_ROWS')
                seen.add(signature);oid=str(r['ord_no']);sym=code(r['stk_cd'])
                qty=integer(r['cntr_qty']);price=Decimal(str(r['cntr_pric']).strip().replace(',','').lstrip('+'))
                if not qty or not price.is_finite() or price<=0:raise ValueError('INVALID_FILL')
                if sym in self.s['positions'] and oid not in byid:raise ValueError('EXTERNAL_FILL_ON_BOT_SYMBOL')
                if oid not in byid:continue
                o=byid[oid]
                if sym!=o['symbol'] or ('매수' if o['side']=='BUY' else '매도') not in str(r.get('io_tp_nm','')):
                    raise ValueError('FILL_IDENTITY_MISMATCH')
                if any(x in str(r.get('io_tp_nm','')) for x in ('취소','정정')):raise ValueError('FILL_KIND_UNKNOWN')
                old=totals.get(oid,(0,Decimal(0)));totals[oid]=(old[0]+qty,old[1]+qty*price)
            # Validate all rows before applying any cumulative fill. Absence
            # from open orders is never interpreted as a cancellation.
            for o in self.s['orders'].values():
                if o['symbol'] not in self.s['positions']:continue
                oid=str(o.get('id') or '')
                if o.get('status') in {'UNKNOWN','INTENT'}:raise ValueError('ORDER_RESULT_UNKNOWN')
                if o.get('last_fill_at') and start<=aware(o['last_fill_at']):raise ValueError('STATUS_PREDATES_LOCAL_FILL')
                total,amount=totals.get(oid,(0,Decimal(0)))
                if total<o.get('filled',0) or total>o['qty']:raise ValueError('FILL_TOTAL_INCONSISTENT')
                if amount<Decimal(str(o.get('amount',0))):raise ValueError('FILL_AMOUNT_INCONSISTENT')
                if oid in opened:
                    sym,qty,left=opened[oid]
                    if o['status'] in {'FILLED','CANCELED','REJECTED'} and left>0:raise ValueError('ORDER_STATUS_CONFLICT')
                    if sym!=o['symbol'] or qty!=o['qty'] or left!=qty-total:raise ValueError('OPEN_FILL_RACE')
                elif o['status'] not in {'FILLED','CANCELED','REJECTED'} and total!=o['qty']:
                    raise ValueError('ORDER_NOT_RESOLVED_BY_ABSENCE')
            for oid,(total,amount) in totals.items():
                o=byid[oid]
                if o['symbol'] in self.s['positions'] and total>o.get('filled',0):self.cumulative_fill(o,total,float(amount))
            self._protection_order_view=dict(known_at=end.isoformat(),known_monotonic=mono,query_started_at=start.isoformat(),opened=opened)
            self._protection_view_error=''
            return True
        except (KeyError,TypeError,ValueError,InvalidOperation) as error:
            self._protection_view_error=str(error)[:160]
            if str(error) in {'ORDER_STATUS_CONFLICT','EXTERNAL_ORDER_ON_BOT_SYMBOL','EXTERNAL_FILL_ON_BOT_SYMBOL',
                'FILL_TOTAL_INCONSISTENT','FILL_AMOUNT_INCONSISTENT','FILL_IDENTITY_MISMATCH','OPEN_IDENTITY_MISMATCH','LOCAL_BROKER_ID_COLLISION'}:
                # A later empty open-order response cannot erase a quantity
                # conflict; require a fresh holdings reconciliation as well.
                self._protection_anchor=None
                self._protection_anchor_conflict=str(error)
            return False

    def protection_certificate(self,symbol,qty,own_intent=None):
        p=self.s['positions'].get(symbol);fail=lambda why:dict(allowed=False,reason=why,quantity=0)
        if type(qty) is not int or qty<=0:return fail('INVALID_PROTECTIVE_QUANTITY')
        if not p or p.get('ownership')!='BOT' or not self.can_exit(symbol,p):return fail('BOT_OWNERSHIP_OR_POLICY_NOT_VERIFIED')
        if own_intent is not None and p.get('exit_requested') is not True:return fail('PERSISTED_PROTECTIVE_EXIT_REQUIRED')
        if not self.connected:return fail('TRANSPORT_DISCONNECTED')
        if p.get('qty_blocked') or symbol in self.s.get('excluded',[]):return fail('QUANTITY_RECONCILIATION_BLOCKED')
        if self.s.get('halt'):return fail('HALT_REQUIRES_RECONCILIATION')
        anchor=getattr(self,'_protection_anchor',None);view=getattr(self,'_protection_order_view',None)
        if not anchor:return fail(getattr(self,'_protection_anchor_conflict',None) or 'FRESH_PROCESS_ACCOUNT_ANCHOR_REQUIRED')
        anchor_at=aware(anchor['known_at'])
        if not anchor_at or anchor_at.date()!=self.clock().date() or anchor['session']!=self.current_session():
            return fail('SAME_SESSION_FULL_ACCOUNT_ANCHOR_REQUIRED')
        initial=anchor['holdings'].get(symbol,{})
        if initial and initial.get('available',0)+anchor['reserved'].get(symbol,0)<initial.get('qty',0):
            return fail('ANCHOR_HAS_UNEXPLAINED_UNSELLABLE_QUANTITY')
        if not view:return fail(getattr(self,'_protection_view_error','COMPLETE_ORDER_STATUS_REQUIRED'))
        at=aware(view['known_at'])
        if (not at or not 0<=(self.clock()-at).total_seconds()<=self.c.stale_seconds
                or not 0<=time.monotonic()-view['known_monotonic']<=self.c.stale_seconds):return fail('ORDER_STATUS_STALE')
        expected=anchor['holdings'].get(symbol,{}).get('qty',0);reserved=0
        for local,o in self.s['orders'].items():
            if o['symbol']!=symbol:continue
            if o is own_intent:continue
            if o['status'] in {'FILLED','CANCELED','REJECTED'} and view['opened'].get(str(o.get('id')),(None,0,0))[2]>0:
                return fail('ORDER_STATUS_CONFLICT')
            if o['status'] in {'UNKNOWN','INTENT'}:return fail('ORDER_RESULT_UNKNOWN')
            change=o.get('filled',0)-anchor['fills'].get(local,0)
            if change<0:return fail('FILL_LEDGER_ROLLBACK')
            if change and not o.get('id'):return fail('FILL_BROKER_ID_UNKNOWN')
            expected+=change if o['side']=='BUY' else -change
            if o['status'] not in {'FILLED','CANCELED','REJECTED'}:
                if str(o.get('id')) not in view['opened']:return fail('ORDER_VIEW_CHANGED_SINCE_QUERY')
                if o['side']=='BUY':return fail('BUY_STILL_PENDING')
                reserved+=o['qty']-o.get('filled',0)
        if type(expected) is not int or expected!=p['qty'] or expected<=0:return fail('ANCHOR_FILL_POSITION_MISMATCH')
        if qty>expected-reserved:return fail('QUANTITY_RESERVED_OR_UNVERIFIED')
        # Preserve one outstanding SELL per symbol, including known partials.
        if reserved:return fail('EXISTING_SELL_PENDING_NO_DUPLICATE')
        quote=p.get('last_quote') or {};time_at=aware(quote.get('time'))
        received=aware(quote.get('received_at'))
        if not received or not 0<=(self.clock()-received).total_seconds()<=self.c.stale_seconds or not time_at or not market_freshness(time_at,self.clock(),self.c.stale_seconds,
            received_at=quote.get('received_at'),received_monotonic=quote.get('received_monotonic'))['fresh']:
            return fail('PROTECTION_QUOTE_NOT_FRESH')
        if not all(type(quote.get(k)) in (int,float) and math.isfinite(quote[k]) and quote[k]>0 for k in ('bid','bid_qty')):
            return fail('PROTECTION_EXECUTABLE_BID_UNKNOWN')
        return dict(allowed=True,reason='VERIFIED_FILL_LEDGER_AND_FRESH_ORDER_STATUS',quantity=expected,
                    anchor_at=anchor['known_at'],orders_known_at=view['known_at'])

    def record_protection_failure(self,symbol,reason):
        state=self.s.setdefault('protection_safety',{});failures=state.setdefault('active_failures',{})
        p=self.s['positions'].get(symbol,{})
        reserved=sum(o['qty']-o.get('filled',0) for o in self.active_orders(symbol,'SELL'))
        row=dict(reason=reason,quantity=p.get('qty'),reserved_sell=reserved,unreserved=max(0,p.get('qty',0)-reserved),
            urgent=True,recovery='COMPLETE_ACCOUNT_AND_ORDER_RECONCILIATION_REQUIRED')
        old=failures.get(symbol)
        if old and all(old.get(k)==v for k,v in row.items()):return
        row['at']=self.clock().isoformat();failures[symbol]=row
        self.save()  # Financial protection latch, not best-effort diagnostics.
        print('URGENT PROTECTION_UNAVAILABLE '+symbol+' '+reason,file=sys.stderr,flush=True)
        try:self.event('PROTECTION_UNAVAILABLE',symbol=symbol,reason=reason,urgent=True)
        except Exception:pass  # A full/failed diagnostic queue cannot erase the latch.

    def audit_protection_health(self):
        failures=self.s.get('protection_safety',{}).get('active_failures',{})
        for symbol in list(failures):
            if symbol not in self.s['positions']:del failures[symbol];self.save()
        if not self.c.live_mode or self.protection_account_fresh():return
        for symbol,p in self.s['positions'].items():
            if p.get('ownership')!='BOT':continue
            pending=self.active_orders(symbol)
            if (self.connected and pending and all(o['side']=='SELL' and o.get('id') and o['status'] in {'ACCEPTED','PARTIAL'}
                and 0<=(self.clock()-aware(o['created'])).total_seconds()<=self.c.order_timeout_seconds for o in pending)
                and sum(o['qty']-o.get('filled',0) for o in pending)>=p['qty']):
                continue  # a just-acknowledged full exit is pending, not a new SELL opportunity
            proof=self.protection_certificate(symbol,p['qty'])
            if not proof['allowed']:self.record_protection_failure(symbol,proof['reason'])
            else:self.clear_protection_failure(symbol)

    def clear_protection_failure(self,symbol):
        state=self.s.get('protection_safety',{});failures=state.get('active_failures',{})
        if symbol in failures:
            state['last_resolved']={**failures.pop(symbol),'symbol':symbol,'resolved_at':self.clock().isoformat(),
                'basis':'PROTECTION_ONLY_ACCOUNT_BUY_AUTHORITY_UNCHANGED'}
            self.save()

    def exit_account_ready(self,symbol,qty):
        if not self.c.live_mode or self.protection_account_fresh():return True
        proof=self.protection_certificate(symbol,qty)
        if not proof['allowed']:self.record_protection_failure(symbol,proof['reason'])
        else:self.clear_protection_failure(symbol)
        return proof['allowed']

    def observe_protection_tick(self,t):
        from core.market_time import tick_freshness,book_freshness
        p=self.s['positions'].get(t.symbol)
        if not p or not t.regular or not t.depth_available or t.bid_qty<=0:return
        if not tick_freshness(t,self.clock(),self.c.stale_seconds)['fresh'] or not book_freshness(t,self.clock(),self.c.stale_seconds)['fresh']:return
        prior=aware(p.get('last_quote',{}).get('time'))
        if prior and prior>t.book_time:return
        p['last_quote']=dict(time=t.book_time.isoformat(),received_at=t.book_received_at,
            received_monotonic=t.book_received_monotonic,
            bid=t.bid,bid_qty=t.bid_qty,ask=t.ask,ask_qty=t.ask_qty,source='PAIRED_RECEIVED_TRADE_BOOK')
