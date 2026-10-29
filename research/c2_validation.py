"""Offline C2 adapters; live engine/config files are never imported as state."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib,json
from pathlib import Path
from automation.entry_evidence import AUTHORITY_GATES
from research.strict_benchmark import FullPathReplay
from research.observed_paper import ObservedAuthority,ObservedPaperSession
from research.c2_retest import C2Detector
from core.market_evidence import aware


class C2Replay(FullPathReplay):
    def __init__(self,start,strategy,cfg,auto,cash,**kwargs):
        if (cfg.risk_fraction!=.02 or cfg.max_daily_loss_fraction!=.04 or cfg.max_positions!=1
                or cfg.max_position_fraction!=1 or cfg.min_reward_risk!=1.5):raise ValueError('C2_FIXED_RISK_PROFILE_REQUIRED')
        if (auto.fast_momentum_enabled or auto.pullback_expansion_target_enabled
                or getattr(auto,'resistance_breakout_expansion_enabled',False)
                or getattr(auto,'support_rebound_reclaim_enabled',False)):
            raise ValueError('C2_UNRELATED_STRATEGIES_MUST_BE_OFF')
        if not auto.confirmed_invalidation_enabled:raise ValueError('C2_EXISTING_CONFIRMED_INVALIDATION_REQUIRED')
        super().__init__(start,'C',cfg,auto,cash,**kwargs)
        self.detector=C2Detector(cfg,self.auto,self.account.e.costs);self.strategy='C2'
        self.profile['requested_strategy']='C2';self.profile['transport_pattern']='C'
        self.profile['hypothesis']='PREBREAK_OVERHEAD_BOX_RETEST_RECLAIM'
        self.profile_sha256=hashlib.sha256(json.dumps(self.profile,sort_keys=True).encode()).hexdigest()
        for name in ('c2_retest.py','c2_validation.py','c2_exits.py','observed_paper.py'):
            self.source_sha256['research/'+name]=hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()

    def step(self,frame,**kwargs):
        self.detector.evaluation_at=frame.available_at
        self.detector.watch_basis=(frame.tick.symbol,frame.tick.time,frame.tick.cumulative,
            (self.watch.get(frame.tick.symbol) or {}).get('status'))
        order=super().step(frame,**kwargs)
        if order and order.get('side')=='BUY':self.detector.consumed(frame.tick.symbol,frame.available_at)
        return order

    def result(self):
        r=super().result();events=deepcopy(list(self.detector.events));summaries=deepcopy(self.detector.episode_summary)
        r.update(strategy='C2',research_only=True,live_activation_allowed=False,
            c2_events=events,c2_event_losses=self.detector.event_losses,
            c2_reason_counts=dict(Counter(e['reason'] for e in events)),
            c2_reason_count_basis='DEDUPLICATED_TRANSITION_ROWS_NOT_UNIQUE_OPPORTUNITIES',
            c2_unique_box_episodes=len(summaries),c2_episodes=summaries,
            c2_stage_counts={k:sum(bool(s.get(k)) for s in summaries.values()) for k in
                ('retest_confirmed','price_zone_exists','trigger_zone_intersection','detector_ready')},
            episode_independence='NONOVERLAPPING_BOX_EPISODES_NOT_INDEPENDENT_TRADES',
            target_is_not_a_fixed_profit_order=True,actual_order_permission=False)
        return r


class C2ObservedAuthority(ObservedAuthority):
    """Compute NEW research gates from observed exogenous authority/account.

    A legacy C signal is NOT required for a new hypothesis. Missing raw account,
    WATCH, market phase or receipt evidence still blocks PAPER admission.
    """
    def __init__(self,account):
        market=account.e.entry_market_reason
        super().__init__(account)
        def boundary(signal,qty,fallback=None,phase='PRE_INTENT'):
            why=self.reason()
            if why:return why
            if signal.get('research_strategy')!='C2' or not account.e.research_paper_allowed(signal):return 'C2_SEALED_PAPER_REQUIRED'
            return market(signal,qty,fallback,phase)
        account.e.entry_market_reason=boundary

    def reason(self):
        why=self.context_reason(require_native_signal=False)
        if why:return why
        row=self.context[self.frame.tick.symbol];authority=row.get('authority') or {};watch=row.get('watch') or {}
        if authority.get('market_state')!='3' or authority.get('session')!='REGULAR':return self._why('C2_OBSERVED_REGULAR_MARKET_UNKNOWN')
        if authority.get('balance_refresh_uncertain') is not False:return self._why('C2_OBSERVED_BALANCE_CERTAINTY_UNKNOWN')
        readiness=authority.get('readiness') or {};at=aware(readiness.get('known_at'))
        if at is None or not 0<=(self.account.e.clock()-at).total_seconds()<=self.account.e.c.stale_seconds or readiness.get('order_ready') is not True:
            return self._why('C2_OBSERVED_GLOBAL_READINESS_UNKNOWN')
        scope=watch.get('status')=='WATCH' or (watch.get('status')=='ELIGIBLE' and watch.get('entry_scope_status')=='WATCH')
        if not scope or type(watch.get('source_count')) is not int:return self._why('C2_OBSERVED_WATCH_UNKNOWN')
        if watch['source_count']<max(2,self.account.e.a.min_source_count) and not watch.get('fallback_source1'):
            return self._why('C2_OBSERVED_SOURCE_QUALIFICATION_BLOCKED')
        return ''


class C2ObservedSession(ObservedPaperSession):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs,replay_factory=C2Replay,authority_factory=C2ObservedAuthority)
