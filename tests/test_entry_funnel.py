import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from automation.entry_funnel import EntryFunnel, observe_evaluation
from automation.candidate_audit import CandidateAudit
from automation.decisions import DecisionLog, gate
from automation.policy import AutomationConfig
from core.settings import Config, KST

AT = datetime(2026, 10, 21, 10, tzinfo=KST)


def trial(name='B', ready=False, unknown=False):
    return dict(setup=name, evaluated=True, candidate_generated=True, entry_ready=ready,
        primary_reason_code='READY' if ready else 'WARMUP_FAIL' if unknown else 'NO_REBREAKOUT',
        failed_reasons=[] if ready else ['WARMUP_FAIL'] if unknown else ['NO_REBREAKOUT'],
        gates={'warmup': gate(not unknown), 'breakout': gate(ready),
               'risk_reward': gate(True, target=1100)}, virtual_entry=1000, virtual_invalidation=980)


class EntryFunnelTests(unittest.TestCase):
    def test_rejected_continuation_is_not_misclassified_as_acceleration_burst(self):
        f=EntryFunnel();item=trial('A')
        item.update(entry_confirmation_basis='ACCELERATED_BREAKOUT',candidate_confirmation_basis='CONFIRMED_CONTINUATION')
        f.evaluation('000001',AT,'WATCH',[item],None,fast_enabled=False)
        counts=f.snapshot(AT)['strategies']
        self.assertEqual(counts['REGULAR:WATCH:A_continuation']['evaluated'],1)
        self.assertNotIn('REGULAR:WATCH:A_burst',counts)
    def test_daily_totals_survive_radar_cycles_and_missing_evaluation_is_visible(self):
        audit = CandidateAudit()
        audit.funnel.rows([{'code':'000001'},{'code':'000002'}], AT, 'RAW')
        for _ in range(3): audit.begin([{'code':'000001','status':'WATCH'}], AT.isoformat(), {})
        audit.funnel.evaluation('000001', AT, 'WATCH', [trial()], {'status':'REJECT','reason':'VELOCITY'})
        result = audit.funnel.snapshot(AT)
        self.assertEqual(result['stages']['RAW'], 2)
        self.assertEqual(result['unique_symbols']['WATCH_AT_SCAN'], 1)
        self.assertEqual(result['raw_not_evaluated'], ['000002'])
        self.assertEqual(result['strategies']['REGULAR:WATCH:B']['evaluated'], 1)

    def test_unknown_data_reject_and_candidate_have_separate_counts(self):
        f=EntryFunnel()
        for item in (trial(unknown=True), trial(), trial(ready=True)):
            f.evaluation('000001', AT, 'WATCH', [item], {'status':'UNKNOWN','reason':'DEPTH_UNKNOWN'})
        counts=f.snapshot(AT)['strategies']['REGULAR:WATCH:B']
        self.assertEqual((counts['UNKNOWN'],counts['REJECT'],counts['SIGNAL_CANDIDATE']),(1,1,1))
        self.assertEqual(f.snapshot(AT)['strategies']['REGULAR:WATCH:FAST']['UNKNOWN'],3)

    def test_abc_priority_does_not_become_a_fast_rejection(self):
        f=EntryFunnel();f.evaluation('000001', AT, 'WATCH',[trial(ready=True)],None,{'pattern':'B'})
        s=f.snapshot(AT)
        self.assertEqual(s['strategies']['REGULAR:WATCH:FAST']['NOT_RUN'],1)
        self.assertNotIn('REJECT',s['strategies']['REGULAR:WATCH:FAST'])
        self.assertEqual(s['reasons']['REGULAR:WATCH:FAST'],{'ABC_PRIORITY':1})

    def test_probe_and_watch_are_not_merged_into_opportunity_count(self):
        f=EntryFunnel()
        for scope in ('SHADOW_PROBE','SHADOW','WATCH'):
            f.evaluation('000001',AT,scope,[trial(ready=True)],None,{'pattern':'B'})
        s=f.snapshot(AT)
        self.assertEqual([s['strategies']['REGULAR:'+scope+':B']['SIGNAL_CANDIDATE'] for scope in ('SHADOW_PROBE','SHADOW','WATCH')],[1,1,1])
        self.assertIn('SCOPES_SEPARATE',s['count_basis'])

    def test_signal_and_order_gate_and_ack_are_separate(self):
        f=EntryFunnel()
        f.evaluation('000001',AT,'WATCH',[trial(ready=True)],None,{'pattern':'B'})
        f.boundary('000001',AT,'DAILY_RISK_LIMIT',{'risk_quantity':gate(False,quantity=0,risk_quantity=0)},{'pattern':'B'})
        f.execution('ORDER_ACCEPTED','000001',AT,'SELL')
        s=f.snapshot(AT)
        self.assertEqual(s['stages']['SIZING_ZERO'],1)
        self.assertEqual(s['stages']['ORDER_ACCEPTED:SELL'],1)
        self.assertNotIn('ORDER_ACCEPTED:BUY',s['stages'])
        self.assertEqual(s['boundaries']['REGULAR:POST_SIGNAL:DAILY_RISK_LIMIT'],1)

    def test_identifiers_examples_and_variable_reasons_are_bounded(self):
        f=EntryFunnel(symbol_limit=8,example_limit=5)
        for i in range(100):
            sym=f'{i:06d}';f.rows([{'code':sym}],AT,'RAW')
            f.evaluation(sym,AT,'WATCH',[trial()],{'status':'REJECT','reason':str(i)})
            f.boundary(sym,AT,str(i),{},None)
        s=f.snapshot(AT)
        self.assertEqual(s['unique_symbols']['RAW'],8)
        self.assertEqual(s['distinct_overflow']['RAW'],92)
        self.assertLessEqual(len(s['examples']),5)
        self.assertLessEqual(len(s['reasons']['REGULAR:WATCH:FAST']),33)
        self.assertLess(len(json.dumps(s)),16000)

    def test_next_day_resets_and_does_not_claim_prior_process_coverage(self):
        f=EntryFunnel();f.rows([{'code':'000001'}],AT,'RAW')
        s=f.snapshot(AT.replace(day=22))
        self.assertEqual(s['stages'],{})
        self.assertIn('PROCESS_LIFETIME_ONLY',s['coverage'])

    def test_diagnostic_failure_does_not_interrupt_the_caller(self):
        engine=SimpleNamespace(candidate_audit=CandidateAudit(),clock=lambda:AT,
            a=SimpleNamespace(fast_momentum_enabled=True))
        tick=SimpleNamespace(symbol='000001',price=1000,ask=1000,bid=999)
        with patch.object(EntryFunnel,'evaluation',side_effect=RuntimeError('telemetry failure')):
            observe_evaluation(engine,tick,{},SimpleNamespace(last_eval={}),None,None,'WATCH')
        self.assertEqual(engine.entry_funnel_errors,1)

    def test_decision_throttling_does_not_erase_boundary_invocations(self):
        events=[];auto=AutomationConfig();cfg=Config()
        engine=SimpleNamespace(candidate_audit=CandidateAudit(),clock=lambda:AT,a=auto,c=cfg,
            policy=SimpleNamespace(mode='LIVE'),s={'daily_loss':0},names={},prices={},
            risk_equity=lambda:81300,capital_context=lambda:{},active_orders=lambda *a:[],
            available_cash=71760,auto_available_capital=lambda:71760,watch_budget=lambda:71760,
            event=lambda *a,**k:events.append((a,k)))
        log=DecisionLog(engine)
        for _ in range(3):log.emit('ENTRY_EVAL','000001','NO_BUY','PATTERN_OR_GATE_NOT_READY',{'warmup':gate(False)},price=1000)
        self.assertEqual(len(events),1)
        self.assertEqual(engine.candidate_audit.funnel.snapshot(AT)['boundaries']['REGULAR:PRE_STRATEGY_OR_NO_SIGNAL:PATTERN_OR_GATE_NOT_READY'],3)

    def test_fast_rejection_retains_observed_threshold_inputs(self):
        from automation.fast_momentum import FastMomentumStrategy
        from core.strategy import Tick
        from datetime import timedelta
        s=FastMomentumStrategy(Config(),AutomationConfig())
        for i in range(8):
            stamp=AT+timedelta(seconds=i)
            tick=Tick('000001',stamp,1000,1000,1000*(i+1),1001,1000,
                depth_available=True,ask_qty=10000,bid_qty=10000,book_time=stamp,
                received_at=stamp.isoformat(),book_received_at=stamp.isoformat())
            s.evaluate(tick,{},received=stamp)
        note=s.last_evaluation['000001']
        self.assertEqual(note['status'],'REJECT')
        self.assertEqual(note['metrics']['ticks_10'],8)
        self.assertEqual(note['metrics']['turnover_10'],8000000)
        self.assertEqual(note['metrics']['uptick_ratio_30'],0)
        json.dumps(note)


class FunnelPipelineTests(unittest.TestCase):
    def lifecycle(self, name, fail_diagnostic=False):
        from tests.test_live_ready_20261021 import LiveReadyLifecycle, SYM
        h=LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.e.candidate_audit=CandidateAudit()
        if fail_diagnostic:
            with patch.object(EntryFunnel,'evaluation',side_effect=RuntimeError('diagnostic only')):
                h.lifecycle(name)
            self.assertGreater(h.e.entry_funnel_errors,0)
        else:
            h.lifecycle(name)
            s=h.e.candidate_audit.funnel.snapshot(h.clock)
            self.assertGreater(s['strategies']['REGULAR:WATCH:'+name]['SIGNAL_CANDIDATE'],0)
            self.assertEqual(s['stages']['ORDER_ACCEPTED:BUY'],1)
            self.assertEqual(s['stages']['ORDER_ACCEPTED:SELL'],1)
            self.assertGreaterEqual(s['stages']['FILL:BUY'],2)
            self.assertGreaterEqual(s['stages']['FILL:SELL'],1)
            buy=next(o for o in h.e.s['orders'].values() if o['side']=='BUY')
            route=EntryFunnel.strategy_name(buy['signal'])
            self.assertEqual(s['unique_execution_orders']['REGULAR:BROKER:'+route+':BUY:FILL'],1)
            self.assertEqual(s['unique_execution_orders']['REGULAR:BROKER:'+route+':SELL:FILL'],1)
        self.assertEqual(h.e.s['positions'],{})
        self.assertEqual(h.e.active_orders(),[])

    def test_A_funnel_through_durable_order_partial_fill_and_exit(self):self.lifecycle('A')
    def test_B_funnel_through_durable_order_partial_fill_and_exit(self):self.lifecycle('B')
    def test_C_cost_rejection_never_reaches_intent_or_broker(self):
        from tests.test_live_ready_20261021 import LiveReadyLifecycle
        h=LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.e.candidate_audit=CandidateAudit();h.test_C_gross_positive_is_rejected_before_intent_after_costs()
        snapshot=h.e.candidate_audit.funnel.snapshot(h.clock)
        self.assertEqual(snapshot['stages'].get('ORDER_ACCEPTED:BUY',0),0)
        self.assertGreater(snapshot['strategies']['REGULAR:WATCH:C']['REJECT'],0)
    def test_FAST_funnel_through_durable_order_partial_fill_and_exit(self):self.lifecycle('FAST')
    def test_telemetry_failure_preserves_order_and_exit_path(self):self.lifecycle('FAST',True)


if __name__=='__main__':unittest.main()
