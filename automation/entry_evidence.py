"""Compact entry evidence, not a second strategy or order-authority source.

No filesystem/network calls. The existing bounded asynchronous recorder owns
serialization, rotation, free-space guards and loss counters. Preserve the
first stage transition before DecisionLog's ordinary display throttling.
"""
from collections import OrderedDict, Counter
from datetime import datetime
import hashlib
import json
from automation.decisions import tick_source_identity, safe_values
from core.market_time import tick_freshness, book_freshness

SETUPS=('A','B','C','D','E2')
AUTHORITY_GATES=('policy','session','connection_account','tick','repeat_slots')


class EntryDecisionEvidence:
    def __init__(self,symbol_limit=12,refresh_seconds=30):
        self.limit=symbol_limit;self.refresh=refresh_seconds
        self.context=OrderedDict();self.last={};self.episodes={};self.serial=0
        self.day=None;self.counts=Counter()

    def bind(self,t,watch,detector,asof):
        if self.day!=asof.date():
            self.context.clear();self.last.clear();self.episodes.clear();self.day=asof.date()
        self.context[t.symbol]=(t,dict(watch),detector,asof)
        self.context.move_to_end(t.symbol)
        while len(self.context)>self.limit:
            old,_=self.context.popitem(last=False)
            self.last.pop(old,None)
            for key in [k for k in self.episodes if k[0]==old]:self.episodes.pop(key)
            self.counts['context_evictions']+=1

    def row(self,e,symbol,decision,reason,gates,signal,extra):
        context=self.context.get(symbol)
        if context is None:
            self.counts['decision_without_bound_context']+=1;return None
        t,watch,detector,bound=context;at=e.clock();sig=signal or {};gates=gates or {};extra=extra or {}
        if bound>at or (at-bound).total_seconds()>e.c.stale_seconds:
            self.counts['expired_bound_context']+=1;return None
        evaluation=detector.last_eval.get(symbol,{})
        if evaluation.get('time')!=t.time or evaluation.get('cumulative')!=t.cumulative:
            evaluation={}
        trials=extra.get('setup_trials') or evaluation.get('trials') or sig.get('setup_trials') or []
        by={('E2' if tr.get('setup')=='E' else tr.get('setup')):tr for tr in trials}
        summaries=[]
        for name in SETUPS:
            tr=by.get(name);key=(symbol,name)
            enabled=(getattr(e.a,'resistance_breakout_expansion_enabled',False) if name=='D' else
                     getattr(e.a,'support_rebound_reclaim_enabled',False) if name=='E2' else True)
            if tr is None:
                summaries.append(dict(setup=name,status='NOT_EVALUATED' if enabled else 'DISABLED',
                    episode_id=None,reasons=['TRIAL_NOT_AVAILABLE' if enabled else 'POLICY_DISABLED']))
                continue
            geometry=tr.get('setup_geometry') or {};formed=bool(geometry)
            episode=self.prepare_episode(symbol,name,geometry,at) if formed else None
            gs=tr.get('gates',{});rr=gs.get('risk_reward',{}).get('values',{})
            score=gs.get('economic',{}).get('values',{}).get('score',{})
            net=rr.get('net_rr')
            if net is None and type(score.get('risk_per_share')) in (int,float) and score['risk_per_share']>0:
                reward=score.get('reward_after_cost')
                if type(reward) in (int,float):net=reward/score['risk_per_share']
            proof=gs.get({'A':'breakout_structure','B':'pullback_structure','C':'box_structure'}.get(name,''),{}).get('values',{})
            unknown=any(g.get('result')=='UNKNOWN' for g in gs.values() if g.get('required',True))
            summaries.append(dict(setup=name,status='UNKNOWN' if unknown else 'READY' if tr.get('entry_ready') else 'REJECT',
                episode_id=episode['id'] if episode else None,structure_first_observed_at=episode['first_at'] if episode else None,
                formed=formed,geometry=geometry,entry=tr.get('virtual_entry'),
                structural_invalidation=proof.get('structural_stop',proof.get('buffered_stop')),
                emergency_stop=proof.get('emergency_stop',tr.get('virtual_invalidation')),
                confirmed_resistance=watch.get('resistance'),target=rr.get('target'),
                target_inputs=rr.get('target_inputs'),gross_rr=rr.get('gross_rr'),net_rr=net,
                native_decision_rr=rr.get('calculated_rr'),native_rr_basis=rr.get('rr_basis','NATIVE_LEGACY_GATE_UNSPECIFIED'),
                net_rr_basis='NATIVE_RR_VALUES' if rr.get('net_rr') is not None else 'EXISTING_NATIVE_ECONOMIC_SCORE' if net is not None else 'UNKNOWN',
                maximum_entry=(rr.get('economics') or {}).get('max_entry_price'),
                maximum_entry_status='NATIVE_COMMON_MODEL' if rr.get('economics') else 'UNKNOWN_NOT_EMITTED_BY_NATIVE_MODEL',
                trigger=gs.get('breakout'),reasons=list(tr.get('failed_reasons',[])),
                gate_results={k:v.get('result') for k,v in gs.items()}))
        final={k:v.get('result') for k,v in gates.items()}
        required_final={k:v.get('result') for k,v in gates.items() if v.get('required',True)}
        risk=gates.get('risk_quantity',{}).get('values',{})
        slots=gates.get('repeat_slots',{}).get('values',{})
        signature=json.dumps([decision,reason,final,
            [(x['setup'],x['status'],x.get('episode_id'),x.get('gate_results')) for x in summaries],
            watch.get('status'),watch.get('source_count'),e.policy.mode,
            e.s.get('halt'),risk.get('quantity'),risk.get('actual_cash')],sort_keys=True)
        previous=self.last.get(symbol)
        self.counts['decision_calls']+=1
        if previous and previous[0]==signature and (at-previous[1]).total_seconds()<self.refresh:
            self.counts['unchanged_decisions_suppressed']+=1;return None
        self.last[symbol]=(signature,at)
        capital=e.capital_context()
        snapshot_at=capital.get('snapshot_at')
        account=dict(snapshot_at=snapshot_at,account_fresh=capital.get('account_fresh'),
            actual_cash=risk.get('actual_cash',capital.get('actual_orderable_cash')),
            book_cash=risk.get('book_cash',e.s.get('cash')),risk_equity=risk.get('equity',capital.get('risk_equity')),
            remaining_risk=risk.get('remaining_risk',capital.get('risk_remaining')),
            daily_loss=e.s.get('daily_loss'),daily_risk_budget=capital.get('daily_risk_budget_krw'),
            risk_fraction=e.c.risk_fraction,max_daily_loss_fraction=e.c.max_daily_loss_fraction,
            min_reward_risk=e.c.min_reward_risk,
            max_positions=e.c.max_positions,max_position_fraction=e.c.max_position_fraction,
            slots=slots.get('slots',len(set(e.s['positions'])|{o['symbol'] for o in e.active_orders(side='BUY')})),
            approved_cap=e.a.capital_limit,entry_budget=capital.get('entry_budget'),
            quantity=risk.get('quantity'),risk_quantity=risk.get('risk_quantity'),
            quantity_status=risk.get('observed_quantity_status') or ('ACTUAL_PRE_INTENT_ENGINE_CALCULATION' if risk else 'NOT_REACHED'),
            snapshot_scope='PRE_INTENT_RISK_GATE' if risk else 'OBSERVED_ENGINE_STATE_NOT_AN_ORDER_REVIEW')
        warm=evaluation.get('gates',{}).get('warmup',{}).get('values',{})
        liquidity=evaluation.get('gates',{}).get('liquidity',{}).get('values',{})
        row=dict(event='ENTRY_DECISION_EVIDENCE',symbol=symbol,asof=at.isoformat(),
            producer_execution='PAPER' if getattr(e,'paper_mode',False) else 'LIVE',
            producer_source_identity=getattr(e,'evidence_source_identity',None),
            input_evidence_basis=getattr(e,'evidence_input_basis','UNVERIFIED_PRODUCER_INPUT'),
            receipt_evidence=tick_source_identity(t),exchange_at=t.time.isoformat(),received_at=t.received_at,
            volume=t.volume,cumulative=t.cumulative,
            processed_at=bound.isoformat(),price=t.price,ask=t.ask,bid=t.bid,
            data_freshness=dict(trade=tick_freshness(t,at,e.c.stale_seconds),
                                book=book_freshness(t,at,e.c.stale_seconds)),
            ask_qty=t.ask_qty,bid_qty=t.bid_qty,depth_source=t.depth_source,
            depth_available=t.depth_available,regular=t.regular,venue=t.venue,
            book_time=t.book_time.isoformat() if t.book_time else None,book_received_at=t.book_received_at,
            watch={k:watch.get(k) for k in ('status','source_count','fallback_source1','first_raw_at','first_watch_at','observed_at','resistance')},
            first_discovery_basis='CACHED_METADATA_UNLESS_JOINED_TO_RAW_RECOGNITION',
            valid_completed_bars=warm.get('completed_bars'),warmup=warm,
            coverage_gap=detector.coverage_gaps.get(symbol),turnover=liquidity,
            strategies=summaries,account=account,
            costs=dict(buy=e.costs.buy,sell=e.costs.sell,tax=e.costs.tax,
                       entry_slippage=e.costs.entry_slippage,exit_slippage=e.costs.exit_slippage),
            authority=dict(mode=e.policy.mode,live_authorized=e.policy.state.get('live_authorized'),
                live_mode=e.c.live_mode,connected=e.connected,account_ok=e.account_ok,
                costs_confirmed=e.a.costs_confirmed,halt=e.s.get('halt'),
                readiness=getattr(e,'last_order_readiness_evidence',None),
                observed_final_gates={k:final.get(k,'UNKNOWN') for k in AUTHORITY_GATES}),
            final_decision=decision,final_reason=reason,final_gate_results=final,
            final_required_gate_results=required_final,
            selected_setup=sig.get('pattern'),order_limit=sig.get('limit'),
            initial_invalidation=sig.get('stop'),target=sig.get('target'),
            order_execution_evidence=sig.get('entry_execution_evidence'),
            identity_basis='CONTIGUOUS_STRUCTURE_EPISODE_NOT_STATISTICAL_INDEPENDENCE',
            completeness='UNKNOWN_IF_RECORDER_LOSS_OR_UNSUBSCRIBED_OR_ANY_REQUIRED_FIELD_MISSING',
            actual_order_permission=False,not_an_order_ledger=True)
        self.counts['rows_prepared']+=1
        return safe_values(row)

    def prepare_episode(self,symbol,name,geometry,at):
        identity=[geometry.get(k) for k in ('pattern','breakout','support','projection','entry_basis',
                                          'confirmation_bar','breakout_bar_at','support_bar_at')]
        digest=hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()[:20]
        key=(symbol,name);prior=self.episodes.get(key)
        if not prior or prior['geometry']!=digest:
            self.serial+=1
            prior=dict(geometry=digest,id=f'{self.day}:{symbol}:{name}:{self.serial}',first_at=at.isoformat())
            self.episodes[key]=prior;self.counts['structural_episodes_started']+=1
        # A transient non-formed quote does not create a new independent setup.
        return prior
