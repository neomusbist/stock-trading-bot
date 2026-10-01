"""완성된 봉과 현재까지 수신한 틱만으로 A/B/C 모멘텀 지속 패턴을 판별한다."""
from collections import deque
from dataclasses import replace
from core.strategy import Detector
from core.util import floor_tick,tick_size
from automation.allocation import efficiency
from automation.session import supported
from automation.decisions import gate

class PatternDetector(Detector):
    def __init__(self,cfg,auto,costs):
        # 원래 순수 Detector는 회귀 계약 유지. 확장 판단에는 시간 마감/전일 상승률 필터가 없다.
        super().__init__(replace(cfg,entry_start='09:00:00',entry_end='23:59:59'))
        self.a=auto; self.costs=costs; self.history={}; self.current={}; self.day_high={}; self.prior_high={}
        self.last_eval={}

    def reset(self):
        super().reset(); self.history.clear(); self.current.clear(); self.day_high.clear(); self.prior_high.clear()
        self.last_eval.clear()

    def ingest(self,t,received):
        if not supported(t.time) or not supported(received): return False
        old=self.last.get(t.symbol)
        if not super().ingest(t,received): return False
        if old and (t.time-old.time).total_seconds()>self.cfg.stale_seconds:
            self.history.pop(t.symbol,None); self.current.pop(t.symbol,None)
        self.prior_high[t.symbol]=self.day_high.get(t.symbol,t.price)
        self.day_high[t.symbol]=max(t.price,self.day_high.get(t.symbol,t.price))
        minute=t.time.replace(second=0,microsecond=0); b=self.current.get(t.symbol)
        if b and b['time']!=minute:
            if (minute-b['time']).total_seconds()==60:
                self.history.setdefault(t.symbol,deque(maxlen=30)).append(b)
            else: self.history.pop(t.symbol,None)
            b=None
        if b is None:
            b={'time':minute,'open':t.price,'high':t.price,'low':t.price,'close':t.price,'volume':0,'first_second':t.time.second}
            self.current[t.symbol]=b
        b['high']=max(b['high'],t.price); b['low']=min(b['low'],t.price); b['close']=t.price; b['volume']+=t.volume
        return True

    def pullback(self,bars):
        if len(bars)<4: return None
        seq=bars[-12:]; peak_i=max(range(1,len(seq)-1),key=lambda i:seq[i]['high'])
        impulse=seq[:peak_i+1]; pull=seq[peak_i+1:]
        if not pull: return None
        peak=seq[peak_i]['high']; base=min(b['low'] for b in impulse); low=min(b['low'] for b in pull)
        rise=peak-base
        if rise/base<self.a.pullback_min_rise or not 0<peak-low<=rise*self.a.pullback_max_retrace: return None
        impulse_volume=sum(b['volume'] for b in impulse)/len(impulse)
        pull_volume=sum(b['volume'] for b in pull)/len(pull)
        if impulse_volume<=0 or pull_volume>impulse_volume*self.a.pullback_volume_ratio: return None
        if pull[-1]['close']<low or low<=base: return None
        return {'pattern':'B','breakout':peak,'support':low,'projection':peak+rise,
                'pullback_volume_ratio':pull_volume/impulse_volume,'retrace':(peak-low)/rise}

    def box(self,bars):
        if len(bars)<self.a.box_min_bars: return None
        seq=bars[-self.a.box_min_bars:]; top=max(b['high'] for b in seq); low=min(b['low'] for b in seq)
        if not 0<(top-low)/low<=self.a.box_max_range: return None
        return {'pattern':'C','breakout':top,'support':low,'projection':top+2*(top-low),'box_width':top-low}

    def signal(self,t,watch):
        self.last_eval[t.symbol]={'time':t.time,'cumulative':t.cumulative,'gates':self.base_gates(t,watch),'trials':[]}
        if not supported(t.time): return None
        if watch.get('status') not in {'WATCH','ELIGIBLE','RESERVE'}: return None
        bars=list(self.history.get(t.symbol,[]))
        valid=[]
        for b in reversed(bars):
            if b['first_second']>self.cfg.stale_seconds: break
            valid.append(b)
        bars=list(reversed(valid))
        if len(bars)<2: return None
        c=self.cfg; buf=list(self.buffers.get(t.symbol,[]))
        if not buf or (t.time-buf[0].time).total_seconds()<c.baseline_seconds+c.acceleration_seconds: return None
        fast=[x for x in buf if 0<=(t.time-x.time).total_seconds()<c.acceleration_seconds]
        base=[x for x in buf if c.acceleration_seconds<=(t.time-x.time).total_seconds()<c.baseline_seconds+c.acceleration_seconds]
        if len(fast)<2 or not base or not 0<t.bid<=t.ask: return None
        turnover=sum(x.price*x.volume for x in fast); baseline=sum(x.price*x.volume for x in base)/c.baseline_seconds
        ratio=turnover/c.acceleration_seconds/baseline if baseline else 0
        if ratio<c.min_turnover_ratio or turnover<c.min_window_turnover or t.price/fast[0].price-1<c.min_price_gain: return None
        if (t.ask-t.bid)/t.price>c.max_spread: return None
        candidates=[self.pullback(bars),self.box(bars)]  # 정상 눌림 B를 먼저 평가한다.
        prior=self.prior_high.get(t.symbol)
        if prior and t.price>prior:
            support=min(b['low'] for b in bars[-2:])
            candidates.append({'pattern':'A','breakout':prior,'support':support,'projection':prior+2*(prior-support)})
        for setup in candidates:
            if not setup: continue
            trial=self.setup_gates(t,watch,setup)
            self.last_eval[t.symbol]['gates'].update(trial)
            self.last_eval[t.symbol]['trials'].append({'setup':setup['pattern'],'gates':trial})
            breakout=setup['breakout']
            if t.price<=breakout or t.ask/breakout-1>c.max_chase: continue
            stop=floor_tick(setup['support']*(1-c.structural_buffer))
            # 호가 확인 뒤 매수1호가가 아닌 매도1호가에 상한 지정가. 비용 계산은 예상 슬리피지도 유지한다.
            limit=floor_tick(min(t.ask,breakout*(1+c.max_chase)))
            if limit<t.ask or not 0<stop<limit or not c.min_stop_fraction<=(limit-stop)/limit<=c.max_stop_fraction: continue
            resistance=watch.get('resistance')
            target=min(resistance,setup['projection']) if resistance is not None else setup['projection']
            if (target-limit)/(limit-stop)<c.min_reward_risk: continue
            horizon=getattr(self.a,'horizon_'+setup['pattern'].lower()+'_minutes')
            confidence=min(1.,0.4+0.1*len(watch.get('source_details',{})))
            score=efficiency(limit,stop,target,confidence,horizon,self.costs,self.a.min_efficiency_score)
            if score['decision']!='TRADE': continue
            self.last_eval[t.symbol]['gates']['pattern']=gate(True,setup=setup['pattern'])
            return {'symbol':t.symbol,'time':t.time.isoformat(),'limit':limit,'stop':stop,**setup,
                'evaluation_gates':self.last_eval[t.symbol]['gates'],'setup_trials':self.last_eval[t.symbol]['trials'],
                'target':target,'target_basis':'과거 저항으로 제한한 관측 패턴 폭 연장 가설; 고정 익절선 아님',
                'turnover':turnover,'turnover_ratio':ratio,'gain':t.price/fast[0].price-1,
                'efficiency':score,'source_details':watch.get('source_details',{}),'source_deltas':watch.get('source_deltas',{}),
                'structure':{k:watch.get(k) for k in ('long_position','overhead_share_estimate','resistance','recent_support')},
                'risk_per_share':score['risk_per_share'],'market_state':'3'}
        return None

    def evaluation_gates(self,t,watch):
        last=self.last_eval.get(t.symbol,{})
        if last.get('time')==t.time and last.get('cumulative')==t.cumulative: return last['gates']
        return self.base_gates(t,watch)

    def setup_gates(self,t,watch,setup):
        c=self.cfg; breakout=setup['breakout']; stop=floor_tick(setup['support']*(1-c.structural_buffer))
        limit=floor_tick(min(t.ask,breakout*(1+c.max_chase))); resistance=watch.get('resistance')
        target=min(resistance,setup['projection']) if resistance is not None else setup['projection']
        distance=(limit-stop)/limit if limit>0 else None
        rr=(target-limit)/(limit-stop) if limit>stop else None
        horizon=getattr(self.a,'horizon_'+setup['pattern'].lower()+'_minutes')
        score=efficiency(limit,stop,target,min(1.,0.4+0.1*len(watch.get('source_details',{}))),horizon,self.costs,self.a.min_efficiency_score)
        return {'breakout':gate(t.price>breakout,price=t.price,breakout=breakout,setup=setup['pattern']),
            'chase':gate(t.ask/breakout-1<=c.max_chase,chase=t.ask/breakout-1,max_chase=c.max_chase,ask=t.ask),
            'invalidation':gate(None if distance is None else 0<stop<limit and c.min_stop_fraction<=distance<=c.max_stop_fraction,
                entry=limit,stop=stop,distance_pct=distance,min_pct=c.min_stop_fraction,max_pct=c.max_stop_fraction),
            'risk_reward':gate(None if rr is None else rr>=c.min_reward_risk,calculated_rr=rr,required_rr=c.min_reward_risk,target=target),
            'resistance':gate(watch.get('status') in {'ELIGIBLE','WATCH','RESERVE'},resistance=resistance,
                distance_pct=resistance/limit-1 if resistance is not None and limit>0 else None,min_room=c.min_resistance_room),
            'economic':gate(score['decision']=='TRADE',score=score,tick_size=tick_size(t.price),tick_cost_fraction=tick_size(t.price)/t.price)}

    def base_gates(self,t,watch):
        c=self.cfg; buf=list(self.buffers.get(t.symbol,[])); bars=list(self.history.get(t.symbol,[]))
        elapsed=(t.time-buf[0].time).total_seconds() if buf else 0
        fast=[x for x in buf if 0<=(t.time-x.time).total_seconds()<c.acceleration_seconds]
        base=[x for x in buf if c.acceleration_seconds<=(t.time-x.time).total_seconds()<c.baseline_seconds+c.acceleration_seconds]
        amount=sum(x.price*x.volume for x in fast); baseline=sum(x.price*x.volume for x in base)/c.baseline_seconds
        ratio=amount/c.acceleration_seconds/baseline if baseline else None
        gain=t.price/fast[0].price-1 if fast else None
        spread=(t.ask-t.bid)/t.price if 0<t.bid<=t.ask and t.price>0 else None
        return {'warmup':gate(elapsed>=c.baseline_seconds+c.acceleration_seconds and len(bars)>=2,elapsed_seconds=elapsed,required_seconds=c.baseline_seconds+c.acceleration_seconds,completed_bars=len(bars)),
                'acceleration':gate(None if ratio is None or gain is None else ratio>=c.min_turnover_ratio and gain>=c.min_price_gain,ratio=ratio,min_ratio=c.min_turnover_ratio,gain=gain,min_gain=c.min_price_gain),
                'liquidity':gate(amount>=c.min_window_turnover,window_turnover=amount,min_turnover=c.min_window_turnover),
                'spread':gate(None if spread is None else spread<=c.max_spread,bid=t.bid,ask=t.ask,spread=spread,max_spread=c.max_spread),
                'structure':gate(watch.get('status') in {'WATCH','ELIGIBLE','RESERVE'},status=watch.get('status'),resistance=watch.get('resistance'),overhead_share=watch.get('overhead_share_estimate')),
                'market_status':gate(watch.get('market_status',{}).get('pass'),status=watch.get('market_status')),
                'pattern':gate(False,reason='완성봉/돌파/추격/무효화/효율 중 신호 미충족')}

