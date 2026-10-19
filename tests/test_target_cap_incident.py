"""Anonymous, pre-decision price fixtures; fake brokers only."""
import contextlib,copy,io,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from dataclasses import replace
from core.settings import Config,KST,ROOT
from core.strategy import Tick
from core.engine import Engine
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs
from tests import test_structural_a_exit as a_fixture
from tests.test_v1 import FakeBroker

AT=datetime(2026,10,15,11,58,21,tzinfo=KST)
# All these bars ended before AT. No post-entry candles or account data.
OHLC=[(7240,7260,7220,7260),(7230,7250,7220,7240),(7250,7250,7230,7240),
      (7230,7250,7230,7250),(7240,7250,7230,7240),(7240,7250,7210,7220),
      (7220,7240,7210,7210),(7240,7250,7220,7240),(7230,7260,7230,7250),
      (7260,7280,7250,7270),(7270,7300,7260,7300),(7270,7300,7270,7290),
      (7300,7300,7280,7300),(7300,7320,7290,7320),(7320,7360,7310,7350),
      (7360,7540,7350,7530)]

class TargetCapTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Config();self.auto=replace(AutomationConfig(),structural_breakout_enabled=True,
            intraday_context_enabled=True,confirmed_invalidation_enabled=True)
        self.d=PatternDetector(self.cfg,self.auto,Costs(.00015,.00015,.002,.002,.003))
        self.bars=[dict(time=AT.replace(second=0)-timedelta(minutes=len(OHLC)-i),
            open=o,high=h,low=l,close=c,volume=(8955,19648,183613)[i-13] if i>=13 else 1000,
            first_second=0,gap_reason=None,venue='KRX') for i,(o,h,l,c) in enumerate(OHLC)]
        for b in self.bars:self.d.price_context.record('999990',b,AT)
        self.tick=Tick('999990',AT,7580,100,1000,7580,7570)
        self.watch={'status':'WATCH','resistance':None,'source_details':{'a':{},'b':{},'c':{},'d':{}}}
        self.setup=dict(pattern='A',breakout=7540,support=7280,projection=8060)
    def gates(self,setup=None,watch=None):
        return self.d.setup_gates(self.tick,watch or self.watch,setup or self.setup,self.bars[-4:])
    def test_widened_stop_does_not_expand_pattern_reward_to_pass_rr(self):
        g=self.gates()
        self.assertEqual(g['breakout_structure']['result'],'PASS')
        self.assertEqual(g['breakout_structure']['values']['target_ceiling'],8200)
        self.assertEqual(g['invalidation']['values']['stop'],7170)
        self.assertEqual(g['risk_reward']['values']['target'],8060)
        self.assertAlmostEqual(g['risk_reward']['values']['calculated_rr'],480/410)
        self.assertEqual(g['risk_reward']['result'],'FAIL')
    def test_structural_ceiling_still_reduces_a_higher_pattern_target(self):
        g=self.gates({**self.setup,'projection':9000})
        self.assertEqual(g['risk_reward']['values']['target'],8200)
        self.assertEqual(g['risk_reward']['result'],'PASS')
    def test_lower_known_resistance_stays_an_upper_bound(self):
        g=self.gates(watch={**self.watch,'resistance':7900})
        self.assertEqual(g['risk_reward']['values']['target'],7900)
        self.assertEqual(g['risk_reward']['result'],'FAIL')
    def test_real_detector_normal_range_breakout_keeps_sufficient_reward(self):
        d=PatternDetector(self.cfg,self.auto,Costs(.00015,.00015,.002,.002,.003))
        for b in a_fixture.NORMAL:d.price_context.record('999990',b,a_fixture.AT)
        d.prior_high['999990']=10030
        t=Tick('999990',a_fixture.AT,10040,100,1000,10040,10030)
        setup=next(s for s in d.candidates(t,a_fixture.NORMAL) if s['pattern']=='A')
        self.assertEqual(setup['support'],9880)
        g=d.setup_gates(t,self.watch,setup,a_fixture.NORMAL[-4:])
        self.assertEqual(g['risk_reward']['result'],'PASS')
        self.assertLessEqual(g['risk_reward']['values']['target'],setup['projection'])
    def test_final_send_boundary_refuses_old_inflated_signal(self):
        g=self.gates();proof=g['breakout_structure']
        signal={**self.setup,'symbol':'999990','time':AT.isoformat(),'limit':7580,'stop':7170,
            'target':8200,'reference_bar_snapshot':self.bars,
            'evaluation_gates':{'spread':{'values':{'bid':7570,'ask':7580}},'breakout_structure':proof},
            'invalidation_policy':proof['values']}
        e=object.__new__(AutonomousEngine);e.c=self.cfg;e.a=self.auto
        for phase in ('PRE_INTENT','SEND'):
            self.assertEqual(e.entry_market_reason(copy.deepcopy(signal),3,phase=phase),
                'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID')
    def test_future_candle_cannot_change_past_target_or_stop(self):
        before=self.gates()
        self.assertFalse(self.d.price_context.record('999990',dict(time=AT,open=7580,
            high=9000,low=7000,close=8500,volume=999999,venue='KRX'),AT))
        self.assertEqual(before,self.gates())

class UnownedOrderEvidenceTests(unittest.TestCase):
    def test_unknown_sell_keeps_evidence_without_adoption_or_extra_order(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as td,contextlib.redirect_stdout(io.StringIO()):
            broker=FakeBroker();e=Engine(Config(),Path(td),broker,lambda:AT)
            e.s['positions']['999990']={'qty':3,'qty_blocked':False}
            fields={'9203':'SYNTH-SELL','9001':'999990','907':'1','905':'-매도','913':'체결',
                '900':'3','902':'0','910':'7410','908':'121028','9201':'SENSITIVE_ACCOUNT'}
            e.on_order_event(fields)
            self.assertTrue(e.s['halt']);self.assertTrue(e.s['positions']['999990']['qty_blocked'])
            self.assertEqual(e.s['positions']['999990']['qty'],3)
            self.assertEqual(e.s['orders'],{});self.assertEqual(broker.orders,[])
            evidence=e.s['unowned_order_events'][-1]
            self.assertEqual(evidence['order_id'],'SYNTH-SELL')
            self.assertEqual(evidence['origin'],'UNKNOWN_NOT_IN_ORDER_JOURNAL')
            self.assertEqual(evidence['fields']['908'],'121028')
            self.assertNotIn('9201',evidence['fields'])
            disk=json.loads((Path(td)/'state.json').read_text(encoding='utf-8'))
            self.assertEqual(disk['unowned_order_events'][-1],evidence)
            self.assertIn('UNOWNED_ORDER_EVENT',(Path(td)/'events.jsonl').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
