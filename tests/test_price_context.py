"""Known pre-entry references, not hindsight winners; fake order boundary only."""
import copy,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.price_context import PriceContext
from automation.capital import Costs
from automation.focus_selection import calculate_tier,select_focus,TIER_FRESH_WEAK,TIER_SIGNAL_READY,TIER_GEOMETRY_BASE_READY
from tests import test_setup_acceleration as fixtures

class PriceContextTests(unittest.TestCase):
    def setUp(self):
        self.time=datetime(2026,10,12,16,55,tzinfo=KST)
        self.cfg=Config();self.auto=replace(AutomationConfig(),entry_context_enabled=True)
        self.costs=Costs(self.auto.commission_buy,self.auto.commission_sell,self.auto.sell_tax,self.cfg.entry_slippage_fraction,self.cfg.exit_slippage_fraction)
        self.d=PatternDetector(self.cfg,self.auto,self.costs)
        self.t=Tick('999990',self.time,13610,10,10000,13610,13590)
        self.watch={'status':'WATCH','source_details':{'a':{},'b':{}},'resistance':14450,'resistance_room_pass':True}
    def refs(self,highs=(13610,13630,13620),start=None):
        start=start or self.time-timedelta(minutes=3)
        return [{'time':start+timedelta(minutes=i),'open':13490,'high':h,'low':13490,'close':13500,
            'first_second':0,'gap_reason':None} for i,h in enumerate(highs)]
    def record(self,bars):
        for b in bars:self.d.price_context.record(self.t.symbol,b,self.time)
    def setup(self,name='A'):return {'pattern':name,'breakout':13600,'peak':13600,'support':13490,'projection':13820}
    def test_known_room_caps_target_for_all_setups_without_widening_stop(self):
        self.record(self.refs())
        for name in ('A','B','C'):
            g=self.d.setup_gates(self.t,self.watch,self.setup(name))
            self.assertEqual(g['risk_reward']['values']['target'],13630)
            self.assertEqual(g['invalidation']['values']['stop'],13470)
            self.assertEqual(g['risk_reward']['result'],'FAIL');self.assertEqual(g['entry_context']['result'],'FAIL')
            self.assertEqual(g['entry_context']['values']['reason'],'CONFIRMED_INTRADAY_RESISTANCE_RR')
    def test_cannot_use_future_confirmation_or_partial_minute(self):
        bars=self.refs(start=self.time-timedelta(minutes=2))
        self.record(bars);v=self.d.price_context.snapshot(self.t.symbol,self.t,13610)
        self.assertIsNone(v['nearest_confirmed_peak'])
    def test_gap_and_invalid_coverage_do_not_manufacture_reference(self):
        for kind in ('gap','coverage'):
            self.d.price_context=PriceContext();bars=self.refs()
            if kind=='gap':bars[0]['time']-=timedelta(minutes=1)
            else:bars[1]['gap_reason']='LOCAL_DROP'
            self.record(bars);self.assertIsNone(self.d.price_context.snapshot(self.t.symbol,self.t,13610)['nearest_confirmed_peak'])
    def test_crossed_peak_preserves_genuine_breakout_path(self):
        self.record(self.refs());t=replace(self.t,price=13640,ask=13640,bid=13630)
        self.assertIsNone(self.d.price_context.snapshot(t.symbol,t,13640)['nearest_confirmed_peak'])
    def test_regular_reference_remains_after_but_never_becomes_strategy_bars(self):
        self.record(self.refs(start=datetime(2026,10,12,14,30,tzinfo=KST)))
        self.assertIsNotNone(self.d.price_context.snapshot(self.t.symbol,self.t,13610)['nearest_confirmed_peak'])
        self.assertEqual(self.d.usable_bars(self.t.symbol,after_only=True),[])
        self.assertEqual(self.d.base_gates(self.t,self.watch)['warmup']['result'],'FAIL')
    def test_transport_reset_and_role_forget_preserve_reference_only(self):
        self.record(self.refs());self.d.reset();self.d.forget(self.t.symbol)
        self.assertIsNotNone(self.d.price_context.snapshot(self.t.symbol,self.t,13610)['nearest_confirmed_peak'])
        self.assertFalse(self.d.buffers);self.assertFalse(self.d.history)
    def test_next_day_does_not_reuse_previous_reference(self):
        self.record(self.refs());t=replace(self.t,time=self.time+timedelta(days=1))
        self.assertIsNone(self.d.price_context.snapshot(t.symbol,t,13610)['nearest_confirmed_peak'])
    def test_bounded_symbols_and_history_do_not_expand_subscriptions(self):
        p=PriceContext(limit=2)
        for symbol in ('111110','111120','111130'):
            for b in self.refs():p.record(symbol,b,self.time)
        self.assertEqual(len(p.rows),2);self.assertNotIn('111110',p.rows)
        self.assertLessEqual(len(p.rows['111130']['bars']),30)
    def test_disabled_policy_preserves_old_projection_and_hash_changes_when_enabled(self):
        self.record(self.refs());self.d.a=replace(self.auto,entry_context_enabled=False)
        g=self.d.setup_gates(self.t,self.watch,self.setup())
        self.assertEqual(g['risk_reward']['values']['target'],13820);self.assertNotIn('entry_context',g)
        self.assertNotEqual(self.auto.fingerprint(),self.d.a.fingerprint())
        with self.assertRaises(ValueError):replace(self.auto,entry_context_enabled=1).validate()
    def test_far_reference_allows_rr_and_keeps_existing_threshold(self):
        self.record(self.refs((14000,14050,14000)))
        g=self.d.setup_gates(self.t,self.watch,self.setup())
        self.assertEqual(g['risk_reward']['result'],'PASS');self.assertEqual(g['entry_context']['result'],'PASS')
        self.assertEqual(g['risk_reward']['values']['required_rr'],1.5)
    def test_transfer_copies_only_symbol_and_earlier_known_references(self):
        self.record(self.refs());target=PriceContext();target.transfer(self.d.price_context,self.t.symbol)
        self.assertEqual(target.snapshot(self.t.symbol,self.t,13610)['nearest_confirmed_peak']['price'],13630)
        self.d.price_context.rows.clear();self.assertIn(self.t.symbol,target.rows)
    def test_restart_bootstrap_recovers_known_reference_without_any_signal_warmup(self):
        sig={'symbol':self.t.symbol,'time':self.time.isoformat(),
            'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in self.refs()]}
        summary=self.d.price_context.bootstrap([sig],self.time)
        self.assertEqual(summary['recovered_complete_references'],3)
        self.assertIsNotNone(self.d.price_context.snapshot(self.t.symbol,self.t,13610)['nearest_confirmed_peak'])
        self.assertFalse(self.d.buffers);self.assertFalse(self.d.history);self.assertFalse(self.d.observation_start)
    def test_bootstrap_never_uses_tomorrow_or_later_signal_to_invent_earlier_context(self):
        bars=[{**b,'time':b['time'].isoformat()} for b in self.refs()]
        for stamp in (self.time+timedelta(seconds=1),self.time+timedelta(days=1),self.time-timedelta(days=1)):
            self.d.price_context.bootstrap([{'symbol':self.t.symbol,'time':stamp.isoformat(),'decision_bar_snapshot':bars}],self.time)
        self.assertEqual(len(self.d.price_context.rows),0)

class ContextOrderTests(unittest.TestCase):
    setUp=fixtures.SetupAccelerationTests.setUp
    feed=fixtures.SetupAccelerationTests.feed
    trial=fixtures.SetupAccelerationTests.trial
    def test_context_rr_blocks_real_pattern_before_fake_order(self):
        self.auto=replace(self.auto,entry_context_enabled=True);self.d.a=self.auto
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
            sig=self.feed(name);self.assertIsNotNone(sig)
            entry=sig['limit']
            # Align to :00 second boundary so bars are recognized as valid completed 1-minute references
            stamp=(self.t.time-timedelta(minutes=3)).replace(second=0,microsecond=0)
            self.d.price_context=PriceContext()
            for i,h in enumerate((entry,entry+20,entry+10)):
                b={'time':stamp+timedelta(minutes=i),'open':entry-100,'low':entry-100,'high':h,'close':entry-50,'gap_reason':None,'first_second':0}
                self.d.price_context.record(self.t.symbol,b,self.t.time)
            self.assertIsNone(self.d.signal(self.t,self.watch));self.assertEqual(self.b.orders,[])
            self.assertIn('ENTRY_CONTEXT_FAIL',self.trial(name)['failed_reasons'])
    def test_unaligned_second_bar_rejected_from_context(self):
        pc=PriceContext()
        now=self.clock.replace(minute=10,second=30,microsecond=0)
        # Bars starting at arbitrary seconds (e.g. :05) must be rejected
        bad_bar={'time':now.replace(second=5),'open':10000,'low':9900,'high':10100,'close':10050,'gap_reason':None,'first_second':0}
        self.assertFalse(pc.record(self.watch['code'],bad_bar,now+timedelta(minutes=2)))


    def test_context_enabled_boundary_rejects_missing_or_mismatched_proof(self):
        sig=self.feed('C');self.assertIsNotNone(sig);self.e.a=replace(self.auto,entry_context_enabled=True)
        self.assertEqual(self.e.entry_market_reason(sig,1,self.t),'ENTRY_CONTEXT_PROOF_MISSING_OR_INVALID')
        self.assertIsNone(self.e.enter(sig,self.t));self.assertEqual(self.b.orders,[])
    def test_valid_enabled_context_can_reach_actual_fake_order_guard(self):
        self.auto=replace(self.auto,entry_context_enabled=True);self.d.a=self.auto;self.e.a=self.auto
        sig=self.feed('C');self.assertIsNotNone(sig)
        self.assertEqual(self.e.enter(sig,self.t)['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1)
    def test_modified_target_or_nonfinite_rr_cannot_reuse_context_proof(self):
        self.auto=replace(self.auto,entry_context_enabled=True);self.d.a=self.auto;self.e.a=self.auto
        original=self.feed('C');self.assertIsNotNone(original)
        sig=copy.deepcopy(original);sig['target']+=10
        self.assertEqual(self.e.entry_market_reason(sig,1,self.t),'ENTRY_CONTEXT_PROOF_MISSING_OR_INVALID')
        sig=copy.deepcopy(original);sig['target']=float('inf');sig['evaluation_gates']['entry_context']['values']['target']=float('inf')
        self.assertEqual(self.e.entry_market_reason(sig,1,self.t),'ENTRY_CONTEXT_RR_CHANGED')
        self.assertEqual(self.b.orders,[])

class FocusContextTests(unittest.TestCase):
    def common(self):return {k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status','liquidity','acceleration')}
    def trial(self):return {'setup':'C','acceleration_required':False,'candidate_generated':True,'entry_ready':False,
        'gates':{k:{'result':'FAIL' if k=='risk_reward' else 'PASS'} for k in ('breakout','liquidity','risk_reward','economic')}}
    def test_impossible_rr_box_does_not_monopolize_geometry_tier(self):
        self.assertEqual(calculate_tier(self.common(),[self.trial()]),TIER_FRESH_WEAK)
    def test_breakout_wait_with_valid_room_keeps_geometry_priority(self):
        t=self.trial();t['gates']['risk_reward']['result']='PASS';t['gates']['breakout']['result']='FAIL'
        self.assertEqual(calculate_tier(self.common(),[t]),TIER_GEOMETRY_BASE_READY)
    def test_valid_A_is_not_displaced_by_invalid_C(self):
        a={'setup':'A','candidate_generated':True,'gates':{'breakout':{'result':'PASS'},'acceleration':{'result':'PASS'}}}
        self.assertEqual(calculate_tier(self.common(),[self.trial(),a]),TIER_SIGNAL_READY)
    def test_new_affordable_candidate_competes_against_untradeable_incumbent(self):
        rows=[{'code':s,'status':'WATCH' if i==0 else 'RESERVE','source_count':1,'discovery_score':10,'affordability':{'entry_eligible':True}} for i,s in enumerate(('111110','111120'))]
        readiness={'111110':{'gates':self.common(),'setup_trials':[self.trial()]}}
        result=select_focus({'111110':rows[0]},rows,readiness,max_watchlist=1)
        self.assertEqual(result.next_watch,['111120'])

if __name__=='__main__':unittest.main()
