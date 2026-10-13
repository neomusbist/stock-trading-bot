"""Opening A failure hypothesis, strictly observation-only; no order objects.

A weak upper-wick burst is only a warning. A completed lower rebound followed
by a close below its retained floor is required to propose an earlier exit.
These relational rules are unvalidated hypotheses, not the operating AE3.
"""
from datetime import datetime,timedelta
from automation.exit_plan import number,fresh
from automation.trade_quality import complete_sequence
from core.util import floor_tick


def observe_opening_failure(cfg,auto,position,asof):
    result={'policy':'OPENING_FAILED_REBOUND_OBSERVATION','asof':asof.isoformat(),
            'action':'NOT_APPLICABLE','orders_enabled':False,'actual_fill':None,
            'realized_net_pnl':None,'profitability_proven':False}
    sig=position.get('signal',{})
    try:opened=datetime.fromisoformat(position['opened'])
    except (KeyError,TypeError,ValueError):return {**result,'action':'UNKNOWN','reason':'ENTRY_TIME_UNKNOWN'}
    if (sig.get('session')!='REGULAR' or sig.get('pattern')!='A' or opened.tzinfo is None
            or opened.date()!=asof.date() or not '09:00:00'<=opened.strftime('%H:%M:%S')<'09:10:00'):
        return {**result,'reason':'NOT_OPENING_REGULAR_A'}
    avg=position.get('avg');risk=position.get('initial_r');high=position.get('high')
    if not all(number(x) and x>0 for x in (avg,risk,high)):
        return {**result,'action':'UNKNOWN','reason':'ENTRY_RISK_UNKNOWN'}
    if not 0<high-avg<risk:
        return {**result,'reason':'NOT_PRE_ONE_R_GAIN'}
    first=opened.replace(second=0,microsecond=0)
    if opened>first:first+=timedelta(minutes=1)
    raw=position.get('protection_bars') or []
    size=min(30,len(raw));bars=complete_sequence(raw,asof,size,first,require_recent=True) if size>=4 else []
    if not bars or not all(number(b.get('volume')) and b['volume']>=0 for b in bars):
        return {**result,'action':'UNKNOWN','reason':'COMPLETE_POST_FILL_BARS_UNKNOWN'}
    peak_i=max(range(len(bars)),key=lambda i:(bars[i]['high'],i));burst=bars[peak_i]
    if burst['high']<high:
        return {**result,'action':'UNKNOWN','reason':'POST_FILL_PEAK_OUTSIDE_COMPLETE_SEQUENCE'}
    wick=burst['high']-max(burst['open'],burst['close']);body=abs(burst['close']-burst['open'])
    weak=burst['close']<(burst['high']+burst['low'])/2
    burst_warning=(wick>body and weak and burst['high']/burst['open']-1>=cfg.min_price_gain)
    rebound=None;floor=None
    for i in range(peak_i+2,len(bars)-1):
        before,b=bars[i-1],bars[i]
        if b['high']<burst['high'] and b['close']>before['high']:
            rebound=b;floor=min(x['low'] for x in bars[peak_i+1:i+1])
    last=bars[-1]
    failed=bool(burst_warning and rebound and last['time']>rebound['time']
                and last['high']<rebound['high'] and last['close']<floor)
    quote=position.get('last_quote') or {};bid=quote.get('bid');ask=quote.get('ask')
    quote_known=(fresh(quote.get('time'),asof,cfg.stale_seconds)
                 and number(bid) and number(ask) and 0<bid<=ask)
    depth=quote.get('bid_qty');qty=position.get('qty')
    depth_known=quote_known and number(depth) and number(qty) and 0<qty<=depth
    modeled=floor_tick(bid*(1-cfg.exit_slippage_fraction)) if depth_known else None
    net=(modeled*(1-auto.commission_sell-auto.sell_tax)-avg*(1+auto.commission_buy))*qty if modeled else None
    return {**result,'action':'EXIT_CANDIDATE' if failed else 'HOLD','reason':'COMPLETED_LOWER_REBOUND_FLOOR_FAILED' if failed else 'OPENING_FAILURE_NOT_CONFIRMED',
        'long_wick_warning':burst_warning,'peak':burst['high'],'peak_bar_at':burst['time'].isoformat(),
        'rebound_at':rebound['time'].isoformat() if rebound else None,'rebound_floor':floor,
        'last_complete_close':last['close'],'one_r_required':False,
        'executable_quote_known':quote_known,'full_quantity_depth_known':bool(depth_known),
        'bid':bid if quote_known else None,'modeled_exit_price':modeled,'modeled_net_pnl':net,
        'cost_basis':'CONFIG_PROPORTIONAL_FEES_TAX_AND_SLIPPAGE_NOT_BROKER_FILLS'}
