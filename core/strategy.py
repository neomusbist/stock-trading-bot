"""순수 계산부: 과거 시점 데이터만 사용하고 진입 근거/무효화 가격을 함께 반환한다."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from core.util import floor_tick

@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime
    price: float
    volume: int
    cumulative: int
    ask: float
    bid: float
    regular: bool = True
    ask_qty: int = 0
    bid_qty: int = 0
    venue: str = 'KRX'
    vi_active: bool = False
    depth_available: bool | None = None
    book_time: datetime | None = None
    depth_source: str = 'UNKNOWN'

class Detector:
    def __init__(self,cfg):
        self.cfg=cfg; self.buffers={}; self.minutes={}; self.completed={}; self.last={}

    def reset(self):
        self.buffers.clear(); self.minutes.clear(); self.completed.clear(); self.last.clear()

    def ingest(self,t,received,preserve_observation=False):
        c=self.cfg
        if not t.regular or t.price<=0 or t.volume<=0 or t.cumulative<=0:
            return False
        if t.time.tzinfo is None or received.tzinfo is None or t.time.date()!=received.date():
            return False
        lag=(received-t.time).total_seconds()
        if lag < -1 or lag>c.stale_seconds:
            return False
        old=self.last.get(t.symbol)
        if old and (t.time<old.time or t.cumulative<=old.cumulative):
            return False
        if old and (t.time-old.time).total_seconds()>c.stale_seconds and not preserve_observation:
            self.buffers.pop(t.symbol,None); self.minutes.pop(t.symbol,None); self.completed.pop(t.symbol,None)
        self.last[t.symbol]=t
        buf=self.buffers.setdefault(t.symbol,deque())
        buf.append(t)
        horizon=max(c.baseline_seconds+c.acceleration_seconds,c.breakout_seconds)+2
        while buf and (t.time-buf[0].time).total_seconds()>horizon:
            buf.popleft()
        minute=t.time.replace(second=0,microsecond=0)
        current=self.minutes.get(t.symbol)
        if current and current['time']!=minute:
            if (minute-current['time']).total_seconds()==60:
                self.completed.setdefault(t.symbol,deque(maxlen=3)).append(current)
            else:
                self.completed.pop(t.symbol,None)
            current=None
        if not current:
            current={'time':minute,'low':t.price,'high':t.price,'first_second':t.time.second}
            self.minutes[t.symbol]=current
        current['low']=min(current['low'],t.price); current['high']=max(current['high'],t.price)
        return True

    def signal(self,t,watch):
        c=self.cfg
        if not c.entry_start<=t.time.strftime('%H:%M:%S')<c.entry_end:
            return None
        buf=list(self.buffers.get(t.symbol,[])); finished=list(self.completed.get(t.symbol,[]))
        if len(finished)<2 or any(b['first_second']>c.stale_seconds for b in finished[-2:]):
            return None
        if not buf or (t.time-buf[0].time).total_seconds()<c.baseline_seconds+c.acceleration_seconds:
            return None
        fast=[x for x in buf if 0<=(t.time-x.time).total_seconds()<c.acceleration_seconds]
        base=[x for x in buf if c.acceleration_seconds<=(t.time-x.time).total_seconds()<c.acceleration_seconds+c.baseline_seconds]
        prior=[x for x in buf[:-1] if (t.time-x.time).total_seconds()<=c.breakout_seconds]
        if len(fast)<2 or not base or not prior or not (0<t.bid<=t.ask):
            return None
        turnover=sum(x.price*x.volume for x in fast)
        baseline=sum(x.price*x.volume for x in base)/c.baseline_seconds
        if baseline<=0: return None
        ratio=(turnover/c.acceleration_seconds)/baseline
        gain=t.price/fast[0].price-1
        breakout=max(x.price for x in prior)
        if gain<c.min_price_gain or ratio<c.min_turnover_ratio or turnover<c.min_window_turnover or t.price<=breakout:
            return None
        if (t.ask-t.bid)/t.price>c.max_spread or t.ask/breakout-1>c.max_chase:
            return None
        if abs(t.price/watch['previous_close']-1)>c.max_gap:
            return None
        # 무효화: 최근 완성 1분봉 두 개의 저점 아래. 현재 미완성 봉은 사용하지 않는다.
        stop=floor_tick(min(b['low'] for b in finished[-2:])*(1-c.structural_buffer))
        limit=floor_tick(min(t.ask*(1+c.entry_slippage_fraction),breakout*(1+c.max_chase)))
        if limit<t.ask or stop<=0 or not c.min_stop_fraction<=(limit-stop)/limit<=c.max_stop_fraction:
            return None
        resistance=watch.get('resistance')
        if resistance is not None and (resistance-limit)/(limit-stop)<c.min_reward_risk:
            return None
        return {'symbol':t.symbol,'limit':limit,'stop':stop,'breakout':breakout,'gain':gain,
                'turnover':turnover,'turnover_ratio':ratio,'time':t.time.isoformat()}

def size(cfg,entry,stop,equity,cash,risk_remaining):
    if not 0<stop<entry or equity<=0 or cash<=0 or risk_remaining<=0:
        return 0
    per_share=entry-stop+entry*(cfg.roundtrip_cost_fraction+cfg.exit_slippage_fraction)
    budget=min(cfg.account_equity,equity)*cfg.risk_fraction
    return max(0,min(int(min(budget,risk_remaining)//per_share),
        int(min(cash,min(cfg.account_equity,equity)*cfg.max_position_fraction)//(entry*(1+cfg.roundtrip_cost_fraction)))))

def protective_stop(cfg,position,price,completed_low=None):
    high=max(position['high'],price)
    stop=position['stop']
    if high-position['avg']>=cfg.protect_at_r*position['initial_r']:
        stop=max(stop,floor_tick(position['avg']*(1+cfg.roundtrip_cost_fraction)),floor_tick(high*(1-cfg.trailing_fraction)))
        if completed_low is not None:
            stop=max(stop,floor_tick(completed_low*(1-cfg.structural_buffer)))
    return high,stop

