"""규칙 효율은 기대값이 아니다. 저항/지지가 UNKNOWN이면 교체하지 않는다."""
import math
from automation.performance import completed_trades

def efficiency(entry,stop,target,confidence,horizon_minutes,costs,minimum=0.1,*,economics=None):
    inputs={'entry':entry,'stop':stop,'target':target,'confidence':confidence,'planned_minutes':horizon_minutes}
    if any(v is None or not isinstance(v,(int,float)) or not math.isfinite(v) for v in inputs.values()):
        return {'decision':'NO_TRADE','basis':'UNKNOWN','inputs':inputs,'score':None}
    if not 0<stop<entry<target or not 0<confidence<=1 or horizon_minutes<=0:
        return {'decision':'NO_TRADE','basis':'INVALID_STRUCTURE','inputs':inputs,'score':None}
    if economics is not None:
        if economics.get('status')!='KNOWN':return {'decision':'NO_TRADE','basis':'UNKNOWN','inputs':inputs,'score':None}
        fee=economics['cost_per_share'];reward=economics['reward_after_cost'];risk=economics['risk_per_share']
    else:
        fee=costs.estimate(entry,target); reward=target-entry-fee
        risk=entry-stop+costs.roundtrip(entry,stop)+stop*costs.exit_slippage
    score=(reward/risk)*confidence/math.sqrt(horizon_minutes/10)
    return {'decision':'TRADE' if reward>0 and score>=minimum else 'NO_TRADE',
            'basis':'HEURISTIC_NOT_EXPECTED_VALUE','inputs':inputs,'cost_per_share':fee,
            'reward_after_cost':reward,'risk_per_share':risk,'score':score}

def empirical_adjustment(fragments,pattern,minimum):
    rows=[r for r in completed_trades(fragments) if r.get('pattern')==pattern and r.get('ownership')=='BOT']
    if len(rows)<minimum: return {'basis':'INSUFFICIENT_SAMPLE','count':len(rows),'net_return':None}
    returns=[r['pnl_estimate']/(r['entry']*r['qty']) for r in rows]
    avg=sum(returns)/len(returns)
    variance=sum((x-avg)**2 for x in returns)/max(1,len(returns)-1)
    return {'basis':'EMPIRICAL_SAMPLE_NOT_FORECAST','count':len(rows),'net_return':avg,
            'conservative_return':avg-2*math.sqrt(variance/len(returns))}

def apply_empirical(signal,fragments,auto):
    sample=empirical_adjustment(fragments,signal.get('pattern'),auto.empirical_min_trades)
    result={**signal,'empirical':sample}
    if sample['basis'].startswith('EMPIRICAL') and signal.get('efficiency',{}).get('risk_per_share',0)>0:
        old=signal['efficiency']; inputs=old['inputs']
        score=sample['conservative_return']/(old['risk_per_share']/inputs['entry'])/math.sqrt(inputs['planned_minutes']/10)
        result['efficiency']={**old,'heuristic_score':old['score'],'score':score,
            'basis':'EMPIRICAL_CONSERVATIVE_SAMPLE_NOT_FORECAST',
            'decision':'TRADE' if sample['conservative_return']>0 and score>=auto.min_efficiency_score else 'NO_TRADE'}
    return result

def comparison(held,candidate,auto,costs,market='UNKNOWN',fragments=()):
    """현재부터의 구조/예상 평가기간/비용으로 비교한다. 과거 평단 손실률은 입력이 아니다."""
    if market!='3' or not auto.replacement_enabled: return {'decision':'HOLD','reason':'시장/교체 정책 미충족'}
    old=efficiency(held.get('price'),held.get('support'),held.get('resistance'),
                   held.get('confidence'),held.get('planned_minutes'),costs,auto.min_efficiency_score)
    candidate=apply_empirical(candidate,fragments,auto)
    new=candidate.get('efficiency',{})
    evidence={'held':held,'hold_efficiency':old,'candidate_efficiency':new,
              'candidate_pattern':candidate.get('pattern'),'safety_margin':auto.replacement_margin}
    if old.get('score') is None or new.get('score') is None or new.get('decision')!='TRADE':
        return {'decision':'HOLD','reason':'구조/효율 UNKNOWN 또는 신규 효율 부족',**evidence}
    if held.get('holding_minutes',0)<auto.replacement_min_hold_minutes or held.get('trend')=='UNKNOWN':
        return {'decision':'HOLD','reason':'보유 평가 기간/추세 근거 부족',**evidence}
    # 기존 매도 비용까지 신규 기회 점수에서 차감. 기존 평가금액이 자본으로 승인되지는 않는다.
    sell_drag=(costs.sell_fee(held['price'])+held['price']*costs.exit_slippage)/new['risk_per_share']
    adjusted=new['score']-sell_drag
    sample=empirical_adjustment(fragments,candidate.get('pattern'),auto.empirical_min_trades)
    enough=sample['basis'].startswith('EMPIRICAL')
    positive=(not enough or sample['conservative_return']>0)
    replace=positive and adjusted>max(0,old['score'])+auto.replacement_margin
    return {'decision':'REPLACE' if replace else 'HOLD','reason':'비용/안전마진 포함 상대효율 비교',
            'new_after_existing_sell_cost':adjusted,'empirical':sample,**evidence}

