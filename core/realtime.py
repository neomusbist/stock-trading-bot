"""수신 전용 스레드: PING 즉시 응답, 재연결마다 로그인/재구독. 주문은 주 스레드만 처리."""
import json
import queue
import threading
import time
from datetime import datetime
from core.settings import ROOT, KST, now
from core.strategy import Tick
from core.util import number, code

def parse_tick(item,received):
    v=item['values']; stamp=datetime.strptime(v['20'],'%H%M%S').time()
    dt=datetime.combine(received.date(),stamp,tzinfo=KST)
    return Tick(code(item['item']),dt,number(v['10']),int(number(v['15'])),int(number(v['13'])),
        number(v['27']),number(v['28']),v.get('290')=='2')

class Feed:
    def __init__(self,rest,symbols):
        self.rest=rest; self.symbols=list(dict.fromkeys(symbols)); self.q=queue.Queue(maxsize=100000)
        self.stop=threading.Event(); self.overflow=threading.Event(); self.socket=None
        self.logged_in=False; self.subscription_lock=threading.RLock()
        self.observation_symbols=frozenset(); self.priority_symbols=None; self.observation_sink=None
        self.observation_q=queue.Queue(maxsize=8192);self.observation_drops=0
        self.thread=threading.Thread(target=self.run,name='Kiwoom-feed',daemon=True)

    def emit(self,event):
        received=now()
        if self.priority_symbols is not None and event.get('trnm')=='REAL':
            priority=[]
            for item in event.get('data',[]):
                if item.get('type')=='0B' and item.get('item') in self.observation_symbols:
                    try:
                        tick=parse_tick(item,received)
                        if self.observation_sink:self.observation_sink(tick,received)
                        if item.get('item') not in self.priority_symbols:self.observation_q.put_nowait((received,tick))
                    except (queue.Full,ValueError,KeyError):self.observation_drops+=1
                if item.get('type')!='0B' or item.get('item') in self.priority_symbols:priority.append(item)
            if not priority:return
            event={**event,'data':priority}
        try: self.q.put_nowait((received,event))
        except queue.Full: self.overflow.set()

    def configure_observation(self,priority,symbols,sink):
        self.priority_symbols=frozenset(priority);self.observation_symbols=frozenset(symbols);self.observation_sink=sink

    def update_symbols(self,symbols):
        with self.subscription_lock:
            updated=list(dict.fromkeys(symbols))
            if updated==self.symbols: return False
            self.symbols=updated
            if self.socket and self.logged_in:
                # refresh=0 그룹을 완전히 교체하며 주문/장상태 구독은 항상 다시 포함한다.
                self.socket.send(json.dumps({'trnm':'REG','grp_no':'1','refresh':'0','data':[
                    {'item':self.symbols,'type':['0B']},{'item':[''],'type':['00','0s']}]}))
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
                                {'item':self.symbols,'type':['0B']},{'item':[''],'type':['00','0s']}]}))
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

