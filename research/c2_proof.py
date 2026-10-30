"""Independent, fail-closed C2 proof verifier for the sealed PAPER engine."""
from datetime import timedelta
import math
from core.market_evidence import aware
from core.util import floor_tick,tick_size
from automation.capital import signal_economics

def verify_c2_proof(signal,tick,cfg,auto):
    try:
        p=signal['c2_structure_proof'];stamp=aware(signal['time']);known=aware(p['known_at'])
        br=aware(p['break_at']);confirmed=aware(p['confirmed_at']);receipt=p['break_receipt']
        if not all((stamp,known,br,confirmed)) or not known<=br<=confirmed<=stamp:return False
        if not receipt.get('trade_receipt_id') or aware(receipt.get('trade_exchange_at'))!=br:return False
        received=aware(receipt.get('trade_received_at'))
        if received is None or not br<=received<=confirmed:return False
        base=p['base'];post=p['post_break_bars']
        if len(base)!=auto.box_min_bars or not post or len(post)>30:return False
        if any(row.get('gap_reason') for row in base+post):return False
        def bar(row):
            tm=aware(row['time']); vals=[row[k] for k in ('open','high','low','close')]
            if tm is None or any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in vals):raise ValueError('BAR')
            op,hi,lo,cl=vals
            if not lo<=min(op,cl)<=max(op,cl)<=hi:raise ValueError('OHLC')
            return tm,hi,lo,op,cl
        b=[bar(x) for x in base];after=[bar(x) for x in post]
        if any(b[i+1][0]-b[i][0]!=timedelta(minutes=1) for i in range(len(b)-1)):return False
        if b[-1][0]+timedelta(minutes=1)>known:return False
        top=max(x[1] for x in b);low=min(x[2] for x in b)
        if not 0<(top-low)/low<=auto.box_max_range or b[-1][4]<b[0][3]:return False
        if not (type(p['break_price']) in (int,float) and top<p['break_price']):return False
        if any(x[0]<=br.replace(second=0,microsecond=0) or x[0]+timedelta(minutes=1)>confirmed for x in after):return False
        if after[0][0]!=br.replace(second=0,microsecond=0)+timedelta(minutes=1):return False
        if any(after[i+1][0]-after[i][0]!=timedelta(minutes=1) for i in range(len(after)-1)):return False
        ret=after[-1]
        if any(x[4]<top for x in after) or not top-tick_size(top)<=ret[2]<=top+tick_size(top) or ret[4]<=top:return False
        if signal['breakout']!=ret[1] or not (tick.price>ret[1] and tick.ask>ret[1] and signal['limit']==tick.ask):return False
        if tick.ask/ret[1]-1>cfg.max_chase:return False
        structural=floor_tick(ret[2]-tick_size(ret[2]));spread=tick.ask-tick.bid
        noise=max([x[1]-x[2] for x in b+after]+[spread,tick_size(tick.price)])
        emergency=floor_tick(structural-noise)
        policy=signal['invalidation_policy']
        if policy.get('structural_stop')!=structural or policy.get('emergency_stop')!=emergency or signal['stop']!=emergency:return False
        ev=p['target_evidence'];source=ev['source_bars']
        if len(source)!=3:return False
        ref=[bar(x) for x in source];center=aware(ev['time']);confirmed_peak=aware(ev['confirmed_at']);known_by=aware(ev['known_by'])
        if not all((center,confirmed_peak,known_by)) or not confirmed_peak<=known_by<=known:return False
        if [x[0] for x in ref]!=[center-timedelta(minutes=1),center,center+timedelta(minutes=1)]:return False
        if any(x.get('observation_source') not in {'ACTUAL_RECEIVED','REST_REFERENCE'} for x in source):return False
        if ref[1][1]!=ev['price'] or ref[1][1]<max(ref[0][1],ref[2][1]):return False
        max_target=ev['price'];watch_ceiling=ev.get('known_watch_ceiling')
        if type(watch_ceiling) in (int,float) and watch_ceiling>top:max_target=min(max_target,watch_ceiling)
        if not tick.ask<signal['target']<=max_target:return False
        econ=signal_economics(cfg,auto,signal)
        return bool(econ.get('status')=='KNOWN' and econ.get('rr_pass'))
    except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError):return False
