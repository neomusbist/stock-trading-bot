"""완성된 봉과 현재까지 수신한 틱만으로 A/B/C 모멘텀 지속 패턴을 판별한다."""
from datetime import datetime, timedelta
from collections import deque
from dataclasses import replace
from copy import deepcopy
from functools import wraps
from threading import RLock
import math
from core.strategy import Detector
from core.util import floor_tick,tick_size
from automation.allocation import efficiency
from automation.session import supported, after_supported
from automation.decisions import gate,tick_source_identity
from automation.price_context import PriceContext

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
        self.coverage_gaps={}
        self.observation_start={}; self.observation_session={}
        self._entry_geometry_cache={}
        self._bar_structure_cache={}
        self._setup_gates_cache={}
        self._state_lock=RLock()
        self.price_context=PriceContext(bar_limit=120 if auto.intraday_context_enabled or auto.structural_breakout_enabled else 30)

    @observation_locked
    def reset(self):
        super().reset(); self.history.clear(); self.current.clear(); self.day_high.clear(); self.prior_high.clear()
        self.after_day_high.clear(); self.after_prior_high.clear()
        self.last_eval.clear(); self.last_gap_reason.clear()
        self.coverage_gaps.clear()
        self._eval_keys.clear()
        self.observation_start.clear(); self.observation_session.clear()
        self._entry_geometry_cache.clear()
        self._bar_structure_cache.clear()
        self._setup_gates_cache.clear()

    @observation_locked
    def ingest(self,t,received):
        if (not supported(t.time) and not after_supported(t.time)) or (not supported(received) and not after_supported(received)): return False
        gap=self.coverage_gaps.get(t.symbol)
        if gap and received<=gap['at']:return False
        resuming=bool(gap and gap.get('pending'))
        old=self.last.get(t.symbol)
        if not super().ingest(t,received,preserve_observation=self.continuous_observation): return False
        if resuming:
            gap['pending']=False
            self.observation_start[t.symbol]=t.time
        if old and (t.time-old.time).total_seconds()>self.cfg.stale_seconds and not self.continuous_observation:
            self.history.pop(t.symbol,None); self.current.pop(t.symbol,None)
            self.last_gap_reason[t.symbol]='DISCONNECT_GAP'
            self.observation_start.pop(t.symbol,None)
        session=(t.time.date(),'AFTER' if after_supported(t.time) else 'REGULAR')
        if t.symbol not in self.observation_start or self.observation_session.get(t.symbol)!=session:
            self.observation_start[t.symbol]=t.time; self.observation_session[t.symbol]=session
        minute=t.time.replace(second=0,microsecond=0); b=self.current.get(t.symbol)
        if t.time.strftime('%H:%M:%S')>='15:40:00':
            self.after_prior_high.setdefault(t.symbol,t.price)
            if b and b['time']!=minute:
                self.after_prior_high[t.symbol]=self.after_day_high.get(t.symbol,t.price)
            self.after_day_high[t.symbol]=max(t.price,self.after_day_high.get(t.symbol,t.price))
        else:
            self.prior_high.setdefault(t.symbol,t.price)
            if b and b['time']!=minute:
                self.prior_high[t.symbol]=self.day_high.get(t.symbol,t.price)
            self.day_high[t.symbol]=max(t.price,self.day_high.get(t.symbol,t.price))
        had_minute_gap = False
        if b and b['time']!=minute:
            if (minute-b['time']).total_seconds()==60:
                # Prior confirmed references are distinct from consecutive signal bars.
                self.price_context.record(t.symbol,{**b,'venue':getattr(self.cfg,'after_default_venue','SOR') if t.time.strftime('%H:%M:%S')>=self.a.after_prep_start else 'KRX'},t.time,self.cfg.stale_seconds)
                self.history.setdefault(t.symbol,deque(maxlen=30)).append(b)
            else:
                had_minute_gap = True
                self.last_gap_reason[t.symbol]='MISSING_MINUTE'
                if self.continuous_observation:
                    self.price_context.record(t.symbol,{**b,'venue':getattr(self.cfg,'after_default_venue','SOR') if t.time.strftime('%H:%M:%S')>=self.a.after_prep_start else 'KRX'},t.time,self.cfg.stale_seconds)
                    self.history.setdefault(t.symbol,deque(maxlen=30)).append(b)
                else:
                    self.history.pop(t.symbol,None)
                    self.completed.pop(t.symbol,None)
            b=None
        if b is None:
            start=self.observation_start.get(t.symbol)
            was_observing = (start is not None and start <= minute) and not had_minute_gap
            gap_reason=None if (was_observing or t.time.second<=self.cfg.stale_seconds) else ('MISSING_MINUTE' if had_minute_gap else 'FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN')
            b={'time':minute,'open':t.price,'high':t.price,'low':t.price,'close':t.price,'volume':0,
               'first_second':0 if was_observing else t.time.second,'last_second':t.time.second,
               'first_trade_time':t.time.isoformat(),'last_trade_time':t.time.isoformat(),
               'tick_count':0,'coverage_start':minute.isoformat(),
               'coverage_end':(minute+timedelta(seconds=60)).isoformat(),
               'local_drop_count':0,'gap_reason':gap_reason}
            b['observation_source']='ACTUAL_RECEIVED'
            self.current[t.symbol]=b
        if resuming:
            # Retained bars are references, not evidence for an unobserved
            # interval. The first resumed minute is explicitly partial.
            b['gap_reason']=gap['reason']
            b['coverage_resumed_at']=received.isoformat()
        b['high']=max(b['high'],t.price); b['low']=min(b['low'],t.price); b['close']=t.price; b['volume']+=t.volume
        b['last_second']=t.time.second; b['last_trade_time']=t.time.isoformat(); b['tick_count']+=1
        capture=getattr(self,'accepted_tick_capture',None)
        if callable(capture):
            try:capture(t,received)
            except Exception:pass  # Diagnostic recording never interrupts protection/ingestion.
        return True

    @observation_locked
    def mark_coverage_gap(self,symbol,at,reason='SUBSCRIPTION_GAP'):
        """Invalidate entry continuity without deleting genuine completed bars."""
        prior=self.coverage_gaps.get(symbol)
        if prior and at<=prior['at']:return False
        self.coverage_gaps[symbol]={'at':at,'reason':reason,'pending':True}
        self.last_gap_reason[symbol]=reason
        # Keep the retained observation container stable for short detach/rejoin
        # handoffs, but never reuse pre-gap prints as an acceleration baseline.
        buffer=self.buffers.get(symbol)
        if buffer is not None:buffer.clear()
        for mapping in (self.minutes,self.completed):mapping.pop(symbol,None)
        current=self.current.get(symbol)
        if current is not None:current['gap_reason']=reason
        self.last_eval.pop(symbol,None);self._eval_keys.pop(symbol,None)
        self._bar_structure_cache.pop(symbol,None);self._setup_gates_cache.pop(symbol,None)
        return True

    def pullback(self,bars):
        return self.pullback_analysis(bars)[0]

    def sustained_turnover(self,t,minimum,current_amount):
        """Research alternative from accepted trades, never close*bar-volume."""
        rows=[x for x in self.buffers.get(t.symbol,()) if 0<=(t.time-x.time).total_seconds()<60]
        start=self.observation_start.get(t.symbol)
        known=bool(start and (t.time-start).total_seconds()>=60 and not self.coverage_gaps.get(t.symbol,{}).get('pending'))
        amount=sum(x.price*x.volume for x in rows)
        return bool(known and current_amount>=5000000 and amount>=minimum),dict(
            basis='ACTUAL_ACCEPTED_60_SECOND_TURNOVER',window_seconds=60,tick_count=len(rows),
            window_turnover=current_amount,minimum_current_turnover=5000000,
            sustained_turnover=amount,required_sustained_turnover=minimum,coverage_known=known)

    def pullback_analysis(self,bars):
        if len(bars)<4:return None,'INSUFFICIENT_USABLE_BARS',{}
        seq=bars[-min(len(bars),24):]
        # A complete opening candle can contain the impulse itself. Excluding
        # it relabelled a lower pullback high as the peak, changing both the
        # close-progress test and the volume denominator. Its actual open is
        # required; a missing open cannot establish an intrabar rise.
        from automation.structural_pullback import pullback_peak_index
        peak_i=pullback_peak_index(seq)
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
        retrace = (peak-low)/rise if rise>0 else 0
        evidence['pullback_retrace'] = retrace
        evidence['pullback_bars'] = len(pull)
        if getattr(self.a, 'chase_quality_filter', False):
            min_retrace = getattr(self.a, 'pullback_min_retrace', 0.20)
            if retrace < min_retrace or len(pull) < 2:
                return None, 'PULLBACK_SHALLOW_OR_UNCONFIRMED', evidence
            if len(bars) >= 10:
                observed_high = max(b['high'] for b in bars)
                # This bounded usable-bar window is not the exchange's day high.
                evidence.update(drawdown_reference_high=observed_high,
                                drawdown_reference_scope='USABLE_BAR_WINDOW',
                                drawdown_reference_bars=len(bars))
                max_drawdown = getattr(self.a, 'pullback_max_drawdown_from_high', 0.05)
                if observed_high > 0 and (observed_high - peak) / observed_high > max_drawdown:
                    return None, 'DOWNTREND_REBOUND_NOT_PULLBACK', evidence
                if len(bars) >= 15:
                    ma_window = min(len(bars), 20)
                    ma = sum(b['close'] for b in bars[-ma_window:]) / ma_window
                    if peak < ma and seq[-1]['close'] < ma:
                        return None, 'DOWNTREND_REBOUND_NOT_PULLBACK', evidence
        impulse_volume=sum(b['volume'] for b in impulse)/len(impulse)
        pull_volume=sum(b['volume'] for b in pull)/len(pull)
        if impulse_volume<=0 or pull_volume>impulse_volume*self.a.pullback_volume_ratio:return None,'PULLBACK_VOLUME_FAIL',evidence
        if low<=base:return None,'PULLBACK_INVALIDATION_FAIL',evidence
        touch = max(i for i, b in enumerate(pull) if b['low'] == low)
        recovery_pivot = max(low, pull[-1]['open'])
        breakout = recovery_pivot
        return {'pattern':'B','breakout':breakout,'peak':peak,'support':low,'projection':peak+rise,
                'pullback_volume_ratio':pull_volume/impulse_volume,'retrace':retrace,
                'recovery_trigger':recovery_pivot,'entry_basis':'SUPPORT_RECOVERY',
                **evidence},None,evidence

    @observation_locked
    def usable_bars(self,symbol,after_only=False):
        if self.coverage_gaps.get(symbol,{}).get('pending'):return []
        bars=list(self.history.get(symbol,()))
        if not bars:return []
        cur=self.current.get(symbol)
        if cur is not None:
            if (cur.get('gap_reason')=='MISSING_MINUTE' or cur.get('coverage_resumed_at')
                    or (cur['time']-bars[-1]['time']).total_seconds()!=60):
                return []
        valid=[]
        for bar in reversed(bars):
            if bar.get('gap_reason') is not None or bar.get('first_second',0)>self.cfg.stale_seconds:break
            if after_only and bar['time'].strftime('%H:%M:%S')<'15:40:00':
                break
            if valid and (valid[-1]['time']-bar['time']).total_seconds()!=60:
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
            cur=self.current.get(symbol)
            if usable_count==0 and cur is not None and (cur.get('gap_reason')=='MISSING_MINUTE' or (cur['time']-history[-1]['time']).total_seconds()!=60):
                diff_reason='MISSING_MINUTE'
            else:
                inv_idx=stored_count-usable_count-1
                if 0<=inv_idx<stored_count:
                    inv_bar=history[inv_idx]
                    if inv_bar.get('gap_reason'):
                        diff_reason=inv_bar['gap_reason']
                    elif inv_bar.get('first_second',0)>self.cfg.stale_seconds:
                        diff_reason='FIRST_TRADE_AFTER_5S_COVERAGE_UNKNOWN'
                    elif inv_idx+1<stored_count and (history[inv_idx+1]['time']-inv_bar['time']).total_seconds()!=60:
                        diff_reason='MISSING_MINUTE'
                    else:
                        diff_reason='PARTIAL_SUBSCRIPTION_START'
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
    def preload_from_context(self, symbol, asof):
        """PriceContext에 보관된 당일 확정 1분봉이 있고 history가 비어있을 때 유효 완성봉을 안전하게 사전 적재한다."""
        if not symbol or len(self.history.get(symbol, ())) > 0:
            return 0
        row = self.price_context.rows.get(symbol)
        if not row or not row.get('bars'):
            return 0
        # REST/reference bars may bound a target, but cannot stand in for
        # receiver-built signal bars or bridge a known subscription gap.
        gap=self.coverage_gaps.get(symbol)
        if gap:return 0
        bars = [b for b in row['bars'] if b.get('observation_source')=='ACTUAL_RECEIVED'
                and isinstance(b.get('time'), datetime) and b['time'].date() == asof.date() and b['time'] + timedelta(minutes=1) <= asof]
        if not bars:
            return 0
        valid_bars = []
        for i, b in enumerate(bars):
            if b.get('gap_reason') is not None or b.get('first_second', 0) > self.cfg.stale_seconds:
                valid_bars.clear()
                continue
            if i > 0 and valid_bars and (b['time'] - valid_bars[-1]['time']).total_seconds() != 60:
                valid_bars.clear()
            valid_bars.append({
                **deepcopy(b),
                'first_second': b.get('first_second', 0),
                'gap_reason': b.get('gap_reason')
            })
        if valid_bars:
            h = self.history.setdefault(symbol, deque(maxlen=30))
            for b in valid_bars:
                if b not in h:
                    h.append(b)
            return len(valid_bars)
        return 0

    @observation_locked
    def forget(self,symbol):
        for mapping in (self.buffers,self.minutes,self.completed,self.last,self.history,self.current,self.day_high,self.prior_high,self.after_day_high,self.after_prior_high,self.last_eval,self.observation_start,self.observation_session):mapping.pop(symbol,None)
        self._eval_keys.pop(symbol,None)
        self.coverage_gaps.pop(symbol,None)
        self._entry_geometry_cache.pop(symbol,None)
        self._bar_structure_cache.pop(symbol,None);self._setup_gates_cache.pop(symbol,None)

    def handoff_from(self,source,symbol):
        if source is None or source is self or not hasattr(getattr(source, '_state_lock', None), '__enter__'):return False
        names=('history','buffers','current','minutes','completed','observation_start',
               'observation_session','day_high','prior_high','after_day_high',
               'after_prior_high','last','last_gap_reason','coverage_gaps')
        # Never hold both detector locks: promotion and demotion can run concurrently.
        with source._state_lock:
            src_fast=getattr(source,'fast_strategy',None)
            fast_snap=src_fast.snapshot_tape(symbol) if src_fast is not None and hasattr(src_fast,'snapshot_tape') else None
            has_state=any(symbol in getattr(source,key) for key in ('observation_start','history','buffers'))
            has_ref=symbol in source.price_context.rows
            if not (has_state or has_ref or fast_snap):return False
            snapshot={key:(deque(source.buffers[symbol]) if key=='buffers' else
                           source.last[symbol] if key=='last' else deepcopy(getattr(source,key)[symbol]))
                      for key in names if symbol in getattr(source,key)}
            reference=PriceContext();reference.day=source.price_context.day
            if has_ref:
                reference.rows[symbol]=deepcopy(source.price_context.rows[symbol])
        with self._state_lock:
            own_gap=self.coverage_gaps.get(symbol)
            src_gap=snapshot.get('coverage_gaps')
            if own_gap and (src_gap is None or src_gap['at']<own_gap['at']):return False
            if has_ref:
                self.price_context.transfer(reference,symbol)
            tgt_fast=getattr(self,'fast_strategy',None)
            fast_restored=False
            if tgt_fast is not None and fast_snap is not None and hasattr(tgt_fast,'restore_tape'):
                fast_restored=tgt_fast.restore_tape(symbol,fast_snap)
            if not has_state:return bool(has_ref or fast_restored)
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
            self._bar_structure_cache.pop(symbol,None);self._setup_gates_cache.pop(symbol,None)
            return True

    def candidates(self,t,bars,is_after=False,venue=None):
        setups=[self.pullback(bars),self.box(bars)]
        prior=(self.after_prior_high if is_after else self.prior_high).get(t.symbol)
        if prior and t.price>prior and len(bars)>=2:
            lookback=min(len(bars),4 if len(bars)>=4 else 3 if len(bars)>=3 else 2)
            support=min(b['low'] for b in bars[-lookback:])
            setups.append({'pattern':'A','breakout':prior,'support':support,'projection':prior+2*(prior-support)})
        elif len(bars)>=3:
            base=bars[-3:]; level=max(b['high'] for b in base); base_low=min(b['low'] for b in base)
            rising_lows=base[0]['low']<base[1]['low']<base[2]['low']
            tight_base=base_low>0 and (level-base_low)/base_low<=0.025 and base[1]['low']>=base[0]['low']
            if (rising_lows or tight_base) and base[-1]['close']>=base[0]['open'] and t.price>level:
                support=min(b['low'] for b in base[-2:])
                if support>0 and level>support:
                    setups.append({'pattern':'A','breakout':level,'support':support,
                                   'projection':floor_tick(level+2.5*(level-base_low)),
                                   'entry_basis':'TREND_BREAKOUT_BASE'})
        elif self.a.structural_breakout_enabled:
            # The existing continuation path used to be unreachable below an
            # old session high. Require real received rising bars; references
            # can bound their local range, never manufacture signal warmup.
            from automation.structural_breakout import confirmed_continuation
            from automation.trade_quality import complete_sequence
            continued,proof=confirmed_continuation(bars,t.time,self.cfg.min_price_gain)
            if continued is True:
                venue=venue or (getattr(self.cfg,'after_default_venue','SOR') if is_after else 'KRX')
                references=self.price_context.reference_bars(t.symbol,t.time,venue)
                count=min(len(references),12)  # same existing pattern horizon
                base=complete_sequence(references,t.time,count,require_recent=True) if count>=3 else []
                if base:
                    local_high=max(b['high'] for b in base)
                    support=min(b['low'] for b in bars[-min(len(bars),4):])
                    if t.price>local_high and support<local_high:
                        setups.append({'pattern':'A','breakout':local_high,'support':support,
                            'projection':local_high+2*(local_high-support),
                            'entry_basis':'LOCAL_CONFIRMED_CONTINUATION',
                            'prior_session_high':prior,'continuation_evidence':proof,
                            'local_range_start':base[0]['time'].isoformat()})
        return [s for s in setups if s]

    def target(self,t,watch,setup,limit):
        resistance=watch.get('resistance')
        # A/C가 신선한 실제 체결로 넘은 저항만 목표의 상한에서 제외한다.
        if setup['pattern']=='A' and resistance is not None and limit>=resistance:resistance=None
        if setup['pattern']=='C' and resistance is not None and t.price>resistance:resistance=None
        return min(resistance,setup['projection']) if resistance is not None else setup['projection']

    def box(self,bars):
        if len(bars)<self.a.box_min_bars: return None
        seq=bars[-self.a.box_min_bars:]; top=max(b['high'] for b in seq); low=min(b['low'] for b in seq)
        if not 0<(top-low)/low<=self.a.box_max_range: return None
        return {'pattern':'C','breakout':top,'support':low,'projection':top+2*(top-low),'box_width':top-low}

    def _completed_bar_geometry(self,symbol,bars,tick_time=None):
        if len(bars)<2:
            return None,'INSUFFICIENT_USABLE_BARS',{},None,False,{}
        minute=tick_time.replace(second=0,microsecond=0) if isinstance(tick_time,datetime) else None
        key=(len(bars),bars[0]['time'],bars[-1]['time'],bars[-1]['close'],bars[-1]['high'],bars[-1]['low'],bars[-1]['volume'],
             minute,self.a.box_min_bars,self.a.box_max_range,self.a.pullback_min_rise,self.a.pullback_max_retrace,
             self.a.pullback_volume_ratio,getattr(self.a,'chase_quality_filter',False),
             getattr(self.a,'pullback_min_retrace',0.20),getattr(self.a,'pullback_max_drawdown_from_high',0.05),
             self.a.structural_breakout_enabled,self.cfg.min_price_gain)
        cached=self._bar_structure_cache.get(symbol)
        if cached is not None and cached[0]==key:
            _,b_setup,b_reason,b_evidence,c_setup,cont_ok,cont_proof=cached
        else:
            b_setup,b_reason,b_evidence=self.pullback_analysis(bars)
            c_setup=self.box(bars)
            cont_ok,cont_proof=False,{}
            if self.a.structural_breakout_enabled and minute is not None and len(bars)>=3:
                from automation.structural_breakout import confirmed_continuation
                cont_ok,cont_proof=confirmed_continuation(bars,tick_time,self.cfg.min_price_gain)
            self._bar_structure_cache[symbol]=(key,b_setup,b_reason,b_evidence,c_setup,cont_ok,cont_proof)
        return (dict(b_setup) if b_setup else None,b_reason,dict(b_evidence) if b_evidence else {},
                dict(c_setup) if c_setup else None,cont_ok,dict(cont_proof) if isinstance(cont_proof,dict) else cont_proof)

    def setup_trials(self,t,watch,gates=None,*,_bars=None,_setups=None,_only=None,_revision=True):
        """A/B/C geometry is evaluated independently; only A requires acceleration."""
        is_after=after_supported(t.time)
        gates=self.base_gates(t,watch) if gates is None else gates
        bars=self.usable_bars(t.symbol,after_only=is_after) if _bars is None else _bars
        venue=watch.get('venue') or (getattr(self.cfg,'after_default_venue','SOR') if is_after else 'KRX')
        b_setup,b_reason,b_evidence,c_setup,cont_ok,cont_proof=self._completed_bar_geometry(t.symbol,bars,t.time)
        if _setups is None:
            setups={}
            if b_setup:setups['B']=b_setup
            if c_setup:setups['C']=c_setup
            if len(bars)>=2:
                prior=(self.after_prior_high if is_after else self.prior_high).get(t.symbol)
                if prior and t.price>prior:
                    lookback=min(len(bars),4 if len(bars)>=4 else 3 if len(bars)>=3 else 2)
                    support=min(b['low'] for b in bars[-lookback:])
                    setups['A']={'pattern':'A','breakout':prior,'support':support,'projection':prior+2*(prior-support)}
                elif len(bars)>=3:
                    base=bars[-3:]; level=max(b['high'] for b in base); base_low=min(b['low'] for b in base)
                    rising_lows=base[0]['low']<base[1]['low']<base[2]['low']
                    tight_base=base_low>0 and (level-base_low)/base_low<=0.025 and base[1]['low']>=base[0]['low']
                    if (rising_lows or tight_base) and base[-1]['close']>=base[0]['open'] and t.price>level:
                        support=min(b['low'] for b in base[-2:])
                        if support>0 and level>support:
                            setups['A']={'pattern':'A','breakout':level,'support':support,
                                         'projection':floor_tick(level+2.5*(level-base_low)),
                                         'entry_basis':'TREND_BREAKOUT_BASE'}
                elif self.a.structural_breakout_enabled and cont_ok is True:
                    from automation.trade_quality import complete_sequence
                    references=self.price_context.reference_bars(t.symbol,t.time,venue)
                    count=min(len(references),12)
                    base=complete_sequence(references,t.time,count,require_recent=True) if count>=3 else []
                    if base:
                        local_high=max(b['high'] for b in base)
                        support=min(b['low'] for b in bars[-min(len(bars),4):])
                        if t.price>local_high and support<local_high:
                            setups['A']={'pattern':'A','breakout':local_high,'support':support,
                                'projection':local_high+2*(local_high-support),
                                'entry_basis':'LOCAL_CONFIRMED_CONTINUATION',
                                'prior_session_high':prior,'continuation_evidence':cont_proof,
                                'local_range_start':base[0]['time'].isoformat()}
        else:
            setups=_setups
        trials=[]
        for name in ('A','B','C'):
            if _only is not None and name!=_only:continue
            setup=setups.get(name)
            required=2 if name=='A' else 4 if name=='B' else self.a.box_min_bars
            why=[]
            if not supported(t.time) and not is_after:why.append('OUT_OF_SUPPORTED_SESSION')
            if not setup:
                why.append('INSUFFICIENT_USABLE_BARS' if len(bars)<required else
                           'PRIOR_HIGH_NOT_BROKEN' if name=='A' else
                           b_reason if name=='B' else 'BOX_NOT_FORMED')
            price_gates=self.setup_gates(t,watch,setup,bars=bars) if setup else {}
            # Market-status authorization remains at the unchanged engine/order boundary.
            common={k:gates[k] for k in ('warmup','spread','structure')}
            if is_after:
                common['liquidity']=gates['liquidity']
            else:
                threshold=self.cfg.min_window_turnover if name=='A' else self.cfg.min_bc_window_turnover
                amount=gates['liquidity']['values']['window_turnover']
                common['liquidity']=gate(amount>=threshold,window_turnover=amount,min_turnover=threshold,setup=name)
            trial_gates={**common,**price_gates}
            if setup and setup.get('entry_basis') in {'CONFIRMED_BREAKOUT_RETEST','CONFIRMED_SUPPORT_RECLAIM'}:
                prior=next((x for x in reversed(self.buffers.get(t.symbol,())) if x.cumulative<t.cumulative and x.time<=t.time),None)
                age=(t.time-prior.time).total_seconds() if prior else None
                trial_gates['reclaim_tick']=gate(None if prior is None else bool(0<=age<=self.cfg.stale_seconds and t.price>prior.price),
                    prior_time=prior.time.isoformat() if prior else None,prior_price=prior.price if prior else None,
                    prior_cumulative=prior.cumulative if prior else None,current_time=t.time.isoformat(),
                    current_price=t.price,current_cumulative=t.cumulative,maximum_age=self.cfg.stale_seconds,
                    reason='FRESH_ADVANCING_RECEIVED_PRINT_REQUIRED')
            continuation_candidate=False
            if name=='A':
                trial_gates['acceleration']=gates['acceleration']
                retest=bool(setup and setup.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST')
                if retest:
                    # Momentum was established by the completed breakout bar;
                    # after a held retest its decay is not a second burst veto.
                    # Current absolute liquidity, net RR and order checks stay.
                    trial_gates['acceleration']=gate(True,**gates['acceleration']['values'],
                        mode='CONFIRMED_BREAKOUT_RETEST',breakout_volume=setup['breakout_volume'],
                        base_volume_mean=setup['base_volume_mean'],breakout_bar_at=setup['breakout_bar_at'])
                local_continuation=bool(setup and setup.get('entry_basis')=='LOCAL_CONFIRMED_CONTINUATION')
                if not retest and self.a.structural_breakout_enabled and price_gates.get('breakout_structure',{}).get('result')=='PASS' and (gates['acceleration']['result']!='PASS' or local_continuation):
                    from automation.structural_breakout import confirmed_continuation
                    venue=watch.get('venue') or (getattr(self.cfg,'after_default_venue','SOR') if is_after else 'KRX')
                    sustained,proof=confirmed_continuation(bars if local_continuation else self.price_context.reference_bars(t.symbol,t.time,venue),t.time,self.cfg.min_price_gain)
                    if sustained is True:
                        continuation_candidate=True
                        # A price-defined continuation need not wait for three
                        # consecutively rising volume bars or a late 3x burst.
                        # Its existing absolute liquidity gate remains required.
                        # Use the existing fee/tax/slippage model once, rather
                        # than approving a gross-RR-only alternative entry.
                        score=price_gates.get('economic',{}).get('values',{}).get('score',{})
                        reward=score.get('reward_after_cost');risk=score.get('risk_per_share')
                        net_rr=reward/risk if isinstance(reward,(int,float)) and isinstance(risk,(int,float)) and risk>0 else None
                        cost_pass=net_rr is not None and net_rr>=self.cfg.min_reward_risk
                        trial_gates['continuation_cost_reward_risk']=gate(cost_pass,
                            calculated_net_rr=net_rr,required_rr=self.cfg.min_reward_risk,
                            basis='SAME_EXISTING_ECONOMIC_COST_MODEL_ONCE')
                        if cost_pass:
                            trial_gates['acceleration']=gate(True,**gates['acceleration']['values'],mode='CONFIRMED_CONTINUATION',continuation=proof,cost_reward_risk=net_rr)
                            if self.a.a_sustained_turnover_enabled and not is_after and trial_gates['liquidity']['result']!='PASS':
                                passed,liquidity=self.sustained_turnover(t,self.cfg.min_window_turnover,amount)
                                trial_gates['liquidity']=gate(passed,**liquidity,mode='CONFIRMED_CONTINUATION_SUSTAINED_TURNOVER',setup=name)
                if bars and len(bars)>=2:
                    swing_low=min(b['low'] for b in bars)
                    extension=(t.price-swing_low)/swing_low if swing_low>0 else 0
                    if extension>0.07 and trial_gates['acceleration'].get('values',{}).get('mode') not in {'CONFIRMED_CONTINUATION','CONFIRMED_BREAKOUT_RETEST'}:
                        ratio=gates['acceleration']['values'].get('ratio')
                        min_ratio=gates['acceleration']['values'].get('min_ratio',self.cfg.min_turnover_ratio)
                        if ratio is not None and ratio<min_ratio*1.2:
                            trial_gates['acceleration']=gate(False,reason='LATE_STAGE_ACCELERATION_INSUFFICIENT',ratio=ratio,required_ratio=min_ratio*1.2)
            elif name=='B' and self.a.b_sustained_turnover_enabled and not is_after and trial_gates['liquidity']['result']!='PASS' and price_gates.get('pullback_structure',{}).get('result')=='PASS' and price_gates.get('risk_reward',{}).get('result')=='PASS':
                passed,liquidity=self.sustained_turnover(t,self.cfg.min_bc_window_turnover,amount)
                trial_gates['liquidity']=gate(passed,**liquidity,mode='CONFIRMED_PULLBACK_SUSTAINED_TURNOVER',setup=name)
            if name!='A' and getattr(self.a, 'chase_quality_filter', False) and bars and len(bars)>=2:
                swing_low=min(b['low'] for b in bars)
                extension=(t.price-swing_low)/swing_low if swing_low>0 else 0
                if extension>0.07:
                    if name=='B' and setup and setup.get('retrace', 0)<0.30:
                        trial_gates['chase']=gate(False,reason='LATE_STAGE_EXTENSION_EXCEEDED',extension=extension,max_extension=0.07)
                    elif name=='C':
                        trial_gates['chase']=gate(False,reason='LATE_STAGE_EXTENSION_EXCEEDED',extension=extension,max_extension=0.07)
            failed=[k for k,v in trial_gates.items() if v['result']!='PASS']
            why.extend('ACCELERATION_FAIL' if k=='acceleration' else
                       trial_gates[k].get('values',{}).get('reason') if trial_gates[k].get('values',{}).get('reason') else
                       'RECOVERY_TRIGGER_NOT_REACHED' if k=='breakout' and name=='B' and setup and setup.get('entry_basis')=='SUPPORT_RECOVERY' else
                       'NO_REBREAKOUT' if k=='breakout' and name=='B' else
                       'BREAKOUT_NOT_REACHED' if k=='breakout' else k.upper()+'_FAIL' for k in failed)
            virtual=price_gates.get('invalidation',{}).get('values',{})
            continuation_entry=(name=='A' and trial_gates.get('acceleration',{}).get('values',{}).get('mode')=='CONFIRMED_CONTINUATION')
            retest_entry=(name=='A' and setup and setup.get('entry_basis')=='CONFIRMED_BREAKOUT_RETEST')
            trials.append({'setup':name,'evaluated':True,'candidate_generated':bool(setup),
                'acceleration_required':name=='A' and not (continuation_entry or retest_entry),'acceleration_observation':gates['acceleration'],
                'entry_confirmation_basis':'CONFIRMED_BREAKOUT_RETEST' if retest_entry else 'CONFIRMED_CONTINUATION' if continuation_entry else setup.get('entry_basis',name) if setup and name=='B' else 'ACCELERATED_BREAKOUT' if name=='A' else name,
                'candidate_confirmation_basis':'CONFIRMED_BREAKOUT_RETEST' if retest_entry else 'CONFIRMED_CONTINUATION' if continuation_candidate else 'ACCELERATED_BREAKOUT' if name=='A' else name,
                'failed_reasons':why,'primary_reason_code':why[0] if why else 'SETUP_PRICE_GATES_PASS',
                'gates':trial_gates,'setup_geometry':setup,
                'impulse_evidence':b_evidence if name=='B' else None,
                'entry_ready':bool(setup and not why),
                'price_action':{'condition':'CONTROLLED_PULLBACK' if name=='B' else 'INTRADAY_BOX' if name=='C' else 'PRIOR_HIGH_BREAKOUT',
                    'formed':bool(setup),'breakout':price_gates.get('breakout',gate(False,reason='GEOMETRY_ABSENT'))},
                'virtual_entry':virtual.get('entry'),'virtual_invalidation':virtual.get('stop')})
        if _revision and len(bars)>=2 and (self.a.a_breakout_retest_entry_enabled or self.a.b_support_reclaim_entry_enabled):
            # Geometry is immutable within one closed-bar prefix; quote/cost
            # gates are always recomputed. One cache per observed symbol.
            key=(t.time.replace(second=0,microsecond=0),self.a.a_breakout_retest_entry_enabled,self.a.b_support_reclaim_entry_enabled,
                 len(bars),bars[0]['time'],bars[-1]['time'],bars[-1]['close'],bars[-1]['high'],bars[-1]['low'],bars[-1]['volume'],bars[-1].get('gap_reason'))
            cached=self._entry_geometry_cache.get(t.symbol)
            if cached is None or cached[0]!=key:
                from automation.structural_breakout import breakout_retest_setup
                from automation.structural_pullback import support_reclaim_setup
                revised={}
                if self.a.a_breakout_retest_entry_enabled and self.a.structural_breakout_enabled:
                    revised['A']=breakout_retest_setup(bars,t.time,self.cfg)
                if self.a.b_support_reclaim_entry_enabled and self.a.structural_pullback_enabled:
                    revised['B']=support_reclaim_setup(bars,t.time,setups.get('B'))
                self._entry_geometry_cache[t.symbol]=(key,revised)
            else:revised=cached[1]
            for index,trial in enumerate(trials):
                name=trial['setup'];new=revised.get(name)
                if not new:continue
                if name=='A':new={**new,'prior_session_high':(self.after_prior_high if is_after else self.prior_high).get(t.symbol)}
                alternative=self.setup_trials(t,watch,gates,_bars=bars,_setups={name:new},_only=name,_revision=False)[0]
                if not trial['entry_ready'] and alternative['entry_ready']:
                    alternative['entry_alternatives']=[trial];trials[index]=alternative
                else:trial['entry_alternatives']=[alternative]
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
        if (not supported(t.time) and not is_after) or watch.get('status') not in {'WATCH','ELIGIBLE','RESERVE','SHADOW'}:return None
        if is_after:
            if watch.get('vi_active') or getattr(t,'vi_active',False) or watch.get('single_price_auction'): return None
            if watch.get('is_etf_etn') or watch.get('after_ineligible'): return None
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
            if 'entry_context' in price_gates:gates['entry_context']=price_gates['entry_context']
            if 'box_direction' in price_gates:gates['box_direction']=price_gates['box_direction']
            if 'box_origin' in price_gates:gates['box_origin']=price_gates['box_origin']
            if 'pullback_structure' in price_gates:gates['pullback_structure']=price_gates['pullback_structure']
            if 'breakout_structure' in price_gates:gates['breakout_structure']=price_gates['breakout_structure']
            if 'box_structure' in price_gates:gates['box_structure']=price_gates['box_structure']
            if 'reclaim_tick' in price_gates:gates['reclaim_tick']=price_gates['reclaim_tick']
            gates['liquidity']=trial['gates']['liquidity']
            required_acceleration=trial['acceleration_required']
            gates['acceleration']={**(trial['gates']['acceleration'] if name=='A' else gates['acceleration']),'required':required_acceleration,'scope':'ENTRY_GATE' if name=='A' else 'OBSERVATION_ONLY'}
            gates['pattern']=gate(True,setup=name,acceleration_required=required_acceleration)
            venue=watch.get('venue') or (getattr(c,'after_default_venue','SOR') if is_after else 'KRX')
            # Observation can establish only a one-share depth prerequisite.
            # Engine sizing and the final order boundary validate the actual quantity.
            planned_qty=1
            return {'symbol':t.symbol,'time':t.time.isoformat(),'limit':limit,'stop':stop,**setup,
                'breakout_level': setup.get('breakout'),
                'confirmed_overhead_resistance': watch.get('resistance'),
                'projected_target': setup.get('projection'),
                'economic_reward_reference': target,
                'receipt_evidence':tick_source_identity(t),
                'evaluation_gates':gates,'setup_trials':trials,'acceleration_required':required_acceleration,
                'entry_confirmation_basis':trial['entry_confirmation_basis'],
                'market_context':context,'discovery_context':watch.get('discovery_context'),
                'price_context':price_gates.get('entry_context',{}).get('values'),
                **({'reference_bar_snapshot':[{**{k:b[k] for k in ('open','high','low','close')},**({'volume':b['volume']} if 'volume' in b else {}),'time':b['time'].isoformat()}
                      for b in self.price_context.reference_bars(t.symbol,t.time,venue)],
                    'reference_received_at':self.price_context.rows.get(t.symbol,{}).get('history_received_at'),
                    'invalidation_policy':price_gates.get('breakout_structure',{}).get('values')}
                   if name=='A' and self.a.structural_breakout_enabled else {}),
                **({'invalidation_policy':price_gates['pullback_structure']['values']}
                   if name=='B' and self.a.structural_pullback_enabled and self.a.confirmed_invalidation_enabled else {}),
                **({'invalidation_policy':price_gates['box_structure']['values']}
                   if name=='C' and self.a.fresh_box_basis_enabled and self.a.confirmed_invalidation_enabled else {}),
                'decision_bar_snapshot':[{**{k:b.get(k) for k in ('open','high','low','close','volume')},
                    **({k:b[k] for k in ('first_second','gap_reason') if k in b} if self.a.structural_pullback_enabled else {}),
                    'time':b['time'].isoformat()} for b in bars[-(30 if self.a.structural_pullback_enabled else max(12,2*self.a.box_min_bars+2) if self.a.fresh_box_basis_enabled else 12):]],
                'target':target,'target_basis':'과거 저항으로 제한한 관측 패턴 폭 연장 가설; 고정 익절선 아님',
                **({'entry_economics':price_gates['risk_reward']['values']['economics'],
                    'entry_zone':price_gates['risk_reward']['values']['entry_zone']} if name in {'A','B'} else {}),
                'turnover':turnover,'turnover_ratio':ratio,'gain':gain,
                'efficiency':score,'source_count':watch.get('source_count',len(watch.get('source_details',{}))),'source_details':watch.get('source_details',{}),'source_deltas':watch.get('source_deltas',{}),
                'fallback_source1':bool(watch.get('fallback_source1')),
                'structure':{k:watch.get(k) for k in ('long_position','overhead_share_estimate','resistance','recent_support')},
                'risk_per_share':score['risk_per_share'],'market_state':'3',
                **({'SHADOW_ONLY':True,'actual_order_permission':False} if watch.get('status')=='SHADOW' else {}),
                'session':'AFTER' if is_after else 'REGULAR','venue':venue,
                'planned_qty':None,'liquidity_mode':'depth' if is_after else 'turnover'}
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
        gates_copy={k:dict(v) for k,v in result['gates'].items()}
        trials_copy=[{**tr,'failed_reasons':list(tr.get('failed_reasons',()))} for tr in result['trials']]
        history_copy=[dict(b) for b in self.history.get(t.symbol,())]
        return signal,gates_copy,trials_copy,history_copy

    @staticmethod
    def _evaluation_key(t,watch):
        # Frozen Tick includes price, volume, quote, depth and session inputs, not just trade time.
        return (t,watch.get('status'),watch.get('resistance'),watch.get('overhead_share_estimate'),
                repr(watch.get('market_status',{})),watch.get('resistance_room_pass'),
                len(watch.get('source_details',{})),watch.get('ask_qty'),watch.get('bid_qty'))

    def setup_gates(self,t,watch,setup,bars=None):
        c=self.cfg; breakout=setup['breakout']
        minute=t.time.replace(second=0,microsecond=0) if isinstance(getattr(t,'time',None),datetime) else None
        bars_key=(len(bars),bars[0]['time'],bars[-1]['time'],bars[-1]['close'],bars[-1]['high'],bars[-1]['low'],bars[-1].get('volume'),bars[-1].get('gap_reason')) if bars else ()
        pc_row=self.price_context.rows.get(t.symbol) if getattr(self,'price_context',None) is not None and self.price_context.day==t.time.date() else None
        pc_key=(len(pc_row['bars']),pc_row.get('last_complete_at'),pc_row.get('history_received_at'),len(pc_row['peaks']),pc_row['peaks'][-1].get('cleared_at') if pc_row['peaks'] else None) if pc_row else ()
        watch_key=(watch.get('status'),watch.get('resistance'),watch.get('resistance_room_pass'),watch.get('venue'),len(watch.get('source_details',{})))
        prior_key=(self.prior_high.get(t.symbol),self.after_prior_high.get(t.symbol))
        sg_key=(id(self.cfg),id(self.a),minute,after_supported(t.time),t.price,t.bid,t.ask,watch_key,prior_key,bars_key,pc_key,repr(setup))
        sym_sg=self._setup_gates_cache.setdefault(t.symbol,{})
        cached_sg=sym_sg.get(setup['pattern'])
        if cached_sg is not None and cached_sg[0]==sg_key:
            res_copy={k:dict(v) for k,v in cached_sg[1].items()}
            asof_iso=t.time.isoformat()
            for sk in ('pullback_structure','breakout_structure','box_structure'):
                if sk in res_copy and isinstance(res_copy[sk].get('values'),dict):
                    res_copy[sk]={**res_copy[sk],'values':{**res_copy[sk]['values'],'asof':asof_iso}}
            return res_copy
        if setup['pattern']=='B':
            breakout=setup.get('breakout',setup['support'])
        stop=floor_tick(setup['support']*(1-c.structural_buffer))
        limit=floor_tick(t.ask if setup['pattern'] in {'A','B'} else min(t.ask,breakout*(1+c.max_chase))); resistance=watch.get('resistance')
        target=self.target(t,watch,setup,limit)
        references=self.price_context.snapshot(t.symbol,t,limit) if self.a.entry_context_enabled else {}
        peak=references.get('nearest_confirmed_peak')
        original_target=target
        if self.a.entry_context_enabled and peak and peak['price'] > limit:
            if setup['pattern'] == 'B':
                if peak['price'] > setup.get('peak', limit):
                    target = min(target, peak['price'])
            elif setup['pattern'] == 'A' and setup.get('entry_basis') == 'TREND_BREAKOUT_BASE':
                if peak['price'] > setup.get('breakout', limit):
                    target = min(target, peak['price'])
            else:
                target = min(target, peak['price'])
        structural=None;structural_pass=None
        if self.a.structural_pullback_enabled and setup['pattern']=='B':
            from automation.structural_pullback import structural_pullback
            structural_pass,structural=structural_pullback(bars,t.time,setup,c,limit,t.bid,t.ask,close_policy=self.a.confirmed_invalidation_enabled,auto=self.a)
            if structural.get('buffered_stop') is not None:stop=structural.get('emergency_stop',structural['buffered_stop'])
            if structural.get('target_ceiling') is not None:target=min(target,structural['target_ceiling'])
        breakout_evidence=None;breakout_pass=None
        if self.a.structural_breakout_enabled and setup['pattern']=='A':
            from automation.structural_breakout import structural_breakout
            venue=watch.get('venue') or (getattr(c,'after_default_venue','SOR') if after_supported(t.time) else 'KRX')
            breakout_pass,breakout_evidence=structural_breakout(self.price_context.reference_bars(t.symbol,t.time,venue),t.time,setup,c,limit,t.bid,t.ask,auto=self.a,decision_bars=bars)
            if breakout_evidence.get('emergency_stop') is not None:stop=breakout_evidence['emergency_stop']
            if breakout_evidence.get('target_ceiling') is not None:
                # A wider invalidation basis can reduce position size, but must
                # not manufacture reward by raising the pattern's prior target.
                target=min(target,breakout_evidence['target_ceiling'])
                if resistance is not None and resistance>limit:target=min(target,resistance)
        box_evidence=None;box_pass=None
        if setup['pattern']=='C' and self.a.fresh_box_basis_enabled and self.a.confirmed_invalidation_enabled:
            from automation.structural_box import structural_box
            known_high=(self.after_prior_high if after_supported(t.time) else self.prior_high).get(t.symbol)
            setup={**setup,'prior_session_high':known_high}
            box_pass,box_evidence=structural_box(bars,t.time,setup,c,self.a,limit,t.bid,t.ask)
            if box_evidence.get('emergency_stop') is not None:stop=box_evidence['emergency_stop']
            if box_evidence.get('target_ceiling') is not None:target=min(target,box_evidence['target_ceiling'])
        distance=(limit-stop)/limit if limit>0 else None
        rr=(target-limit)/(limit-stop) if limit>stop else None
        gross_rr=rr;rr_basis='GROSS_PRICE_DISTANCE'
        economics=None;entry_zone=None
        if setup['pattern'] in {'A','B'}:
            from automation.capital import signal_economics,viable_entry_zone
            proof=breakout_evidence if setup['pattern']=='A' else structural
            valuation_signal=dict(pattern=setup['pattern'],limit=limit,stop=stop,target=target)
            if proof:valuation_signal['invalidation_policy']={'enabled':True,**proof}
            economics=signal_economics(c,self.a,valuation_signal)
            rr=economics.get('net_rr');rr_basis='AB_COMMON_NET_REWARD_RISK'
            is_recovery = (setup['pattern'] == 'B' and setup.get('entry_basis') == 'SUPPORT_RECOVERY')
            entry_zone=viable_entry_zone(economics,breakout,c.max_chase,is_recovery=is_recovery)
            entry_zone['structure_confirmed']=(breakout_pass is True if setup['pattern']=='A' and self.a.structural_breakout_enabled
                else structural_pass is True if setup['pattern']=='B' and self.a.structural_pullback_enabled else True)
        horizon=getattr(self.a,'horizon_'+setup['pattern'].lower()+'_minutes')
        score=efficiency(limit,stop,target,min(1.,0.4+0.1*len(watch.get('source_details',{}))),horizon,self.costs,self.a.min_efficiency_score,economics=economics)
        cost_risk=score.get('risk_per_share');cost_reward=score.get('reward_after_cost')
        net_rr=(cost_reward/cost_risk if isinstance(cost_risk,(int,float)) and cost_risk>0
                and isinstance(cost_reward,(int,float)) else None)
        invalidation_pass=bool(distance is not None and 0<stop<limit and c.min_stop_fraction<=distance<=c.max_stop_fraction)
        structure_sized=(breakout_pass is True or structural_pass is True and self.a.confirmed_invalidation_enabled or box_pass is True)
        if structure_sized:
            # Price distance is not account risk. The unchanged final risk gate
            # sizes from the emergency stop or refuses even a single share.
            invalidation_pass=bool(distance is not None and 0<stop<limit and distance>=c.min_stop_fraction)
        if getattr(self.a, 'chase_quality_filter', False) and distance is not None and limit > 0:
            min_dist = getattr(self.a, 'min_stop_distance_fraction', 0.012)
            step = tick_size(limit)
            required_ticks = math.ceil((limit * min_dist) / step)
            min_stop_distance_krw = required_ticks * step
            if (limit - stop) < min_stop_distance_krw:
                invalidation_pass = False
        if bars and invalidation_pass:
            swing_low=min(b['low'] for b in bars)
            extension=(t.price-swing_low)/swing_low if swing_low>0 else 0
            if setup['pattern']=='A' and extension>0.07:
                avg_span=sum(b['high']-b['low'] for b in bars[-min(len(bars),4):])/min(len(bars),4)
                if (limit-stop)<avg_span*1.1:
                    invalidation_pass=False
            elif getattr(self.a, 'chase_quality_filter', False) and extension>0.07:
                if setup['pattern']=='B' and setup.get('retrace', 0)<0.30:
                    invalidation_pass=False
                elif setup['pattern']=='C':
                    invalidation_pass=False
        peak_val = setup.get('peak', float('inf'))
        b_over_peak = (setup['pattern'] == 'B' and limit >= peak_val)
        if setup['pattern'] == 'B' and setup.get('entry_basis') == 'SUPPORT_RECOVERY':
            breakout_pass = (t.price >= breakout and not b_over_peak)
            breakout_reason = ('PEAK_ALREADY_REACHED' if b_over_peak else
                               'RECOVERY_TRIGGER_NOT_REACHED' if t.price < breakout else None)
        else:
            breakout_pass = (t.price > breakout and limit > breakout and not b_over_peak)
            breakout_reason = ('PEAK_ALREADY_REACHED' if b_over_peak else
                               'BREAKOUT_QUOTE_NOT_CONFIRMED' if t.price > breakout and limit <= breakout else None)
        result={'breakout':gate(breakout_pass,price=t.price,breakout=breakout,setup=setup['pattern'],
                ask=t.ask,entry=limit,reason=breakout_reason),
            'chase':gate(t.ask/breakout-1<=c.max_chase,chase=t.ask/breakout-1,max_chase=c.max_chase,ask=t.ask),
            'invalidation':gate(invalidation_pass if distance is not None else None,
                entry=limit,stop=stop,distance_pct=distance,min_pct=c.min_stop_fraction,
                max_pct=None if structure_sized else c.max_stop_fraction,
                **({'upper_bound_basis':'ACCOUNT_RISK_AND_INTEGER_QUANTITY'} if structure_sized else {})),
            'risk_reward':gate(economics['rr_pass'] if economics is not None else None if rr is None else rr>=c.min_reward_risk,calculated_rr=rr,required_rr=c.min_reward_risk,target=target,
                rr_basis=rr_basis,gross_rr=gross_rr,net_rr=net_rr,
                **({'economics':economics,'entry_zone':entry_zone,'target_inputs':dict(pattern_projection=setup['projection'],
                    original_target=original_target,radar_resistance=resistance,structural_ceiling=(proof or {}).get('target_ceiling'),
                    confirmed_overhead=(proof or {}).get('nearest_confirmed_peak'),b_peak=setup.get('peak'),
                    b_peak_cap_enabled=setup['pattern']=='B' and not self.a.pullback_expansion_target_enabled)} if economics is not None else {})),
            'resistance':gate(watch.get('status') in {'ELIGIBLE','WATCH','RESERVE'} and (setup['pattern']!='B' or watch.get('resistance_room_pass') is not False),resistance_room_pass=watch.get('resistance_room_pass'),resistance=resistance,
                distance_pct=resistance/limit-1 if resistance is not None and limit>0 else None,min_room=c.min_resistance_room),
            'order_price':gate(limit>=t.ask,limit=limit,ask=t.ask),
            'economic':gate(score['decision']=='TRADE',score=score,tick_size=tick_size(t.price),tick_cost_fraction=tick_size(t.price)/t.price)}
        if self.a.entry_context_enabled:
            result['entry_context']=gate(rr is not None and rr>=c.min_reward_risk,
                enabled=True,reason='CONFIRMED_INTRADAY_RESISTANCE_RR' if peak and (rr is None or rr<c.min_reward_risk) else None,
                original_target=original_target,target=target,required_rr=c.min_reward_risk,calculated_rr=rr,**references)
        if structural is not None:result['pullback_structure']=gate(structural_pass,enabled=True,**structural)
        if breakout_evidence is not None:result['breakout_structure']=gate(breakout_pass,enabled=True,**breakout_evidence)
        if box_evidence is not None:result['box_structure']=gate(box_pass,enabled=True,**box_evidence)
        if self.a.fresh_box_basis_enabled and setup['pattern']=='C':
            from automation.trade_quality import fresh_box_basis
            passed,evidence=fresh_box_basis(bars,t.time,self.a.box_min_bars,self.a.box_max_range)
            result['box_origin']=gate(passed,enabled=True,**evidence)
        if self.a.confirmed_box_direction and setup['pattern']=='C':
            from automation.trade_quality import box_direction
            passed,evidence=box_direction(bars,t.time,self.a.box_min_bars)
            result['box_direction']=gate(passed,enabled=True,**evidence)
        sym_sg[setup['pattern']]=(sg_key,result)
        return result

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
        fast=[];base=[];window_ticks=[]
        for x in buf:
            age=(t.time-x.time).total_seconds()
            if 0<=age<c.acceleration_seconds:fast.append(x)
            elif c.acceleration_seconds<=age<c.baseline_seconds+c.acceleration_seconds:base.append(x)
            if 0<=age<c.baseline_seconds:window_ticks.append(x)
        amount=sum(x.price*x.volume for x in fast); baseline=sum(x.price*x.volume for x in base)/c.baseline_seconds
        window_amount=sum(x.price*x.volume for x in window_ticks)
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
            liq_gate=gate(window_amount>=c.min_window_turnover,window_turnover=window_amount,min_turnover=c.min_window_turnover)
        gates={'warmup':gate(elapsed>=c.baseline_seconds+c.acceleration_seconds and len(bars)>=2,elapsed_seconds=elapsed,observation_start=start.isoformat() if start else None,buffer_span_seconds=buffer_span,required_seconds=c.baseline_seconds+c.acceleration_seconds,completed_bars=len(bars)),
                'acceleration':accel_gate,
                'liquidity':liq_gate,
                'spread':gate(None if spread is None else spread<=c.max_spread,bid=t.bid,ask=t.ask,spread=spread,max_spread=c.max_spread),
                'structure':gate(watch.get('status') in {'WATCH','ELIGIBLE','RESERVE','SHADOW'},status=watch.get('status'),resistance=watch.get('resistance'),overhead_share=watch.get('overhead_share_estimate')),
                'market_status':gate(watch.get('market_status',{}).get('pass'),status=watch.get('market_status')),
                'pattern':gate(False,reason='완성봉/돌파/추격/무효화/효율 중 신호 미충족')}
        return gates,bars,len(fast),len(base)

