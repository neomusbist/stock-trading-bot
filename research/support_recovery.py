"""Second sequential hypothesis: received micro recovery at a closed pullback.

The target and support are independent known prices. Three actual post-touch
prints and an improving fresh bid confirm defense; no recovery-bar completion
or target expansion. Shares/cost/freshness/permission use the first model.
"""
from collections import deque
from datetime import timedelta
import hashlib,json
from statistics import median
from automation.capital import entry_economics,viable_entry_zone
from automation.entry_context_audit import confirmed_overhead
from core.market_evidence import aware,bar_evidence,known_signal_bars
from core.market_time import tick_freshness,book_freshness
from core.util import floor_tick,tick_size
from research.initial_structure import InitialBaseResearch

class SupportRecoveryResearch(InitialBaseResearch):
    name='SUPPORT_RECOVERY_RESEARCH'
    def __init__(self,cfg,auto,*,noise_policy='three_bar_range'):
        if noise_policy not in {'three_bar_range','received_prefix'}:raise ValueError('UNKNOWN_NOISE_POLICY')
        super().__init__(cfg,auto);self.recent={};self.prefix={};self.noise_policy=noise_policy
        if noise_policy=='received_prefix':self.name='SUPPORT_RECOVERY_PREFIX_RESEARCH'
    def reset(self,symbol):
        super().reset(symbol);self.recent.pop(symbol,None);self.prefix.pop(symbol,None)
    def evaluate(self,t,bars,references,asof,**kwargs):
        if t.regular and tick_freshness(t,asof,self.cfg.stale_seconds)['fresh']:
            tape=self.recent.setdefault(t.symbol,deque(maxlen=3))
            if not tape or t.cumulative>tape[-1]['cumulative']:
                row={'time':t.time,'known_at':asof,'price':t.price,'bid':t.bid,'ask':t.ask,'cumulative':t.cumulative,
                    'received_at':t.received_at,'quote_known_at':asof,
                    'quote_fresh':book_freshness(t,asof,self.cfg.stale_seconds)['fresh']}
                tape.append(row)
                if self.noise_policy=='received_prefix':
                    prefix=self.prefix.setdefault(t.symbol,deque());prefix.append(dict(row))
                    while prefix and (asof-prefix[0]['known_at']).total_seconds()>65:prefix.popleft()
            elif t.cumulative==tape[-1]['cumulative']:
                # Quote knowledge must never move the received trade backwards or forwards.
                tape[-1].update(bid=t.bid,ask=t.ask,quote_known_at=asof,
                    quote_fresh=book_freshness(t,asof,self.cfg.stale_seconds)['fresh'])
        return super().evaluate(t,bars,references,asof,**kwargs)

    def prefix_noise(self,symbol,end,support):
        """Pretrigger observed one-jump/spread envelope; NOT a volatility forecast.

        Missing coverage is UNKNOWN. No percentile, RR-fitted multiplier, or
        bar interpolation. The structural invalidation is unaffected.
        """
        rows=[r for r in self.prefix.get(symbol,()) if end-timedelta(seconds=60)<=r['known_at']<=end]
        detail={'policy':'PRETRIGGER_60S_MAX_RECEIVED_JUMP_AND_PRINT_QUOTE_SPREAD',
            'cutoff':end.isoformat(),'actual_trade_rows':len(rows),'forecast_validated':False}
        if len(rows)<3:return None,{**detail,'reason':'PREFIX_TOO_FEW_ACTUAL_PRINTS'}
        span=(rows[-1]['known_at']-rows[0]['known_at']).total_seconds()
        gaps=[(b['known_at']-a['known_at']).total_seconds() for a,b in zip(rows,rows[1:])]
        detail.update(first=rows[0]['known_at'].isoformat(),last=rows[-1]['known_at'].isoformat(),
            span_seconds=span,max_receipt_gap_seconds=max(gaps))
        if span<60-self.cfg.stale_seconds or max(gaps)>self.cfg.stale_seconds:
            return None,{**detail,'reason':'PREFIX_COVERAGE_INCOMPLETE'}
        if any(not r['quote_fresh'] or not 0<r['bid']<=r['ask'] for r in rows):
            return None,{**detail,'reason':'PREFIX_QUOTE_HISTORY_UNKNOWN'}
        jump=max(abs(b['price']-a['price']) for a,b in zip(rows,rows[1:]))
        spread=max(r['ask']-r['bid'] for r in rows)
        detail.update(max_adjacent_price_jump=jump,max_observed_print_quote_spread=spread)
        return max(jump,spread,tick_size(support)),detail
    def plan(self,t,bars,references,asof,input_revision=None):
        def fail(reason,status='REJECT',**kw):return {'status':status,'stage':'STRUCTURE','reason':reason,**kw}
        base,reason=known_signal_bars(list(bars)[-3:],asof,3)
        if reason:return fail(reason,'UNKNOWN')
        touch=base[-1];support=touch['low']
        if not (support<base[-2]['low'] and touch['close']<base[-2]['close']):
            return fail('NO_NEW_CLOSED_PULLBACK_TOUCH')
        tape=list(self.recent.get(t.symbol,()))
        if len(tape)<3:return fail('POST_TOUCH_RECEIVED_PATH_INSUFFICIENT','UNKNOWN')
        cached=self.cache.get(t.symbol);pending=self.pending.get(t.symbol)
        if (cached and cached.get('touch_at')==touch['time'].isoformat() and pending
                and 0<=(asof-aware(pending['trigger_at'])).total_seconds()<=self.cfg.stale_seconds
                and t.price>cached['level']):return cached
        left,dip,recovered=tape
        if (left['time']<touch['time']+timedelta(minutes=1)
                or not 0<=(recovered['time']-left['time']).total_seconds()<=self.cfg.stale_seconds):
            return fail('POST_TOUCH_PATH_NOT_CURRENT','UNKNOWN')
        if min(x['price'] for x in tape)<support:return fail('RECEIVED_SUPPORT_BROKEN')
        if not (left['price']>dip['price'] and recovered['price']>left['price']
                and recovered['bid']>dip['bid'] and dip['quote_fresh'] and recovered['quote_fresh']):
            return fail('NO_RECEIVED_DIP_RECOVERY_AND_BID_IMPROVEMENT')
        level=left['price']
        valid=[]
        for b in references:
            ev=bar_evidence(b)
            if ev['valid'] and ev['available_at']<=left['known_at']:valid.append({**b,'time':ev['start']})
        peak=confirmed_overhead(valid,left['known_at'],level)
        if not peak:return fail('NO_PREKNOWN_OVERHEAD_ABOVE_RECOVERY','UNKNOWN',level=level)
        pivot=aware(peak['time']);trio=[b for b in valid if pivot-timedelta(minutes=1)<=b['time']<=pivot+timedelta(minutes=1)]
        rising=(len(trio)==3 and trio[1]['high']>trio[0]['high'] and trio[1]['close']>trio[0]['close']
                and support>min(b['low'] for b in trio[:2]))
        if not rising:return fail('PREKNOWN_UPLEG_NOT_DEFENDED')
        target=floor_tick(peak['price']);buffer=max(tick_size(support),support*self.cfg.structural_buffer)
        structural=floor_tick(support-buffer)
        spread=t.ask-t.bid if 0<t.bid<=t.ask else 0
        noise=max(median(b['high']-b['low'] for b in base),spread,tick_size(support))
        noise_evidence={'policy':'UNCHANGED_MEDIAN_THREE_RECEIVED_BAR_RANGES'}
        if self.noise_policy=='received_prefix':
            noise,noise_evidence=self.prefix_noise(t.symbol,left['known_at'],support)
            if noise is None:return fail(noise_evidence['reason'],'UNKNOWN',noise_evidence=noise_evidence,
                level=level,support=support,structural_stop=structural,target=target,target_evidence=peak,
                micro_left_at=left['known_at'].isoformat(),touch_at=touch['time'].isoformat(),
                emergency_stop=None,price_zone='UNKNOWN_EMERGENCY_NOISE_NOT_ESTABLISHED')
        emergency=floor_tick(structural-noise)
        key=hashlib.sha256(json.dumps([self.name,t.symbol,touch['time'].isoformat(),level,structural,emergency,peak],sort_keys=True).encode()).hexdigest()[:24]
        economics=entry_economics(level+tick_size(level),structural,emergency,target,self.costs,self.cfg.min_reward_risk)
        economics['entry_basis']='UNOBSERVED_FIRST_TRIGGER_TICK_FEASIBILITY_PROBE'
        zone=viable_entry_zone(economics,level,self.cfg.max_chase)
        result={'status':'KNOWN' if zone.get('trigger_compatible_zone') else 'REJECT',
            'stage':'ECONOMIC_FEASIBILITY','reason':'COST_ZONE_EXISTS' if zone.get('trigger_compatible_zone') else 'COST_ZONE_EMPTY_BEFORE_TRIGGER',
            'structure_id':key,'first_structure_at':asof.isoformat(),'touch_at':touch['time'].isoformat(),
            'level':level,'support':support,'structural_stop':structural,'emergency_stop':emergency,
            'noise_band':noise,'noise_basis':noise_evidence['policy'],'noise_evidence':noise_evidence,
            'target':target,'target_evidence':{**peak,'available_at':max(aware(b['available_at']) for b in trio).isoformat(),
                'source':'PREKNOWN_UPLEG_PEAK_BEFORE_MICRO_RECOVERY'},
            'base_times':[b['time'].isoformat() for b in base],'formation_economics':economics,'entry_zone':zone,
            'micro_defense':{'rows':[{**x,'time':x['time'].isoformat(),'known_at':x['known_at'].isoformat(),
                'quote_known_at':x['quote_known_at'].isoformat()} for x in tape],
                'aggressor_buy_volume':'UNKNOWN_PRICE_AND_BID_RECOVERY_ONLY'},
            'structure_prices_frozen':True,'actual_order_permission':False}
        self.cache[t.symbol]=result
        if result['status']=='KNOWN':
            self.pending[t.symbol]={'structure_id':key,'trigger_at':asof.isoformat(),'trade_received_at':t.received_at,
                'prior_time':dip['time'].isoformat(),'prior_price':dip['price'],'prior_cumulative':dip['cumulative'],
                'trigger_price':t.price,'trigger_cumulative':t.cumulative}
        return result
