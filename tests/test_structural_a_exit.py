"""Independent strategy cases; no credentials, networking or operating journals."""
import copy,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.price_context import PriceContext
from automation.structural_breakout import structural_breakout,confirmed_continuation
from automation.intraday_context import parse_minutes,IntradayContextWorker,context_targets
from tests import test_automation as fixture
from tests.test_structural_pullback import NORMAL as B_NORMAL
from automation.structural_pullback import structural_pullback
from automation.radar import RadarWorker
from unittest.mock import patch

AT=fixture.START
CFG=Config()
AUTO=replace(AutomationConfig(),structural_breakout_enabled=True,intraday_context_enabled=True,confirmed_invalidation_enabled=True)
COSTS=Costs(.00015,.00015,.002,.002,.003)

def candles(values,at=AT):
    return [dict(time=at-timedelta(minutes=len(values)-i),open=o,high=h,low=l,close=c,volume=100,first_second=0,venue='KRX')
            for i,(o,h,l,c) in enumerate(values)]

NORMAL=candles([(9970,9980,9960,9970)]*10+[
    (9980,10030,9970,10010),(10000,10010,9880,9920),
    (9920,10000,9920,10000),(10000,10020,10000,10020),(10010,10020,10000,10010)])
SETUP={'pattern':'A','breakout':10030,'support':9880,'projection':10330}
# Keep NORMAL as the old gross-only counterexample. A separate wider observed
# base tests a paid-cost path; production never solves a target/stop backwards.
VIABLE_A=copy.deepcopy(NORMAL);VIABLE_A[11]['low']=9500
CONTINUING=copy.deepcopy(VIABLE_A)
CONTINUING[-1].update(open=10020,high=10025,low=10010,close=10025)
for b,v in zip(CONTINUING[-3:],(100,200,300)):b['volume']=v

def price_gates(raw=NORMAL,entry=10040,setup=None):
    d=PatternDetector(CFG,AUTO,COSTS)
    for bar in raw:d.price_context.record('999990',bar,AT)
    tick=Tick('999990',AT,entry,100,1000,entry,entry-10)
    watch={'status':'WATCH','resistance':20000,'source_details':{'a':{},'b':{}}}
    if setup is None:
        if raw is NORMAL:
            setup=SETUP
        else:
            sup=min(b['low'] for b in raw)
            setup={'pattern':'A','breakout':10030,'support':sup,'projection':10030+2*(10030-sup)}
    return d.setup_gates(tick,watch,setup,raw[-3:]),d,tick

class EntryStructureTests(unittest.TestCase):
    def test_meaningful_range_floor_and_noise_sized_hard_line(self):
        gates,_,_=price_gates()
        proof=gates['breakout_structure']['values']
        self.assertEqual(proof['support'],9880)
        self.assertEqual(proof['structural_stop'],9870)
        self.assertEqual(proof['emergency_stop'],9850)
        self.assertGreater(gates['risk_reward']['values']['gross_rr'],1.5)
        self.assertEqual(gates['risk_reward']['result'],'FAIL')
        self.assertLess(gates['risk_reward']['values']['net_rr'],1.5)
    def test_three_micro_bars_do_not_assert_prior_structure(self):
        result,e=structural_breakout(NORMAL[-3:],AT,SETUP,CFG,10040,10030,10040)
        self.assertIsNone(result);self.assertEqual(e['reason'],'PRIOR_STRUCTURE_UNAVAILABLE')
    def test_old_high_caps_reward_even_after_micro_breakout(self):
        raw=copy.deepcopy(NORMAL)
        raw[2].update(high=10060,close=9980)
        gates,_,_=price_gates(raw)
        self.assertEqual(gates['risk_reward']['values']['target'],10060)
        self.assertEqual(gates['risk_reward']['result'],'FAIL')
    def test_completed_close_over_high_retires_resistance(self):
        raw=copy.deepcopy(NORMAL);raw[2].update(high=10000)
        _,e=structural_breakout(raw,AT,SETUP,CFG,10040,10030,10040)
        self.assertIsNone(e['nearest_confirmed_peak'])
    def test_duplicate_shifted_and_missing_recent_bars_are_not_proof(self):
        for raw in [NORMAL+[NORMAL[-1]],NORMAL[:-1],NORMAL[:-1]+[{**NORMAL[-1],'time':AT-timedelta(seconds=30)}]]:
            result,_=structural_breakout(raw,AT,SETUP,CFG,10040,10030,10040)
            self.assertIsNot(result,True)
    def test_future_peak_cannot_cap_past_target(self):
        raw=NORMAL+[dict(time=AT,open=10040,high=20000,low=10030,close=19000)]
        a=structural_breakout(NORMAL,AT,SETUP,CFG,10040,10030,10040)
        self.assertEqual(a,structural_breakout(raw,AT,SETUP,CFG,10040,10030,10040))
    def test_opening_can_use_two_completed_bars_without_waiting_fifteen_minutes(self):
        at=AT.replace(hour=9,minute=2)
        raw=candles([(10000,10010,9990,10000),(10000,10040,10000,10030)],at)
        result,e=structural_breakout(raw,at,{'breakout':10040,'support':9990},CFG,10050,10040,10050)
        self.assertTrue(result);self.assertEqual(e['known_bars'],2)
    def test_unknown_or_crossed_quote_is_never_valid(self):
        for bid,ask in [(0,10040),(10050,10040),(float('nan'),10040)]:
            self.assertIsNot(structural_breakout(NORMAL,AT,SETUP,CFG,10040,bid,ask)[0],True)
    def test_wider_price_structure_is_not_confused_with_account_risk_percent(self):
        raw=copy.deepcopy(NORMAL);raw[11]['low']=9700
        gates,_,_=price_gates(raw)
        self.assertGreater(gates['invalidation']['values']['distance_pct'],.03)
        self.assertEqual(gates['invalidation']['result'],'PASS')
        self.assertEqual(gates['risk_reward']['result'],'FAIL')  # valid floor is not sufficient net reward
        self.assertEqual(gates['invalidation']['values']['upper_bound_basis'],'ACCOUNT_RISK_AND_INTEGER_QUANTITY')
    def test_b_close_policy_keeps_proof_and_reserves_noise_separately(self):
        d=PatternDetector(CFG,AUTO,COSTS);setup=d.pullback(B_NORMAL)
        _,e=structural_pullback(B_NORMAL,B_NORMAL[-1]['time']+timedelta(minutes=1),setup,CFG,1038,1037,1038,close_policy=True)
        self.assertLess(e['emergency_stop'],e['structural_stop'])
    def test_flags_are_strict_booleans(self):
        for name in ('structural_breakout_enabled','intraday_context_enabled','confirmed_invalidation_enabled'):
            with self.assertRaises(ValueError):replace(AUTO,**{name:'true'}).validate()
    def test_sustained_rise_is_distinct_from_ten_second_volume_spike(self):
        self.assertTrue(confirmed_continuation(CONTINUING,AT,CFG.min_price_gain)[0])
        d=PatternDetector(CFG,AUTO,COSTS)
        for b in CONTINUING:d.price_context.record('999990',b,AT)
        d.prior_high['999990']=10030
        t=Tick('999990',AT,10040,100,1000,10040,10030)
        gates={'warmup':{'result':'PASS','values':{}},'spread':{'result':'PASS','values':{}},
            'structure':{'result':'PASS','values':{}},'liquidity':{'result':'PASS','values':{'window_turnover':40000000}},
            'acceleration':{'result':'FAIL','values':{'ratio':1.3,'min_ratio':3.,'gain':.002,'min_gain':.003}}}
        trials=d.setup_trials(t,{'status':'WATCH','resistance':20000,'source_details':{'a':{},'b':{}}},gates,_bars=CONTINUING)
        a=trials[0]
        self.assertTrue(a['entry_ready']);self.assertEqual(a['gates']['acceleration']['values']['mode'],'CONFIRMED_CONTINUATION')
        gates['liquidity']['values']['window_turnover']=10000000
        self.assertFalse(d.setup_trials(t,{'status':'WATCH','resistance':20000},gates,_bars=CONTINUING)[0]['entry_ready'])
    def test_flat_floor_missing_volume_and_future_bar_cannot_confirm_continuation(self):
        raw=copy.deepcopy(CONTINUING);raw[-1]['low']=raw[-2]['low']
        self.assertFalse(confirmed_continuation(raw,AT,CFG.min_price_gain)[0])
        raw=copy.deepcopy(CONTINUING);raw[-1]['volume']=0
        self.assertIsNone(confirmed_continuation(raw,AT,CFG.min_price_gain)[0])
        raw=copy.deepcopy(CONTINUING);raw[-1]['time']=AT
        self.assertIsNone(confirmed_continuation(raw,AT,CFG.min_price_gain)[0])

def api_data(at=AT):
    rows=[]
    for bar in NORMAL+[dict(time=at,open=10040,high=12000,low=9000,close=11000,volume=100)]:
        rows.append(dict(cntr_tm=bar['time'].strftime('%Y%m%d%H%M%S'),open_pric=str(bar['open']),
            high_pric=str(bar['high']),low_pric=str(bar['low']),cur_prc=str(bar['close']),trde_qty=str(bar['volume'])))
    return {'stk_cd':'999990','stk_min_pole_chart_qry':list(reversed(rows))}

class ReferenceTests(unittest.TestCase):
    def test_current_minute_is_excluded_and_market_time_is_preserved(self):
        bars=parse_minutes(api_data(),AT,'KRX','999990')
        self.assertEqual(len(bars),15);self.assertEqual(bars[-1]['time'],AT-timedelta(minutes=1))
        self.assertEqual(bars[-1]['source'],'REST_REFERENCE')
    def test_wrong_symbol_venue_ohlc_and_duplicate_reject(self):
        wrong=api_data();wrong['stk_cd']='999980'
        broken=api_data();broken['stk_min_pole_chart_qry'][-1]['low_pric']='30000'
        dup=api_data();dup['stk_min_pole_chart_qry'].append(dup['stk_min_pole_chart_qry'][-1])
        for data,venue in [(wrong,'KRX'),(broken,'KRX'),(dup,'KRX'),(api_data(),'UNKNOWN')]:
            with self.assertRaises(ValueError):parse_minutes(data,AT,venue,'999990')
    def test_actual_empty_nxt_record_is_neither_candle_nor_ready_reference(self):
        data={'stk_cd':'999990_NX','stk_min_pole_chart_qry':[dict(cntr_tm='',cur_prc='',trde_qty='',open_pric='',high_pric='',low_pric='')]}
        self.assertEqual(parse_minutes(data,AT,'NXT','999990'),[])
        rest=Mock();rest.query.return_value=data
        packet=IntradayContextWorker(rest,clock=lambda:AT).fetch('999990','NXT')
        self.assertEqual(packet['error'],'REFERENCE_NO_HISTORY');self.assertFalse(packet['bars'])
        data['stk_min_pole_chart_qry'][0]['high_pric']='10000'
        with self.assertRaises(ValueError):parse_minutes(data,AT,'NXT','999990')
    def test_rest_merge_never_creates_warmup_or_signal_bars(self):
        d=PatternDetector(CFG,AUTO,COSTS)
        d.price_context.merge_history('999990',parse_minutes(api_data(),AT,'KRX','999990'),AT,'KRX')
        self.assertFalse(d.history);self.assertFalse(d.buffers);self.assertFalse(d.observation_start)
        self.assertEqual(len(d.price_context.reference_bars('999990',AT,'KRX')),15)
        self.assertFalse(d.price_context.reference_bars('999990',AT-timedelta(seconds=1),'KRX'))
        self.assertFalse(d.price_context.reference_bars('999990',AT,'NXT'))
    def test_handoff_preserves_reference_receipt_and_does_not_claim_warmup(self):
        a=PriceContext(bar_limit=120);b=PriceContext(bar_limit=120)
        a.merge_history('999990',parse_minutes(api_data(),AT,'KRX','999990'),AT,'KRX')
        b.transfer(a,'999990')
        self.assertEqual(a.reference_bars('999990',AT),b.reference_bars('999990',AT))
        self.assertFalse(b.reference_bars('999990',AT-timedelta(seconds=1)))
    def test_cross_venue_history_replaces_instead_of_mixing_price_references(self):
        a=PriceContext(bar_limit=120)
        a.merge_history('999990',parse_minutes(api_data(),AT,'KRX','999990'),AT,'KRX')
        other=parse_minutes(api_data(),AT,'NXT','999990')
        for b in other:b['high']+=10
        a.merge_history('999990',other,AT,'NXT')
        self.assertFalse(a.reference_bars('999990',AT,'KRX'))
        self.assertEqual(a.reference_bars('999990',AT,'NXT')[0]['high'],other[0]['high'])
    def test_worker_reads_only_chart_with_matching_venue(self):
        rest=Mock();rest.query.return_value=api_data()
        worker=IntradayContextWorker(rest,clock=lambda:AT)
        worker.fetch('999990','NXT')
        self.assertEqual(rest.query.call_args.args[0],'ka10080')
        self.assertEqual(rest.query.call_args.args[1]['stk_cd'],'999990_NX')
        worker.set_targets({str(i):'KRX' for i in range(100)})
        self.assertEqual(len(worker.targets),14)
    def test_two_preliminary_shadow_candidates_earn_context_but_never_order_permission(self):
        gates={k:{'result':'PASS'} for k in ('warmup','spread','structure','liquidity','breakout','chase','acceleration')}
        rows={str(i):{'timestamp':AT.isoformat(),'setup_trials':[{'setup':'A','candidate_generated':True,'gates':gates}]} for i in range(20)}
        selected=context_targets([str(i) for i in range(12)],rows,'KRX',AT)
        self.assertEqual(len(selected),14);self.assertTrue(set(map(str,range(12)))<=set(selected))
        self.assertEqual(len(context_targets([],rows,'KRX',AT+timedelta(seconds=6))),0)
    def test_moving_affordable_raw_gets_bounded_early_status_admission(self):
        worker=object.__new__(RadarWorker);worker.a=AUTO;worker.radar=SimpleNamespace(watch={},weak={},policy=SimpleNamespace(locked=lambda s:False),affordable=lambda r:True)
        worker.status_waiting={};worker.status_sequence=0;worker.status_cache={};worker.status_priority_symbols=frozenset(str(i) for i in range(12))
        rows=[{'code':str(i),'source_details':{'s':{}},'discovery_context':{'last_cycle_return':.012 if i==55 else 0}} for i in range(85)]
        with patch('automation.radar.now',return_value=AT):order=worker.scheduled_items(rows)
        self.assertEqual([x['code'] for x in order[:12]],list(map(str,range(12))))
        self.assertEqual(order[12]['code'],'55');self.assertEqual(order[13]['code'],'12')
        self.assertEqual(len(order),85)
        worker.status_cache['55']={'checked_at':AT.isoformat(),'pass':True}
        with patch('automation.radar.now',return_value=AT):order=worker.scheduled_items(rows)
        # Cheap fresh cache reuse is evaluated before the first admission HTTP;
        # it consumes no part of the aging floor or the 20-query scan budget.
        self.assertEqual(order[12]['code'],'55')
        self.assertEqual(order[13]['code'],'12')
    def test_recently_discovered_rising_rank_is_admitted_before_its_next_delta(self):
        worker=object.__new__(RadarWorker);worker.a=AUTO;worker.radar=SimpleNamespace(watch={},weak={},policy=SimpleNamespace(locked=lambda s:False),affordable=lambda r:True)
        worker.status_waiting={};worker.status_sequence=0;worker.status_cache={};worker.status_priority_symbols=frozenset()
        rows=[{'code':str(i),'source_details':{'s':{'values':{'flu_rt':5.81}}},'first_raw_at':(AT-timedelta(minutes=10)).isoformat()} for i in range(85)]
        rows[55]['first_raw_at']=(AT-timedelta(seconds=20)).isoformat()
        with patch('automation.radar.now',return_value=AT):order=worker.scheduled_items(rows)
        self.assertEqual(order[0]['code'],'55');self.assertEqual(order[1]['code'],'0')

class ExitAndOrderTests(unittest.TestCase):
    setUp=fixture.AutomationTests.setUp
    make_engine=fixture.AutomationTests.make_engine
    def enable(self):
        self.auto=replace(self.auto,structural_breakout_enabled=True,intraday_context_enabled=True,confirmed_invalidation_enabled=True)
        self.engine=self.make_engine();self.engine.reconcile(self.snapshot);self.engine.connected=True;self.engine.market='3'
        gates,d,tick=price_gates(VIABLE_A)
        policy=gates['breakout_structure']['values']
        self.sig={**SETUP,'support':9500,'projection':11090,'symbol':'999990','time':AT.isoformat(),'limit':10040,'stop':policy['emergency_stop'],
            'target':gates['risk_reward']['values']['target'],'evaluation_gates':{**gates,'spread':{'result':'PASS','values':{'bid':10030,'ask':10040}}},
            'reference_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in VIABLE_A],
            'reference_received_at':(AT-timedelta(seconds=1)).isoformat(),'invalidation_policy':copy.deepcopy(policy),
            'efficiency':gates['economic']['values']['score']}
        self.tick=tick
    def buy_new(self):
        self.enable();o=self.engine.enter(self.sig,self.tick);self.assertIsNotNone(o)
        self.engine.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        self.engine.actual={'999990':{'qty':o['qty'],'available':o['qty'],'avg':o['limit']}}
        return self.engine.s['positions']['999990']
    def test_new_order_sizes_from_emergency_line_and_keeps_soft_structure(self):
        p=self.buy_new();self.assertEqual(p['initial_stop'],9470)
        self.assertEqual(p['signal']['invalidation_policy']['structural_stop'],9490)
        self.assertLessEqual(p['qty']*self.sig['efficiency']['risk_per_share'],self.engine.risk_equity()*self.cfg.risk_fraction)
    def test_gui_distinguishes_structural_close_line_and_emergency_risk_line(self):
        from operator_ui_service import position_invalidation_lines
        p=self.buy_new()
        self.assertEqual(position_invalidation_lines(p,True),(9490,9470,True))
        self.assertEqual(position_invalidation_lines(p,False),(9470,9470,False))
    def test_single_touch_between_structural_and_hard_line_holds(self):
        p=self.buy_new();t=replace(self.tick,price=9480,ask=9490,bid=9470)
        self.engine.on_tick(t);self.assertFalse(p['exit_requested']);self.assertEqual(len(self.broker.orders),1)
    def test_completed_close_below_structure_requests_exit(self):
        p=self.buy_new();self.clock[0]=AT+timedelta(minutes=1)
        self.engine.observe_protection_bar('999990',dict(time=AT,open=10040,high=10050,low=9480,close=9480))
        self.assertTrue(p['exit_requested']);self.assertEqual(p['exit_reason'],'확정봉 종가의 구조적 무효화')
    def test_intra_minute_wick_with_recovery_does_not_exit(self):
        p=self.buy_new();self.clock[0]=AT+timedelta(minutes=1)
        self.engine.observe_protection_bar('999990',dict(time=AT,open=10040,high=10050,low=9480,close=9520))
        self.assertFalse(p['exit_requested'])
    def test_fresh_emergency_price_exits_without_waiting_for_close(self):
        p=self.buy_new();self.engine.on_tick(replace(self.tick,price=9460,ask=9470,bid=9450))
        self.assertTrue(p['exit_requested']);self.assertEqual(len(self.broker.orders),2)
    def test_stale_trade_and_stale_quote_do_not_fake_emergency(self):
        p=self.buy_new();self.clock[0]+=timedelta(seconds=6)
        p['last_quote']={'time':AT.isoformat(),'bid':9450,'ask':9460}
        self.engine.on_tick(replace(self.tick,price=9460,ask=9470,bid=9450))
        self.assertFalse(p['exit_requested'])
    def test_stale_completed_bar_does_not_undo_current_recovery(self):
        p=self.buy_new();self.clock[0]=AT+timedelta(minutes=2)
        self.engine.observe_protection_bar('999990',dict(time=AT,open=10040,high=10050,low=9480,close=9480))
        self.assertFalse(p['exit_requested'])
    def test_proof_stop_and_reference_tampering_never_orders(self):
        for change in ('stop','soft','future','bar'):
            self.enable();s=copy.deepcopy(self.sig)
            if change=='stop':s['stop']+=10
            if change=='soft':s['invalidation_policy']['structural_stop']+=10
            if change=='future':s['reference_received_at']=(AT+timedelta(seconds=1)).isoformat()
            if change=='bar':s['reference_bar_snapshot'][0]['low']-=10
            self.assertIsNone(self.engine.enter(s,self.tick));self.assertEqual(self.broker.orders,[])
    def test_final_fresh_book_above_limit_blocks_actual_mock_order(self):
        self.enable();self.engine.entry_quote_provider=lambda symbol:(self.tick,{'time':AT.isoformat(),'best_bid':10040,'best_ask':10050})
        self.assertIsNone(self.engine.enter(self.sig,self.tick));self.assertEqual(self.broker.orders,[])
    def test_cash_affordable_but_one_share_risk_over_budget_never_orders(self):
        self.cfg=replace(self.cfg,risk_fraction=.0001)
        self.enable();self.assertGreater(self.engine.available_cash,self.sig['limit'])
        self.assertIsNone(self.engine.enter(self.sig,self.tick));self.assertEqual(self.broker.orders,[])
    def test_fabricated_continuation_cannot_replace_acceleration_at_order_boundary(self):
        self.enable()
        self.sig['evaluation_gates']['acceleration']={'result':'PASS','values':{'mode':'CONFIRMED_CONTINUATION','continuation':{'fake':True}}}
        self.assertIsNone(self.engine.enter(self.sig,self.tick));self.assertEqual(self.broker.orders,[])
