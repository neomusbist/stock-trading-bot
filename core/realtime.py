"""수신 전용 스레드: PING 즉시 응답, 재연결마다 로그인/재구독. 주문은 주 스레드만 처리."""
import json
import math
import queue
import threading
import time
from collections import Counter,deque
from datetime import datetime
from dataclasses import replace
from core.settings import ROOT, KST, now
from core.strategy import Tick
from core.util import number, code
from core.market_time import market_freshness,tick_freshness


def current_book_tick(tick, book, checked_at, stale_seconds):
    """Use a fresh 0D execution quote without altering its accepted trade.

    0B and 0D are independent streams: a newer BBO need not equal the BBO
    attached to the last print. Trade time, receipt, price and volume remain
    immutable. Callers must also verify the receiver still holds this trade.
    """
    if not isinstance(book,dict) or book.get('depth_available') is not True:return tick
    bid,ask=book.get('best_bid'),book.get('best_ask')
    if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (bid,ask))
            or not 0<bid<=ask):return tick
    if any(type(book.get(k)) is not int or book[k]<=0 for k in ('bid_qty','ask_qty')):return tick
    try:
        received=datetime.fromisoformat(book['received_at']);book_at=datetime.fromisoformat(book['time'])
        if received.tzinfo is None or received>checked_at:return tick
        # A different BBO may supersede the print only when it arrived after
        # that print. A still-fresh older 0D is not evidence for the new price.
        if (bid,ask)!=(tick.bid,tick.ask):
            trade_received=datetime.fromisoformat(tick.received_at) if tick.received_at else tick.time
            if received<trade_received:return tick
            trade_mono=getattr(tick,'received_monotonic',None)
            book_mono=book.get('received_monotonic')
            if (type(trade_mono) in (int,float) and type(book_mono) in (int,float)
                    and book_mono<trade_mono):return tick
        if not tick_freshness(tick,checked_at,stale_seconds)['fresh']:return tick
        if not market_freshness(book_at,checked_at,stale_seconds,received_at=received,
                received_monotonic=book.get('received_monotonic'))['fresh']:return tick
        old_received=datetime.fromisoformat(tick.book_received_at) if tick.book_received_at else None
        if old_received is not None and received<old_received:return tick
        if tick.book_time is not None and book_at<tick.book_time:return tick
    except (KeyError,TypeError,ValueError):return tick
    return replace(tick,bid=bid,ask=ask,bid_qty=book['bid_qty'],ask_qty=book['ask_qty'],
        depth_available=True,book_time=book_at,depth_source='0D',
        book_received_at=received.isoformat(),book_received_monotonic=book.get('received_monotonic'))

def stream_symbol(value):
    """Exchange suffixes are accepted only at the read-only stream boundary."""
    if isinstance(value,str) and value.endswith(('_NX','_AL')):
        return code(value[:-3])
    return code(value)

def parse_book(item,received,received_monotonic=None):
    """0D: best prices 41/51, best quantities 61/71, totals 121/125.
    0C provides prices 27/28 only; absent depth is not zero liquidity.
    """
    v=item.get('values',{})
    available=all(v.get(k) not in (None,'') for k in ('41','51','61','71'))
    quote_time=datetime.combine(received.date(),datetime.strptime(v['21'],'%H%M%S').time(),tzinfo=KST) if v.get('21') else received
    result={'best_ask':number(v.get('41') or 0),'best_bid':number(v.get('51') or 0),
            'ask_qty':int(number(v.get('61') or 0)),'bid_qty':int(number(v.get('71') or 0)),
            'total_ask_qty':int(number(v.get('121') or 0)),'total_bid_qty':int(number(v.get('125') or 0)),
            'time':quote_time.isoformat(),'received_at':received.isoformat(),'source':'0D',
            'received_monotonic':received_monotonic,
            'raw_fields':{k:v.get(k) for k in ('21','41','51','61','71','121','125')}}
    result['depth_available']=bool(available and 0<result['best_bid']<=result['best_ask'])
    return result

def parse_tick(item,received,book=None,book_stale_seconds=5,received_monotonic=None):
    v=item['values']; stamp=datetime.strptime(v['20'],'%H%M%S').time()
    dt=datetime.combine(received.date(),stamp,tzinfo=KST)
    # WATCH/main path and SHADOW/fast path must read the same attached book.
    book=item.get('_book',{}) if book is None else book
    book=book or {}
    ask=number(v['27']);bid=number(v['28'])
    depth_time=None;depth_available=False;depth_source='DATA_UNAVAILABLE'
    book_received=None;book_monotonic=None
    ask_qty=bid_qty=0
    if all(v.get(k) not in (None,'') for k in ('61','71')):
        ask_qty=int(number(v['61']));bid_qty=int(number(v['71']))
        depth_available=True;depth_time=received;depth_source='0B_FIELDS'
        book_received=received;book_monotonic=received_monotonic
    elif book:
        try:depth_time=datetime.fromisoformat(book['time'])
        except (KeyError,TypeError,ValueError):depth_time=None
        try:book_received=datetime.fromisoformat(book.get('received_at',book['time']))
        except (KeyError,TypeError,ValueError):book_received=None
        book_monotonic=book.get('received_monotonic')
        fresh=market_freshness(depth_time,received,book_stale_seconds,
            received_at=book_received,received_monotonic=book.get('received_monotonic'),
            monotonic_now=received_monotonic)['fresh']
        matched=book.get('best_ask')==ask and book.get('best_bid')==bid
        depth_available=bool(fresh and matched and book.get('depth_available'))
        depth_source='0D' if depth_available else 'BOOK_STALE' if not fresh else 'BOOK_PRICE_MISMATCH' if not matched else 'DATA_UNAVAILABLE'
        if depth_available:
            ask_qty=int(book['ask_qty']);bid_qty=int(book['bid_qty'])
    venue=v.get('9081') or 'KRX'
    return Tick(stream_symbol(item['item']),dt,number(v['10']),int(number(v['15'])),int(number(v['13'])),
        ask,bid,v.get('290')=='2',ask_qty=ask_qty,bid_qty=bid_qty,venue=venue,
        depth_available=depth_available,book_time=depth_time,depth_source=depth_source,
        received_at=received.isoformat(),received_monotonic=received_monotonic,
        book_received_at=book_received.isoformat() if book_received is not None else None,
        book_received_monotonic=book_monotonic)

class FeedMessageQueue(queue.Queue):
    """FIFO with receiver-built priority counts; queries never scan tick history."""
    def _init(self,maxsize):
        super()._init(maxsize)
        self._routing=deque();self._barriers=0;self._symbols=Counter()

    def _put(self,item):
        message=item[1]
        data=message.get('data',[])
        barrier=(message.get('trnm') in {'REG','ERROR','DISCONNECTED'} or
                 any(row.get('type') in {'00','0s'} for row in data))
        symbols=frozenset(row.get('item') for row in data if row.get('item'))
        super()._put(item)
        self._routing.append((barrier,symbols));self._barriers+=int(barrier)
        self._symbols.update(symbols)

    def _get(self):
        item=super()._get()
        barrier,symbols=self._routing.popleft();self._barriers-=int(barrier)
        for symbol in symbols:
            self._symbols[symbol]-=1
            if not self._symbols[symbol]:del self._symbols[symbol]
        return item

    def protected_pending(self,critical):
        with self.mutex:
            return self._barriers>0 or any(self._symbols.get(symbol,0)>0 for symbol in critical)


class Feed:
    def __init__(self,rest,symbols,index_subscription_enabled=False):
        self.rest=rest; self.symbols=list(dict.fromkeys(symbols)); self.q=FeedMessageQueue(maxsize=100000)
        self.stop=threading.Event(); self.overflow=threading.Event(); self.socket=None
        self.logged_in=False; self.transport_failed=False; self.subscription_lock=threading.RLock()
        self.observation_symbols=frozenset(); self.priority_symbols=None; self.observation_sink=None
        self.trade_review=None  # bounded evidence sink, independent of SHADOW routing
        self.observation_q=queue.Queue(maxsize=8192);self.observation_drops=0
        self.observation_gaps={}
        self.orderbooks={}
        self.latest_ticks={}
        from automation.market_context import IndexContext
        self.index_context=IndexContext()
        self.last_message_at=None
        # Production REG ACKs omit grp_no. Keep the optional index subscription
        # off until its ACK can be distinguished from the trading subscription.
        self.index_subscription_enabled=index_subscription_enabled
        self.wire_symbols=self.subscription_items(self.symbols)
        self.thread=threading.Thread(target=self.run,name='Kiwoom-feed',daemon=True)

    def subscription_items(self,symbols):
        stamp=now().astimezone(KST).strftime('%H:%M:%S')
        cfg=getattr(self.rest,'cfg',None)
        after=getattr(cfg,'after_enabled',False) is True and '15:40:00'<=stamp<'20:00:00'
        venue=getattr(cfg,'after_default_venue','SOR') if after else 'KRX'
        suffix={'NXT':'_NX','SOR':'_AL'}.get(venue,'')
        return [code(symbol)+suffix for symbol in symbols]

    def emit(self,event):
        received=now()
        received_monotonic=time.monotonic()
        if event.get('trnm')=='REAL':event={**event,'_received_monotonic':received_monotonic}
        if event.get('trnm') not in {'ERROR','DISCONNECTED'}:self.last_message_at=received
        with self.subscription_lock:
            capture=getattr(self,'trade_review',None)
            if capture and event.get('trnm') in {'REG','ERROR','DISCONNECTED'}:
                capture.stream(event['trnm'],received)
            if event.get('trnm')=='REG' and str(event.get('grp_no'))=='2':
                if event.get('return_code') not in (None,0,'0'):self.index_context.clear()
                return  # Diagnostic ACK is never an order/market subscription ACK.
            if event.get('trnm') in {'ERROR','DISCONNECTED'}:
                self.transport_failed=True;self.logged_in=False
                self.orderbooks.clear();self.latest_ticks.clear()
                self.index_context.clear()
            if event.get('trnm')=='REAL':
                accepted_items=[]
                for item in event.get('data',[]):
                    itype=item.get('type'); raw_item=item.get('item')
                    if itype=='0J':
                        v=item.get('values',{})
                        try:
                            stamp=datetime.combine(received.date(),datetime.strptime(v['20'],'%H%M%S').time(),tzinfo=KST)
                            self.index_context.ingest(raw_item,stamp,v.get('10'),received,v.get('12'))
                        except (ValueError,KeyError):pass
                        continue
                    sym=stream_symbol(raw_item) if raw_item else None
                    if sym and itype in {'0B','0D'}:
                        if sym in self.symbols and raw_item not in self.wire_symbols:
                            continue  # Queued frames from the prior exchange cannot refresh the new stream.
                        item['_subscription_item']=raw_item
                        item['item']=sym
                    accepted_items.append(item)
                    if sym and itype=='0D':
                        self.orderbooks[sym]=parse_book(item,received,received_monotonic)
                        item['_book']=self.orderbooks[sym]
                        if capture:capture.book(sym,item['_book'],received)
                    elif sym and itype=='0B':
                        item['_book']=self.orderbooks.get(sym)
                        try:
                            tick=parse_tick(item,received,book=item['_book'],book_stale_seconds=getattr(self.rest.cfg,'stale_seconds',5),received_monotonic=received_monotonic)
                            item['_observation_tick']=(received,tick)
                            if capture:capture.tick(tick,received)
                            prior=self.latest_ticks.get(sym)
                            if sym in self.symbols and (prior is None or tick.time>=prior.time):self.latest_ticks[sym]=tick
                        except (ValueError,KeyError):pass
                event={**event,'data':accepted_items}
            if self.priority_symbols is not None and event.get('trnm')=='REAL':
                priority=[]
                for item in event.get('data',[]):
                    if item.get('type')=='0J':continue  # Already consumed; do not load the order/position queue.
                    if item.get('type')=='0B' and item.get('item') in self.observation_symbols:
                        try:
                            parsed=item.get('_observation_tick')
                            tick=parsed[1] if parsed else parse_tick(item,received,book=item.get('_book') or self.orderbooks.get(code(item.get('item',''))),book_stale_seconds=getattr(self.rest.cfg,'stale_seconds',5),received_monotonic=received_monotonic)
                            # Frozen value for the same received frame, shared by observation and main.
                            item['_observation_tick']=(received,tick)
                            if self.observation_sink:self.observation_sink(tick,received)
                            if item.get('item') not in self.priority_symbols:self.observation_q.put_nowait((received,tick))
                        except (queue.Full,ValueError,KeyError):
                            self.observation_drops+=1
                            self.observation_gaps[item.get('item')]=received
                    # Diagnostic books are already stored above for their paired ticks.
                    # Do not put all SHADOW depth traffic on the order/position queue.
                    # Order events, market state, and every protected/WATCH book stay FIFO.
                    if item.get('type') not in {'0B','0D'} or item.get('item') in self.priority_symbols:priority.append(item)
                if not priority:return
                event={**event,'data':priority}
            elif event.get('trnm')=='REAL':
                data=[item for item in event.get('data',[]) if item.get('type')!='0J']
                if not data:return
                event={**event,'data':data}
        try: self.q.put_nowait((received,event))
        except queue.Full: self.overflow.set()

    def configure_observation(self,priority,symbols,sink):
        with self.subscription_lock:
            self.priority_symbols=frozenset(priority);self.observation_symbols=frozenset(symbols);self.observation_sink=sink

    def update_symbols(self,symbols):
        with self.subscription_lock:
            updated=list(dict.fromkeys(symbols))
            wire=self.subscription_items(updated)
            if updated==self.symbols and wire==self.wire_symbols: return False
            venue_changed=self.subscription_items(self.symbols)!=self.wire_symbols
            capture=getattr(self,'trade_review',None)
            if capture:
                capture.subscription(set(updated)-set(self.symbols),set(self.symbols)-set(updated),now())
            self.symbols=updated
            self.wire_symbols=wire
            if venue_changed:self.latest_ticks.clear();self.orderbooks.clear()
            for mapping in (self.latest_ticks,self.orderbooks):
                for symbol in set(mapping)-set(updated):mapping.pop(symbol,None)
            if self.socket and self.logged_in:
                # refresh=0 그룹을 완전히 교체하며 주문/장상태 구독은 항상 다시 포함한다.
                try:
                    from websocket import WebSocketException
                except ImportError:
                    WebSocketException = OSError
                try:
                    self.socket.send(json.dumps({'trnm':'REG','grp_no':'1','refresh':'0','data':[
                        {'item':self.wire_symbols,'type':['0B','0D']},{'item':[''],'type':['00','0s']}]}))
                except (WebSocketException,ConnectionError,TimeoutError) as error:
                    # A closed transport must not terminate the market worker.
                    # Keep the desired universe for the receiver's normal reconnect.
                    self.emit({'trnm':'DISCONNECTED','reason':type(error).__name__,'stage':'SUBSCRIPTION_SEND'})
                    try:self.socket.close()
                    except (WebSocketException,ConnectionError,TimeoutError):pass
            return True

    def refresh_exchange(self):
        """No role/list changes: refresh only an actual exchange phase change."""
        with self.subscription_lock:
            if self.subscription_items(self.symbols)==self.wire_symbols:return False
            return self.update_symbols(self.symbols)

    def entry_snapshot(self,symbol):
        """Receiver-owned latest frame, read without network calls or engine mutation."""
        with self.subscription_lock:
            return self.latest_ticks.get(symbol),dict(self.orderbooks.get(symbol,{}) or {})

    def context_snapshot(self,stamp):
        with self.subscription_lock:return self.index_context.snapshot(stamp)

    def run(self):
        import websocket
        attempt=0
        while not self.stop.is_set():
            try:
                session_guard=getattr(self.rest,'session_guard',None)
                if session_guard is not None and not session_guard():
                    self.emit({'trnm':'OUT_OF_SUPPORTED_SESSION'}); return
                token=self.rest.get_token()
                def on_open(ws):
                    if session_guard is not None and not session_guard(): ws.close(); return
                    ws.send(json.dumps({'trnm':'LOGIN','token':token}))
                def on_message(ws,raw):
                    try:
                        m=json.loads(raw)
                        if m.get('trnm')=='PING':
                            self.last_message_at=now(); ws.send(raw); return
                        if m.get('trnm')=='LOGIN':
                            if m.get('return_code') not in (0,'0'):
                                self.emit({'trnm':'ERROR','reason':'로그인 실패'}); ws.close(); return
                            self.logged_in=True;self.transport_failed=False
                            self.wire_symbols=self.subscription_items(self.symbols)
                            ws.send(json.dumps({'trnm':'REG','grp_no':'1','refresh':'0','data':[
                                {'item':self.wire_symbols,'type':['0B','0D']},{'item':[''],'type':['00','0s']}]}))
                            # Independent diagnostic group; WATCH churn never refreshes indices.
                            if self.index_subscription_enabled:
                                ws.send(json.dumps({'trnm':'REG','grp_no':'2','refresh':'0',
                                    'data':[{'item':['001','101'],'type':['0J']}]}))
                        else: self.emit(m)
                    except Exception:
                        self.emit({'trnm':'ERROR','reason':'실시간 메시지 형식 오류'}); ws.close()
                host='mockapi' if self.rest.cfg.broker_environment=='mock' else 'api'
                self.socket=websocket.WebSocketApp(f'wss://{host}.kiwoom.com:10000/api/dostk/websocket',
                    on_open=on_open,on_message=on_message,
                    on_error=lambda ws,error:self.emit({'trnm':'ERROR','reason':type(error).__name__}),
                    on_close=lambda *args:self.emit({'trnm':'DISCONNECTED'}))
                self.socket.run_forever(ping_interval=20,ping_timeout=10)
                self.logged_in=False
            except Exception as e: self.emit({'trnm':'ERROR','reason':type(e).__name__})
            if self.stop.is_set(): break
            attempt+=1; self.stop.wait(min(15,2**min(attempt,4)))

    def start(self): self.thread.start()
    def close(self):
        self.stop.set()
        if self.socket: self.socket.close()
        self.thread.join(timeout=3)

