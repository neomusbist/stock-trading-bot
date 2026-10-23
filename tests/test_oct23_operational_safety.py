"""Causal quote joins, unchanged-account latency and predecision target evidence."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.engine import AutonomousEngine
from automation.fast_momentum import FastMomentumStrategy
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy
from core.realtime import current_book_tick
from core.settings import Config, KST
from core.strategy import Tick
from tests.test_automation import FakeBroker
from tests.test_r6_fast_repair import ticks, watch

AT = datetime(2026, 10, 22, 9, 15, tzinfo=KST)


class QuoteCausalityTests(unittest.TestCase):
    def frame(self):
        tick = Tick('999990', AT, 1001, 100, 1000, 1001, 1000,
                    received_at=AT.isoformat(), received_monotonic=100.,
                    depth_available=False)
        book = dict(best_bid=999, best_ask=1000, bid_qty=1000, ask_qty=1000,
                    depth_available=True, time=AT.isoformat(),
                    received_at=(AT-timedelta(milliseconds=100)).isoformat(),
                    received_monotonic=99.9)
        return tick, book

    def test_changed_book_received_before_new_trade_cannot_replace_its_bbo(self):
        tick, book = self.frame()
        with patch('core.market_time.time.monotonic', return_value=100.1):
            self.assertEqual(current_book_tick(tick, book, AT+timedelta(milliseconds=100), 5), tick)

    def test_same_wall_clock_but_earlier_monotonic_book_cannot_replace_bbo(self):
        tick, book = self.frame()
        book['received_at'] = AT.isoformat()
        with patch('core.market_time.time.monotonic', return_value=100.1):
            self.assertEqual(current_book_tick(tick, book, AT+timedelta(milliseconds=100), 5), tick)

    def test_genuinely_later_changed_quote_keeps_original_trade(self):
        tick, book = self.frame()
        book.update(received_at=(AT+timedelta(milliseconds=50)).isoformat(), received_monotonic=100.05)
        with patch('core.market_time.time.monotonic', return_value=100.1):
            joined = current_book_tick(tick, book, AT+timedelta(milliseconds=100), 5)
        self.assertEqual(joined.ask, 1000)
        self.assertEqual((joined.time, joined.received_at, joined.received_monotonic, joined.volume, joined.cumulative),
                         (tick.time, tick.received_at, tick.received_monotonic, tick.volume, tick.cumulative))


class ReconcileCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.clock = [AT]
        cfg = replace(Config(), risk_fraction=.02, max_daily_loss_fraction=.04)
        auto = replace(AutomationConfig(), costs_confirmed=True)
        self.e = AutonomousEngine(cfg, auto, Policy(self.root/'policy.json'), self.root/'book',
                                  FakeBroker(), lambda: self.clock[0])
        self.e.console.emit = lambda *args: None
        self.snapshot = dict(cash=71822., equity=71822., holdings={}, orders=[])
        self.e.reconcile(self.snapshot)

    def test_timestamp_only_flat_refresh_does_not_repeat_durable_io(self):
        prior = self.e.path.read_bytes()
        self.clock[0] += timedelta(seconds=5)
        with patch('core.engine.atomic_json', wraps=__import__('core.engine', fromlist=['atomic_json']).atomic_json) as writes:
            self.e.reconcile(self.snapshot)
        self.assertEqual(writes.call_count, 0)
        self.assertEqual(self.e.path.read_bytes(), prior)
        self.assertTrue(self.e.account_ok)
        self.assertEqual(self.e.s['automation']['capital_snapshot']['timestamp'], self.clock[0].isoformat())

    def test_identical_flat_refresh_does_not_copy_closed_history(self):
        copies=[]
        class ArchivedRows(list):
            def __deepcopy__(rows,memo):
                copies.append(len(rows))
                return list(rows)
        self.e.s['closed_history_evidence']=ArchivedRows([{'value':i} for i in range(1000)])
        self.e.reconcile(self.snapshot)
        copies.clear()
        self.clock[0]+=timedelta(seconds=5)
        self.e.reconcile(self.snapshot)
        self.assertEqual(copies,[])
        self.assertTrue(self.e.account_ok)

    def test_nested_closed_history_change_is_durable_without_aliasing(self):
        self.e.s['closed_history_evidence']=[{'cost':{'confirmed_fee':10}}]
        self.e.reconcile(self.snapshot)
        self.e.s['closed_history_evidence'][0]['cost']['confirmed_fee']=11
        self.clock[0]+=timedelta(seconds=5)
        self.e.reconcile(self.snapshot)
        actual=json.loads(self.e.path.read_bytes())
        self.assertEqual(actual['closed_history_evidence'][0]['cost']['confirmed_fee'],11)
        self.assertEqual(self.e.reconcile_checkpoint_stats['last_action'],'DURABLE_CHECKPOINT')

    def test_cash_change_is_durable_before_account_is_usable(self):
        self.clock[0] += timedelta(seconds=5)
        changed = {**self.snapshot, 'cash': 71000.}
        with patch('core.engine.atomic_json', wraps=__import__('core.engine', fromlist=['atomic_json']).atomic_json) as writes:
            self.e.reconcile(changed)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(json.loads(self.e.path.read_bytes()), self.e.s)

    def test_critical_state_change_is_never_discarded_as_timestamp_only(self):
        self.clock[0] += timedelta(seconds=5)
        self.e.s['daily_loss'] += 10.
        with patch('core.engine.atomic_json', wraps=__import__('core.engine', fromlist=['atomic_json']).atomic_json) as writes:
            self.e.reconcile(self.snapshot)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(json.loads(self.e.path.read_bytes())['daily_loss'], 10.)

    def test_failed_changed_checkpoint_blocks_account_and_retries_durably(self):
        prior = self.e.path.read_bytes()
        with patch('core.util.os.fsync', side_effect=OSError('controlled disk fault')):
            with self.assertRaises(OSError):
                self.e.reconcile({**self.snapshot, 'cash': 71000.})
        self.assertFalse(self.e.account_ok)
        self.assertEqual(self.e.path.read_bytes(), prior)
        self.e.reconcile({**self.snapshot, 'cash': 71000.})
        self.assertEqual(json.loads(self.e.path.read_bytes()), self.e.s)


class FastTargetEvidenceTests(unittest.TestCase):
    def strategy(self):
        cfg = replace(Config(), risk_fraction=.02, max_daily_loss_fraction=.04)
        auto = replace(AutomationConfig(), costs_confirmed=True)
        strategy = FastMomentumStrategy(cfg, auto)
        data = ticks(start=AT)
        for tick in data:
            strategy.ingest_tick(tick, tick.time)
        return strategy, data[-1]

    def test_new_high_with_only_distant_daily_target_cannot_invent_seven_percent_headroom(self):
        strategy, tick = self.strategy()
        daily_only=watch(resistance=1500, at=AT)
        daily_only.pop('nearest_confirmed_peak',None)
        geometry = strategy.calculate_invalidation_and_target(tick, {}, daily_only, asof=tick.time)
        self.assertFalse(geometry['pass'])
        self.assertEqual(geometry['reason'], 'INTRADAY_TARGET_CONTEXT_UNAVAILABLE')

    def test_preknown_intraday_ceiling_allows_normal_breakout(self):
        strategy, tick = self.strategy()
        known = watch(at=AT)
        known['nearest_confirmed_peak'] = dict(price=1045, time=(AT-timedelta(minutes=3)).isoformat(),
            confirmed_at=(AT-timedelta(minutes=2)).isoformat(), available_at=(AT-timedelta(minutes=2)).isoformat())
        signal = strategy.evaluate(tick, known, ingest=False, received=tick.time)
        self.assertIsNotNone(signal, strategy.last_evaluation.get(tick.symbol))
        self.assertEqual(signal.target, 1045)
        self.assertEqual(signal.extra['geometry']['predecision_day_high'], 1000)

    def test_future_intraday_peak_cannot_authorize_a_daily_target(self):
        strategy, tick = self.strategy()
        known = watch(resistance=1500, at=AT)
        known['nearest_confirmed_peak'] = dict(price=1400, time=AT.isoformat(),
            confirmed_at=(tick.time+timedelta(seconds=1)).isoformat())
        geometry = strategy.calculate_invalidation_and_target(tick, {}, known, asof=tick.time)
        self.assertFalse(geometry['pass'])
        self.assertEqual(geometry['reason'], 'INTRADAY_TARGET_CONTEXT_UNAVAILABLE')


class PullbackCeilingTests(unittest.TestCase):
    def test_existing_known_peak_is_not_ignored_by_b(self):
        self.check_ceiling(False)

    def test_opt_in_target_experiment_still_cannot_erase_known_overhead(self):
        self.check_ceiling(True)

    def check_ceiling(self, expansion):
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        auto=replace(AutomationConfig(),costs_confirmed=True,entry_context_enabled=True,
                     pullback_expansion_target_enabled=expansion)
        e=AutonomousEngine.__new__(AutonomousEngine)
        from automation.capital import Costs
        detector=PatternDetector(cfg,auto,Costs(.00015,.00015,.002,.002,.003))
        tick=Tick('999990',AT,10380,100,1000,10380,10370,
                  depth_available=True,ask_qty=10000,bid_qty=10000,book_time=AT)
        setup=dict(pattern='B',breakout=10400,support=10300,projection=11200)
        with patch.object(detector.price_context,'snapshot',return_value={'nearest_confirmed_peak':{'price':10400}}):
            gates=detector.setup_gates(tick,{'resistance':12000},setup,[])
        self.assertEqual(gates['risk_reward']['values']['target'],10400)

    def test_b_can_pass_existing_peak_target_without_expansion(self):
        from automation.capital import Costs
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        auto=replace(AutomationConfig(),costs_confirmed=True,structural_pullback_enabled=True,
                     confirmed_invalidation_enabled=True,pullback_expansion_target_enabled=False)
        detector=PatternDetector(cfg,auto,Costs(.00015,.00015,.002,.002,.003))
        values=[(10000,10500,9980,10480,5000),(10480,11500,10400,11480,8000),
                (11480,12000,11450,11980,10000),(11950,11950,10900,10950,3000),
                (10950,10950,10700,10720,2000),(10720,10760,10710,10750,4000)]
        bars=[dict(time=AT-timedelta(minutes=len(values)-i),open=o,high=h,low=l,close=c,
                   volume=v,first_second=0) for i,(o,h,l,c,v) in enumerate(values)]
        setup=detector.pullback(bars)
        self.assertIsNotNone(setup)
        tick=Tick('999990',AT,10960,100,1000,10960,10950,
                  depth_available=True,ask_qty=10000,bid_qty=10000,book_time=AT)
        gates=detector.setup_gates(tick,{'resistance':13000},setup,bars)
        self.assertEqual(gates['risk_reward']['result'],'PASS',gates)
        self.assertLessEqual(gates['risk_reward']['values']['target'],12000)


class FunnelIdentityTests(unittest.TestCase):
    def test_repeat_signals_partial_fills_and_scopes_are_not_unique_opportunities(self):
        from automation.entry_funnel import EntryFunnel
        from automation.decisions import gate
        funnel=EntryFunnel()
        signal=dict(pattern='A',limit=1000,stop=970,target=1080,
                    entry_confirmation_basis='CONFIRMED_CONTINUATION')
        trial=dict(setup='A',entry_ready=True,candidate_generated=True,
                   entry_confirmation_basis='CONFIRMED_CONTINUATION',gates={'warmup':gate(True)})
        for _ in range(3):funnel.evaluation('999990',AT,'WATCH',[trial],None,signal)
        funnel.evaluation('999990',AT,'SHADOW',[trial],None,signal)
        order=dict(local='ONE_ORDER',side='BUY',signal=signal)
        for kind in ('ORDER_ACCEPTED','FILL','FILL'):
            funnel.execution(kind,'999990',AT,'BUY',order)
        snapshot=funnel.snapshot(AT)
        self.assertEqual(snapshot['unique_signal_fingerprints']['REGULAR:WATCH:A_continuation'],1)
        self.assertEqual(snapshot['unique_signal_fingerprints']['REGULAR:SHADOW:A_continuation'],1)
        self.assertEqual(snapshot['unique_execution_orders']['REGULAR:BROKER:A_continuation:BUY:FILL'],1)
        self.assertEqual(snapshot['strategies']['REGULAR:WATCH:A_continuation']['evaluated'],3)
        self.assertIn('UNKNOWN',snapshot['independent_market_opportunities'])

    def test_accepted_order_local_resolves_symbol_and_buy_side(self):
        from automation.candidate_audit import CandidateAudit
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        engine=AutonomousEngine(replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04),
            replace(AutomationConfig(),costs_confirmed=True),Policy(Path(temp.name)/'policy.json'),
            Path(temp.name)/'book',FakeBroker(),lambda:AT)
        engine.console.emit=lambda *args:None
        engine.candidate_audit=CandidateAudit()
        engine.s['orders']['LOCAL']=dict(local='LOCAL',symbol='999990',side='BUY',signal={'pattern':'B'})
        engine.event('ORDER_ACCEPTED',local='LOCAL',id='FAKE_ACK')
        snapshot=engine.candidate_audit.funnel.snapshot(AT)
        self.assertEqual(snapshot['stages']['ORDER_ACCEPTED:BUY'],1)
        self.assertEqual(snapshot['unique_execution_orders']['REGULAR:BROKER:B:BUY:ORDER_ACCEPTED'],1)


class OpeningInterleaveTests(unittest.TestCase):
    def test_first_print_does_not_become_its_own_predecision_high(self):
        from tests.test_r6_fast_live_pipeline import FastPipelineProductionLikeFlagsTests,SYM
        harness=FastPipelineProductionLikeFlagsTests('runTest');harness.setUp();self.addCleanup(harness.doCleanups)
        with patch.object(harness.e.fast_strategy,'evaluate',return_value=None) as evaluate, \
                patch.object(harness.detector.price_context,'day_high',return_value=1001):
            harness.feed(harness.frame(1001))
        self.assertTrue(evaluate.called)
        self.assertIsNone(evaluate.call_args.args[1].get('prior_day_high'))

    def test_queued_opening_prints_and_account_refreshes_preserve_protection_and_order(self):
        from tests.test_oct23_entry_delivery import WatchBurstTests
        harness=WatchBurstTests('runTest');harness.setUp();self.addCleanup(harness.doCleanups)
        engine=harness.h.e
        from automation.candidate_audit import CandidateAudit
        engine.candidate_audit=CandidateAudit()
        original=harness.h.feed
        consumed=[0];account_checks=[0]
        def consume(*args,**kwargs):
            consumed[0]+=1
            if consumed[0]%32==0:
                with patch.object(engine,'save',wraps=engine.save) as saved:
                    engine.reconcile(harness.h.snap)
                self.assertEqual(saved.call_count,0,'unchanged account must not stall the print FIFO')
                account_checks[0]+=1
            return original(*args,**kwargs)
        harness.h.feed=consume
        harness.burst()
        self.assertEqual(consumed[0],311)
        self.assertEqual(account_checks[0],9)
        self.assertEqual(len(harness.h.orders('BUY')),1)
        self.assertEqual(engine.candidate_audit.funnel.snapshot(harness.h.clock[0])['stages']['ORDER_ACCEPTED:BUY'],1)


if __name__ == '__main__':
    unittest.main()
