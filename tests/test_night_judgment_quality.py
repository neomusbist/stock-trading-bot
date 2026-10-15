"""Dedicated overnight regressions. Mock brokers and received candle fixtures only."""
import copy,unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from datetime import timedelta
from core.strategy import Tick
from automation.exit_plan import build_plan
from automation.patterns import PatternDetector
from tests.test_structural_a_exit import CFG,AUTO,COSTS,AT,CONTINUING
from tests import test_subscription_refresh as wire
from automation.entry_timing import ObservationTimeline,annotate_signal
from tools.exit_policy_replay import compare


class SustainedLocalRiseTests(unittest.TestCase):
    def detector(self,rows=CONTINUING,auto=AUTO):
        d=PatternDetector(CFG,auto,COSTS)
        for b in rows:d.price_context.record('999990',b,AT)
        # An earlier observed session high is not this new base's breakout.
        d.prior_high['999990']=11000
        return d

    def tick(self,price=10040):return Tick('999990',AT,price,100,1000,price,price-10)

    def common(self):
        return {'warmup':{'result':'PASS','values':{}},'spread':{'result':'PASS','values':{}},
                'structure':{'result':'PASS','values':{}},'liquidity':{'result':'PASS','values':{'window_turnover':40000000}},
                'acceleration':{'result':'FAIL','values':{'ratio':1.3,'min_ratio':3.,'gain':.002,'min_gain':.003}}}

    def watch(self):return {'status':'WATCH','venue':'KRX','resistance':20000,'source_details':{'a':{},'b':{}}}

    def trial(self,d=None,rows=CONTINUING,common=None,watch=None):
        d=d or self.detector(rows)
        return d.setup_trials(self.tick(),watch or self.watch(),common or self.common(),_bars=rows)[0]

    def test_current_structure_can_form_A_below_an_older_session_high(self):
        a=self.trial()
        self.assertTrue(a['candidate_generated'])
        self.assertTrue(a['entry_ready'])
        self.assertEqual(a['gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        self.assertEqual(a['setup_geometry']['breakout'],10030)

    def test_near_known_overhead_keeps_local_rise_blocked(self):
        rows=copy.deepcopy(CONTINUING);rows[2].update(high=10060,close=9980)
        a=self.trial(rows=rows)
        self.assertTrue(a['candidate_generated']);self.assertFalse(a['entry_ready'])
        self.assertEqual(a['gates']['risk_reward']['result'],'FAIL')

    def test_known_session_high_outside_the_reference_window_caps_local_target(self):
        d=self.detector();d.prior_high['999990']=10060
        a=self.trial(d=d)
        self.assertTrue(a['candidate_generated']);self.assertFalse(a['entry_ready'])
        self.assertEqual(a['gates']['risk_reward']['values']['target'],10060)
        self.assertEqual(a['gates']['risk_reward']['result'],'FAIL')

    def test_warmup_and_liquidity_are_not_bypassed_by_continuation(self):
        for gate in ('warmup','liquidity'):
            common=self.common()
            if gate=='warmup':common[gate]['result']='FAIL'
            else:common[gate]['values']['window_turnover']=10000000
            a=self.trial(common=common)
            self.assertTrue(a['candidate_generated']);self.assertFalse(a['entry_ready'])

    def test_flag_off_preserves_the_original_session_high_boundary(self):
        d=self.detector(auto=replace(AUTO,structural_breakout_enabled=False))
        self.assertFalse(self.trial(d=d)['candidate_generated'])

    def test_weak_rebound_volume_flat_lows_and_partial_bar_cannot_form_local_A(self):
        for kind in ('volume','low','partial'):
            rows=copy.deepcopy(CONTINUING)
            if kind=='volume':rows[-1]['volume']=50
            elif kind=='low':rows[-1]['low']=rows[-2]['low']
            else:rows[-1]['time']=AT
            self.assertFalse(self.trial(rows=rows)['candidate_generated'])

    def test_reference_history_alone_cannot_manufacture_received_signal_bars(self):
        d=self.detector()
        self.assertFalse(self.trial(d=d,rows=[])['candidate_generated'])

    def test_lower_rebound_below_the_recent_range_ceiling_stays_blocked(self):
        rows=copy.deepcopy(CONTINUING);rows[-10]['high']=10100
        self.assertFalse(self.trial(rows=rows)['candidate_generated'])

    def test_no_local_continuation_if_reference_was_received_later(self):
        d=self.detector();d.price_context.rows['999990']['history_received_at']=(AT+timedelta(seconds=1)).isoformat()
        self.assertFalse(self.trial(d=d)['candidate_generated'])

    def test_future_reference_is_not_a_past_local_breakout_or_target(self):
        d=self.detector();before=self.trial(d=d)
        self.assertFalse(d.price_context.record('999990',{'time':AT,'open':10040,'high':50000,'low':10040,'close':50000,'volume':999999},AT))
        self.assertEqual(before,self.trial(d=d))

    def test_after_local_continuation_does_not_inherit_the_burst_exit_policy(self):
        after=AT.replace(hour=16,minute=20)
        rows=copy.deepcopy(CONTINUING)
        for b in rows:b.update(time=b['time']+(after-AT),venue='SOR')
        auto=replace(AUTO,after_momentum_failure_enabled=True)
        d=PatternDetector(CFG,auto,COSTS)
        for b in rows:d.price_context.record('999990',b,after)
        d.after_prior_high['999990']=11000
        t=Tick('999990',after,10040,100,1000,10040,10030,venue='SOR')
        watch={**self.watch(),'venue':'SOR'};common=self.common();common['acceleration']['result']='PASS'
        a=d.setup_trials(t,watch,common,_bars=rows)[0]
        self.assertTrue(a['entry_ready']);self.assertEqual(a['gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        sig={**a['setup_geometry'],'session':'AFTER','venue':'SOR','limit':10040,'stop':9850,
             'target':10330,'invalidation_policy':a['gates']['breakout_structure']['values'],'evaluation_gates':a['gates']}
        self.assertFalse(build_plan(CFG,auto,sig)['after_failed_rebound'])


class LocalRiseReceiverTests(unittest.TestCase):
    setUp=wire.SubscriptionRefreshTests.setUp
    frame=wire.SubscriptionRefreshTests.frame
    send=wire.SubscriptionRefreshTests.send

    def observed(self):
        self.e.a=replace(self.auto,AUTO_TRADING_CAP_KRW=81300,structural_breakout_enabled=True,intraday_context_enabled=True,confirmed_invalidation_enabled=True)
        self.d=PatternDetector(self.cfg,self.e.a,self.e.costs);self.d.continuous_observation=True
        self.d.day_high[wire.SYM]=11000
        finish=self.clock.replace(hour=10,minute=30,second=0,microsecond=0)
        cumulative=0
        for i,b in enumerate(CONTINUING):
            scale=1 if i<len(CONTINUING)-2 else i-len(CONTINUING)+3
            for sec in range(60):
                at=finish-timedelta(minutes=len(CONTINUING)-i)+timedelta(seconds=sec)
                price=(b['open'],b['high'],b['low'],b['close'])[min(sec,3)]
                volume=1000*scale;cumulative+=volume
                self.assertTrue(self.d.ingest(Tick(wire.SYM,at,price,volume,cumulative,price,price-10),at))
        self.clock=finish;self.e.reset_detector=False
        self.watch.update(resistance=20000,source_details={'a':{},'b':{}},venue='KRX')
        self.radar.cache[(finish.date(),wire.SYM)]=[
            {'datetime':(finish-timedelta(days=500-i)).date().isoformat(),
             'open':9900,'high':10000,'low':9800,'close':9950,'volume':200000}
            for i in range(500)]
        return Tick(wire.SYM,finish,10040,3000,cumulative+3000,10040,10030)

    def test_real_receiver_to_engine_and_mock_broker_uses_local_signal_and_actual_risk(self):
        t=self.observed();self.send(self.frame(t),{wire.SYM:self.watch})
        buys=[o for o in self.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1);o=buys[0]
        self.assertEqual(o['signal']['entry_basis'],'LOCAL_CONFIRMED_CONTINUATION')
        self.assertEqual(o['signal']['evaluation_gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        self.assertLess(o['signal']['breakout'],11000)
        self.assertEqual(o['status'],'ACCEPTED');self.assertEqual(len(self.broker.orders),1)
        self.assertGreater(o['qty'],0);self.assertLess(o['signal']['stop'],o['limit'])
        self.assertEqual(o['signal']['approved_cap_krw'],81300)
        self.assertLessEqual(o['qty']*o['signal']['efficiency']['risk_per_share'],o['signal']['account_risk_budget_krw'])
        self.assertEqual(o['signal']['invalidation_policy']['reason'],'CONFIRMED_RANGE_BASE')
        self.e.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        p=self.e.s['positions'][wire.SYM]
        self.assertEqual(p['initial_r'],p['avg']-p['initial_stop'])
        self.assertEqual(p['exit_plan']['initial_target'],o['signal']['target'])
        self.assertFalse(p['exit_requested'])
        self.e.actual={wire.SYM:{'qty':p['qty'],'available':p['qty'],'avg':p['avg']}}
        soft=p['signal']['invalidation_policy']['structural_stop'];hard=p['initial_stop']
        touch=(soft+hard)/2
        self.clock=t.time+timedelta(seconds=1)
        self.send(self.frame(Tick(wire.SYM,self.clock,touch,100,t.cumulative+100,touch+10,touch)))
        self.assertFalse(p['exit_requested'])  # soft touch is not a completed close
        self.clock=t.time+timedelta(minutes=1)
        self.send(self.frame(Tick(wire.SYM,self.clock,soft+10,100,t.cumulative+200,soft+20,soft+10)))
        sells=[x for x in self.e.s['orders'].values() if x['side']=='SELL']
        self.assertEqual(len(sells),1);sell=sells[0]
        self.assertEqual(p['exit_reason'],'확정봉 종가의 구조적 무효화')
        self.e.cumulative_fill(sell,sell['qty'],sell['qty']*(soft+10))
        self.assertFalse(self.e.s['positions']);self.assertEqual(len(self.e.s['trades']),1)
        self.assertTrue(self.e.s['trades'][0]['exit_trigger_evidence'])

    def test_old_signal_is_still_rejected_at_the_final_order_boundary(self):
        t=self.observed();self.assertTrue(self.d.ingest(t,t.time))
        signal=self.d.signal(t,self.watch);self.assertIsNotNone(signal)
        self.clock+=timedelta(seconds=self.cfg.stale_seconds+1)
        self.assertIsNone(self.e.enter(signal,t));self.assertEqual(self.broker.orders,[])


class DayLineageTests(unittest.TestCase):
    def test_restarted_trade_matches_the_GUI_day_discovery_timeline(self):
        at=AT.replace(hour=10,minute=30);first_raw=at-timedelta(hours=1);first_watch=first_raw+timedelta(minutes=2)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'timeline.json';prior=ObservationTimeline(path)
            prior.raw([{'code':'999990','first_raw_at':first_raw.isoformat()}],first_raw)
            prior.watch('999990',first_watch);prior.signal('999990',first_watch+timedelta(minutes=3));prior.save(first_watch+timedelta(minutes=4))
            engine=SimpleNamespace(observation_timeline=ObservationTimeline(path))
            detector=SimpleNamespace(observation_start={'999990':at-timedelta(minutes=2)})
            sig={'symbol':'999990','time':at.isoformat()}
            annotate_signal(engine,detector,{'first_raw_at':(at-timedelta(minutes=10)).isoformat(),'first_watch_at':(at-timedelta(minutes=5)).isoformat()},sig)
            self.assertEqual(sig['candidate_detected_at'],first_raw.isoformat())
            self.assertEqual(sig['opportunity_timing']['first_watch_at'],first_watch.isoformat())
            self.assertEqual(sig['opportunity_timing']['first_detector_signal_at'],(first_watch+timedelta(minutes=3)).isoformat())
            self.assertEqual(sig['opportunity_timing']['observation_started_at'],detector.observation_start['999990'].isoformat())
            self.assertTrue(sig['opportunity_timing']['diagnostic_only'])
            self.assertNotIn('first_risk_eligible_at',sig['opportunity_timing'])

    def test_prior_day_and_future_discovery_cannot_replace_current_observation(self):
        for value in (AT-timedelta(days=1),AT+timedelta(seconds=1)):
            timeline=ObservationTimeline();timeline.day=AT.date();timeline.rows['999990']={'first_raw_at':value.isoformat()}
            engine=SimpleNamespace(observation_timeline=timeline)
            sig={'symbol':'999990','time':AT.isoformat()};detector=SimpleNamespace(observation_start={})
            annotate_signal(engine,detector,{},sig)
            self.assertIsNone(sig['candidate_detected_at']);self.assertIsNone(sig['opportunity_timing']['observation_started_at'])


class ExitAttributionTests(unittest.TestCase):
    def trade(self):
        return dict(symbol='999990',trade_id='synthetic',opened=(AT-timedelta(minutes=5)).isoformat(),closed=AT.isoformat(),
                    qty=2,entry=10000,exit=10200,exit_price=10200,pnl_estimate=360,MFE=300,MAE=20,
                    initial_stop=9800,initial_risk=200,holding_seconds=300,completed=True,reason='구조/이익보호선 이탈',
                    pattern='A',signal={'pattern':'A','stop':9800,'target':10400,'limit':10000,'entry_build_id':'LEGACY'},pnl_source='BROKER_QUERY')

    def test_old_build_returns_are_not_reported_as_current_AE3_expectancy(self):
        r=compare([self.trade()]);self.assertEqual(r['ae3_execution_evidence_count'],0)
        self.assertNotIn('existing_actual_policy',r);self.assertEqual(r['recorded_historical_policy']['count'],1)
        self.assertEqual(r['trade_results'][0]['policies']['AE3']['status'],'HISTORICAL_EXIT_NOT_VALIDATED_AE3')
        self.assertFalse(r['new_policy_expectancy_proven'])

    def test_manual_exit_is_not_AE3_even_when_book_adopted_by_a_new_build(self):
        t=self.trade();t.update(reason='사용자 수동 매도',exit_build_id='NEW_BUILD',
                              exit_trigger_evidence={'exit_state':{'plan':{'profit_policy':'AE3'}}})
        self.assertEqual(compare([t])['ae3_execution_evidence_count'],0)


if __name__=='__main__':unittest.main()
