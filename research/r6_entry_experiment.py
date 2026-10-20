"""Opt-in offline A/C target hypothesis, with no engine/order integration.

Compare the existing micro projection with the independently confirmed range's
projection and its known resistance ceiling. A target is never solved from an
RR requirement. This is a hypothesis, not proof of a reachable future price.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import math

from automation.allocation import efficiency
from automation.exit_plan import build_plan
from automation.trade_quality import complete_sequence


def compare_confirmed_range_target(trial, bars, asof, cfg, auto, costs):
    out={'policy':'CONFIRMED_RANGE_TARGET_OFFLINE_EXPERIMENT','status':'UNKNOWN',
         'actual_order_permission':False,'SHADOW_ONLY':True,'profitability_proven':False,
         'candidate_signal':None,'net_pnl':None,'MFE':None,'MAE':None}
    def finish(status,reason,**details):return {**out,'status':status,'reason':reason,**details}
    if not isinstance(asof,datetime) or asof.tzinfo is None:return finish('UNKNOWN','AWARE_DECISION_TIME_REQUIRED')
    if trial.get('setup') not in {'A','C'} or not trial.get('candidate_generated'):
        return finish('WAIT','NO_CONFIRMED_A_OR_C_SHAPE')
    gates=trial.get('gates',{})
    if gates.get('breakout',{}).get('result')!='PASS':return finish('WAIT','BREAKOUT_NOT_REACHED')
    if not bars:return finish('UNKNOWN','ORIGINAL_RECEIVED_BARS_MISSING')
    required=auto.box_min_bars if trial['setup']=='C' else 2
    if len(bars)<required or len(complete_sequence(bars,asof,len(bars),require_recent=True))!=len(bars):
        return finish('UNKNOWN','CONSECUTIVE_COMPLETED_SIGNAL_BARS_MISSING')
    for bar in bars:
        try:
            at=bar['time'] if isinstance(bar['time'],datetime) else datetime.fromisoformat(bar['time'])
            known=datetime.fromisoformat(bar['available_at'])
            if (not at.tzinfo or not known.tzinfo or not at.date()==known.date()==asof.date()
                    or at+timedelta(minutes=1)>asof or known>asof
                    or bar.get('observation_source')!='ACTUAL_RECEIVED'
                    or bar.get('gap_reason') or bar.get('first_second',0)>cfg.stale_seconds):
                return finish('UNKNOWN','BAR_COVERAGE_OR_AVAILABILITY_UNPROVEN')
        except (KeyError,TypeError,ValueError):return finish('UNKNOWN','BAR_COVERAGE_OR_AVAILABILITY_UNPROVEN')
    proof_gate=gates.get('breakout_structure' if trial['setup']=='A' else 'box_structure',{})
    if proof_gate.get('result')!='PASS':return finish('REJECT','STRUCTURAL_PROOF_NOT_PASSED')
    proof=proof_gate.get('values',{})
    try:
        known=datetime.fromisoformat(proof['asof'])
        if known>asof or known.date()!=asof.date():return finish('UNKNOWN','STRUCTURE_FROM_FUTURE')
        projection=proof['projection'];ceiling=proof['target_ceiling']
        entry=trial['virtual_entry'];emergency=proof['emergency_stop'];structural=proof['structural_stop']
        target=min(projection,ceiling)
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0
               for v in (entry,structural,emergency,projection,ceiling,target)):
            return finish('UNKNOWN','PRICE_GEOMETRY_UNKNOWN')
        if not 0<emergency<structural<entry<target:return finish('REJECT','INVALID_STRUCTURAL_GEOMETRY')
        # All original gates except the two explicitly recomputed target/cost
        # gates must still pass, including spread, turnover, chase and warmup.
        blocked=sorted(k for k,v in gates.items() if k not in {'risk_reward','economic'} and v.get('result')!='PASS')
        if blocked:return finish('REJECT','UNCHANGED_GATES_FAILED',blocked=blocked)
        signal={'pattern':trial['setup'],'limit':entry,'stop':emergency,'emergency':emergency,'target':target,
                'invalidation_policy':{'enabled':True,'structural_stop':structural,'emergency_stop':emergency}}
        plan=build_plan(cfg,auto,signal)
        inputs=gates['economic']['values']['score']['inputs']
        score=efficiency(entry,emergency,target,inputs['confidence'],inputs['planned_minutes'],costs,auto.min_efficiency_score)
        details={'original_target':gates['risk_reward']['values'].get('target'),
                 'candidate_target':target,'target_source':'PREDECISION_CONFIRMED_RANGE_PROJECTION_CAPPED_BY_KNOWN_RESISTANCE',
                 'candidate_net_rr':plan.get('net_reward_risk'),'required_net_rr':cfg.min_reward_risk,
                 'costs':deepcopy(getattr(costs,'__dict__',{})),'cost_score':score,
                 'structural_stop':structural,'emergency_stop':emergency}
        if not plan.get('reward_guard_pass') or score['decision']!='TRADE':
            return finish('REJECT','NET_REWARD_OR_COSTS_FAILED',**details)
        return finish('OBSERVE','OFFLINE_HYPOTHESIS_NOT_ORDER_AUTHORIZATION',candidate_signal=signal,**details)
    except (KeyError,TypeError,ValueError,ZeroDivisionError):return finish('UNKNOWN','STRUCTURAL_PRICE_OR_COST_EVIDENCE_MISSING')
