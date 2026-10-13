"""AFTER burst failure vs healthy pullback; all orders use the mock broker."""
import copy,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from automation.after_momentum import after_momentum_failure
from automation.policy import AutomationConfig
from core.settings import KST
from core.strategy import Tick
from tests import test_automation as fixture

OPEN=datetime(2026,10,13,16,36,32,tzinfo=KST)
START=OPEN.replace(minute=37,second=0)
VALUES=[(17600,17700,17590,17610,20000),
        (17610,17620,17530,17550,10000),
        (17550,17650,17540,17630,6000),
        (17590,17600,17520,17530,5000),
        (17530,17560,17500,17510,4000),
        (17510,17530,17480,17490,3000)]

def position(values=VALUES):
    bars=[dict(time=(START+timedelta(minutes=i)).isoformat(),open=o,high=h,low=l,close=c,volume=v,first_second=0)
          for i,(o,h,l,c,v) in enumerate(values)]
    return dict(opened=OPEN.isoformat(),avg=17460,high=max(v[1] for v in values),
                initial_stop=17180,initial_r=280,stop=17180,protection_bars=bars,
                signal={'pattern':'A','session':'AFTER'})

ASOF=START+timedelta(minutes=len(VALUES))

# Anonymized completed SOR market candles, not an account/order/API capture.
# Retained Oct13 16:37-16:58 values; the historical executable quote is unknown.
OBSERVED=[
    (17610,17700,17570,17600,31389),(17600,17680,17560,17600,16977),
    (17600,17600,17510,17520,8264),(17510,17560,17500,17560,3552),
    (17560,17630,17540,17560,9898),(17580,17660,17540,17610,8729),
    (17610,17700,17560,17650,16792),(17660,17660,17600,17610,19477),
    (17620,17640,17610,17640,1992),(17630,17630,17530,17530,4715),
    (17550,17640,17520,17620,5113),(17590,17640,17570,17630,1676),
    (17630,17630,17580,17580,1577),(17580,17580,17530,17530,2574),
    (17530,17540,17520,17520,2933),(17520,17600,17520,17590,2642),
    (17590,17590,17530,17530,474),(17530,17570,17510,17520,2848),
    (17520,17550,17510,17540,2543),(17570,17570,17500,17500,3320),
    (17510,17510,17460,17460,3085),(17470,17470,17450,17450,4363)]

class AfterMomentumTests(unittest.TestCase):
    def test_observed_burst_first_confirms_failure_at_1659_without_future_candles(self):
        verdicts=[]
        for size in range(1,len(OBSERVED)+1):
            p=position(OBSERVED[:size])
            at=START+timedelta(minutes=size)
            if after_momentum_failure(p,at)[0] is True:verdicts.append(at)
        self.assertEqual(verdicts,[START+timedelta(minutes=22)])
        self.assertGreater(OBSERVED[-1][3],position()['initial_stop'])
    def test_equal_peak_retest_retires_the_older_lower_rebound(self):
        rows=copy.deepcopy(VALUES)+[(17490,17500,17460,17470,2000)]
        rows[3]=(17590,17700,17520,17530,5000)
        self.assertFalse(after_momentum_failure(position(rows),START+timedelta(minutes=len(rows)))[0])
    def test_failed_rebound_exits_above_initial_stop_below_one_r(self):
        p=position();failed,e=after_momentum_failure(p,ASOF)
        self.assertTrue(failed);self.assertLess(p['high']-p['avg'],p['initial_r'])
        self.assertGreater(e['closes'][-1],p['initial_stop']);self.assertEqual(e['rebound_floor'],17540)
    def test_first_controlled_pullback_without_confirmed_rebound_holds(self):
        p=position();p['protection_bars'][2].update(high=17590,close=17580)
        self.assertFalse(after_momentum_failure(p,ASOF)[0])
    def test_higher_low_retained_during_three_bar_pullback_holds(self):
        p=position();p['protection_bars'][2]['low']=17450
        self.assertFalse(after_momentum_failure(p,ASOF)[0])
    def test_new_completed_peak_resets_old_failure(self):
        p=position();p['protection_bars'][3].update(high=17720,close=17700)
        p['high']=17720
        self.assertFalse(after_momentum_failure(p,ASOF)[0])
    def test_reclaiming_close_holds_even_with_faded_volume(self):
        p=position();p['protection_bars'][-1].update(high=17620,close=17610)
        self.assertFalse(after_momentum_failure(p,ASOF)[0])
    def test_first_two_falling_bars_do_not_preempt_confirmation(self):
        p=position();p['protection_bars']=p['protection_bars'][:-1]
        self.assertFalse(after_momentum_failure(p,ASOF-timedelta(minutes=1))[0])
    def test_expanding_sell_volume_does_not_veto_price_failure(self):
        p=position();p['protection_bars'][-1]['volume']=9000
        self.assertTrue(after_momentum_failure(p,ASOF)[0])
    def test_missing_nonfinite_or_negative_volume_is_unknown(self):
        for v in (None,float('nan'),float('inf'),-1,'300'):
            p=position();p['protection_bars'][-1]['volume']=v
            self.assertIsNone(after_momentum_failure(p,ASOF)[0])
    def test_stale_future_partial_or_missing_bar_cannot_force_exit(self):
        cases=[]
        p=position();cases.append((p,ASOF+timedelta(minutes=1)))
        p=position();p['protection_bars'][-1]['time']=ASOF.isoformat();cases.append((p,ASOF))
        p=position();p['protection_bars'][0]['time']=OPEN.isoformat();cases.append((p,ASOF))
        p=position();del p['protection_bars'][1];cases.append((p,ASOF))
        for p,at in cases:self.assertIsNone(after_momentum_failure(p,at)[0])
    def test_partial_entry_minute_does_not_supply_false_peak(self):
        p=position();p['protection_bars'].insert(0,{**p['protection_bars'][0],'time':OPEN.replace(second=0).isoformat()})
        self.assertIsNone(after_momentum_failure(p,ASOF)[0])
    def test_entry_minute_peak_requires_actual_post_fill_tick_time(self):
        p=position();p['protection_bars'][0]['high']=17650;p['high']=17700
        p['after_entry_peak']={'price':17700,'time':(OPEN+timedelta(seconds=10)).isoformat()}
        self.assertTrue(after_momentum_failure(p,ASOF)[0])
        p['after_entry_peak']['time']=(OPEN-timedelta(seconds=1)).isoformat()
        self.assertIsNone(after_momentum_failure(p,ASOF)[0])
    def test_other_session_setup_or_continuation_does_not_inherit_burst_exit(self):
        for change in ({'session':'REGULAR'},{'pattern':'B'},
                       {'evaluation_gates':{'acceleration':{'values':{'mode':'CONFIRMED_CONTINUATION'}}}}):
            p=position();p['signal'].update(change)
            self.assertFalse(after_momentum_failure(p,ASOF)[0])
    def test_naive_entry_timestamp_is_unknown(self):
        p=position();p['opened']=OPEN.replace(tzinfo=None).isoformat()
        self.assertIsNone(after_momentum_failure(p,ASOF)[0])
    def test_flag_is_off_by_default_and_not_string_enabled(self):
        self.assertFalse(AutomationConfig().after_momentum_failure_enabled)
        with self.assertRaises(ValueError):replace(AutomationConfig(),after_momentum_failure_enabled='true').validate()

class AfterEngineTests(unittest.TestCase):
    setUp=fixture.AutomationTests.setUp
    make_engine=fixture.AutomationTests.make_engine
    def prepare(self,enabled=True,new_policy=True):
        self.clock[0]=OPEN
        self.cfg=replace(self.cfg,after_enabled=True)
        self.auto=replace(self.auto,confirmed_invalidation_enabled=True,after_momentum_failure_enabled=enabled)
        self.engine=self.make_engine();self.engine.reconcile(self.snapshot)
        self.engine.connected=True;self.engine.market='3'
        p=position();p.update(qty=3,ownership='BOT',qty_blocked=False,low=17460,last_tick=OPEN.isoformat(),exit_requested=False,exit_attempts=0)
        p['protection_bars']=[]
        if new_policy:p['signal']['invalidation_policy']={'structural_stop':17180,'noise_band':30}
        self.engine.s['positions']['999990']=p
        self.engine.actual={'999990':{'qty':3,'available':3,'avg':17460}}
        return p
    def feed(self,p):
        for b in position()['protection_bars']:
            self.clock[0]=datetime.fromisoformat(b['time'])+timedelta(minutes=1)
            self.engine.observe_protection_bar('999990',b)
    def test_confirmed_failure_latches_evidence_and_submits_once(self):
        p=self.prepare();self.feed(p)
        self.assertTrue(p['exit_requested']);self.assertEqual(p['exit_reason'],'AFTER 급등 후 반등 실패 확인')
        e=copy.deepcopy(p['exit_trigger_evidence']['after_momentum'])
        self.engine.drive_exits({'999990':17480});self.engine.drive_exits({'999990':17480})
        self.assertEqual(len(self.broker.orders),1);self.assertEqual(self.broker.orders[0][0],'SELL')
        p['after_momentum_evidence']={'later':'different'}
        self.engine.request_exit('999990','later')
        self.assertEqual(p['exit_trigger_evidence']['after_momentum'],e)
    def test_existing_position_has_no_retroactive_exit(self):
        p=self.prepare(True,False);self.feed(p)
        self.assertFalse(p['exit_requested']);self.assertEqual(self.broker.orders,[])
    def test_disabled_flag_keeps_original_protection(self):
        p=self.prepare(False,True);self.feed(p)
        self.assertFalse(p['exit_requested']);self.assertEqual(self.broker.orders,[])
    def test_stale_tick_does_not_supply_entry_minute_peak(self):
        p=self.prepare();self.clock[0]=OPEN+timedelta(seconds=10)
        self.engine.on_tick(Tick('999990',OPEN,17800,100,100,17810,17790))
        self.assertNotIn('after_entry_peak',p)
