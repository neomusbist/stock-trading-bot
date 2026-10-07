"""수신 전용 스레드: PING 즉시 응답, 재연결마다 로그인/재구독. 주문은 주 스레드만 처리."""
import json
import queue
import threading
import time
from datetime import datetime
from core.settings import ROOT, KST, now
from core.strategy import Tick
from core.util import number, code

def parse_book(item,received):
    """0D: best prices 41/51, best quantities 61/71, totals 121/125.
    0C provides prices 27/28 only; absent depth is not zero liquidity.
    """
    v=item.get('values',{})
    available=all(v.get(k) not in (None,'') for k in ('41','51','61','71'))
    result={'best_ask':number(v.get('41') or 0),'best_bid':number(v.get('51') or 0),
            'ask_qty':int(number(v.get('61') or 0)),'bid_qty':int(number(v.get('71') or 0)),
            'total_ask_qty':int(number(v.get('121') or 0)),'total_bid_qty':int(number(v.get('125') or 0)),
            'time':received.isoformat(),'source':'0D',
            'raw_fields':{k:v.get(k) for k in ('21','41','51','61','71','121','125')}}
    result['depth_available']=bool(available and 0<result['best_bid']<=result['best_ask'])
    return result

def parse_tick(item,received,book=None,book_stale_seconds=5):
    v=item['values']; stamp=datetime.strptime(v['20'],'%H%M%S').time()
    dt=datetime.combine(received.date(),stamp,tzinfo=KST)
    # WATCH/main path and SHADOW/fast path must read the same attached book.
    book=item.get('_book',{}) if book is None else book
    book=book or {}
    ask=number(v['27']);bid=number(v['28'])
    depth_time=None;depth_available=False;depth_source='DATA_UNAVAILABLE'
    ask_qty=bid_qty=0
    if all(v.get(k) not in (None,'') for k in ('61','71')):
        ask_qty=int(number(v['61']));bid_qty=int(number(v['71']))
        depth_available=True;depth_time=received;depth_source='0B_FIELDS'
    elif book:
        try:depth_time=datetime.fromisoformat(book['time'])
        except (KeyError,TypeError,ValueError):depth_time=None
        fresh=bool(depth_time and depth_time.tzinfo and depth_time.date()==received.date()
                   and 0<=(received-depth_time).total_seconds()<=book_stale_seconds)
        matched=book.get('best_ask')==ask and book.get('best_bid')==bid
        depth_available=bool(fresh and matched and book.get('depth_available'))
        depth_source='0D' if depth_available else 'BOOK_STALE' if not fresh else 'BOOK_PRICE_MISMATCH' if not matched else 'DATA_UNAVAILABLE'
        if depth_available:
            ask_qty=int(book['ask_qty']);bid_qty=int(book['bid_qty'])
    venue=v.get('9081') or 'KRX'
    return Tick(code(item['item']),dt,number(v['10']),int(number(v['15'])),int(number(v['13'])),
        ask,bid,v.get('290')=='2',ask_qty=ask_qty,bid_qty=bid_qty,venue=venue,
        depth_available=depth_available,book_time=depth_time,depth_source=depth_source)

class Feed:
    def __init__(self,rest,symbols):
        self.rest=rest; self.symbols=list(dict.fromkeys(symbols)); self.q=queue.Queue(maxsize=100000)
        self.stop=threading.Event(); self.overflow=threading.Event(); self.socket=None
        self.logged_in=False; self.subscription_lock=threading.RLock()
        self.observation_symbols=frozenset(); self.priority_symbols=None; self.observation_sink=None
        self.observation_q=queue.Queue(maxsize=8192);self.observation_drops=0
        self.orderbooks={}
        self.thread=threading.Thread(target=self.run,name='Kiwoom-feed',daemon=True)

    def emit(self,event):
        received=now()
        with self.subscription_lock:
            if event.get('trnm') in {'ERROR','DISCONNECTED'}:self.orderbooks.clear()
            if event.get('trnm')=='REAL':
                for item in event.get('data',[]):
                    itype=item.get('type'); raw_item=item.get('item')
                    sym=code(raw_item) if raw_item else None
                    if sym and itype=='0D':
                        self.orderbooks[sym]=parse_book(item,received)
                        item['_book']=self.orderbooks[sym]
                    elif sym and itype=='0B':
                        item['_book']=self.orderbooks.get(sym)
            if self.priority_symbols is not None and event.get('trnm')=='REAL':
                priority=[]
                for item in event.get('data',[]):
                    if item.get('type')=='0B' and item.get('item') in self.observation_symbols:
                        try:
                            tick=parse_tick(item,received,book=item.get('_book') or self.orderbooks.get(code(item.get('item',''))),book_stale_seconds=getattr(self.rest.cfg,'stale_seconds',5))
                            # Frozen value for the same received frame, shared by observation and main.
                            item['_observation_tick']=(received,tick)
                            if self.observation_sink:self.observation_sink(tick,received)
                            if item.get('item') not in self.priority_symbols:self.observation_q.put_nowait((received,tick))
                        except (queue.Full,ValueError,KeyError):self.observation_drops+=1
                    if item.get('type')!='0B' or item.get('item') in self.priority_symbols:priority.append(item)
                if not priority:return
                event={**event,'data':priority}
        try: self.q.put_nowait((received,event))
        except queue.Full: self.overflow.set()

    def configure_observation(self,priority,symbols,sink):
        with self.subscription_lock:
            self.priority_symbols=frozenset(priority);self.observation_symbols=frozenset(symbols);self.observation_sink=sink

    def update_symbols(self,symbols):
        with self.subscription_lock:
            updated=list(dict.fromkeys(symbols))
            if updated==self.symbols: return False
            self.symbols=updated
            if self.socket and self.logged_in:
                # refresh=0 그룹을 완전히 교체하며 주문/장상태 구독은 항상 다시 포함한다.
                self.socket.send(json.dumps({'trnm':'REG','grp_no':'1','refresh':'0','data':[
                    {'item':self.symbols,'type':['0B','0D']},{'item':[''],'type':['00','0s']}]}))
            return True

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
                        if m.get('trnm')=='PING': ws.send(raw); return
                        if m.get('trnm')=='LOGIN':
                            if m.get('return_code') not in (0,'0'):
                                self.emit({'trnm':'ERROR','reason':'로그인 실패'}); ws.close(); return
                            self.logged_in=True
                            ws.send(json.dumps({'trnm':'REG','grp_no':'1','refresh':'0','data':[
                                {'item':self.symbols,'type':['0B','0D']},{'item':[''],'type':['00','0s']}]}))
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

