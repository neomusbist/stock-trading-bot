"""Independent, inactive initial-base hypothesis. No production runner imports.

Order: known overhead -> local invalidation -> observed noise -> cost zone ->
actual crossing -> fresh L1 -> common integer sizing. No R-derived projection.
"""
from datetime import timedelta
import hashlib,json
from statistics import median
from automation.capital import Costs,entry_economics,viable_entry_zone,entry_quantity
from automation.entry_context_audit import confirmed_overhead
from automation.allocation import efficiency
from core.market_evidence import aware,bar_evidence,known_signal_bars
from core.market_time import tick_freshness,book_freshness
from core.util import floor_tick,tick_size


class InitialBaseResearch:
    name='INITIAL_BASE_RESEARCH'
    def __init__(self,cfg,auto):
        self.cfg=cfg;self.auto=auto
        self.costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,
                         cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
        self.cache={};self.previous={};self.pending={}
    def reset(self,symbol):
        for mapping in (self.cache,self.previous,self.pending):mapping.pop(symbol,None)

    def plan(self,t,bars,references,asof,input_revision=None):
        def fail(reason,status='REJECT',**kw):
            return {'status':status,'stage':'STRUCTURE','reason':reason,**kw}
        base,reason=known_signal_bars(list(bars)[-3:],asof,3)
        if reason:return fail(reason,'UNKNOWN')
        if not (base[0]['low']<=base[1]['low']<=base[2]['low'] and base[2]['close']>base[0]['close']):
            return fail('NO_LOCAL_RISING_SUPPORT')
        cached=self.cache.get(t.symbol)
        if input_revision is not None and cached and cached.get('input_revision')==input_revision:
            return cached
        level=max(b['high'] for b in base);support=min(b['low'] for b in base)
        valid=[]
        for b in references:
            ev=bar_evidence(b)
            if ev['valid'] and ev['available_at']<=asof:
                valid.append({**b,'time':ev['start']})
        # Select relative to the STRUCTURE, not the current ask. Never skip a
        # nearby resistance to find a further target that will pass RR.
        peak=confirmed_overhead(valid,asof,level)
        if not peak:return fail('NO_PREKNOWN_OVERHEAD_ABOVE_BASE','UNKNOWN',level=level)
        pivot=aware(peak['time']);trio=[b for b in valid if pivot-timedelta(minutes=1)<=b['time']<=pivot+timedelta(minutes=1)]
        known=max(aware(b['available_at']) for b in trio)
        target=floor_tick(peak['price'])
        key=hashlib.sha256(json.dumps([self.name,t.symbol,
            [[b['time'].isoformat()]+[b[k] for k in ('open','high','low','close')] for b in base],
            peak,known.isoformat()],sort_keys=True).encode()).hexdigest()[:24]
        if cached and cached.get('structure_id')==key:return cached
        spread=t.ask-t.bid if 0<t.bid<=t.ask and book_freshness(t,asof,self.cfg.stale_seconds)['fresh'] else 0
        buffer=max(tick_size(support),support*self.cfg.structural_buffer)
        structural=floor_tick(support-buffer)
        noise=max(median(b['high']-b['low'] for b in base),spread,tick_size(support))
        emergency=floor_tick(structural-noise)
        probe=level+tick_size(level)
        economics=entry_economics(probe,structural,emergency,target,self.costs,self.cfg.min_reward_risk)
        economics['entry_basis']='UNOBSERVED_FIRST_TRIGGER_TICK_FEASIBILITY_PROBE'
        zone=viable_entry_zone(economics,level,self.cfg.max_chase)
        result={'status':'KNOWN' if zone.get('trigger_compatible_zone') else 'REJECT',
            'stage':'ECONOMIC_FEASIBILITY','reason':'COST_ZONE_EXISTS' if zone.get('trigger_compatible_zone') else 'COST_ZONE_EMPTY_BEFORE_TRIGGER',
            'structure_id':key,'first_structure_at':asof.isoformat(),'level':level,'support':support,
            'structural_stop':structural,'emergency_stop':emergency,'noise_band':noise,
            'noise_basis':'MEDIAN_ACTUAL_LOCAL_BAR_RANGE_AND_KNOWN_SPREAD',
            'target':target,'target_evidence':{**peak,'available_at':known.isoformat(),
                'source':'PREKNOWN_CONFIRMED_REFERENCE_NOT_R_PROJECTION'},
            'base_times':[b['time'].isoformat() for b in base],'formation_economics':economics,'entry_zone':zone,
            'input_revision':input_revision,
            'structure_prices_frozen':True,'actual_order_permission':False}
        self.cache[t.symbol]=result;self.pending.pop(t.symbol,None)
        return result

    def evaluate(self,t,bars,references,asof,*,cash_budget=None,risk_budget=None,receipt=None,input_revision=None):
        plan=self.plan(t,bars,references,asof,input_revision)
        detail={**plan,'actual_order_permission':False,'SHADOW_ONLY':True}
        if not t.regular or not tick_freshness(t,asof,self.cfg.stale_seconds)['fresh']:
            return None,{**detail,'status':'UNKNOWN','reason':'TRADE_STALE_OR_FUTURE'}
        prior=self.previous.get(t.symbol)
        current={'time':t.time,'received_at':aware(t.received_at),'cumulative':t.cumulative,'price':t.price}
        if prior is None or (t.time,t.cumulative)>(prior['time'],prior['cumulative']):self.previous[t.symbol]=current
        if plan['status']!='KNOWN':return None,detail
        level=plan['level']
        crossing=(prior is not None and prior['cumulative']<t.cumulative and prior['price']<=level<t.price
                  and 0<=(t.time-prior['time']).total_seconds()<=self.cfg.stale_seconds
                  and prior['received_at'] is not None and prior['received_at']<=asof)
        if crossing:self.pending[t.symbol]={'structure_id':plan['structure_id'],'trigger_at':asof.isoformat(),
            'trade_received_at':t.received_at,'prior_time':prior['time'].isoformat(),
            'prior_price':prior['price'],'prior_cumulative':prior['cumulative'],
            'trigger_price':t.price,'trigger_cumulative':t.cumulative}
        trigger=self.pending.get(t.symbol)
        if not trigger or trigger['structure_id']!=plan['structure_id']:
            return None,{**detail,'stage':'TRIGGER','reason':'NO_OBSERVED_POST_STRUCTURE_CROSSING'}
        detail.update(trigger=trigger,stage='EXECUTION')
        if not t.regular or not tick_freshness(t,asof,self.cfg.stale_seconds)['fresh']:
            return None,{**detail,'status':'UNKNOWN','reason':'TRADE_STALE_OR_FUTURE'}
        if (asof-aware(trigger['trigger_at'])).total_seconds()>self.cfg.stale_seconds:
            return None,{**detail,'reason':'TRIGGER_EXPIRED'}
        if t.price<=level:return None,{**detail,'reason':'BREAKOUT_FAILED_BEFORE_QUOTE'}
        if t.vi_active:return None,{**detail,'reason':'VI_NO_ENTRY'}
        if not (0<t.bid<=t.ask and t.ask_qty>0 and t.bid_qty>0 and t.depth_available is True
                and t.depth_source in {'0D','0B_FIELDS'} and book_freshness(t,asof,self.cfg.stale_seconds)['fresh']):
            return None,{**detail,'status':'UNKNOWN','reason':'FRESH_EXECUTABLE_L1_UNKNOWN'}
        if (t.ask-t.bid)/t.price>self.cfg.max_spread:
            return None,{**detail,'reason':'SPREAD_TOO_WIDE'}
        if t.ask-t.bid>plan['noise_band']:return None,{**detail,'reason':'SPREAD_EXCEEDS_FROZEN_NOISE'}
        if max(t.ask,t.price)>level*(1+self.cfg.max_chase):return None,{**detail,'reason':'CHASE_ABOVE_LOCAL_BASE'}
        economics=entry_economics(t.ask,plan['structural_stop'],plan['emergency_stop'],plan['target'],self.costs,self.cfg.min_reward_risk)
        zone=viable_entry_zone(economics,level,self.cfg.max_chase)
        detail.update(economics=economics,entry_zone=zone)
        if not economics['rr_pass'] or not zone.get('current_order_price_in_zone'):
            return None,{**detail,'reason':'ASK_OUTSIDE_FIXED_COST_ZONE'}
        sizing=(entry_quantity(economics,cash_budget,risk_budget) if cash_budget is not None and risk_budget is not None
                else {'quantity':None,'status':'UNKNOWN_ACCOUNT_BUDGET'})
        if sizing['quantity']==0:return None,{**detail,'reason':'ZERO_COMMON_RISK_OR_CASH_QUANTITY','sizing':sizing}
        if sizing['quantity'] and min(t.ask_qty,t.bid_qty)<sizing['quantity']:
            return None,{**detail,'reason':'DEPTH_BELOW_COMMON_QUANTITY','sizing':sizing}
        # This signal can only be considered by a separately owned PAPER broker.
        signal={'symbol':t.symbol,'pattern':self.name,'name':t.symbol,'time':asof.isoformat(),
            'signal_detected_at':asof.isoformat(),'ttl_seconds':self.cfg.stale_seconds,
            'limit':t.ask,'stop':plan['emergency_stop'],'emergency':plan['emergency_stop'],'target':plan['target'],
            'max_chase_price':floor_tick(level*(1+self.cfg.max_chase)),
            'planned_qty':sizing['quantity'],'session':'REGULAR','venue':t.venue,
            'invalidation_policy':{'enabled':True,'structural_stop':plan['structural_stop'],
                'emergency_stop':plan['emergency_stop'],'noise_band':plan['noise_band']},
            'entry_economics':economics,'entry_zone':zone,'research_structure':plan,'trigger_evidence':trigger,
            'receipt_evidence':receipt,'SHADOW_ONLY':True,'actual_order_permission':False,
            'efficiency':efficiency(t.ask,plan['emergency_stop'],plan['target'],.5,self.auto.horizon_a_minutes,
                self.costs,self.auto.min_efficiency_score,economics=economics),
            'heuristic_basis':'EXISTING_EFFICIENCY_WEIGHT_NOT_WIN_PROBABILITY'}
        return signal,{**detail,'status':'L1_SIGNAL','reason':'INDEPENDENT_TRIGGER_IN_FIXED_ECONOMIC_ZONE',
            'sizing':sizing,'actual_portfolio_orderable':False}
