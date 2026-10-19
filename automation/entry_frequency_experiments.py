"""Offline ABC alternatives. Nothing imports this module from an order path.

Uses the existing detector, real received prints and unchanged price/risk
geometry. Passing repeated ticks are one pattern episode, not filled trades.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from collections import Counter

from automation.patterns import PatternDetector
from automation.capital import Costs
from automation.trade_quality import complete_sequence
from core.market_time import tick_freshness


class SixBarContinuationDetector(PatternDetector):
    """Experiment: A's flow acceleration can be confirmed by six real bars.

The original A pattern, executable spread, structural proof, liquidity,
invalidation, target and RR must still pass. No fresh high alone creates a buy.
"""
    def signal(self,t,watch):
        signal=super().signal(t,watch)
        if signal is not None:
            signal.update(SHADOW_ONLY=True,diagnostic_only=True,actual_order_permission=False,
                          experimental_policy='OFFLINE_SIX_BAR_CONTINUATION')
        return signal

    def setup_trials(self,t,watch,gates=None,*,_bars=None):
        trials=deepcopy(super().setup_trials(t,watch,gates,_bars=_bars))
        bars=self.usable_bars(t.symbol) if _bars is None else _bars
        seq=complete_sequence(bars,t.time,6,require_recent=True)
        if not seq:return trials
        before,rise=seq[:3],seq[3:]
        if any(type(b.get('volume')) not in (int,float) or b['volume']<=0 for b in seq):return trials
        gain=rise[-1]['close']/rise[0]['open']-1
        valid=(rise[0]['low']<=rise[1]['low']<=rise[2]['low']
            and rise[0]['close']<rise[1]['close']<rise[2]['close']
            and rise[-1]['close']>rise[0]['high'] and gain>=self.cfg.min_price_gain
            and sum(b['volume'] for b in rise)>=sum(b['volume'] for b in before))
        if not valid:return trials
        for trial in trials:
            if trial['setup']!='A' or not trial['candidate_generated']:continue
            if trial['gates'].get('breakout_structure',{}).get('result')!='PASS':continue
            old=trial['gates']['acceleration']
            trial['gates']['acceleration']={'result':'PASS','values':{
                **old['values'],'mode':'OFFLINE_SIX_BAR_TREND_VOLUME',
                'actual_completed_bar_times':[b['time'].isoformat() for b in seq],
                'completed_gain':gain,'prior_volume':sum(b['volume'] for b in before),
                'rising_volume':sum(b['volume'] for b in rise),'orders_enabled':False}}
            trial['failed_reasons']=[r for r in trial['failed_reasons'] if r!='ACCELERATION_FAIL']
            trial['entry_ready']=not trial['failed_reasons']
        return trials


class ConfirmedLocalReclaimDetector(PatternDetector):
    """Baseline plus a different confirmed local pattern; research only."""
    def signal(self,t,watch):
        baseline=super().signal(t,watch)
        from automation.fast_momentum import FastMomentumStrategy
        from automation.reclaim_research import reclaim_trial
        asof=getattr(self,'research_asof',None) or t.time
        quote=FastMomentumStrategy(self.cfg,self.a,self.costs)._quote(t,asof)
        context=self.price_context.snapshot(t.symbol,t,t.ask)
        signal,trial=reclaim_trial(t,self.usable_bars(t.symbol),watch,self.cfg,self.a,self.costs,
            asof,self.last_eval[t.symbol]['gates'],quote_reason=quote,context=context)
        self.last_eval[t.symbol]['trials'].append(trial)
        signal=baseline or signal
        if signal is not None:
            signal={**signal,'SHADOW_ONLY':True,'diagnostic_only':True,'actual_order_permission':False,
                    'experimental_policy':'BASELINE_PLUS_CONFIRMED_LOCAL_RECLAIM'}
        return signal


def research_variants(cfg,auto):
    """Predeclared sensitivity cases; these are not recommended LIVE settings."""
    return [
        ('BASELINE',cfg,auto,PatternDetector),
        ('A_RATIO_2_5',replace(cfg,min_turnover_ratio=2.5),auto,PatternDetector),
        ('TURNOVER_20M_15M',replace(cfg,min_window_turnover=20000000,min_bc_window_turnover=15000000),auto,PatternDetector),
        ('RATIO_2_5_AND_TURNOVER_20M_15M',replace(cfg,min_turnover_ratio=2.5,
            min_window_turnover=20000000,min_bc_window_turnover=15000000),auto,PatternDetector),
        ('CONFIRMED_SIX_BAR_CONTINUATION',cfg,auto,SixBarContinuationDetector),
        ('CONFIRMED_LOCAL_RECLAIM',cfg,auto,ConfirmedLocalReclaimDetector),
    ]


def replay_variants(samples,cfg,auto,cutoff,*,continuous_coverage_verified=False,evaluation_interval_seconds=0.):
    if not isinstance(cutoff,datetime) or cutoff.tzinfo is None:raise ValueError('AWARE_CUTOFF_REQUIRED')
    if type(continuous_coverage_verified) is not bool:raise ValueError('EXPLICIT_COVERAGE_REQUIRED')
    import math
    if (type(evaluation_interval_seconds) not in (int,float) or not math.isfinite(evaluation_interval_seconds)
            or evaluation_interval_seconds<0):raise ValueError('INVALID_REPLAY_INTERVAL')
    # Arrival order, never a hindsight exchange-time repair.
    samples=sorted((s for s in samples if s.received_at<=cutoff
                    and (getattr(s,'processed_at',None) or s.received_at)<=cutoff),key=lambda s:s.received_at)
    if any(s.provenance not in {'REAL_WS','SIMULATED'} for s in samples):raise ValueError('EXPLICIT_PROVENANCE_REQUIRED')
    result={'orders_enabled':False,'actual_order_permission':False,'profitability_proven':False,
        'data_kind':sorted({s.provenance for s in samples}),'cutoff':cutoff.isoformat(),
        'continuous_coverage_verified':continuous_coverage_verified,'variants':[],
        'evaluation_interval_seconds':evaluation_interval_seconds,
        'timing_scope':'RECORDED_PROCESSING_WHERE_PRESENT; ORIGINAL_COUNTER_TRACE_NOT_REPLAYED'}
    for name,c,a,detector_type in research_variants(cfg,auto):
        costs=Costs(a.commission_buy,a.commission_sell,a.sell_tax,c.entry_slippage_fraction,c.exit_slippage_fraction)
        detector=detector_type(c,a,costs);detector.continuous_observation=continuous_coverage_verified
        episodes={};combos={};accepted=Counter();rejected=Counter();execution_stale=Counter();evaluated_at={}
        for sample in samples:
            # Historical monotonic counters cannot be compared with this
            # process's current counter. Replay wall-clock evidence explicitly;
            # its missing counter/clock-correction trace never proves a fill.
            t=replace(sample.tick,received_monotonic=None)
            if not detector.ingest(t,sample.received_at):rejected[t.symbol]+=1;continue
            accepted[t.symbol]+=1
            asof=getattr(sample,'processed_at',None) or sample.received_at
            if not tick_freshness(t,asof,c.stale_seconds)['fresh']:
                execution_stale[t.symbol]+=1;continue
            if t.symbol in evaluated_at and (asof-evaluated_at[t.symbol]).total_seconds()<evaluation_interval_seconds:continue
            evaluated_at[t.symbol]=asof
            detector.research_asof=asof
            signal=detector.signal(t,dict(sample.watch));evaluation=detector.last_eval[t.symbol]
            for trial in evaluation['trials']:
                combos.setdefault((t.symbol,trial['setup']),set()).update(trial['failed_reasons'])
            if signal is None:continue
            # One geometry per completed minute. Do not turn every passing tick
            # into an independent trade or imply a broker could have filled it.
            key=(t.symbol,signal['pattern'],t.time.replace(second=0,microsecond=0),
                 signal.get('breakout'),signal.get('stop'))
            episodes.setdefault(key,{**signal,'actual_order_permission':False,'diagnostic_only':True,
                'SHADOW_ONLY':True,'received_at':sample.received_at.isoformat(),
                'processed_at':asof.isoformat(),'timing_basis':'RECORDED_PROCESSING' if getattr(sample,'processed_at',None) else 'RECEIPT_ONLY_PROCESSING_DELAY_UNKNOWN',
                'counter_trace':'NOT_REPLAYED_CLOCK_CORRECTION_UNKNOWN',
                'actual_fill':None,'actual_net_pnl':None,'MFE':None,'MAE':None,
                'risk_evaluation':'ENGINE_ACCOUNT_AND_FINAL_QUOTE_BOUNDARY_NOT_REPLAYED'})
        result['variants'].append({'name':name,'unique_observed_symbols':len(accepted),
            'accepted_prints':sum(accepted.values()),'rejected_prints':sum(rejected.values()),
            'execution_stale_prints':sum(execution_stale.values()),
            'unique_symbol_patterns':len(combos),'detector_episodes':len(episodes),
            'actual_trades':None,'net_expectancy':None,'mean_net_pnl':None,
            'missed_market_opportunities':None,'episodes':list(episodes.values()),
            'failure_symbol_pattern_counts':dict(Counter(r for reasons in combos.values() for r in reasons))})
    result['limitation']='Detector episodes require causal quote/fill/exit data before any trade or expectancy claim.'
    return result
