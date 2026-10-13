"""Setup-specific acceleration: real pattern math, synthetic ticks, guarded fake broker."""
import contextlib,copy,io,json,tempfile,time,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import Policy,AutomationConfig
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.shadow_observation import ShadowObserver
from automation.focus_selection import calculate_tier,TIER_SIGNAL_READY
from automation.observation_worker import ObservationWorker
from automation.decisions import BUILD_ID
from automation.readiness import authorization_valid
from tests import test_pipeline as pipeline
from tests.test_automation import FakeBroker

SYM='999990';START=datetime(2026,10,7,10,0,tzinfo=KST)
class SetupAccelerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        out=contextlib.redirect_stdout(io.StringIO());out.__enter__();self.addCleanup(out.__exit__,None,None,None)
        self.clock=START;self.cfg=replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04,
            max_position_fraction=1,max_positions=1,max_entries_per_day=3)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
        self.policy=Policy(self.root/'policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.b=FakeBroker();self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.b,lambda:self.clock)
        self.e.reconcile({'cash':20420,'equity':20420,'holdings':{},'orders':[]});self.e.connected=True;self.e.market='3'
        self.e.market_status={SYM:{'pass':True}}
        self.watch={'code':SYM,'status':'WATCH','source_count':2,'market_status':{'pass':True},'resistance':11000,
            'resistance_room_pass':True,'source_details':{'one':{},'two':{}}}
        self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
    def feed(self,pattern,fast=False,final_price=None):
        cumulative=0;rows=pipeline.replay_prices(pattern)
        for i,(sec,price,vol) in enumerate(rows):
            if not fast and i<len(rows)-1:vol*=20
            if i==len(rows)-1 and final_price is not None:price=final_price
            self.clock=START+timedelta(seconds=sec);cumulative+=vol
            self.t=Tick(SYM,self.clock,price,vol,cumulative,price+10,price-10)
            self.assertTrue(self.d.ingest(self.t,self.clock))
        self.e.last_account=time.monotonic()
        return self.d.signal(self.t,self.watch)
    def trial(self,name):return next(t for t in self.d.last_eval[SYM]['trials'] if t['setup']==name)
    def slow_signal(self,name):
        sig=self.feed(name);self.assertEqual(self.d.base_gates(self.t,self.watch)['acceleration']['result'],'FAIL')
        self.assertIsNotNone(sig);self.assertEqual(sig['pattern'],name);return sig
    def test_A_acceleration_ratio_fail_remains_blocked(self):
        self.assertIsNone(self.feed('A'));self.assertIn('ACCELERATION_FAIL',self.trial('A')['failed_reasons'])
        self.assertTrue(self.trial('A')['acceleration_required']);self.assertEqual(self.b.orders,[])
    def test_A_gain_fail_remains_blocked_even_with_ratio_pass(self):
        self.feed('A',fast=True);last=self.d.last[SYM]
        # Valid A breakout, but under 0.3% gain in the ten-second fast window.
        self.d.prior_high[SYM]=last.price-10
        for i,t in enumerate(self.d.buffers[SYM]):
            if (last.time-t.time).total_seconds()<self.cfg.acceleration_seconds:
                self.d.buffers[SYM][i]=replace(t,price=last.price-10)
        self.assertEqual(self.d.base_gates(last,self.watch)['acceleration']['result'],'FAIL')
        self.assertIsNone(self.d.signal(last,self.watch));self.assertEqual(self.b.orders,[])
    def test_B_slow_rebreakout_reaches_trial_and_order_guard(self):
        sig=self.slow_signal('B');self.assertTrue(self.trial('B')['entry_ready'])
        self.assertFalse(self.trial('B')['acceleration_required'])
        with patch.object(self.b,'order_guard',wraps=self.b.order_guard) as guard:
            order=self.e.enter(sig,self.t)
        self.assertEqual(order['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1);guard.assert_called_once()
    def test_C_slow_box_breakout_reaches_trial_and_order_guard(self):
        sig=self.slow_signal('C');self.assertTrue(self.trial('C')['entry_ready'])
        self.assertFalse(self.trial('C')['acceleration_required'])
        with patch.object(self.b,'order_guard',wraps=self.b.order_guard) as guard:
            order=self.e.enter(sig,self.t)
        self.assertEqual(order['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1);guard.assert_called_once()
    def test_B_without_rebreakout_never_signals(self):
        self.assertIsNone(self.feed('B',final_price=10150));self.assertIn('NO_REBREAKOUT',self.trial('B')['failed_reasons'])
        self.assertEqual(self.b.orders,[])
    def test_C_without_breakout_never_signals(self):
        self.assertIsNone(self.feed('C',final_price=9970));self.assertIn('BREAKOUT_NOT_REACHED',self.trial('C')['failed_reasons'])
        self.assertEqual(self.b.orders,[])
    def test_no_controlled_pullback_and_no_box_are_logged(self):
        self.feed('A');self.assertFalse(self.trial('B')['candidate_generated']);self.assertFalse(self.trial('C')['candidate_generated'])
        self.assertEqual([t['setup'] for t in self.d.last_eval[SYM]['trials']],['A','B','C']);self.assertEqual(self.b.orders,[])
    def test_B_resistance_room_failure_is_not_bypassed(self):
        self.watch['resistance_room_pass']=False;self.assertIsNone(self.feed('B'))
        self.assertIn('RESISTANCE_FAIL',self.trial('B')['failed_reasons']);self.assertEqual(self.b.orders,[])
    def test_RR_failure_blocks_B_and_C(self):
        for name in ('B','C'):
            with self.subTest(setup=name):
                self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
                self.feed(name);sig=self.d.signal(self.t,self.watch);self.assertIsNotNone(sig)
                watch={**self.watch,'resistance':self.t.price+20}
                # C only ignores a resistance already broken; this ceiling is still ahead.
                self.assertIsNone(self.d.signal(self.t,watch))
                trial=next(x for x in self.d.last_eval[SYM]['trials'] if x['setup']==name)
                self.assertIn('RISK_REWARD_FAIL',trial['failed_reasons']);self.assertEqual(self.b.orders,[])
    def test_Economic_failure_blocks_B_and_C(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
            with patch('automation.patterns.efficiency',return_value={'decision':'NO_TRADE','risk_per_share':100}):
                self.assertIsNone(self.feed(name));self.assertIn('ECONOMIC_FAIL',self.trial(name)['failed_reasons'])
        self.assertEqual(self.b.orders,[])
    def test_risk_zero_blocks_valid_B_and_C_at_existing_engine(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);sig=self.slow_signal(name)
            self.e.s['daily_loss']=800
            self.assertIsNone(self.e.enter(sig,self.t));self.assertEqual(self.b.orders,[])
    def test_low_liquidity_still_blocks_slow_B_and_C(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.feed(name)
            self.d.buffers[SYM]=type(self.d.buffers[SYM])(replace(t,volume=1) for t in self.d.buffers[SYM])
            self.assertIsNone(self.d.signal(self.t,self.watch));self.assertIn('LIQUIDITY_FAIL',self.trial(name)['failed_reasons'])
        self.assertEqual(self.b.orders,[])
    def test_spread_still_blocks_B_and_C(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.feed(name)
            bad=replace(self.t,ask=self.t.price+100,bid=self.t.price-100)
            self.assertIsNone(self.d.signal(bad,self.watch));self.assertIn('SPREAD_FAIL',self.trial(name)['failed_reasons'])
    def test_falling_below_breakout_does_not_generate_B_or_C_signal(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.feed(name)
            bad=replace(self.t,price=9900,ask=9910,bid=9890)
            self.assertIsNone(self.d.signal(bad,self.watch))
        self.assertEqual(self.b.orders,[])
    def test_shadow_slow_B_C_stays_isolated(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.feed(name);events=[]
            observer=ShadowObserver(self.cfg,self.auto,self.e.costs,lambda k,**v:events.append(v))
            observer.update([{**self.watch,'affordability':{'entry_eligible':True}}],self.clock)
            observer.detector=self.d
            next_tick=replace(self.t,time=self.t.time+timedelta(seconds=1),cumulative=self.t.cumulative+100)
            before=copy.deepcopy(self.e.s);observer.tick(next_tick,next_tick.time)
            self.assertEqual(self.e.s,before);self.assertEqual(self.b.orders,[])
            trial=next(t for t in events[-1]['setup_trials'] if t['setup']==name)
            self.assertNotIn('ACCELERATION_FAIL',trial['failed_reasons'])
            self.assertFalse(events[-1]['actual_order_permission'])
    def test_focus_recognizes_valid_B_C_without_acceleration_permission(self):
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);sig=self.slow_signal(name)
            self.assertEqual(calculate_tier(sig['evaluation_gates'],sig['setup_trials']),TIER_SIGNAL_READY)
    def test_runtime_view_remains_compact_and_carries_setup_tier(self):
        sig=self.slow_signal('C');worker=ObservationWorker(self.cfg,self.auto,self.e.costs,self.root)
        worker.event('SHADOW_EVAL',timestamp=self.clock.isoformat(),symbol=SYM,
            gates=sig['evaluation_gates'],setup_trials=sig['setup_trials'])
        view=worker.runtime_view()['evaluations'][SYM]
        self.assertEqual(view['tier'],TIER_SIGNAL_READY);self.assertNotIn('setup_trials',view)
    def test_fast_A_existing_thresholds_and_signal_are_unchanged(self):
        sig=self.feed('A',fast=True);self.assertEqual(sig['pattern'],'A')
        self.assertGreaterEqual(sig['turnover_ratio'],3);self.assertGreaterEqual(sig['gain'],.003)
        self.assertTrue(sig['acceleration_required'])
    def test_old_build_authorization_is_not_reused(self):
        p=self.root/'runtime';p.mkdir(exist_ok=True)
        (p/'operator_user_authorization.json').write_text(json.dumps({'build_id':'V1.3-preopen-risk2-20261007'}),encoding='utf-8')
        ok,reasons=authorization_valid(self.root,self.cfg,self.auto,self.policy,self.clock)
        self.assertFalse(ok);self.assertIn('LIVE_APPROVAL_BUILD_MISMATCH',reasons)
        self.assertEqual(BUILD_ID,'V1.5-risk2-watch12-bc20M-structuralAE2-20261013')
    def set_turnover(self,amount):
        fast=[i for i,t in enumerate(self.d.buffers[SYM]) if 0<=(self.t.time-t.time).total_seconds()<self.cfg.acceleration_seconds]
        for i in fast:self.d.buffers[SYM][i]=replace(self.d.buffers[SYM][i],volume=0)
        i=fast[0];self.d.buffers[SYM][i]=replace(self.d.buffers[SYM][i],price=10000,volume=amount//10000)
    def test_B_C_exact_20M_pass_below_20M_fail(self):
        for name in ('B','C'):
            for amount in (19990000,20000000,29900000):
                with self.subTest(setup=name,turnover=amount):
                    self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.feed(name);self.set_turnover(amount)
                    sig=self.d.signal(self.t,self.watch);trial=self.trial(name)
                    self.assertEqual(trial['gates']['liquidity']['values']['min_turnover'],20000000)
                    self.assertEqual(trial['gates']['liquidity']['result'],'PASS' if amount>=20000000 else 'FAIL')
                    self.assertEqual(sig is not None,amount>=20000000)
        self.assertEqual(self.b.orders,[])
    def test_A_liquidity_is_still_exactly_30M(self):
        self.feed('A',fast=True)
        for amount in (20000000,29900000,30000000):
            self.set_turnover(amount);self.d.signal(self.t,self.watch)
            liq=self.trial('A')['gates']['liquidity']
            self.assertEqual(liq['values']['min_turnover'],30000000)
            self.assertEqual(liq['result'],'PASS' if amount>=30000000 else 'FAIL')
    def test_watch12_source1_fallback_competes_normally(self):
        from automation.radar import DynamicRadar
        radar=DynamicRadar(self.cfg,replace(self.auto,min_source_count=2,fallback_source1=True),self.policy,fallback_source1=True)
        radar.available_capital=20000
        raw=[{'code':str(100000+i),'reference_price':2000,'source_details':{'one':{'observed_rank':i+1,'values':{}}}} for i in range(15)]
        rows,watch,_=radar.update(raw,lambda row:{'status':'ELIGIBLE','overhead_share_estimate':0},self.clock)
        self.assertEqual(len(watch),12);self.assertEqual(self.cfg.max_watchlist,12)
        self.assertTrue(all(row['fallback_source1'] for row in watch));self.assertEqual(self.b.orders,[])
    def test_observational_acceleration_does_not_hide_risk_rejection_in_log(self):
        sig=self.slow_signal('B');self.e.s['daily_loss']=800
        self.assertIsNone(self.e.enter(sig,self.t));self.assertEqual(self.b.orders,[])
        records=[json.loads(line) for line in (self.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        entry=next(r for r in reversed(records) if r['event']=='ENTRY_EVAL')
        self.assertNotIn('ACCELERATION',entry['additional_reason_codes'])
        self.assertEqual(entry['gates']['acceleration']['result'],'FAIL')
        self.assertFalse(entry['gates']['acceleration']['required'])
        from operator_service import decision_line
        self.assertNotIn('급가속 부족',decision_line(entry))
    def test_zero_cap_blocks_valid_B_C(self):
        self.e.a=replace(self.auto,AUTO_TRADING_CAP_KRW=0)
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);sig=self.slow_signal(name)
            self.assertIsNone(self.e.enter(sig,self.t))
        self.assertEqual(self.b.orders,[])

if __name__=='__main__':unittest.main()
