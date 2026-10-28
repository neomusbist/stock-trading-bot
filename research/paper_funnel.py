"""Bounded evidence counts, never a second strategy or order engine."""
from collections import Counter
import math


class PaperFunnel:
    def __init__(self,limit=4096):
        self.limit=limit;self.watch=set();self.structures=set();self.validated=set();self.economics=set()
        self.account_gates=set();self.decisions=0;self.watch_rows=0;self.unknown=Counter()

    def add(self,target,key):
        if len(target)<self.limit:target.add(key)
        elif key not in target:self.unknown['COUNT_BUDGET_EXCEEDED']+=1

    def feed(self,row):
        try:self._feed(row)
        except (TypeError,ValueError,AttributeError,KeyError):self.unknown['MALFORMED_FUNNEL_ROW']+=1

    def _feed(self,row):
        if row.get('event')=='WATCH_METADATA':self.watch_rows+=1
        if row.get('event')=='WATCH_METADATA' and row.get('active',True) and (row.get('watch') or {}).get('status')=='WATCH':
            if row.get('symbol'):self.add(self.watch,row['symbol'])
            else:self.unknown['WATCH_SYMBOL_MISSING']+=1
        if row.get('event')!='ENTRY_DECISION_EVIDENCE':return
        trials=row.get('strategies',[])
        if not isinstance(trials,list) or any(not isinstance(t,dict) for t in trials):
            self.unknown['MALFORMED_STRATEGY_LIST']+=1;return
        self.decisions+=1
        for tr in trials:
            setup=tr.get('setup');episode=tr.get('episode_id')
            if not episode:
                if tr.get('formed'):self.unknown['STRUCTURE_ID_MISSING']+=1
                continue
            session=row.get('capture_session');symbol=row.get('symbol')
            if not session:self.unknown['PRODUCER_SESSION_UNKNOWN']+=1
            if not symbol:self.unknown['STRUCTURE_SYMBOL_UNKNOWN']+=1
            if not all(isinstance(v,str) for v in (setup,episode)):
                self.unknown['STRUCTURE_ID_SCHEMA_UNKNOWN']+=1;continue
            key=(setup,str(session),str(symbol),episode);self.add(self.structures,key)
            g=tr.get('gate_results',{});structure=g.get({'A':'breakout_structure','B':'pullback_structure','C':'box_structure'}.get(setup,''))
            if structure=='PASS':self.add(self.validated,key)
            elif structure in (None,'UNKNOWN'):self.unknown['INDEPENDENT_STRUCTURE_UNKNOWN']+=1
            rr=tr.get('net_rr');required=(row.get('account') or {}).get('min_reward_risk')
            if all(type(v) in (int,float) and math.isfinite(v) for v in (rr,required)):
                if rr>=required and tr.get('status')=='READY' and (tr.get('trigger') or {}).get('result')=='PASS':self.add(self.economics,key)
            else:self.unknown['ECONOMICS_UNKNOWN']+=1
            authority=row.get('authority') or {};required_gates=row.get('final_required_gate_results') or {}
            qty=(row.get('account') or {}).get('quantity')
            if (row.get('producer_execution')=='LIVE' and row.get('input_evidence_basis')=='ACTUAL_RECEIVED_LIVE_PIPELINE'
                and row.get('selected_setup')==setup and type(qty) is int and qty>0 and required_gates
                and all(v=='PASS' for v in required_gates.values())
                and all(authority.get('observed_final_gates',{}).get(k)=='PASS' for k in ('policy','session','connection_account','tick','repeat_slots'))):
                self.add(self.account_gates,key)

    def result(self,replay,source_counts):
        def per_setup(values):return dict(Counter(k[0] for k in values))
        c2=replay.get('c2_stage_counts')
        return dict(observed_watch_unique_symbols=len(self.watch),
            watch_completeness='UNKNOWN_NO_WATCH_METADATA' if not self.watch_rows else 'OBSERVED_SCOPE_ONLY_CHECK_CAPTURE_LOSSES',
            structure_fingerprints=per_setup(self.structures) if self.decisions else None,
            independent_structure_pass=per_setup(self.validated) if self.decisions else None,
            economic_and_trigger_pass=per_setup(self.economics) if self.decisions else None,
            native_reported_preintent_pass_NOT_revalidated=per_setup(self.account_gates) if self.decisions else None,
            observed_context_PAPER_accepted_buys=source_counts.get('MEASURED_CONTEXT_PAPER_ACCEPTED_BUYS',0),
            actual_historical_orderability='UNKNOWN_UNLESS_SAME_RECEIPT_CONTEXT_VALIDATED_NOT_PROVEN_BY_REPORTED_PASS',
            missing_native_decisions='UNKNOWN_NOT_ZERO_MARKET_OPPORTUNITIES' if not self.decisions else None,
            c2_state_machine_counts=c2,c2_counts_are_not_external_independent_validation=True,
            paper_buy_intents=replay.get('entry_funnel',{}).get('ledger_buy_orders',source_counts.get('MEASURED_CONTEXT_PAPER_BUY_INTENTS',0)+source_counts.get('SYNTHETIC_PAPER_BUY_INTENTS',0)),
            paper_total_submitted_orders=replay.get('orders_submitted'),
            paper_buy_fills=sum(f.get('side')=='BUY' for f in replay.get('fills',[])),
            paper_closed_positions=replay.get('completed_count'),estimated_net_pnl_closed=replay.get('net_pnl_closed'),
            actual_broker_trades=False,profitability_proven=False,unknown_counts=dict(self.unknown),
            record_loss_increase=source_counts.get('REPORTED_CAPTURE_LOSS',0),
            preexisting_record_loss_counter=source_counts.get('PREEXISTING_CAPTURE_LOSS_UNKNOWN',0),
            record_loss_scope='SHARED_RECORDER_TARGET_SYMBOL_AND_TIME_RANGE_UNKNOWN_DO_NOT_SUM_OVERLAPPING_SAMPLES',
            source_packet_loss_count=None,source_packet_loss_status='UNKNOWN_NOT_INFERRED_FROM_RECORDER_COUNTERS',
            counter_basis='UNIQUE_OBSERVED_STRUCTURE_IDS_NOT_INDEPENDENT_TRADES_NOT_TICK_EVALUATIONS')
