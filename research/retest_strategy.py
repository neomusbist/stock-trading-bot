"""Unproven paper hypothesis: confirmed breakout, closed retest, renewed high.

Target is an actually known intraday pivot, never a price reverse-solved from R.
Exit policy is the existing engine's confirmed-structure/emergency/AE3 policy.
No LIVE runner imports this module. There is no arbitrary volume multiplier.
"""
from dataclasses import asdict
from statistics import median

from automation.local_breakout import LocalBreakoutSignal
from automation.exit_plan import build_plan
from automation.allocation import efficiency
from core.market_time import tick_freshness,book_freshness
from core.util import floor_tick,tick_size
from research.causal_data import aware,finite,known_signal_bars


class ConfirmedRetestStrategy:
    def __init__(self,cfg,auto,costs):self.cfg=cfg;self.auto=auto;self.costs=costs

    def evaluate(self,t,bars,watch,capital_limit,risk_budget,asof):
        def reject(reason,status='REJECT',**proof):return None,{'pass':False,'reason':reason,'status':status,**proof}
        bars,reason=known_signal_bars(bars[-12:],asof,7)
        if reason:return reject(reason,'UNKNOWN')
        if any(not finite(b.get('volume')) for b in bars):return reject('BAR_VOLUME_EVIDENCE_MISSING','UNKNOWN')
        if not t.regular or not tick_freshness(t,asof,self.cfg.stale_seconds)['fresh']:
            return reject('TRADE_STALE_OR_FUTURE','UNKNOWN')
        if not (finite(t.ask) and finite(t.bid) and 0<t.bid<=t.ask and t.depth_available is True
                and t.depth_source in {'0D','0B_FIELDS'} and finite(t.ask_qty) and finite(t.bid_qty)
                and book_freshness(t,asof,self.cfg.stale_seconds)['fresh']):
            return reject('MEASURED_FRESH_BOOK_REQUIRED','UNKNOWN')
        if (t.ask-t.bid)/t.price>self.cfg.max_spread:return reject('SPREAD_TOO_WIDE')
        base=bars[:-2];breakout,retest=bars[-2:];level=max(b['high'] for b in base)
        if not breakout['open']<=level<breakout['close']:return reject('NO_CONFIRMED_BREAKOUT')
        if retest['close']<=level or retest['low']<=min(b['low'] for b in base):return reject('RETEST_STRUCTURE_FAILED')
        if t.price<=retest['high'] or t.ask<=retest['high']:return reject('RETEST_RECOVERY_NOT_REACHED')
        ceiling=level*(1+min(.005,self.cfg.max_chase))
        if t.ask>ceiling:return reject('MAX_CHASE_EXCEEDED')
        peak=watch.get('nearest_confirmed_peak') or {}
        peak_available=aware(peak.get('available_at'));peak_confirmed=aware(peak.get('confirmed_at'))
        if (not finite(peak.get('price')) or not peak_available or not peak_confirmed
                or peak_available>asof or peak_confirmed>asof
                or peak.get('observation_source')!='ACTUAL_RECEIVED'):
            return reject('KNOWN_INTRADAY_TARGET_UNAVAILABLE','UNKNOWN')
        entry=t.ask;target=floor_tick(peak['price'])
        if target<=entry:return reject('NO_REWARD_TO_KNOWN_RESISTANCE')
        structural=floor_tick(retest['low']-tick_size(retest['low']))
        # Distinct lines, each tied to an observed price structure. The lower
        # confirmed base, not a fixed loss percentage, defines the emergency.
        emergency=floor_tick(min(b['low'] for b in base)-(t.ask-t.bid))
        if not 0<emergency<structural<entry:return reject('INVALID_STRUCTURAL_GEOMETRY')
        proto={'pattern':'LOCAL_RETEST','limit':entry,'stop':emergency,'emergency':emergency,'target':target,
            'invalidation_policy':{'enabled':True,'structural_stop':structural,'emergency_stop':emergency,
                                   'noise_band':structural-emergency}}
        plan=build_plan(self.cfg,self.auto,proto)
        if not plan['reward_guard_pass']:return reject('R6_NET_REWARD_GUARD',net_rr=plan['net_reward_risk'])
        unit=entry-emergency+self.costs.roundtrip(entry,emergency)+emergency*self.costs.exit_slippage
        qty=max(0,min(int(capital_limit//(entry*(1+self.costs.buy))),int(risk_budget//unit)))
        if qty<1:return reject('ZERO_QUANTITY_RISK_OR_CAPITAL')
        signal=LocalBreakoutSignal(t.symbol,watch.get('name',t.symbol),entry,structural,target,emergency,qty,
            plan['net_reward_risk'],(target-entry)/(entry-emergency),plan['reward_after_cost_per_share'],unit,
            pattern='LOCAL_RETEST',created_at=asof,ttl_seconds=self.cfg.stale_seconds,max_chase_price=ceiling,
            reason='CONFIRMED_BREAKOUT_RETEST_RECOVERY',extra={'actual_order_permission':False,'SHADOW_ONLY':True,
                'profitability_proven':False,'breakout_level':level,'breakout_bar':breakout,'retest_bar':retest,
                'base_support':min(b['low'] for b in base),'target_evidence':peak,
                'volume_ratio_diagnostic_only':breakout['volume']/median(b['volume'] for b in base),
                'exit_policy':'EXISTING_R6_CONFIRMED_STRUCTURE_EMERGENCY_AND_AE3'})
        return signal,{'pass':True,'status':'PAPER_HYPOTHESIS','reason':signal.reason}


def paper_engine_signal(signal,cfg,auto,costs,*,confidence=.8,horizon_minutes=30,source_evidence=None):
    """Existing R6 heuristic inputs are declared assumptions, not probabilities."""
    if not signal.extra.get('SHADOW_ONLY') or signal.extra.get('actual_order_permission') is not False:
        raise ValueError('ORIGINAL_SHADOW_SIGNAL_REQUIRED')
    structural=signal.stop;hard=signal.emergency
    return {'symbol':signal.symbol,'name':signal.name,'pattern':signal.pattern,
        'time':signal.created_at.isoformat(),'signal_detected_at':signal.created_at.isoformat(),
        'ttl_seconds':signal.ttl_seconds,'limit':signal.limit,'stop':hard,'emergency':hard,'target':signal.target,
        'max_chase_price':signal.max_chase_price,'session':'REGULAR','venue':'KRX',
        'source_evidence':source_evidence,
        'invalidation_policy':{'enabled':True,'structural_stop':structural,'emergency_stop':hard,
                               'noise_band':structural-hard},
        'efficiency':efficiency(signal.limit,hard,signal.target,confidence,horizon_minutes,costs,auto.min_efficiency_score),
        'heuristic_basis':'EXISTING_R6_WEIGHT_ASSUMPTION_NOT_EMPIRICAL_WIN_PROBABILITY',
        'SHADOW_ONLY':True,'actual_order_permission':False,'research_signal':asdict(signal)}
