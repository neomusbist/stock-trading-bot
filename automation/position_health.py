"""Trade silence is not a socket failure. Main writer only; no broker mutations here."""
import time,math
from datetime import datetime
from core.util import floor_tick,tick_size


class PositionDataHealth:
    def __init__(self,engine,feed,account,subscriptions):
        self.engine=engine;self.feed=feed;self.account=account;self.subscriptions=subscriptions
        if not isinstance(getattr(account,'position_symbols',None),(set,frozenset)):
            account.position_symbols=frozenset()
        self.waiting={};self.logged={};self.quotes={}
        engine.position_health=self
        engine.position_data_stale=self.should_exit

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
        for mapping in (self.waiting,self.quotes,self.logged):
            for symbol in set(mapping)-held:mapping.pop(symbol,None)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)&held)

    def accept_book(self,symbol,book,current,source='0D'):
        e=self.engine;p=e.s['positions'].get(symbol)
        if not p or p.get('ownership','BOT')!='BOT':return False
        age=self.age(book.get('time'),current)
        bid=book.get('best_bid',book.get('bid',0));ask=book.get('best_ask')
        if age is None or age>e.c.position_stale_seconds or not isinstance(bid,(int,float)) or not math.isfinite(bid) or bid<=0:return False
        if source=='0D' and (not isinstance(ask,(int,float)) or not math.isfinite(ask) or ask<bid):return False
        # The existing REST book route is KRX only. Never substitute it for AFTER.
        if source=='ka10004' and (e.current_session()!='REGULAR' or not book.get('regular') or book.get('status',{}).get('pass') is False):return False
        prior=self.quotes.get(symbol)
        if prior and self.age(prior['time'],current) is not None and self.age(prior['time'],current)<age:return False
        self.quotes[symbol]={'time':book['time'],'bid':bid,'ask':ask,'source':source}
        p['last_quote']=dict(self.quotes[symbol]);e.prices[symbol]=bid
        self.waiting.pop(symbol,None)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)-{symbol})
        # A quote is not a trade/bar: no MFE/trailing/warmup changes, and no fabricated tick.
        emergency=min(p['stop']-tick_size(p['stop']),floor_tick(p['stop']*(1-e.c.structural_buffer)))
        if bid<=emergency:
            e.request_exit(symbol,'호가 급락/유동성 붕괴')
        return True

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
        ws_age=self.age(self.feed.last_message_at,current)
        account_age=time.monotonic()-e.last_account
        subscribed=symbol in self.subscriptions.symbols and symbol not in self.subscriptions.waiting
        ws_fresh=e.connected and ws_age is not None and ws_age<=max(e.c.position_stale_seconds*2,e.c.reconcile_seconds*3)
        account_fresh=e.account_ok and account_age<=e.c.reconcile_seconds*3
        evidence={'checked_at':current.isoformat(),'last_tick_age':self.age(position.get('last_tick'),current),'quote_age':quote_age,
                  'quote_source':quote.get('source'),'ws_heartbeat_age':ws_age,'connected':e.connected,
                  'account_age':account_age,'account_ok':e.account_ok,'subscription_state':'ACTIVE' if subscribed else 'PENDING_OR_ABSENT',
                  'event_loop_lag':getattr(e,'event_loop_lag',None)}
        # A real disconnect must not be excused by a previously cached book.
        if not e.connected:
            reason='GLOBAL_DISCONNECTED';exit_now=True
        elif quote_age is not None and quote_age<=e.c.position_stale_seconds and ws_fresh and subscribed:
            reason='TRADE_QUIET_QUOTE_FRESH';exit_now=False;self.waiting.pop(symbol,None)
        else:
            first=self.waiting.setdefault(symbol,current)
            grace=max(e.c.position_stale_seconds,e.c.reconcile_seconds*3)
            elapsed=(current-first).total_seconds()
            # Bound the blind period by one existing watchdog/reconciliation window.
            # Failed price verification cannot hold a position indefinitely on PING/account alone.
            exit_now=elapsed>=grace
            reason='POSITION_DATA_UNVERIFIED_TIMEOUT' if exit_now else 'POSITION_QUOTE_RECHECK_PENDING'
            evidence.update(recheck_seconds=elapsed,recheck_deadline_seconds=grace)
            if e.current_session()=='REGULAR':
                requested=symbol not in self.account.position_symbols
                self.account.position_symbols=frozenset(set(self.account.position_symbols)|{symbol})
                if requested:self.account.request.set()
        evidence.update(reason_code=reason,ws_fresh=ws_fresh,account_fresh=account_fresh,emergency_exit=exit_now)
        position['data_health']=evidence
        key=(reason,exit_now)
        prior=self.logged.get(symbol)
        if prior is None or prior[0]!=key or (current-prior[1]).total_seconds()>=5:
            e.event('POSITION_DATA_HEALTH',symbol=symbol,**evidence);self.logged[symbol]=(key,current)
        self.account.position_symbols=frozenset(set(self.account.position_symbols)&set(e.s['positions']))
        return exit_now
