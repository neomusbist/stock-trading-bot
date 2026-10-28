"""Paired, causal PAPER exit experiments. Nothing is installed in LIVE.

All order/fill/timeout/flat/depth/cost handling remains in the actual engine.
Only post-entry protective-line hypotheses differ. References require complete
consecutive bars; a future high/low is never passed to a prior decision.
"""
from collections import defaultdict,deque
from types import MethodType
from automation.capital import entry_economics
from core.util import floor_tick,tick_size

POLICIES=('BASELINE','INITIAL_STRUCTURE','AFTER_1R_COST_BREAKEVEN','CONFIRMED_PIVOT_TRAIL')


def install_exit_experiment(replay,policy):
    if policy not in POLICIES:raise ValueError('UNKNOWN_EXIT_EXPERIMENT')
    e=replay.account.e
    if not e.research_paper_allowed({'research_strategy':'C2'}):raise ValueError('SEALED_PAPER_REQUIRED')
    replay.exit_experiment=policy
    if policy=='BASELINE':return
    bars=defaultdict(lambda:deque(maxlen=3));original_bar=e.observe_protection_bar;initial={};breakeven={}
    def protection(self,p,price,completed_low=None):
        high=max(p['high'],price);stop=p['stop']
        if policy=='AFTER_1R_COST_BREAKEVEN' and high>=p['avg']+p['initial_r']:
            s=p['signal'];inv=s['invalidation_policy'];key=(p['trade_id'],p['avg'])
            structure,emergency=initial.setdefault(p['trade_id'],(inv['structural_stop'],inv['emergency_stop']))
            candidate=breakeven.get(key,floor_tick(p['avg']))
            # Find a cost-covering tick using the existing cash-flow model.
            # The extra slippage reserve remains conservative; no fill at that
            # line is assumed and a real future gap can still lose money.
            for _ in range(1000):
                economics=entry_economics(p['avg'],structure,emergency,candidate,self.costs,self.c.min_reward_risk,filled=True)
                if economics.get('status')=='KNOWN' and economics['reward_after_cost']>=0:break
                candidate+=tick_size(candidate)
            else:return high,stop
            breakeven[key]=candidate
            if price>candidate:
                stop=max(stop,candidate);inv['emergency_stop']=max(inv['emergency_stop'],candidate)
                p['research_exit_activation']=dict(policy=policy,known_at=self.clock().isoformat(),
                    one_r_basis='ENTRY_PRICE_MINUS_INITIAL_EMERGENCY_SAME_AS_NATIVE_INITIAL_R',line=candidate)
        return high,stop
    def complete(self,symbol,bar):
        p=self.s['positions'].get(symbol)
        if policy=='CONFIRMED_PIVOT_TRAIL' and p:
            q=bars[symbol]
            if not q or bar['time']>q[-1]['time']:q.append(dict(bar))
            if len(q)==3:
                left,center,right=q
                if ((center['time']-left['time']).total_seconds()==60
                        and (right['time']-center['time']).total_seconds()==60
                        and center['low']<left['low'] and center['low']<right['low']):
                    candidate=floor_tick(center['low']-tick_size(center['low']))
                    if candidate<right['close'] and candidate>p['stop']:
                        p['stop']=candidate
                        p['research_exit_activation']=dict(policy=policy,known_at=self.clock().isoformat(),
                            pivot_at=center['time'].isoformat(),confirmed_by=right['time'].isoformat(),line=candidate)
        return original_bar(symbol,bar)
    e.protection=MethodType(protection,e);e.observe_protection_bar=MethodType(complete,e)


def compare_paired_exits(frames,cfg,auto,cash,prepare,*,latency_ms=25):
    """prepare(replay) installs only already-known WATCH/reference events.

    Market receipt gaps remain gaps. No last-frame forced liquidation. A caller
    cannot interpret several policies on one entry as independent trades.
    """
    from research.c2_validation import C2Replay
    result={};entry_signature=None
    for policy in POLICIES:
        if not frames:break
        r=C2Replay(frames[0].available_at,'C2',cfg,auto,cash,latency_ms=latency_ms)
        try:
            install_exit_experiment(r,policy);prepare(r)
            for frame in frames:r.step(frame)
            row=r.result();buys=[x for x in row['fills'] if x['side']=='BUY']
            signature=[(x['at'],x['price'],x['qty']) for x in buys]
            if entry_signature is None:entry_signature=signature
            if signature!=entry_signature:raise ValueError('EXIT_COMPARISON_ENTRY_CHANGED')
            row['paired_entry_signature']=signature;row['independent_performance_evidence']=False
            result[policy]=row
        finally:r.close()
    return result
