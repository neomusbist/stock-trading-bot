"""기존 보유 평가금액을 섞지 않는 봇 자본과 명시적 거래비용."""
from dataclasses import dataclass
from decimal import Decimal
import math

# All detector setups use one cost/risk contract. A gross-only C/D gate could
# pass a candidate which the same account cannot justify after costs.
COMMON_COST_PATTERNS = frozenset({'A', 'B', 'C', 'D', 'E'})

@dataclass(frozen=True)
class Costs:
    buy: float
    sell: float
    tax: float
    entry_slippage: float
    exit_slippage: float

    def buy_fee(self,amount): return amount*self.buy
    def sell_fee(self,amount): return amount*(self.sell+self.tax)
    def roundtrip(self,entry,exit_price):
        return self.buy_fee(entry)+self.sell_fee(exit_price)
    def estimate(self,entry,target):
        return self.roundtrip(entry,target)+entry*self.entry_slippage+target*self.exit_slippage

def owned_equity(cash,positions,prices):
    return cash+sum(p['qty']*prices.get(s,p['avg']) for s,p in positions.items() if p.get('ownership','BOT')=='BOT')

def initial_allocation(snapshot,limit):
    return min(max(0,snapshot['cash']),max(0,limit))


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def loss_reserve(entry,exit_price,costs,*,filled=False):
    if not all(_finite(x) and x>0 for x in (entry,exit_price)):return float('inf')
    D=lambda x:Decimal(str(x))
    return float(max(Decimal(0),D(entry)*(1+D(costs.buy)+(0 if filled else D(costs.entry_slippage)))
                     -D(exit_price)*(1-D(costs.sell)-D(costs.tax)-D(costs.exit_slippage))))


def entry_economics(entry,structural_stop,emergency_stop,target,costs,minimum_rr,
                    *,filled=False,actual_entry_fee_per_share=None,
                    actual_exit_price=None,actual_exit_fee_per_share=None):
    """One A/B cash-flow model. Reserves are estimates, never broker charges.

    The quoted/order-limit price is not inflated into a fictitious execution.
    Pre-trade entry slippage is a conservative cost reserve on BOTH outcomes.
    An observed fill already includes its execution deviation, so that reserve
    is zero after fill. Confirmed fees override estimates only when supplied.
    """
    unknown={'status':'UNKNOWN','rr_pass':False,'net_rr':None,'gross_rr':None,
        'max_entry_price':None,'risk_per_share':None,'reward_after_cost':None,
        'actual_order_permission':False,'reason':'INVALID_OR_MISSING_PRICE_COST_INPUT'}
    values=(entry,structural_stop,emergency_stop,target,minimum_rr)
    rates=(costs.buy,costs.sell,costs.tax,costs.entry_slippage,costs.exit_slippage)
    if (any(not _finite(x) or x<=0 for x in values)
            or any(not _finite(x) or not 0<=x<1 for x in rates)
            or costs.sell+costs.tax+costs.exit_slippage>=1
            or not emergency_stop<=structural_stop<entry):return unknown
    for fee in (actual_entry_fee_per_share,actual_exit_fee_per_share):
        if fee is not None and (not _finite(fee) or fee<0):return unknown
    if actual_exit_price is not None and (not _finite(actual_exit_price) or actual_exit_price<=0):return unknown
    D=lambda x:Decimal(str(x))
    e,s,h,t,r=map(D,values);buy,sell,tax,entry_slip,exit_slip=map(D,rates)
    buy_fee=D(actual_entry_fee_per_share) if filled and actual_entry_fee_per_share is not None else e*buy
    reserve=Decimal(0) if filled else e*entry_slip
    buy_cash=e+buy_fee+reserve
    exit_factor=1-sell-tax-exit_slip
    reward=t*exit_factor-buy_cash;risk=buy_cash-h*exit_factor
    # Before a fill all terms are linear in the order-price input.
    max_raw=(t+r*h)*exit_factor/((1+r)*(1+buy+(0 if filled else entry_slip)))
    if filled and actual_entry_fee_per_share is not None:
        max_raw=(t+r*h)*exit_factor/(1+r)-buy_fee
    from core.util import floor_tick,tick_size
    maximum=floor_tick(float(max_raw))
    while D(maximum)>max_raw:maximum=floor_tick(maximum-tick_size(maximum-1))
    result=dict(status='KNOWN',basis='AB_COMMON_CASH_FLOW_V1',entry_price=entry,
        entry_basis='ACTUAL_FILL' if filled else 'OBSERVED_ASK_ORDER_LIMIT',
        structural_stop=structural_stop,emergency_stop=emergency_stop,target=target,
        buy_fee_per_share=float(buy_fee),entry_slippage_reserve=float(reserve),
        target_sell_fee_per_share=float(t*(sell+tax)),target_exit_slippage_reserve=float(t*exit_slip),
        emergency_sell_fee_per_share=float(h*(sell+tax)),emergency_exit_slippage_reserve=float(h*exit_slip),
        one_share_cash=float(buy_cash),risk_per_share=float(risk),reward_after_cost=float(reward),
        cost_per_share=float(t-e-reward),gross_rr=float((t-e)/(e-h)),net_rr=float(reward/risk),
        structural_gross_rr=float((t-e)/(e-s)),minimum_net_rr=minimum_rr,
        rr_pass=bool(reward>=r*risk),max_entry_price=maximum,max_entry_price_unrounded=float(max_raw),
        above_max_entry=bool(e>max_raw),actual_order_permission=False,
        costs=dict(buy=costs.buy,sell=costs.sell,tax=costs.tax,entry_slippage=costs.entry_slippage,exit_slippage=costs.exit_slippage),
        entry_fee_basis='CONFIRMED' if filled and actual_entry_fee_per_share is not None else 'ESTIMATED',
        slippage_basis='PRETRADE_STRESS_RESERVE_NOT_AN_EXECUTION_ABOVE_LIMIT' if not filled else 'ACTUAL_ENTRY_DEVIATION_ALREADY_IN_FILL')
    if actual_exit_price is not None:
        x=D(actual_exit_price);exit_fee=D(actual_exit_fee_per_share) if actual_exit_fee_per_share is not None else x*(sell+tax)
        result.update(observed_exit_price=actual_exit_price,observed_exit_fee_per_share=float(exit_fee),
            realized_net_per_share=float(x-exit_fee-e-buy_fee),
            realized_costs_confirmed=bool(filled and actual_entry_fee_per_share is not None and actual_exit_fee_per_share is not None))
    return result


def signal_economics(cfg,auto,signal,*,entry=None,filled=False,actual_entry_fee_per_share=None):
    policy=signal.get('invalidation_policy') or {}
    entry=signal.get('limit') if entry is None else entry
    use_policy=auto.confirmed_invalidation_enabled and policy.get('enabled',True)
    structure=policy.get('structural_stop',signal.get('stop')) if policy.get('enabled',True) else signal.get('stop')
    emergency=policy.get('emergency_stop',signal.get('emergency',signal.get('stop'))) if use_policy else signal.get('emergency',signal.get('stop'))
    costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
    econ=entry_economics(entry,structure,emergency,signal.get('target'),costs,cfg.min_reward_risk,
                         filled=filled,actual_entry_fee_per_share=actual_entry_fee_per_share)
    paper_align=bool(signal.get('pattern')=='A' and getattr(auto,'setup_a_paper_rr_alignment_enabled',False) and not getattr(cfg,'live_mode',False))
    if paper_align and econ.get('status')=='KNOWN':
        planned=entry_economics(entry,structure,structure,signal.get('target'),costs,cfg.min_reward_risk,
                                filled=filled,actual_entry_fee_per_share=actual_entry_fee_per_share)
        econ['planned_exit_risk_per_share']=planned.get('risk_per_share')
        econ['structural_gross_rr']=planned.get('gross_rr')
        econ['structural_net_rr']=planned.get('net_rr')
        econ['structural_max_entry_price']=planned.get('max_entry_price')
        econ['rr_stop_basis']=getattr(auto,'setup_a_rr_stop_basis','EMERGENCY_STOP')
        if econ['rr_stop_basis']=='STRUCTURAL_STOP' and planned.get('status')=='KNOWN':
            # Keep emergency_stop risk_per_share for 2% account / 4% daily sizing,
            # while evaluating RR gate on the planned candle-close structural_stop.
            econ['emergency_net_rr']=econ['net_rr']
            econ['emergency_gross_rr']=econ['gross_rr']
            econ['emergency_max_entry_price']=econ['max_entry_price']
            econ['net_rr']=planned['net_rr']
            econ['gross_rr']=planned['gross_rr']
            econ['max_entry_price']=planned['max_entry_price']
            econ['rr_pass']=planned['rr_pass']
    return econ


def viable_entry_zone(economics,trigger_price=None,max_chase=None,is_recovery=False):
    """Price feasibility is neither a trigger nor order permission."""
    from core.util import floor_tick,tick_size
    result=dict(status='UNKNOWN',price_zone_exists=None,trigger_compatible_zone=None,
        maximum_entry=economics.get('max_entry_price'),minimum_trigger_entry=None,
        trigger_confirmed=False,actual_order_permission=False)
    if economics.get('status')!='KNOWN':return result
    support=economics['structural_stop'];lower=floor_tick(support)+tick_size(floor_tick(support))
    upper=economics['max_entry_price'];result.update(status='KNOWN',minimum_structure_entry=lower,price_zone_exists=lower<=upper)
    if trigger_price is None:return result
    if not _finite(trigger_price) or trigger_price<=0:return result
    trigger=floor_tick(trigger_price);minimum=max(lower,trigger if is_recovery else trigger+tick_size(trigger))
    chase=floor_tick(trigger_price*(1+max_chase)) if _finite(max_chase) and max_chase>=0 else upper
    result.update(minimum_trigger_entry=minimum,trigger_price=trigger_price,maximum_chase_entry=chase,
        maximum_admissible_entry=min(upper,chase),trigger_compatible_zone=minimum<=min(upper,chase),
        current_order_price_in_zone=minimum<=economics['entry_price']<=min(upper,chase))
    return result


def entry_quantity(economics,cash_budget,risk_budget):
    valid=economics.get('status')=='KNOWN' and all(_finite(v) and v>=0 for v in (cash_budget,risk_budget))
    unit=economics.get('risk_per_share');cash=economics.get('one_share_cash')
    # Decimal input amounts must not lose one share at an exact budget
    # boundary because binary float floor division rounded the divisor up.
    cq=int(Decimal(str(cash_budget))/Decimal(str(cash))) if valid and _finite(cash) and cash>0 else 0
    rq=int(Decimal(str(risk_budget))/Decimal(str(unit))) if valid and _finite(unit) and unit>0 else 0
    return dict(quantity=max(0,min(cq,rq)),capital_quantity=max(0,cq),risk_quantity=max(0,rq))

