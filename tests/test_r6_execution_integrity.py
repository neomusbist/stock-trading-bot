"""Receiver/order/exit regressions with a 71,395 KRW synthetic account.

No real broker, credentials, live runtime, future prices or mocked strategy
signals. Recorded incident assertions in test_mk_exit_authority remain separate.
"""
import contextlib
import io
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.engine import AutonomousEngine
from automation.focus_selection import calculate_tier
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig, Policy
from automation.radar import DynamicRadar
from automation import runner
from core.realtime import Feed
from core.settings import Config, KST
from core.strategy import Tick
from tests.test_automation import FakeBroker
from tests import test_mk_exit_authority as mk_fixture
from tests.test_r6_fast_repair import CAUSAL

AT = datetime(2026, 10, 19, 10, 30, tzinfo=KST)
CASH = 71395.7759
SYM = '999990'  # Deliberately synthetic; never label this fixture a real stock.


class AccountFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        output = contextlib.redirect_stdout(io.StringIO()); output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.at = AT
        self.cfg = replace(Config(), live_mode=True, risk_fraction=.02,
                           max_daily_loss_fraction=.04, max_position_fraction=1.,
                           max_positions=1, max_watchlist=12, after_enabled=True,
                           max_entries_per_day=None, max_entries_per_symbol=None)
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300,
                            costs_confirmed=True, fast_momentum_enabled=True,
                            confirmed_invalidation_enabled=True, min_source_count=2,
                            max_consecutive_losses=None, fresh_box_basis_enabled=True,
                            setup_aware_watch_priority=True, confirmed_box_direction=True,
                            structural_pullback_enabled=True, structural_breakout_enabled=True,
                            intraday_context_enabled=True, after_momentum_failure_enabled=True)
        self.policy = Policy(self.root/'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.broker = FakeBroker()
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy,
                                  self.root/'book', self.broker, lambda: self.at)
        self.e.reconcile({'cash':CASH, 'equity':CASH, 'orders':[], 'holdings':{}})
        self.e.connected = True; self.e.market = '3'; self.e.last_account = time.monotonic()
        self.e.market_status[SYM] = {'pass':True, 'reason_code':'SYNTHETIC_NORMAL_STATUS'}
        self.detector = PatternDetector(self.cfg, self.auto, self.e.costs)
        self.detector.fast_strategy = self.e.fast_strategy
        self.radar = DynamicRadar(self.cfg, self.auto, self.policy, fallback_source1=True)
        self.radar.cache = {}
        self.account = SimpleNamespace(need_fills=False)
        self.prices = {}

    def simple_signal(self, at=None):
        return {'symbol':SYM, 'pattern':'C', 'time':(at or self.at).isoformat(),
                'limit':2450, 'stop':2420, 'target':2650, 'source_count':2,
                'efficiency':{'decision':'TRADE','score':1.,
                              'inputs':{'confidence':.8, 'planned_minutes':10}}}

    def quote(self, ask=2450, at=None):
        at = at or self.at
        return Tick(SYM, at, ask, 1000, 1000, ask, ask-5,
                    ask_qty=10000, bid_qty=10000, depth_available=True, book_time=at)


class OrderBoundaryTests(AccountFixture):
    def setUp(self):
        super().setUp()
        # Isolate final-quote/clock contract with a minimal C signal. Full
        # strategy/order tests below retain the actual operational switches.
        self.e.a=replace(self.auto,confirmed_box_direction=False,fresh_box_basis_enabled=False)

    def test_one_tick_tolerance_cannot_authorize_a_different_unsent_limit(self):
        self.e.a = replace(self.e.a, enable_ask_1tick_tolerance=True)
        signal = self.simple_signal()
        self.e.entry_quote_provider = lambda _: (self.quote(2455), {})
        self.assertEqual(self.e.entry_market_reason(signal, 29, phase='PRE_HTTP'),
                         'ENTRY_QUOTE_ABOVE_LIMIT')
        self.assertEqual(signal['limit'], 2450)
        self.assertEqual(self.broker.orders, [])

    def test_exact_limit_and_actual_small_account_quantity_remain_valid(self):
        signal = self.simple_signal()
        self.e.entry_quote_provider = lambda _: (self.quote(), {})
        self.assertIsNone(self.e.entry_market_reason(signal, 29, phase='PRE_HTTP'))
        quantity = self.e.quantity_details(signal,CASH,CASH,self.e.daily_risk_budget())['quantity']
        self.assertEqual(quantity, 29)
        self.assertLessEqual(quantity*signal['limit'], CASH)

    def test_fresh_quote_does_not_revive_expired_abc_trigger(self):
        signal = self.simple_signal(self.at-timedelta(seconds=self.cfg.stale_seconds+1))
        self.e.entry_quote_provider = lambda _: (self.quote(), {})
        self.assertEqual(self.e.entry_market_reason(signal, 29, phase='PRE_HTTP'),
                         'ENTRY_SIGNAL_DATA_STALE')

    def test_receipt_counter_keeps_source_expired_after_wall_clock_correction(self):
        signal = self.simple_signal()
        signal['entry_source_timing'] = {
            'exchange_at':signal['time'], 'received_at':self.at.isoformat(),
            'received_monotonic':1000.}
        self.e.entry_quote_provider = lambda _: (self.quote(), {})
        with patch('time.monotonic', return_value=1000.+self.cfg.stale_seconds+1):
            self.assertEqual(self.e.entry_market_reason(signal,29,phase='PRE_HTTP'),
                             'ENTRY_SIGNAL_DATA_STALE')

    def test_bounded_exchange_lead_remains_valid_but_large_future_is_blocked(self):
        self.e.entry_quote_provider = lambda _: (self.quote(), {})
        for lead in (.051, .293):
            self.assertIsNone(self.e.entry_market_reason(
                self.simple_signal(self.at+timedelta(seconds=lead)),29,phase='PRE_HTTP'))
        self.assertEqual(self.e.entry_market_reason(
            self.simple_signal(self.at+timedelta(seconds=2)),29,phase='PRE_HTTP'),
            'ENTRY_SIGNAL_DATA_STALE')

    def test_source_counter_is_bound_before_intent_and_not_renewed_at_http(self):
        self.e.a=replace(self.e.a,structural_breakout_enabled=False)
        signal=self.simple_signal(); signal['pattern']='A'
        tick=replace(self.quote(),received_at=self.at.isoformat(),received_monotonic=1000.)
        current=[tick];self.e.entry_quote_provider=lambda _:(current[0],{})
        original=self.broker.submit
        def delayed(*args,**kwargs):
            self.at+=timedelta(seconds=self.cfg.stale_seconds+1)
            current[0]=replace(tick,time=self.at,received_at=self.at.isoformat(),
                               received_monotonic=1000.+(self.at-AT).total_seconds())
            return original(*args,**kwargs)
        self.broker.submit=delayed
        # Start and PRE_INTENT at 1000; PRE_HTTP at 1006 via the nested guard.
        def counter():return 1000.+(self.at-AT).total_seconds()
        with patch('time.monotonic',side_effect=counter):
            self.e.last_account=1000.
            order=self.e.enter(signal,tick)
        self.assertIsNotNone(order)
        self.assertEqual(order['status'],'REJECTED')
        self.assertEqual(order['signal']['entry_source_timing']['received_monotonic'],1000.)
        self.assertIn('ENTRY_SIGNAL_DATA_STALE',str(order))
        self.assertEqual(self.broker.orders,[])

    def test_single_share_affordability_and_wider_emergency_reduce_quantity(self):
        signal=self.simple_signal()
        narrow=self.e.quantity_details(signal,CASH,CASH,self.e.daily_risk_budget())
        wide=self.e.quantity_details(dict(signal,emergency=2200),CASH,CASH,self.e.daily_risk_budget())
        expensive=self.e.quantity_details(dict(signal,limit=100000,stop=99000),CASH,CASH,self.e.daily_risk_budget())
        self.assertEqual(narrow['quantity'],29)
        self.assertLess(wide['quantity'],narrow['quantity'])
        self.assertEqual(expensive['quantity'],0)
        self.assertEqual(expensive['reason_code'],'ONE_SHARE_UNAFFORDABLE')


class FocusQuarantineTests(unittest.TestCase):
    def test_shadow_redesign_cannot_raise_live_signal_or_geometry_tier(self):
        gates = {k:{'result':'PASS'} for k in
                 ('warmup','acceleration','liquidity','spread','structure','market_status')}
        trial = {'setup':'EarlyMomentumBreakout','candidate_generated':True,
                 'entry_ready':True,'gates':{'breakout':{'result':'PASS'}}}
        self.assertEqual(calculate_tier(gates,[trial]), 2)
        self.assertEqual(calculate_tier(gates,[dict(trial,setup='A')]), 0)

    def test_invalid_historical_replay_cannot_print_a_pass_or_scan_logs(self):
        script=Path(__file__).resolve().parents[1]/'audit/r6_opportunity_and_orderpath_20261019/replay_4_targets.py'
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('INVALID_FOR_CAUSAL_REPLAY',result.stderr)
        self.assertNotIn('Loaded',result.stdout)


class ReceiverPromotionTests(AccountFixture):
    def seed_receiver(self, *, session='REGULAR', symbol=SYM):
        if session == 'AFTER':
            self.at = AT.replace(hour=16, minute=30)
        start = self.at
        with patch('core.realtime.now',return_value=self.at):
            self.feed = Feed(SimpleNamespace(cfg=self.cfg), [symbol])
        self.feed.configure_observation(set(), {symbol}, None)
        self.e.entry_quote_provider = self.feed.entry_snapshot
        for i, price in enumerate(CAUSAL):
            self.at = start+timedelta(seconds=i, microseconds=100000)
            fields = {'20':self.at.strftime('%H%M%S'),'10':str(price),'15':'1000',
                      '13':str(1000*(i+1)),'27':str(price),'28':str(price-1),
                      '290':'2','61':'10000','71':'10000',
                      '9081':'SOR' if session=='AFTER' else 'KRX'}
            book = {'type':'0D','item':self.feed.wire_symbols[0],'values':{'21':fields['20'],
                    '41':str(price),'51':str(price-1),'61':'10000','71':'10000'}}
            with patch('core.realtime.now', return_value=self.at):
                self.feed.emit({'trnm':'REAL','data':[book,{'type':'0B','item':self.feed.wire_symbols[0],'values':fields}]})
        self.assertTrue(self.feed.q.empty())
        self.assertEqual(self.feed.observation_q.qsize(), len(CAUSAL))
        self.row = {'code':symbol,'name':'Synthetic causal support','status':'SHADOW',
                    'reference_price':984,'resistance':1045,'resistance_asof':start.isoformat(),
                    'source_count':2,'discovery_score':1.,
                    'market_status':{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'}}
        # Historical daily evidence known before the decision, not future maxima.
        runner.qualify_raw(self.e,[self.row],self.at.isoformat())
        self.radar.cache[(self.at.date(),symbol)] = [
            {'datetime':(start-timedelta(days=751-i)).date().isoformat(),
             'time':start-timedelta(days=751-i),'open':980,'high':1045,
             'low':900,'close':1000,'volume':2000000} for i in range(750)]

    def test_real_receiver_queue_reaches_existing_fast_and_small_account_order(self):
        self.seed_receiver()
        lookup = {}; by_symbol = {SYM:self.row}
        calls = []
        def evaluate(t, received):
            calls.append((t.symbol,received))
            runner.evaluate_observed_entry(self.e,self.detector,t,received,lookup,
                self.radar,self.prices,self.account,by_symbol)
        runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
                               on_opportunity=evaluate)
        # A busy ingestion pass may defer evaluation to the next bounded pass.
        for _ in range(2):
            if self.broker.orders:break
            runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
                                   on_opportunity=evaluate)
        self.assertIn(SYM,lookup)
        self.assertEqual(len(self.broker.orders),1)
        side,symbol,quantity,limit = self.broker.orders[0]
        self.assertEqual((side,symbol,limit),('BUY',SYM,1001))
        self.assertLessEqual(quantity*limit,CASH)
        self.assertGreater(quantity,0)
        self.assertEqual(len(calls),1)
        order=next(iter(self.e.s['orders'].values()))
        self.assertEqual(order['status'],'ACCEPTED')
        # Broker transport/fills are simulated, but the real ledger and exit
        # machinery handle them. Duplicate candidate delivery is not a new BUY.
        tick=self.feed.latest_ticks[SYM]
        evaluate(tick,self.at)
        self.assertEqual(len(self.broker.orders),1)
        self.e.cumulative_fill(order,quantity,quantity*limit)
        self.e.actual={SYM:{'qty':quantity,'available':quantity,'avg':limit}}
        self.assertEqual(order['status'],'FILLED')
        position=self.e.s['positions'][SYM]
        self.assertEqual(position['qty'],quantity)
        self.assertEqual((position['stop'],position['emergency']),(989,985))

    def test_fast_receipt_timestamp_can_win_full_watch_priority(self):
        self.seed_receiver()
        self.e.c = replace(self.cfg,max_watchlist=1)
        runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0)
        incumbent = {'code':'999980','name':'Synthetic incumbent','status':'WATCH',
                     'source_count':2,'discovery_score':100.}
        lookup = {'999980':incumbent}
        promoted = runner.evaluate_shadow_opportunity(self.e,self.detector,
            self.feed.latest_ticks[SYM],self.row,self.radar,self.prices,self.account,
            by_symbol={SYM:self.row,'999980':incumbent},lookup=lookup)
        self.assertTrue(promoted)
        self.assertEqual(set(lookup),{SYM})

    def drain_opportunity(self,callback):
        return runner.prewarm_handoff(self.e,self.detector,self.feed,set(self.feed.symbols),set(),set(),0,
                                      on_opportunity=callback)

    def test_order_and_disconnect_frames_take_priority_without_discarding_observations(self):
        self.seed_receiver();calls=[]
        control=(self.at,{'trnm':'DISCONNECTED','reason':'synthetic barrier'})
        self.feed.q.put_nowait(control)
        self.drain_opportunity(lambda *args:calls.append(args))
        self.assertEqual(calls,[])
        self.assertEqual(self.feed.q.get_nowait(),control);self.feed.q.task_done()
        self.drain_opportunity(lambda *args:calls.append(args))
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.e.fast_strategy.get_tape(SYM).ticks),len(CAUSAL))

    def test_a_queued_opportunity_expires_instead_of_using_a_new_quote(self):
        self.seed_receiver();calls=[]
        self.feed.transport_failed=True
        self.drain_opportunity(lambda *args:calls.append(args))
        self.feed.transport_failed=False;self.at+=timedelta(seconds=6)
        self.drain_opportunity(lambda *args:calls.append(args))
        self.assertEqual(calls,[])
        self.assertEqual(self.broker.orders,[])

    def test_actual_risk_and_permission_blocks_remain_in_routed_order_path(self):
        for blocked in ('STOP','DAILY_LIMIT','HARD_LOCK','ACCOUNT_STALE','SPREAD'):
            with self.subTest(block=blocked):
                # New, independent account/tape for each risk counterexample.
                case=ReceiverPromotionTests();case.setUp()
                try:
                    symbol='005930' if blocked=='HARD_LOCK' else SYM
                    case.seed_receiver(symbol=symbol)
                    if blocked=='STOP':case.policy.command('STOP')
                    elif blocked=='DAILY_LIMIT':case.e.s['daily_loss']=case.e.daily_risk_budget()
                    elif blocked=='ACCOUNT_STALE':case.e.last_account-=60
                    elif blocked=='SPREAD':
                        tick=case.feed.latest_ticks[SYM]
                        case.feed.latest_ticks[SYM]=replace(tick,ask=tick.price+50)
                    lookup={};by_symbol={symbol:case.row}
                    callback=lambda t,r:runner.evaluate_observed_entry(case.e,case.detector,t,r,lookup,
                        case.radar,case.prices,case.account,by_symbol)
                    for _ in range(3):case.drain_opportunity(callback)
                    self.assertEqual(case.broker.orders,[])
                finally:case.doCleanups()

    def test_after_session_requires_separate_authorization(self):
        self.seed_receiver(session='AFTER')
        lookup={};by_symbol={SYM:self.row}
        callback=lambda t,r:runner.evaluate_observed_entry(self.e,self.detector,t,r,lookup,
            self.radar,self.prices,self.account,by_symbol)
        for _ in range(3):self.drain_opportunity(callback)
        self.assertIsNotNone(self.e.entry_quote_provider(SYM)[0])
        self.assertEqual(self.e.new_entry_reason(SYM),'AFTER_LIVE_NOT_AUTHORIZED')
        self.assertEqual(self.broker.orders,[])

    def test_after_routed_signal_with_separate_simulated_approval_can_submit(self):
        self.seed_receiver(session='AFTER');self.e.after_live_authorized=True
        lookup={};by_symbol={SYM:self.row}
        callback=lambda t,r:runner.evaluate_observed_entry(self.e,self.detector,t,r,lookup,
            self.radar,self.prices,self.account,by_symbol)
        for _ in range(3):self.drain_opportunity(callback)
        self.assertEqual(len(self.broker.orders),1)
        self.assertLessEqual(self.broker.orders[0][2]*self.broker.orders[0][3],CASH)

    def test_pending_scheduler_is_bounded_fair_and_preserves_the_latest_frame(self):
        self.seed_receiver()
        t=self.feed.latest_ticks[SYM];calls=[]
        self.feed._opportunity_pending={str(i).zfill(6):(self.at,replace(t,symbol=str(i).zfill(6)))
                                        for i in range(8)}
        newer=replace(t,symbol='000000',cumulative=t.cumulative+100)
        self.feed._opportunity_pending['000000']=(self.at,newer)
        for count in range(4):
            self.assertEqual(runner.drain_observed_opportunities(self.e,self.feed,
                lambda tick,received:calls.append(tick)),2)
            self.assertEqual(len(calls),2*(count+1))
        self.assertEqual([t.symbol for t in calls],[str(i).zfill(6) for i in range(8)])
        self.assertEqual(calls[0].cumulative,newer.cumulative)

    def test_bounded_future_exchange_tick_does_not_erase_fast_readiness(self):
        for lead in (.051,.293,2.):
            stamp=self.at+timedelta(seconds=lead)
            tick=replace(self.quote(at=stamp),received_at=self.at.isoformat())
            self.detector.last[SYM]=tick
            self.detector.last_eval[SYM]={'time':stamp,'gates':{'warmup':{'result':'FAIL'},
                'market_status':{'result':'PASS'}},'trials':[]}
            self.e.fast_strategy.active_signals[SYM]=SimpleNamespace(created_at=self.at,ttl_seconds=15.)
            ready=runner.focus_readiness(self.detector,{},self.at)
            if lead<=1:self.assertTrue(ready[SYM]['fast_signal_ready'])
            else:self.assertNotIn(SYM,ready)


class FastPolicyWithoutGlobalFlagTests(mk_fixture.MKExitAuthorityTests):
    # Reuse saved incident fixture and all its safety regressions with the
    # global ABC switch OFF; FAST still declares its own structural policy.
    def setUp(self):
        super().setUp()
        self.e.a = replace(self.auto, confirmed_invalidation_enabled=False)

    def test_common_emergency_uses_the_receiver_clock_contract_when_fast_is_unavailable(self):
        self.e.fast_strategy=None
        self.at=datetime.fromisoformat(self.p['opened'])+timedelta(seconds=10)
        for lead in (2.,.293):
            tick=replace(self.tick(16830),time=self.at+timedelta(seconds=lead),
                         received_at=self.at.isoformat())
            self.e.on_tick(tick)
            self.assertEqual(self.p['exit_requested'],lead<=1)
        self.assertEqual(len(self.broker.orders),1)


if __name__ == '__main__':
    unittest.main()
