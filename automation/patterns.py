"""완성된 봉과 현재까지 수신한 틱만으로 A/B/C 모멘텀 지속 패턴을 판별한다."""
from datetime import timedelta
from collections import deque
from dataclasses import replace
from copy import deepcopy
from functools import wraps
from threading import RLock
from core.strategy import Detector
from core.util import floor_tick,tick_size
from automation.allocation import efficiency
from automation.session import supported, after_supported
from automation.decisions import gate

def observation_locked(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self._state_lock:return method(self,*args,**kwargs)
    return call

class PatternDetector(Detector):
    def __init__(self,cfg,auto,costs):
        # 원래 순수 Detector는 회귀 계약 유지. 확장 판단에는 시간 마감/전일 상승률 필터가 없다.
        super().__init__(replace(cfg,entry_start='09:00:00',entry_end='23:59:59'))
        self.a=auto; self.costs=costs; self.history={}; self.current={}; self.day_high={}; self.prior_high={}
        self.after_day_high={}; self.after_prior_high={}
        self.last_eval={}
        self._eval_keys={}
        self.continuous_observation=False
        self.last_gap_reason={}
        self.observation_start={}; self.observation_session={}
        self._state_lock=RLock()

    @observation_locked
    def reset(self):
        super().reset(); self.history.clear(); self.current.clear(); self.day_high.clear(); self.prior_high.clear()
        self.after_day_high.clear(); self.after_prior_high.clear()
        self.last_eval.clear(); self.last_gap_reason.clear()
        self._eval_keys.clear()
        self.observation_start.clear(); self.observation_session.clear()

    @observation_locked
    def ingest(self,t,received):
        if (not supported(t.time) and not after_supported(t.time)) or (not supported(received) and not after_supported(received)): return False
        old=self.last.get(t.symbol)
        if not super().ingest(t,received,preserve_observation=self.continuous_observation): return False
        if old and (t.time-old.time).total_seconds()>self.cfg.stale_seconds and not self.continuous_observation:
            self.history.pop(t.symbol,None); self.current.pop(t.symbol,None)
            self.last_gap_reason[t.symbol]='DISCONNECT_GAP'
            self.observation_start.pop(t.symbol,None)
        session=(t.time.date(),'AFTER' if after_supported(t.time) else 'REGULAR')
        if t.symbol not in self.observation_start or self.observation_session.get(t.symbol)!=session:
            self.observation_start[t.symbol]=t.time; self.observation_session[t.symbol]=session
        if t.time.strftime('%H:%M:%S')>='15:40:00':
            self.after_prior_high[t.symbol]=self.after_day_high.get(t.symbol,t.price)
            self.after_day_high[t.symbol]=max(t.price,self.after_day_high.get(t.symbol,t.price))
        else:
            self.prior_high[t.symbol]=self.day_high.get(t.symbol,t.price)
            self.day_high[t.symbol]=max(t.price,self.day_high.get(t.symbol,t.price))
        minute=t.time.replace(second=0,microsecond=0); b=self.current.get(t.symbol)
        if b and b['time']!=minute:
            if (minute-b['time']).total_seconds()==60:
                self.history.setdefault(t.symbol,deque(maxlen=30)).append(b)
            else:
                self.history.pop(t.symbol,None)
                self.last_gap_reason[t.symbol]='MISSING_MINUTE'
            b=None
        if b is None:
            start=self.observation_start.get(t.symbol)
            was_observing = start is not None and start <= minute
            gap_reason=None if (was_observing or t.time.second<=self.cfg.stale_seconds) else 'FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN'
            b={'time':minute,'open':t.price,'high':t.price,'low':t.price,'close':t.price,'volume':0,
               'first_second':0 if was_observing else t.time.second,'last_second':t.time.second,
               'first_trade_time':t.time.isoformat(),'last_trade_time':t.time.isoformat(),
               'tick_count':0,'coverage_start':minute.isoformat(),
               'coverage_end':(minute+timedelta(seconds=60)).isoformat(),
               'local_drop_count':0,'gap_reason':gap_reason}
            self.current[t.symbol]=b
        b['high']=max(b['high'],t.price); b['low']=min(b['low'],t.price); b['close']=t.price; b['volume']+=t.volume
        b['last_second']=t.time.second; b['last_trade_time']=t.time.isoformat(); b['tick_count']+=1
        return True

    def pullback(self,bars):
        return self.pullback_analysis(bars)[0]

    def pullback_analysis(self,bars):
        if len(bars)<4:return None,'INSUFFICIENT_USABLE_BARS',{}
        seq=bars[-12:]; peak_i=max(range(1,len(seq)-1),key=lambda i:seq[i]['high'])
        impulse=seq[:peak_i+1]; pull=seq[peak_i+1:]
        if not pull:return None,'NO_CONTROLLED_PULLBACK',{}
        peak=seq[peak_i]['high']; base=min(b['low'] for b in impulse); low=min(b['low'] for b in pull)
        rise=peak-base
        # An isolated opening/downward wick must not manufacture an upward impulse.
        # Use the existing minimum rise, now confirmed by completed closes as well.
        first_close=impulse[0]['close'];peak_close=impulse[-1]['close']
        # A genuine rise can be contained in the first fully observed candle.
        # Confirm from its open, rather than discarding that candle's real body.
        start_price=impulse[0].get('open',first_close)
        close_progress=peak_close/start_price-1 if start_price>0 else None
        evidence={'impulse_first_close':first_close,'impulse_peak_close':peak_close,
                  'impulse_start_price':start_price,'impulse_start_basis':'FIRST_COMPLETE_OPEN' if 'open' in impulse[0] else 'FIRST_CLOSE_FALLBACK',
                  'impulse_close_progress':close_progress,'minimum_rise':self.a.pullback_min_rise,
                  'wick_based_rise':rise,'impulse_base':base,'impulse_peak':peak}
        if close_progress is None or close_progress<self.a.pullback_min_rise:
            return None,'IMPULSE_CLOSE_PROGRESS_INSUFFICIENT',evidence
        if base<=0 or rise/base<self.a.pullback_min_rise or not 0<peak-low<=rise*self.a.pullback_max_retrace:
            return None,'NO_CONTROLLED_PULLBACK',evidence
        impulse_volume=sum(b['volume'] for b in impulse)/len(impulse)
        pull_volume=sum(b['volume'] for b in pull)/len(pull)
        if impulse_volume<=0 or pull_volume>impulse_volume*self.a.pullback_volume_ratio:return None,'PULLBACK_VOLUME_FAIL',evidence
        if low<=base:return None,'PULLBACK_INVALIDATION_FAIL',evidence
        rebreakout = min(peak, max(b['high'] for b in pull[-2:])) if len(pull) >= 2 else peak
        return {'pattern':'B','breakout':rebreakout,'peak':peak,'support':low,'projection':peak+rise,
                'pullback_volume_ratio':pull_volume/impulse_volume,'retrace':(peak-low)/rise,
                **evidence},None,evidence

    @observation_locked
    def usable_bars(self,symbol,after_only=False):
        valid=[]
        for bar in reversed(self.history.get(symbol,())):
            if bar.get('gap_reason') is not None or bar['first_second']>self.cfg.stale_seconds:break
            if after_only and bar['time'].strftime('%H:%M:%S')<'15:40:00':
                break
            valid.append(bar)
        return list(reversed(valid))

    @observation_locked
    def quality_summary(self,symbol,after_only=False):
        history=list(self.history.get(symbol,[]))
        if after_only:
            history=[b for b in history if b['time'].strftime('%H:%M:%S')>='15:40:00']
        usable=self.usable_bars(symbol,after_only=after_only)
        stored_count=len(history)
        usable_count=len(usable)
        diff_reason=None
        if stored_count>usable_count:
            inv_idx=stored_count-usable_count-1
            if 0<=inv_idx<stored_count:
                inv_bar=history[inv_idx]
                diff_reason=inv_bar.get('gap_reason') or ('FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN' if inv_bar.get('first_second',0)>self.cfg.stale_seconds else 'PARTIAL_SUBSCRIPTION_START')
        elif stored_count==0 and symbol in self.last_gap_reason:
            diff_reason=self.last_gap_reason[symbol]
        return {
            'stored_completed_bars':stored_count,
            'usable_completed_bars':usable_count,
            'difference_reason':diff_reason,
            'data_quality_status':'DATA_QUALITY_CHANGE_UNVERIFIED'
        }

    @observation_locked
    def observation_fresh(self,symbol,stamp):
        last=self.last.get(symbol)
        return bool(last and last.time.date()==stamp.date() and
                    0<=(stamp-last.time).total_seconds()<=self.cfg.stale_seconds)

    @observation_locked
    def forget(self,symbol):
        for mapping in (self.buffers,self.minutes,self.completed,self.last,self.history,self.current,self.day_high,self.prior_high,self.after_day_high,self.after_prior_high,self.last_eval,self.observation_start,self.observation_session):mapping.pop(symbol,None)
        self._eval_keys.pop(symbol,None)

    def handoff_from(self,source,symbol):
        if source is None or source is self:return False
        names=('history','buffers','current','minutes','completed','observation_start',
               'observation_session','day_high','prior_high','after_day_high',
               'after_prior_high','last','last_gap_reason')
        # Never hold both detector locks: promotion and demotion can run concurrently.
        with source._state_lock:
            if not any(symbol in getattr(source,key) for key in ('observation_start','history','buffers')):return False
            snapshot={key:(deque(source.buffers[symbol]) if key=='buffers' else
                           source.last[symbol] if key=='last' else deepcopy(getattr(source,key)[symbol]))
                      for key in names if symbol in getattr(source,key)}
        with self._state_lock:
            src_last=snapshot.get('last');tgt_last=self.last.get(symbol)
            if tgt_last and (src_last is None or (src_last.time,src_last.cumulative)<(tgt_last.time,tgt_last.cumulative)):return False
            src_bars=len(snapshot.get('history',()))
            tgt_bars=len(self.history.get(symbol,()))
            if tgt_bars>src_bars and src_last and self.observation_fresh(symbol,src_last.time):return False
            src_start=snapshot.get('observation_start');tgt_start=self.observation_start.get(symbol)
            if (src_last and self.observation_fresh(symbol,src_last.time) and src_start and tgt_start
                    and self.observation_session.get(symbol)==snapshot.get('observation_session')
                    and tgt_start<src_start and tgt_bars>=src_bars):return False
            # Copy the original time-bounded deque. A 300-tick count cap truncates
            # the 70-second baseline precisely when a candidate becomes busy.
            for key in names:
                mapping=getattr(self,key)
                if key in snapshot:mapping[symbol]=snapshot[key]
                else:mapping.pop(symbol,None)
            self.last_eval.pop(symbol,None);self._eval_keys.pop(symbol,None)
            return True

    def candidates(self,t,bars,is_after=False):
        setups=[self.pullback(bars),self.box(bars)]
        prior=(self.after_prior_high if is_after else self.prior_high).get(t.symbol)
        if prior and t.price>prior and len(bars)>=2:
            lookback=min(len(bars),4 if len(bars)>=4 else 3 if len(bars)>=3 else 2)
            support=min(b['low'] for b in bars[-lookback:])
            setups.append({'pattern':'A','breakout':prior,'support':support,'projection':prior+2*(prior-support)})
        return [s for s in setups if s]

    def target(self,t,watch,setup,limit):
        resistance=watch.get('resistance')
        # A/C/O가 신선한 실제 체결로 넘은 저항만 목표의 상한에서 제외한다.
        if setup['pattern'] in {'A','C','O'} and resistance is not None and t.price>resistance:resistance=None
        return min(resistance,setup['projection']) if resistance is not None else setup['projection']

    def opening_momentum(self, t, watch, buf=None):
        """Setup O (Opening Momentum):
        09:00:00~09:05:00 개장 초반 유효 상승세를 포착.
        완성 1분봉 2~5개를 기다리지 않고, 09:00 정규장 시작 이후의 실제 체결·호가·거래대금 흐름으로 평가.
        단순 1회 체결이나 급등률만으로 매수하지 않고:
        - 최소 3회 이상 체결
        - 최소 3초 이상 관측
        - 시가 대비 +0.3% 이상 상승 유지
        - 최근 틱 최고가 돌파/유지
        - 직전 틱 대비 가격 하락 되밀림 없음
        - 최소 거래대금 500만원 이상
        - 스프레드 정상 (<= max_spread)
        - 구조적 지지선(시가 및 관측 저점) 기반 손절선 산출
        """
        c = self.cfg
        if not getattr(self.a, 'setup_o_enabled', False): return None
        if after_supported(t.time): return None
        t_str = t.time.strftime('%H:%M:%S')
        end_time = getattr(self.a, 'setup_o_end_time', '09:05:00')
        if not ('09:00:00' <= t_str < end_time): return None
        if buf is None: buf = list(self.buffers.get(t.symbol, []))
        opening_ticks = [x for x in buf if x.regular and x.time.strftime('%H:%M:%S') >= '09:00:00']
        min_ticks = getattr(self.a, 'setup_o_min_ticks', 3)
        if len(opening_ticks) < min_ticks: return None
        elapsed = (t.time - opening_ticks[0].time).total_seconds()
        min_seconds = getattr(self.a, 'setup_o_min_seconds', 3.0)
        if elapsed < min_seconds: return None
        open_price = watch.get('open') or opening_ticks[0].price
        if open_price <= 0: return None
        min_gain = getattr(self.a, 'setup_o_min_gain_over_open', 0.003)
        if t.price < open_price * (1.0 + min_gain): return None
        highest_so_far = max(x.price for x in opening_ticks)
        if t.price < highest_so_far: return None
        if len(opening_ticks) >= 2 and opening_ticks[-1].price < opening_ticks[-2].price: return None
        opening_turnover = sum(x.price * x.volume for x in opening_ticks)
        min_turnover = getattr(self.a, 'setup_o_min_turnover', 5000000.0)
        if opening_turnover < min_turnover: return None
        if not (0 < t.bid <= t.ask): return None
        spread = (t.ask - t.bid) / t.price
        if spread > c.max_spread: return None
        support = min(open_price, min(x.price for x in opening_ticks))
        stop = floor_tick(support * (1.0 - c.structural_buffer))
        if stop >= t.price: return None
        distance = (t.price - stop) / t.price
        if distance > c.max_stop_fraction or distance < c.min_stop_fraction: return None
        breakout = open_price
        limit = floor_tick(min(t.ask, breakout * (1.0 + c.max_chase)))
        if limit < t.ask: return None
        projection = limit + 2.0 * (limit - stop)
        return {
            'pattern': 'O',
            'breakout': breakout,
            'support': support,
            'projection': projection,
            'open_price': open_price,
            'opening_turnover': opening_turnover,
            'opening_ticks_count': len(opening_ticks),
        }

    def box(self,bars):
        if len(bars)<self.a.box_min_bars: return None
        seq=bars[-self.a.box_min_bars:]; top=max(b['high'] for b in seq); low=min(b['low'] for b in seq)
        if not 0<(top-low)/low<=self.a.box_max_range: return None
        return {'pattern':'C','breakout':top,'support':low,'projection':top+2*(top-low),'box_width':top-low}

    def setup_trials(self,t,watch,gates=None,*,_bars=None):
        """A/B/C geometry is evaluated independently; only A requires acceleration."""
        is_after=after_supported(t.time)
        gates=self.base_gates(t,watch) if gates is None else gates
        bars=self.usable_bars(t.symbol,after_only=is_after) if _bars is None else _bars
        setups={s['pattern']:s for s in self.candidates(t,bars,is_after=is_after)}
        is_opening_o = (getattr(self.a, 'setup_o_enabled', False) and not is_after and '09:00:00' <= t.time.strftime('%H:%M:%S') < getattr(self.a, 'setup_o_end_time', '09:05:00'))
        if is_opening_o:
            o_setup = self.opening_momentum(t, watch)
            if o_setup: setups['O'] = o_setup
        _,b_reason,b_evidence=self.pullback_analysis(bars)
        trials=[]
        setups_to_test = ('A','B','C','O') if is_opening_o else ('A','B','C')
        for name in setups_to_test:
            setup=setups.get(name)
            required=0 if name=='O' else 2 if name=='A' else 4 if name=='B' else self.a.box_min_bars
            why=[]
            if not supported(t.time) and not is_after:why.append('OUT_OF_SUPPORTED_SESSION')
            if not setup:
                why.append('INSUFFICIENT_OPENING_DATA' if name=='O' else
                           'INSUFFICIENT_USABLE_BARS' if len(bars)<required else
                           'PRIOR_HIGH_NOT_BROKEN' if name=='A' else
                           b_reason if name=='B' else 'BOX_NOT_FORMED')
            price_gates=self.setup_gates(t,watch,setup,bars=bars) if setup else {}
            # Market-status authorization remains at the unchanged engine/order boundary.
            common={k:gates[k] for k in ('warmup','spread','structure')}
            if name=='O':
                buf=[x for x in list(self.buffers.get(t.symbol,[])) if x.regular and x.time.strftime('%H:%M:%S')>='09:00:00']
                min_ticks=getattr(self.a,'setup_o_min_ticks',3)
                min_sec=getattr(self.a,'setup_o_min_seconds',3.0)
                elapsed=(t.time-buf[0].time).total_seconds() if buf else 0
                turnover=sum(x.price*x.volume for x in buf)
                min_turnover=getattr(self.a,'setup_o_min_turnover',5000000.0)
                open_p=watch.get('open') or (buf[0].price if buf else t.price)
                common['warmup']=gate(len(buf)>=min_ticks and elapsed>=min_sec,ticks=len(buf),min_ticks=min_ticks,elapsed=elapsed,min_seconds=min_sec,setup='O')
                common['liquidity']=gate(turnover>=min_turnover,window_turnover=turnover,min_turnover=min_turnover,setup='O')
                common['structure']=gate(open_p>0 and t.price>=open_p,open_price=open_p,price=t.price,setup='O')
            elif is_after:
                common['liquidity']=gates['liquidity']
            else:
                threshold=self.cfg.min_window_turnover if name=='A' else self.cfg.min_bc_window_turnover
                amount=gates['liquidity']['values']['window_turnover']
                common['liquidity']=gate(amount>=threshold,window_turnover=amount,min_turnover=threshold,setup=name)
            trial_gates={**common,**price_gates}
            if name=='A':
                trial_gates['acceleration']=gates['acceleration']
                if bars and len(bars)>=2:
                    swing_low=min(b['low'] for b in bars)
                    extension=(t.price-swing_low)/swing_low if swing_low>0 else 0
                    if extension>0.07:
                        ratio=gates['acceleration']['values'].get('ratio')
                        min_ratio=gates['acceleration']['values'].get('min_ratio',self.cfg.min_turnover_ratio)
                        if ratio is not None and ratio<min_ratio*1.2:
                            trial_gates['acceleration']=gate(False,reason='LATE_STAGE_ACCELERATION_INSUFFICIENT',ratio=ratio,required_ratio=min_ratio*1.2)
            elif name=='O':
                min_gain=getattr(self.a,'setup_o_min_gain_over_open',0.003)
                open_p=watch.get('open') or (buf[0].price if buf else t.price)
                accel_ok=bool(open_p>0 and t.price>=open_p*(1+min_gain) and (len(buf)<2 or buf[-1].price>=buf[-2].price))
                trial_gates['acceleration']=gate(accel_ok,min_gain=min_gain,setup='O')
            failed=[k for k,v in trial_gates.items() if v['result']!='PASS']
            why.extend('ACCELERATION_FAIL' if k=='acceleration' else
                       'NO_REBREAKOUT' if k=='breakout' and name=='B' else
                       'BREAKOUT_NOT_REACHED' if k=='breakout' else k.upper()+'_FAIL' for k in failed)
            virtual=price_gates.get('invalidation',{}).get('values',{})
            trials.append({'setup':name,'evaluated':True,'candidate_generated':bool(setup),
                'acceleration_required':name in ('A','O'),'acceleration_observation':trial_gates.get('acceleration',gates['acceleration']),
                'failed_reasons':why,'primary_reason_code':why[0] if why else 'SETUP_PRICE_GATES_PASS',
                'gates':trial_gates,'setup_geometry':setup,
                'impulse_evidence':b_evidence if name=='B' else None,
                'entry_ready':bool(setup and not why),
                'price_action':{'condition':'OPENING_MOMENTUM' if name=='O' else 'CONTROLLED_PULLBACK' if name=='B' else 'INTRADAY_BOX' if name=='C' else 'PRIOR_HIGH_BREAKOUT',
                    'formed':bool(setup),'breakout':price_gates.get('breakout',gate(False,reason='GEOMETRY_ABSENT'))},
                'virtual_entry':virtual.get('entry'),'virtual_invalidation':virtual.get('stop')})
        return trials

    @observation_locked
    def signal(self,t,watch):
        # One evaluation owns its window and bars. Never cache live safety inputs across ticks.
        gates,bars,fast_count,base_count=self._base_evaluation(t,watch)
        trials=self.setup_trials(t,watch,gates,_bars=bars)
        from automation.market_context import symbol_context
        context=symbol_context(bars,t,watch.get('index_context'))
        context['indices']=watch.get('market_environment',{})
        for trial in trials:trial['context']=context
        self.last_eval[t.symbol]={'time':t.time,'cumulative':t.cumulative,'gates':gates,'trials':trials}
        self._eval_keys[t.symbol]=self._evaluation_key(t,watch)
        is_after=after_supported(t.time)
        if (not supported(t.time) and not is_after) or watch.get('status') not in {'WATCH','ELIGIBLE','RESERVE'}:return None
        if is_after:
            if watch.get('vi_active') or getattr(t,'vi_active',False) or watch.get('single_price_auction'): return None
            if watch.get('is_etf_etn') or watch.get('after_ineligible'): return None
        is_opening_o = (getattr(self.a, 'setup_o_enabled', False) and not is_after and '09:00:00' <= t.time.strftime('%H:%M:%S') < getattr(self.a, 'setup_o_end_time', '09:05:00'))
        if is_opening_o and len(bars) < 2:
            o_trial = next((x for x in trials if x['setup'] == 'O'), None)
            if o_trial and o_trial['entry_ready']:
                setup = o_trial['setup_geometry']
                price_gates = o_trial['gates']
                stop = o_trial['virtual_invalidation']
                limit = o_trial['virtual_entry']
                if limit >= t.ask and 0 < t.bid <= t.ask:
                    target = price_gates['risk_reward']['values']['target']
                    score = price_gates['economic']['values']['score']
                    gates.update({key: price_gates[key] for key in ('breakout', 'chase', 'invalidation', 'risk_reward', 'resistance', 'order_price', 'economic')})
                    gates['warmup'] = price_gates['warmup']
                    gates['liquidity'] = price_gates['liquidity']
                    gates['structure'] = price_gates['structure']
                    gates['acceleration'] = {**price_gates['acceleration'], 'required': True, 'scope': 'ENTRY_GATE'}
                    gates['pattern'] = gate(True, setup='O', acceleration_required=True)
                    venue = watch.get('venue') or 'KRX'
                    return {
                        'symbol': t.symbol, 'time': t.time.isoformat(), 'limit': limit, 'stop': stop, **setup,
                        'evaluation_gates': gates, 'setup_trials': trials, 'acceleration_required': True,
                        'market_context': context, 'discovery_context': watch.get('discovery_context'),
                        'decision_bar_snapshot': [{**{k: b.get(k) for k in ('open', 'high', 'low', 'close', 'volume')},
                            'time': b['time'].isoformat()} for b in bars[-12:]],
                        'target': target, 'target_basis': '개장 모멘텀 구조적 위험비 기반 가설; 고정 익절선 아님',
                        'turnover': price_gates['liquidity']['values'].get('window_turnover', 0),
                        'turnover_ratio': 1.0, 'gain': (t.price - setup.get('open_price', t.price)) / setup.get('open_price', t.price) if setup.get('open_price') else 0.0,
                        'efficiency': score, 'source_count': watch.get('source_count', len(watch.get('source_details', {}))),
                        'source_details': watch.get('source_details', {}), 'source_deltas': watch.get('source_deltas', {}),
                        'fallback_source1': bool(watch.get('fallback_source1')),
                        'structure': {k: watch.get(k) for k in ('long_position', 'overhead_share_estimate', 'resistance', 'recent_support')},
                        'risk_per_share': score['risk_per_share'], 'market_state': '3',
                        'session': 'REGULAR', 'venue': venue,
                        'planned_qty': None, 'liquidity_mode': 'turnover'
                    }
        # Keep warmup, turnover, spread and existing tick-window prerequisites.
        if any(gates[k]['result']!='PASS' for k in ('warmup','spread','structure')):return None
        c=self.cfg
        if fast_count<2 or not base_count or not 0<t.bid<=t.ask:return None
        turnover=gates['liquidity']['values']['window_turnover']
        ratio=gates['acceleration']['values']['ratio'];gain=gates['acceleration']['values']['gain']
        # Preserve the existing B, C, A precedence when more than one setup qualifies.
        for name in ('B','C','A'):
            trial=next(x for x in trials if x['setup']==name)
            if not trial['entry_ready']:continue
            setup=trial['setup_geometry']
            price_gates=trial['gates']
            stop=trial['virtual_invalidation'];limit=trial['virtual_entry']
            if limit<t.ask:continue
            target=price_gates['risk_reward']['values']['target']
            score=price_gates['economic']['values']['score']
            gates.update({key:price_gates[key] for key in ('breakout','chase','invalidation','risk_reward','resistance','order_price','economic')})
            gates['liquidity']=trial['gates']['liquidity']
            gates['acceleration']={**gates['acceleration'],'required':name=='A','scope':'ENTRY_GATE' if name=='A' else 'OBSERVATION_ONLY'}
            gates['pattern']=gate(True,setup=name,acceleration_required=name=='A')
            venue=watch.get('venue') or (getattr(c,'after_default_venue','SOR') if is_after else 'KRX')
            # Observation can establish only a one-share depth prerequisite.
            # Engine sizing and the final order boundary validate the actual quantity.
            planned_qty=1
            return {'symbol':t.symbol,'time':t.time.isoformat(),'limit':limit,'stop':stop,**setup,
                'evaluation_gates':gates,'setup_trials':trials,'acceleration_required':name=='A',
                'market_context':context,'discovery_context':watch.get('discovery_context'),
                'decision_bar_snapshot':[{**{k:b.get(k) for k in ('open','high','low','close','volume')},
                    'time':b['time'].isoformat()} for b in bars[-12:]],
                'target':target,'target_basis':'과거 저항으로 제한한 관측 패턴 폭 연장 가설; 고정 익절선 아님',
                'turnover':turnover,'turnover_ratio':ratio,'gain':gain,
                'efficiency':score,'source_count':watch.get('source_count',len(watch.get('source_details',{}))),'source_details':watch.get('source_details',{}),'source_deltas':watch.get('source_deltas',{}),
                'fallback_source1':bool(watch.get('fallback_source1')),
                'structure':{k:watch.get(k) for k in ('long_position','overhead_share_estimate','resistance','recent_support')},
                'risk_per_share':score['risk_per_share'],'market_state':'3',
                'session':'AFTER' if is_after else 'REGULAR','venue':venue,
                'planned_qty':None,'liquidity_mode':'depth' if is_after else 'turnover'}
        if is_opening_o:
            o_trial = next((x for x in trials if x['setup'] == 'O'), None)
            if o_trial and o_trial['entry_ready']:
                setup = o_trial['setup_geometry']
                price_gates = o_trial['gates']
                stop = o_trial['virtual_invalidation']
                limit = o_trial['virtual_entry']
                if limit >= t.ask and 0 < t.bid <= t.ask:
                    target = price_gates['risk_reward']['values']['target']
                    score = price_gates['economic']['values']['score']
                    gates.update({key: price_gates[key] for key in ('breakout', 'chase', 'invalidation', 'risk_reward', 'resistance', 'order_price', 'economic')})
                    gates['warmup'] = price_gates['warmup']
                    gates['liquidity'] = price_gates['liquidity']
                    gates['structure'] = price_gates['structure']
                    gates['acceleration'] = {**price_gates['acceleration'], 'required': True, 'scope': 'ENTRY_GATE'}
                    gates['pattern'] = gate(True, setup='O', acceleration_required=True)
                    venue = watch.get('venue') or 'KRX'
                    return {
                        'symbol': t.symbol, 'time': t.time.isoformat(), 'limit': limit, 'stop': stop, **setup,
                        'evaluation_gates': gates, 'setup_trials': trials, 'acceleration_required': True,
                        'market_context': context, 'discovery_context': watch.get('discovery_context'),
                        'decision_bar_snapshot': [{**{k: b.get(k) for k in ('open', 'high', 'low', 'close', 'volume')},
                            'time': b['time'].isoformat()} for b in bars[-12:]],
                        'target': target, 'target_basis': '개장 모멘텀 구조적 위험비 기반 가설; 고정 익절선 아님',
                        'turnover': price_gates['liquidity']['values'].get('window_turnover', 0),
                        'turnover_ratio': 1.0, 'gain': (t.price - setup.get('open_price', t.price)) / setup.get('open_price', t.price) if setup.get('open_price') else 0.0,
                        'efficiency': score, 'source_count': watch.get('source_count', len(watch.get('source_details', {}))),
                        'source_details': watch.get('source_details', {}), 'source_deltas': watch.get('source_deltas', {}),
                        'fallback_source1': bool(watch.get('fallback_source1')),
                        'structure': {k: watch.get(k) for k in ('long_position', 'overhead_share_estimate', 'resistance', 'recent_support')},
                        'risk_per_share': score['risk_per_share'], 'market_state': '3',
                        'session': 'REGULAR', 'venue': venue,
                        'planned_qty': None, 'liquidity_mode': 'turnover'
                    }
        return None

    @observation_locked
    def evaluation_gates(self,t,watch):
        last=self.last_eval.get(t.symbol,{})
        if last and self._eval_keys.get(t.symbol)==self._evaluation_key(t,watch): return last['gates']
        return self.base_gates(t,watch)

    @observation_locked
    def evaluation_snapshot(self,t,watch):
        """Own one diagnostic result across concurrent history handoffs.

        The caller writes logs outside the detector lock. No file I/O or
        broker operation may run while acquiring an observation snapshot.
        """
        signal=self.signal(t,watch)
        result=self.last_eval[t.symbol]
        return signal,deepcopy(result['gates']),deepcopy(result['trials']),deepcopy(list(self.history.get(t.symbol,())))

    @staticmethod
    def _evaluation_key(t,watch):
        # Frozen Tick includes price, volume, quote, depth and session inputs, not just trade time.
        return (t,watch.get('status'),watch.get('resistance'),watch.get('overhead_share_estimate'),
                repr(watch.get('market_status',{})),watch.get('resistance_room_pass'),
                len(watch.get('source_details',{})),watch.get('ask_qty'),watch.get('bid_qty'))

    def setup_gates(self,t,watch,setup,bars=None):
        c=self.cfg; breakout=setup['breakout']
        if setup['pattern']=='B' and t.price>=setup.get('peak',breakout):
            breakout=setup['peak']
        stop=floor_tick(setup['support']*(1-c.structural_buffer))
        limit=floor_tick(min(t.ask,breakout*(1+c.max_chase))); resistance=watch.get('resistance')
        target=self.target(t,watch,setup,limit)
        distance=(limit-stop)/limit if limit>0 else None
        rr=(target-limit)/(limit-stop) if limit>stop else None
        horizon=getattr(self.a,'horizon_'+setup['pattern'].lower()+'_minutes')
        score=efficiency(limit,stop,target,min(1.,0.4+0.1*len(watch.get('source_details',{}))),horizon,self.costs,self.a.min_efficiency_score)
        invalidation_pass=bool(distance is not None and 0<stop<limit and c.min_stop_fraction<=distance<=c.max_stop_fraction)
        if bars and setup['pattern']=='A' and invalidation_pass:
            swing_low=min(b['low'] for b in bars)
            extension=(t.price-swing_low)/swing_low if swing_low>0 else 0
            if extension>0.07:
                avg_span=sum(b['high']-b['low'] for b in bars[-min(len(bars),4):])/min(len(bars),4)
                if (limit-stop)<avg_span*1.1:
                    invalidation_pass=False
        return {'breakout':gate(t.price>breakout and limit>breakout,price=t.price,breakout=breakout,setup=setup['pattern'],
                ask=t.ask,entry=limit,reason='BREAKOUT_QUOTE_NOT_CONFIRMED' if t.price>breakout and limit<=breakout else None),
            'chase':gate(t.ask/breakout-1<=c.max_chase,chase=t.ask/breakout-1,max_chase=c.max_chase,ask=t.ask),
            'invalidation':gate(invalidation_pass if distance is not None else None,
                entry=limit,stop=stop,distance_pct=distance,min_pct=c.min_stop_fraction,max_pct=c.max_stop_fraction),
            'risk_reward':gate(None if rr is None else rr>=c.min_reward_risk,calculated_rr=rr,required_rr=c.min_reward_risk,target=target),
            'resistance':gate(watch.get('status') in {'ELIGIBLE','WATCH','RESERVE'} and (setup['pattern']!='B' or watch.get('resistance_room_pass') is not False),resistance_room_pass=watch.get('resistance_room_pass'),resistance=resistance,
                distance_pct=resistance/limit-1 if resistance is not None and limit>0 else None,min_room=c.min_resistance_room),
            'order_price':gate(limit>=t.ask,limit=limit,ask=t.ask),
            'economic':gate(score['decision']=='TRADE',score=score,tick_size=tick_size(t.price),tick_cost_fraction=tick_size(t.price)/t.price)}

    @observation_locked
    def base_gates(self,t,watch):
        return self._base_evaluation(t,watch)[0]

    def _base_evaluation(self,t,watch):
        """Per-call only: gates, completed bars, and original tick-window prerequisites."""
        c=self.cfg
        is_after=after_supported(t.time)
        buf=[x for x in list(self.buffers.get(t.symbol,[])) if x.time.strftime('%H:%M:%S')>='15:40:00'] if is_after else list(self.buffers.get(t.symbol,[]))
        bars=self.usable_bars(t.symbol,after_only=is_after)
        start=self.observation_start.get(t.symbol)
        elapsed=max(0,(t.time-start).total_seconds()) if start is not None else 0
        buffer_span=(t.time-buf[0].time).total_seconds() if buf else 0
        fast=[];base=[]
        for x in buf:
            age=(t.time-x.time).total_seconds()
            if 0<=age<c.acceleration_seconds:fast.append(x)
            elif c.acceleration_seconds<=age<c.baseline_seconds+c.acceleration_seconds:base.append(x)
        amount=sum(x.price*x.volume for x in fast); baseline=sum(x.price*x.volume for x in base)/c.baseline_seconds
        ratio=amount/c.acceleration_seconds/baseline if baseline else None
        gain=t.price/fast[0].price-1 if fast else None
        spread=(t.ask-t.bid)/t.price if 0<t.bid<=t.ask and t.price>0 else None
        min_ratio=getattr(c,'after_a_accel_ratio',2.5) if is_after else c.min_turnover_ratio
        min_gain=getattr(c,'after_a_min_gain',0.003) if is_after else c.min_price_gain
        accel_pass=None if ratio is None or gain is None else ratio>=min_ratio and gain>=min_gain
        accel_gate=gate(accel_pass,ratio=ratio,min_ratio=min_ratio,gain=gain,min_gain=min_gain,session='AFTER' if is_after else 'REGULAR')
        if is_after:
            planned_qty=1
            available=getattr(t,'depth_available',None)
            ask_qty=getattr(t,'ask_qty',0)
            bid_qty=getattr(t,'bid_qty',0)
            if available is None:  # Legacy synthetic Tick compatibility only.
                ask_qty=ask_qty or watch.get('ask_qty',0)
                bid_qty=bid_qty or watch.get('bid_qty',0)
                available=ask_qty>0 and bid_qty>0
            depth_pass=bool(ask_qty>=planned_qty and bid_qty>=planned_qty and ask_qty>0 and bid_qty>0 and spread is not None and spread<=c.max_spread) if available else None
            liq_gate=gate(depth_pass,mode='depth',planned_qty=planned_qty,ask_qty=ask_qty,bid_qty=bid_qty,
                quantity_scope='MINIMUM_OBSERVATION_ONLY',actual_quantity_check='ENGINE_AND_ORDER_BOUNDARY',
                depth_available=available,data_status='AVAILABLE' if available else 'DATA_UNAVAILABLE',
                depth_source=getattr(t,'depth_source','UNKNOWN'),book_time=t.book_time.isoformat() if getattr(t,'book_time',None) else None,
                window_turnover=amount,min_turnover=0)
        else:
            liq_gate=gate(amount>=c.min_window_turnover,window_turnover=amount,min_turnover=c.min_window_turnover)
        gates={'warmup':gate(elapsed>=c.baseline_seconds+c.acceleration_seconds and len(bars)>=2,elapsed_seconds=elapsed,observation_start=start.isoformat() if start else None,buffer_span_seconds=buffer_span,required_seconds=c.baseline_seconds+c.acceleration_seconds,completed_bars=len(bars)),
                'acceleration':accel_gate,
                'liquidity':liq_gate,
                'spread':gate(None if spread is None else spread<=c.max_spread,bid=t.bid,ask=t.ask,spread=spread,max_spread=c.max_spread),
                'structure':gate(watch.get('status') in {'WATCH','ELIGIBLE','RESERVE'},status=watch.get('status'),resistance=watch.get('resistance'),overhead_share=watch.get('overhead_share_estimate')),
                'market_status':gate(watch.get('market_status',{}).get('pass'),status=watch.get('market_status')),
                'pattern':gate(False,reason='완성봉/돌파/추격/무효화/효율 중 신호 미충족')}
        return gates,bars,len(fast),len(base)

