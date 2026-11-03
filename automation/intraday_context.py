"""Bounded read-only history for WATCH structure; never real-time warmup."""
from collections import OrderedDict
from datetime import datetime,timedelta
import queue,threading,time
from core.settings import KST,now
from core.util import number
from automation.trade_quality import complete_sequence


def parse_minutes(data,received,venue,symbol):
    suffix={'KRX':'','NXT':'_NX','SOR':'_AL'}
    if venue not in suffix:raise ValueError('REFERENCE_VENUE_UNKNOWN')
    if data.get('stk_cd') not in {symbol,symbol+suffix[venue]}:raise ValueError('REFERENCE_SYMBOL_MISMATCH')
    rows=data.get('stk_min_pole_chart_qry')
    if not isinstance(rows,list):raise ValueError('REFERENCE_ARRAY_MISSING')
    result=[];seen=set()
    for row in rows:
        # No-history responses can contain one entirely empty placeholder.
        # A missing timestamp with actual prices is still malformed data.
        if not row.get('cntr_tm') and all(row.get(k) in (None,'') for k in ('cur_prc','trde_qty','open_pric','high_pric','low_pric')):continue
        stamp=datetime.strptime(row['cntr_tm'],'%Y%m%d%H%M%S').replace(tzinfo=KST)
        if stamp.second or stamp.date()!=received.date() or stamp+timedelta(minutes=1)>received:continue
        if stamp in seen:raise ValueError('DUPLICATE_REFERENCE_BAR')
        seen.add(stamp)
        bar={'time':stamp,'open':number(row['open_pric']),'high':number(row['high_pric']),
             'low':number(row['low_pric']),'close':number(row['cur_prc']),
             'volume':number(row['trde_qty'],signed=True),'source':'REST_REFERENCE',
             'venue':venue,'available_at':received.isoformat()}
        if bar['volume']<0 or not complete_sequence([bar],received,1):raise ValueError('INVALID_REFERENCE_BAR')
        result.append(bar)
    return sorted(result,key=lambda b:b['time'])[-120:]


class IntradayContextWorker:
    def __init__(self,rest,clock=now,interval=5.,ttl=300.):
        self.rest=rest;self.clock=clock;self.interval=interval;self.ttl=ttl
        self.q=queue.Queue(maxsize=14);self.stop=threading.Event();self.lock=threading.Lock()
        self.targets={};self.cache=OrderedDict();self.thread=None

    def set_targets(self,targets):
        with self.lock:self.targets=dict(list(targets.items())[:14])

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,daemon=True,name='intraday-context');self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=1.)

    def fetch(self,symbol,venue):
        suffix={'KRX':'','NXT':'_NX','SOR':'_AL'}.get(venue)
        if suffix is None:raise ValueError('REFERENCE_VENUE_UNKNOWN')
        data=self.rest.query('ka10080',{'stk_cd':symbol+suffix,'tic_scope':'1','upd_stkpc_tp':'1'},path='/api/dostk/chart')
        received=self.clock()
        bars=parse_minutes(data,received,venue,symbol)
        raw_rows=data.get('stk_min_pole_chart_qry') or []
        today_str=received.strftime('%Y%m%d')
        highs=[]
        for r in raw_rows:
            if str(r.get('cntr_tm','')).startswith(today_str):
                try:
                    h=number(r.get('high_pric'))
                    if h>0:highs.append(h)
                except Exception:pass
        session_high=max(highs,default=None)
        return {'symbol':symbol,'venue':venue,'received_at':received.isoformat(),
                'bars':bars,'session_high':session_high,'source':'REST_REFERENCE',**({'error':'REFERENCE_NO_HISTORY'} if not bars else {})}

    def run(self):
        while not self.stop.is_set():
            current=self.clock()
            with self.lock:
                candidates=[(s,v) for s,v in self.targets.items() if
                    (s,v) not in self.cache or (current-self.cache[s,v]).total_seconds()>=self.ttl]
            if candidates:
                symbol,venue=candidates[0]
                try:
                    packet=self.fetch(symbol,venue)
                except Exception as error:
                    packet={'symbol':symbol,'venue':venue,'received_at':self.clock().isoformat(),
                            'error':type(error).__name__,'bars':[]}
                with self.lock:
                    # Failed reads back off; they never become proof of adequate data.
                    self.cache[symbol,venue]=self.clock() if packet.get('error') in (None,'REFERENCE_NO_HISTORY') else self.clock()-timedelta(seconds=self.ttl-self.interval*3)
                    self.cache.move_to_end((symbol,venue))
                    while len(self.cache)>256:self.cache.popitem(last=False)
                try:self.q.put_nowait(packet)
                except queue.Full:pass
            self.stop.wait(self.interval)


def context_targets(watch,evaluations,venue,asof):
    """Up to two observed preliminary setups may earn reference data, never order permission."""
    targets={symbol:venue for symbol in list(watch)[:12]}
    for symbol,row in sorted(evaluations.items(),key=lambda p:p[1].get('timestamp',''),reverse=True):
        if len(targets)>=14:break
        if symbol in targets:continue
        try:
            if not 0<=(asof-datetime.fromisoformat(row['timestamp'])).total_seconds()<=5:continue
        except (KeyError,TypeError,ValueError):continue
        for trial in row.get('setup_trials',[]):
            gates=trial.get('gates',{})
            needed=('warmup','spread','structure','liquidity','breakout','chase')+ (('acceleration',) if trial.get('setup')=='A' else ())
            if trial.get('candidate_generated') and all(gates.get(k,{}).get('result')=='PASS' for k in needed):
                targets[symbol]=venue;break
    return targets
