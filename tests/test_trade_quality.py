"""Price-structure experiments through actual detector/engine and fake broker boundaries."""
import copy,json,unittest
from dataclasses import replace
from datetime import timedelta
from datetime import datetime
from unittest.mock import Mock,patch
from automation.policy import AutomationConfig
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.trade_quality import box_direction,confirmed_protection
from automation.runner import process_message
from automation.focus_selection import calculate_tier,TIER_FRESH_WEAK,TIER_SIGNAL_READY
from core.strategy import Tick
from tests import test_setup_acceleration as entry_fixture
from tests import test_position_data_health as health_fixture


def bars(start,lows,highs=None):
    return [{'time':start+timedelta(minutes=i),'open':low+10,'high':(highs[i] if highs else low+30),
        'low':low,'close':low+15,'volume':100,'first_second':0,'gap_reason':None} for i,low in enumerate(lows)]


class BoxQualityTests(unittest.TestCase):
    setUp=entry_fixture.SetupAccelerationTests.setUp
    feed=entry_fixture.SetupAccelerationTests.feed
    trial=entry_fixture.SetupAccelerationTests.trial

    def enable(self):
        self.auto=replace(self.auto,confirmed_box_direction=True)
        self.d.a=self.auto;self.e.a=self.auto

    def test_declining_narrow_range_is_not_a_base_even_when_width_and_breakout_fit(self):
        self.enable();self.clock+=timedelta(minutes=5)
        b=bars(self.clock-timedelta(minutes=5),[9990,9980,9970,9960,9950])
        setup=self.d.box(b);self.assertIsNotNone(setup)
        t=Tick(entry_fixture.SYM,self.clock,setup['breakout']+10,100,10000,setup['breakout']+10,setup['breakout'])
        result=self.d.setup_gates(t,self.watch,setup,b)
        self.assertEqual(result['breakout']['result'],'PASS')
        self.assertEqual(result['box_direction']['result'],'FAIL')
        self.assertEqual(result['box_direction']['values']['reason'],'DECLINING_RANGE')
        self.assertEqual(self.b.orders,[])

    def test_genuine_flat_box_slow_acceleration_can_still_reach_fake_order_boundary(self):
        self.enable();sig=self.feed('C');self.assertIsNotNone(sig)
        self.assertEqual(sig['pattern'],'C');self.assertEqual(sig['evaluation_gates']['acceleration']['result'],'FAIL')
        self.assertEqual(sig['evaluation_gates']['box_direction']['result'],'PASS')
        self.assertEqual(self.e.enter(sig,self.t)['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1)

    def test_missing_or_forged_direction_proof_blocks_before_broker(self):
        self.enable();sig=self.feed('C');self.assertIsNotNone(sig)
        for mode in ('missing','forged','descending'):
            bad=copy.deepcopy(sig)
            if mode=='missing':bad['evaluation_gates'].pop('box_direction')
            elif mode=='forged':bad['evaluation_gates']['box_direction']['values']['last_low']+=10
            else:
                b=bars(self.t.time.replace(second=0,microsecond=0)-timedelta(minutes=5),[9990,9980,9970,9960,9950])
                bad['decision_bar_snapshot']=[{**x,'time':x['time'].isoformat()} for x in b]
            self.assertIsNone(self.e.enter(bad,self.t))
        self.assertEqual(self.b.orders,[])

    def test_partial_future_missing_or_discontinuous_bars_are_unknown(self):
        self.enable();asof=self.clock+timedelta(minutes=5)
        for change in ('partial','future','gap','invalid','missing'):
            b=bars(self.clock,[9990]*5)
            if change=='partial':b[-1]['time']=asof
            elif change=='future':b[-1]['time']=asof+timedelta(minutes=1)
            elif change=='gap':b[2]['time']-=timedelta(minutes=1)
            elif change=='invalid':b[2]['low']=float('nan')
            else:b=b[:-1]
            self.assertIsNone(box_direction(b,asof,5)[0])

    def test_breakout_economic_risk_guards_still_block(self):
        self.enable();self.assertIsNone(self.feed('C',final_price=9970))
        self.assertEqual(self.b.orders,[])
        self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
        with patch('automation.patterns.efficiency',return_value={'decision':'NO_TRADE','risk_per_share':100}):
            self.assertIsNone(self.feed('C'))
        self.d=PatternDetector(self.cfg,self.auto,self.e.costs);sig=self.feed('C');self.assertIsNotNone(sig)
        self.e.s['daily_loss']=800
        self.assertIsNone(self.e.enter(sig,self.t));self.assertEqual(self.b.orders,[])

    def test_A_B_and_disabled_C_keep_existing_rules(self):
        for name in ('A','B','C'):
            self.auto=replace(self.auto,confirmed_box_direction=name!='C');self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
            self.feed(name)
            self.assertNotIn('box_direction',self.trial(name)['gates'])

    def test_experiment_flags_default_off_strict_boolean_and_authorization_hash_changes(self):
        default=AutomationConfig();self.assertFalse(default.confirmed_box_direction);self.assertFalse(default.confirmed_structure_protection)
        for field in ('confirmed_box_direction','confirmed_structure_protection'):
            self.assertNotEqual(default.fingerprint(),replace(default,**{field:True}).fingerprint())
            with self.assertRaises(ValueError):replace(default,**{field:1}).validate()

    def test_rejected_descending_C_does_not_keep_high_priority_watch_slot(self):
        common={k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status')}
        common['acceleration']={'result':'FAIL'}
        c={'setup':'C','acceleration_required':False,'candidate_generated':True,'gates':{
            'liquidity':{'result':'PASS'},'box_direction':{'result':'FAIL'},'breakout':{'result':'PASS'}}}
        self.assertEqual(calculate_tier(common,[c]),TIER_FRESH_WEAK)
        b={'setup':'B','acceleration_required':False,'candidate_generated':True,'entry_ready':True,'gates':{
            'liquidity':{'result':'PASS'},'breakout':{'result':'PASS'}}}
        self.assertEqual(calculate_tier(common,[c,b]),TIER_SIGNAL_READY)

    def test_GUI_explains_new_rejection_in_Korean(self):
        from operator_ui_service import setup_wait_summary
        text=setup_wait_summary({'setup_trials':[{'setup':'C','failed_reasons':['BOX_DIRECTION_FAIL']}]})
        self.assertIn('내려가는',text)

    def test_five_fake_roundtrips_with_quality_flags_return_to_normal_waiting(self):
        self.auto=replace(self.auto,confirmed_box_direction=True,confirmed_structure_protection=True,
            stable_position_book_confirmation=True,max_consecutive_losses=None)
        self.cfg=replace(self.cfg,max_entries_per_day=None,max_entries_per_symbol=None)
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.b,lambda:self.clock)
        self.e.reconcile({'cash':20000,'equity':20000,'holdings':{},'orders':[]})
        self.e.connected=True;self.e.market='3';self.d=PatternDetector(self.cfg,self.auto,self.e.costs)
        template=self.feed('C');self.assertIsNotNone(template)
        for i in range(5):
            delta=timedelta(minutes=i*20);sym=f'9999{i}0';self.clock=self.t.time+delta
            sig=copy.deepcopy(template);sig['symbol']=sym;sig['time']=self.clock.isoformat()
            for b in sig['decision_bar_snapshot']:b['time']=(datetime.fromisoformat(b['time'])+delta).isoformat()
            ok,evidence=box_direction(sig['decision_bar_snapshot'],self.clock,5);self.assertTrue(ok)
            sig['evaluation_gates']['box_direction']={'result':'PASS','values':{'enabled':True,**evidence}}
            self.e.market_status[sym]={'pass':True};tick=replace(self.t,symbol=sym,time=self.clock)
            self.e.last_account=__import__('time').monotonic()
            order=self.e.enter(sig,tick);self.assertIsNotNone(order)
            q=order['qty'];self.e.cumulative_fill(order,q,q*sig['limit'])
            self.clock+=timedelta(seconds=1)
            self.e.reconcile({'cash':20000,'equity':20000,'holdings':{sym:{'qty':q,'available':q,'avg':sig['limit']}},'orders':[],
                'query_started_at':self.clock.isoformat()})
            self.e.request_exit(sym,'구조/이익보호선 이탈');self.e.drive_exits({sym:sig['limit']+50})
            sell=self.e.active_orders(sym,'SELL')[0]
            fill_price=sig['limit']+(50 if i%2==0 else -50)
            self.e.cumulative_fill(sell,q,q*fill_price);self.clock+=timedelta(seconds=1)
            self.e.reconcile({'cash':20000,'equity':20000,'holdings':{},'orders':[],'query_started_at':self.clock.isoformat()})
            self.assertFalse(self.e.s['positions']);self.assertFalse(self.e.active_orders())
            self.assertEqual(self.e.s['halt'],'');self.assertTrue(self.e.account_ok)
        self.assertEqual(len(self.e.s['trades']),5);self.assertEqual(len(self.b.orders),10)


class StructureProtectionTests(unittest.TestCase):
    setUp=health_fixture.PositionHealthTests.setUp
    sell_count=health_fixture.PositionHealthTests.sell_count

    def enable(self):
        self.e.a=replace(self.e.a,confirmed_structure_protection=True)
        self.p=self.e.s['positions'][health_fixture.SYM]
        self.p['high']=7350;self.clock=health_fixture.BASE+timedelta(minutes=5)

    def tick(self,price):return Tick(health_fixture.SYM,self.clock,price,100,10000,price+10,price)

    def pivot(self,lows=(7310,7290,7320)):
        return bars(health_fixture.BASE.replace(second=0,microsecond=0)+timedelta(minutes=1),lows)

    def test_small_profitable_pullback_without_confirmed_structure_does_not_exit(self):
        self.enable();self.e.on_tick(self.tick(7270),completed_low=7320)
        self.assertEqual(self.p['stop'],7140);self.assertEqual(self.sell_count(),0)
        legacy=replace(self.e.a,confirmed_structure_protection=False);self.e.a=legacy
        self.e.on_tick(self.tick(7270),completed_low=7320)
        self.assertEqual(self.sell_count(),1)

    def test_confirmed_post_entry_higher_low_raises_and_real_breach_exits(self):
        self.enable()
        for b in self.pivot():self.e.observe_protection_bar(health_fixture.SYM,b)
        self.e.on_tick(self.tick(7340))
        self.assertEqual(self.p['stop'],7280);self.assertEqual(self.sell_count(),0)
        self.assertEqual(self.p['protection_evidence']['reason'],'CONFIRMED_HIGHER_LOW_RAISED_STOP')
        self.e.on_tick(self.tick(7270));self.assertEqual(self.sell_count(),1)

    def test_initial_invalidation_and_emergency_bid_exit_are_immediate(self):
        self.enable();self.e.on_tick(self.tick(7140));self.assertEqual(self.sell_count(),1)

    def test_bid_collapse_is_not_delayed_by_pivot_confirmation(self):
        self.enable();self.e.on_tick(replace(self.tick(7270),bid=7100));self.assertEqual(self.sell_count(),1)

    def test_entry_partial_minute_future_and_missing_bar_do_not_raise_stop(self):
        self.enable();self.p['protection_bars']=self.pivot()
        for kind in ('entry_partial','future','gap'):
            b=self.pivot()
            if kind=='entry_partial':b[0]['time']=health_fixture.BASE.replace(second=0,microsecond=0)
            elif kind=='future':b[-1]['time']=self.clock
            else:b[1]['gap_reason']='LOCAL_DROP'
            self.p['protection_bars']=b
            self.assertEqual(confirmed_protection(self.cfg,self.e.a,self.p,7340,self.clock)[1],7140)

    def test_no_stop_widening_and_unprofitable_pivot_does_not_tighten(self):
        self.enable();self.p['stop']=7300;self.p['protection_bars']=self.pivot()
        self.assertEqual(confirmed_protection(self.cfg,self.e.a,self.p,7340,self.clock)[1],7300)
        self.p['stop']=7140;self.p['protection_bars']=self.pivot((7240,7220,7240))
        self.assertEqual(confirmed_protection(self.cfg,self.e.a,self.p,7340,self.clock)[1],7140)

    def test_completed_bars_and_raised_stop_persist_in_existing_journal(self):
        self.enable()
        for b in self.pivot()*2:self.e.observe_protection_bar(health_fixture.SYM,b)
        self.assertEqual(len(self.p['protection_bars']),3)
        self.e.on_tick(self.tick(7340));self.e.save()
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][health_fixture.SYM]
        self.assertEqual(stored['stop'],7280);self.assertEqual(stored['confirmed_protection_low'],7290)
        self.assertEqual(stored['protection_bars'],self.p['protection_bars'])

    def test_restart_with_same_approved_settings_preserves_structural_stop(self):
        # Configure the flat engine before creating its test position, matching deployment rules.
        p=copy.deepcopy(self.e.s['positions'][health_fixture.SYM]);self.e.s['positions'].clear();self.e.save()
        auto=replace(self.e.a,confirmed_structure_protection=True)
        self.e=AutonomousEngine(self.cfg,auto,self.policy,self.root/'book',self.b,lambda:self.clock)
        self.e.s['positions'][health_fixture.SYM]=p;self.enable()
        for b in self.pivot():self.e.observe_protection_bar(health_fixture.SYM,b)
        self.e.on_tick(self.tick(7340));self.e.save()
        restored=AutonomousEngine(self.cfg,auto,self.policy,self.root/'book',self.b,lambda:self.clock)
        saved=restored.s['positions'][health_fixture.SYM]
        self.assertEqual(saved['stop'],7280);self.assertEqual(saved['confirmed_protection_low'],7290)
        self.assertEqual(len(saved['protection_bars']),3)

    def test_real_runner_delivers_completed_bars_before_tick_protection(self):
        self.enable();d=Mock();d.ingest.return_value=True;d.completed={health_fixture.SYM:self.pivot()}
        t=self.tick(7340)
        packet={'trnm':'REAL','data':[{'type':'0B','item':health_fixture.SYM,'_observation_tick':(self.clock,t),'values':{}}]}
        with patch('automation.runner.now',return_value=self.clock):
            process_message(self.e,d,packet,self.clock,{health_fixture.SYM},{},Mock(),self.e.prices,self.account,True)
        self.assertEqual(self.p['stop'],7280);self.assertEqual(self.sell_count(),0)

    def test_user_stop_flat_still_exits(self):
        self.enable();self.policy.command('STOP_FLAT');self.e.controls();self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),1)

    def test_after_forced_flatten_is_not_delayed_by_pivot_confirmation(self):
        self.enable();self.e.after_enabled=True;self.clock=self.clock.replace(hour=19,minute=56)
        self.e.last_account=__import__('time').monotonic()
        self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),1)
