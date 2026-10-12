"""Trade silence is not a socket failure. Main writer only; no broker mutations here."""
import time,math
from datetime import datetime
from core.util import floor_tick,tick_size


class PositionDataHealth:
    def __init__(self,engine,feed,account,subscriptions):
        self.engine=engine;self.feed=feed;self.account=account;self.subscriptions=subscriptions
        if not isinstance(getattr(account,'position_symbols',None),(set,frozenset)):
            account.position_symbols=frozenset()
        if not isinstance(getattr(account,'position_context',None),dict):account.position_context={}
        self.probes={}
        self.waiting={};self.logged={};self.quotes={}
        engine.position_health=self
        engine.position_data_stale=self.should_exit
        if callable(getattr(feed,'entry_snapshot',None)):engine.entry_quote_provider=self.entry_snapshot
        if callable(getattr(feed,'context_snapshot',None)):engine.market_context_provider=self.market_context

    def market_context(self,stamp):
        # Diagnostic adapters must never interrupt account/order processing.
        try:
            result=self.feed.context_snapshot(stamp)
            if isinstance(result,dict):return result
        except (AttributeError,TypeError,ValueError):pass
        return {'status':'UNKNOWN','reason':'INDEX_CONTEXT_UNAVAILABLE'}

    def entry_snapshot(self,symbol):
        return self.feed.entry_snapshot(symbol)

    @staticmethod
    def age(stamp,current):
        try:
            parsed=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(stamp)
            if not parsed.tzinfo or parsed.date()!=current.date():return None
            delta=(current-parsed).total_seconds()
            return delta if delta>=0 else None
        except (ValueError,TypeError):return None

    def prune(self):
        held=set(self.engine.s['positions'])
        for mapping in (self.waiting,self.quotes,self.logged,self.probes):
            for symbol in set(mapping)-held:mapping.pop(symbol,None)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)&held)
        self.account.position_context={s:v for s,v in self.account.position_context.items() if s in held}

    def probe_results(self,snapshot,current):
        for symbol,book in snapshot.get('books',{}).items():
            if symbol in self.waiting:
                self.probes[symbol]={'result_at':current.isoformat(),'result':'QUOTE_RECEIVED',
                    'quote_time':book.get('time'),'venue':book.get('venue'),'source':book.get('source')}
        for symbol,error in snapshot.get('book_errors',{}).items():
            if symbol in self.waiting:
                self.probes[symbol]={'result_at':current.isoformat(),'result':'PROBE_FAILED',
                    'error':error.get('error'),'api':error.get('api')}

    def accept_book(self,symbol,book,current,source='0D'):
        e=self.engine;p=e.s['positions'].get(symbol)
        if not p or p.get('ownership','BOT')!='BOT':return False
        age=self.age(book.get('time'),current)
        bid=book.get('best_bid',book.get('bid',0));ask=book.get('best_ask')
        if age is None or not isinstance(bid,(int,float)) or not math.isfinite(bid) or bid<=0:return False
        if source=='0D' and (not isinstance(ask,(int,float)) or not math.isfinite(ask) or ask<bid):return False
        if source=='ka10004':
            if e.current_session()=='AFTER':
                venue=(p.get('signal') or {}).get('venue') or p.get('venue') or e.after_default_venue
                if (book.get('session')!='AFTER' or book.get('venue')!=venue or venue not in {'NXT','SOR'}
                        or not '16:00:00'<=book.get('time','')[11:19]<'20:00:00'
                        or not isinstance(ask,(int,float)) or not math.isfinite(ask) or ask<bid):return False
            elif e.current_session()!='REGULAR' or not book.get('regular') or book.get('status',{}).get('pass') is False:return False
        confirmed=False
        if age>e.c.position_stale_seconds:
            confirmed=source=='ka10004' and self.confirm_unchanged_book(symbol,book,current)
            if not confirmed:return False
        prior=self.quotes.get(symbol)
        if prior and self.age(prior['time'],current) is not None and self.age(prior['time'],current)<age:return False
        self.quotes[symbol]={'time':book['time'],'bid':bid,'ask':ask,'source':source}
        if confirmed:self.quotes[symbol].update(snapshot_confirmed_at=book['received_at'],confirmation='MATCHED_STANDING_BOOK')
        p['last_quote']=dict(self.quotes[symbol]);e.prices[symbol]=bid
        self.waiting.pop(symbol,None)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)-{symbol})
        # A quote is not a trade/bar: no MFE/trailing/warmup changes, and no fabricated tick.
        emergency=min(p['stop']-tick_size(p['stop']),floor_tick(p['stop']*(1-e.c.structural_buffer)))
        if bid<=emergency:
            e.request_exit(symbol,'호가 급락/유동성 붕괴')
        return True

    def confirm_unchanged_book(self,symbol,book,current):
        """Confirm a standing book via a NEW read, not by relabeling its quote timestamp.

        This is position management only. Entry freshness still uses the original
        market timestamp and cannot consume this separate confirmation.
        """
        e=self.engine
        if not getattr(e.a,'stable_position_book_confirmation',False):return False
        received_age=self.age(book.get('received_at'),current)
        ws_age=self.age(self.feed.last_message_at,current)
        if (received_age is None or received_age>e.c.position_stale_seconds or not e.connected or not e.account_ok
                or ws_age is None or ws_age>max(e.c.position_stale_seconds*2,e.c.reconcile_seconds*3)
                or time.monotonic()-e.last_account>e.c.reconcile_seconds*3
                or symbol not in self.subscriptions.symbols or symbol in self.subscriptions.waiting):return False
        if book.get('source')!='ka10004' or book.get('requested_symbol')!=symbol+{'KRX':'','NXT':'_NX','SOR':'_AL'}.get(book.get('venue'),'INVALID'):return False
        with self.feed.subscription_lock:prior=dict(self.feed.orderbooks.get(symbol,{}) or {})
        if prior.get('time')!=book.get('time'):return False
        for key in ('best_bid','best_ask','bid_qty','ask_qty'):
            value=book.get(key)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or value!=prior.get(key):return False
        return bool(prior.get('depth_available'))

    def trade_seen(self,symbol):
        self.waiting.pop(symbol,None)

    def should_exit(self,symbol,position,current):
        e=self.engine
        # Latest receiver book avoids queued-message delays; copy under its brief lock.
        with self.feed.subscription_lock:
            book=dict(self.feed.orderbooks.get(symbol,{}) or {})
        if book:self.accept_book(symbol,book,current)
        quote=self.quotes.get(symbol,{})
        quote_age=self.age(quote.get('time'),current)
        confirmation_age=self.age(quote.get('snapshot_confirmed_at'),current)
        ws_age=self.age(self.feed.last_message_at,current)
        account_age=time.monotonic()-e.last_account
        subscribed=symbol in self.subscriptions.symbols and symbol not in self.subscriptions.waiting
        ws_fresh=e.connected and ws_age is not None and ws_age<=max(e.c.position_stale_seconds*2,e.c.reconcile_seconds*3)
        account_fresh=e.account_ok and account_age<=e.c.reconcile_seconds*3
        evidence={'checked_at':current.isoformat(),'last_tick_age':self.age(position.get('last_tick'),current),'quote_age':quote_age,
                  'quote_source':quote.get('source'),'ws_heartbeat_age':ws_age,'connected':e.connected,
                  'account_age':account_age,'account_ok':e.account_ok,'subscription_state':'ACTIVE' if subscribed else 'PENDING_OR_ABSENT',
                  'event_loop_lag':getattr(e,'event_loop_lag',None),
                  'snapshot_confirmation_age':confirmation_age,'snapshot_confirmation':quote.get('confirmation')}
        # A real disconnect must not be excused by a previously cached book.
        if not e.connected:
            reason='GLOBAL_DISCONNECTED';exit_now=True
        elif quote_age is not None and quote_age<=e.c.position_stale_seconds and ws_fresh and subscribed:
            reason='TRADE_QUIET_QUOTE_FRESH';exit_now=False;self.waiting.pop(symbol,None)
        elif (quote.get('confirmation')=='MATCHED_STANDING_BOOK' and confirmation_age is not None
                and confirmation_age<=e.c.position_stale_seconds and ws_fresh and account_fresh and subscribed):
            reason='TRADE_QUIET_REST_BOOK_CONFIRMED';exit_now=False;self.waiting.pop(symbol,None)
        else:
            first=self.waiting.setdefault(symbol,current)
            grace=max(e.c.position_stale_seconds,e.c.reconcile_seconds*3)
            elapsed=(current-first).total_seconds()
            # Bound the blind period by one existing watchdog/reconciliation window.
            # Failed price verification cannot hold a position indefinitely on PING/account alone.
            exit_now=elapsed>=grace
            reason='POSITION_DATA_UNVERIFIED_TIMEOUT' if exit_now else 'POSITION_QUOTE_RECHECK_PENDING'
            evidence.update(recheck_seconds=elapsed,recheck_deadline_seconds=grace)
            requested=symbol not in self.account.position_symbols
            self.account.position_symbols=frozenset(set(self.account.position_symbols)|{symbol})
            if e.current_session()=='AFTER':
                venue=(position.get('signal') or {}).get('venue') or position.get('venue') or e.after_default_venue
                self.account.position_context={**self.account.position_context,symbol:{'session':'AFTER','venue':venue}}
            if requested:
                self.probes[symbol]={'requested_at':current.isoformat(),'result':'REQUESTED',
                    'session':e.current_session(),'venue':self.account.position_context.get(symbol,{}).get('venue','KRX')}
                self.account.request.set()
        evidence['probe']=dict(self.probes.get(symbol,{}))
        evidence.update(reason_code=reason,ws_fresh=ws_fresh,account_fresh=account_fresh,emergency_exit=exit_now)
        position['data_health']=evidence
        key=(reason,exit_now)
        prior=self.logged.get(symbol)
        if prior is None or prior[0]!=key or (current-prior[1]).total_seconds()>=5:
            e.event('POSITION_DATA_HEALTH',symbol=symbol,**evidence);self.logged[symbol]=(key,current)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)&set(e.s['positions']))
        return exit_now
