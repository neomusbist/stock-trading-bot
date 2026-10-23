"""Existing A continuation research policy; no new setup or real orders."""
import copy
from datetime import timedelta
import unittest

from automation.patterns import PatternDetector
from automation.structural_breakout import confirmed_continuation
from core.strategy import Tick
from tests import test_structural_a_exit as shape
from tests import test_live_ready_20261021 as lifecycle
MEASUREMENTS=[]


class TrendContinuationEntryTests(unittest.TestCase):
    def rising(self):
        rows=copy.deepcopy(shape.CONTINUING)
        # Pre-existing range low gives enough real price room after costs;
        # it is supplied before entry, never solved backwards by the strategy.
        rows[11]['low']=9550
        for row,volume in zip(rows[-3:],(300,180,240)):row['volume']=volume
        return rows

    def test_rising_price_structure_does_not_need_three_increasing_volume_bars(self):
        self.assertTrue(confirmed_continuation(self.rising(),shape.AT,shape.CFG.min_price_gain)[0])

    def test_adequate_liquidity_continuation_can_replace_failed_three_times_acceleration(self):
        rows=self.rising();d=PatternDetector(shape.CFG,shape.AUTO,shape.COSTS)
        for b in rows:d.price_context.record('999990',b,shape.AT)
        d.prior_high['999990']=10030
        tick=Tick('999990',shape.AT,10040,100,1000,10040,10030)
        gates={'warmup':{'result':'PASS','values':{}},'spread':{'result':'PASS','values':{}},
            'structure':{'result':'PASS','values':{}},'liquidity':{'result':'PASS','values':{'window_turnover':40000000}},
            'acceleration':{'result':'FAIL','values':{'ratio':1.3,'min_ratio':3.,'gain':.002,'min_gain':.003}}}
        watch={'status':'WATCH','resistance':20000,'source_details':{'a':{},'b':{}}}
        trial=d.setup_trials(tick,watch,gates,_bars=rows)[0]
        self.assertTrue(trial['entry_ready'],trial)
        self.assertEqual(trial['gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        gates['liquidity']['values']['window_turnover']=10000000
        self.assertFalse(d.setup_trials(tick,watch,gates,_bars=rows)[0]['entry_ready'])

    def test_price_breakdown_missing_volume_and_incomplete_bar_still_fail(self):
        for change in ({'low':10000},{'close':9990},{'volume':0},{'time':shape.AT}):
            rows=self.rising();rows[-1].update(change)
            self.assertIsNot(confirmed_continuation(rows,shape.AT,shape.CFG.min_price_gain)[0],True)

    def test_continuation_cannot_use_gross_rr_when_net_rr_is_inadequate(self):
        rows=self.rising();rows[11]['low']=9880
        d=PatternDetector(shape.CFG,shape.AUTO,shape.COSTS)
        for b in rows:d.price_context.record('999990',b,shape.AT)
        d.prior_high['999990']=10030
        tick=Tick('999990',shape.AT,10040,100,1000,10040,10030)
        gates={'warmup':{'result':'PASS','values':{}},'spread':{'result':'PASS','values':{}},
            'structure':{'result':'PASS','values':{}},'liquidity':{'result':'PASS','values':{'window_turnover':40000000}},
            'acceleration':{'result':'FAIL','values':{'ratio':1.3,'min_ratio':3.,'gain':.002,'min_gain':.003}}}
        a=d.setup_trials(tick,{'status':'WATCH','resistance':20000},gates,_bars=rows)[0]
        self.assertFalse(a['entry_ready'])
        self.assertEqual(a['gates']['continuation_cost_reward_risk']['result'],'FAIL')
        self.assertEqual(a['candidate_confirmation_basis'],'CONFIRMED_CONTINUATION')
        self.assertEqual(a['gates']['risk_reward']['values']['rr_basis'],'GROSS_PRICE_DISTANCE')
        self.assertIn('net_rr',a['gates']['risk_reward']['values'])

    def test_causal_received_gradual_A_reaches_durable_intent_and_fake_acceptance(self):
        h=lifecycle.LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.watch('A');rows=self.rising()
        for i,b in enumerate(rows):
            start=lifecycle.AT-timedelta(minutes=len(rows)-i)
            volume=10000 if i<len(rows)-3 else (7500,4500,6000)[i-(len(rows)-3)]
            for sec,price in ((0,b['open']),(15,b['high']),(30,b['low']),(59,b['close'])):
                h.feed(start+timedelta(seconds=sec),price,volume,execute=False)
        h.feed(lifecycle.AT,10025,1000,execute=False)
        h.feed(lifecycle.AT+timedelta(seconds=5),10040,4000)
        buys=[o for o in h.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1,h.detector.last_eval.get(lifecycle.SYM))
        buy=buys[0];self.assertEqual(buy['signal']['pattern'],'A')
        self.assertFalse(buy['signal']['acceleration_required'])
        self.assertEqual(buy['signal']['entry_confirmation_basis'],'CONFIRMED_CONTINUATION')
        self.assertEqual(buy['signal']['evaluation_gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        self.assertEqual(buy['status'],'ACCEPTED')
        self.assertEqual(h.broker.intent_checks[0]['side'],'BUY')
        h.fill(buy,buy['qty'],buy['limit'])
        # A valid structure tolerates a normal pullback above its invalidation.
        for sec,price in ((1,10060),(2,10080),(3,10050),(4,10070)):
            h.feed(h.clock+timedelta(seconds=sec),price,100)
        first=h.clock.replace(second=0,microsecond=0)+timedelta(minutes=1)
        for minute,(o,hi,lo,c) in enumerate(((10060,10090,10040,10080),
                                            (10080,10120,10060,10100),
                                            (10100,10110,10070,10090))):
            for sec,price in ((0,o),(15,hi),(30,lo),(59,c)):
                h.feed(first+timedelta(minutes=minute,seconds=sec),price,100)
        self.assertFalse([o for o in h.e.s['orders'].values() if o['side']=='SELL'])
        pos=h.e.s['positions'][lifecycle.SYM]
        hard=pos['signal']['invalidation_policy']['emergency_stop']
        h.feed(h.clock+timedelta(seconds=1),hard-10,100)
        sells=[o for o in h.e.s['orders'].values() if o['side']=='SELL']
        self.assertEqual(len(sells),1)
        h.fill(sells[0],sells[0]['qty'],hard-10)
        self.assertEqual(h.e.s['positions'],{})
        MEASUREMENTS.append({'scope':'SYNTHETIC_CAUSAL_A_CONTINUATION_NOT_MARKET_PERFORMANCE',
            'entry':buy['limit'],'structural_invalidation':buy['signal']['invalidation_policy']['structural_stop'],
            'emergency':hard,'quantity':buy['qty'],'target':buy['signal']['target'],
            'net_rr':buy['signal']['evaluation_gates']['acceleration']['values']['cost_reward_risk'],
            'normal_pullback_completed_minutes':3,'fake_buy_accepted':True,'position_closed_by_emergency':True})


if __name__=='__main__':unittest.main()
