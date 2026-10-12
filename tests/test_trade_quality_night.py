"""Dedicated quality changes; no edits to the baseline test inventory or real broker."""
import copy, json, unittest
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from core.settings import Config, KST
from core.strategy import protective_stop
from automation.policy import AutomationConfig
from automation.trade_quality import box_direction, complete_sequence, confirmed_protection
from automation.price_context import PriceContext
from automation.entry_context_audit import confirmed_overhead
from automation.entry_timing import annotate_signal, watch_started, mark_risk_eligible
from tests import test_setup_acceleration as entry_fixture
from tests import test_standing_book_confirmation as book_fixture
from tests import test_trade_quality as quality_fixture
from tests import test_price_context as context_fixture

START=datetime(2026,10,12,10,0,tzinfo=KST)


def candles(values,start=START):
    return [{'time':start+timedelta(minutes=i),'open':o,'high':h,'low':l,'close':c,
        'volume':100,'first_second':0,'gap_reason':None} for i,(o,h,l,c) in enumerate(values)]


class DirectionAndReferenceTests(unittest.TestCase):
    def test_declining_range_requires_completed_reversal_not_just_live_breakout(self):
        b=candles([(100,103,99,100),(100,101,97,98),(98,100,96,97),(97,99,97,98),(98,100,98,99)])
        passed,why=box_direction(b,START+timedelta(minutes=5),5)
        self.assertFalse(passed);self.assertEqual(why['reason'],'DECLINING_RANGE')
        self.assertFalse(why['completed_reversal'])

    def test_confirmed_reversal_with_lower_endpoints_is_preserved(self):
        b=candles([(103,106,101,102),(102,103,97,98),(98,100,96,97),(97,99,97,98),(98,100,98,100)])
        passed,why=box_direction(b,START+timedelta(minutes=5),5)
        self.assertTrue(passed);self.assertTrue(why['endpoint_declining'])
        self.assertTrue(why['completed_reversal']);self.assertEqual(why['reason'],'CONFIRMED_REVERSAL_BASE')

    def test_flat_and_rising_boxes_still_pass_direction_only(self):
        for b in (candles([(100,101,99,100)]*5),candles([(100+i,102+i,99+i,101+i) for i in range(5)])):
            self.assertTrue(box_direction(b,START+timedelta(minutes=5),5)[0])

    def test_old_contiguous_box_cannot_hide_latest_missing_minute(self):
        b=candles([(100,101,99,100)]*5)
        self.assertIsNone(box_direction(b,START+timedelta(minutes=6),5)[0])
        gap=candles([(100,101,99,100)],START+timedelta(minutes=5))[0];gap['gap_reason']='TRANSPORT_GAP'
        self.assertIsNone(box_direction(b+[gap],START+timedelta(minutes=6),5)[0])

    def test_future_partial_shifted_missing_and_invalid_bars_are_unknown(self):
        base=candles([(100,101,99,100)]*5);asof=START+timedelta(minutes=5)
        for mode in ('future','partial','second','microsecond','gap','missing','nan','bounds','bool','naive'):
            b=copy.deepcopy(base)
            if mode=='future':b[-1]['time']=asof+timedelta(minutes=1)
            elif mode=='partial':b[-1]['time']=asof
            elif mode=='second':b[-1]['time']+=timedelta(seconds=1)
            elif mode=='microsecond':b[-1]['time']+=timedelta(microseconds=1)
            elif mode=='gap':b[2]['gap_reason']='TRANSPORT_GAP'
            elif mode=='missing':b.pop()
            elif mode=='nan':b[2]['close']=float('nan')
            elif mode=='bounds':b[2]['low']=102
            elif mode=='bool':b[2]['close']=True
            else:b[2]['time']=b[2]['time'].replace(tzinfo=None)
            with self.subTest(mode=mode):self.assertIsNone(box_direction(b,asof,5)[0])
        self.assertEqual(complete_sequence(base,asof,0),[])

    def context(self):
        p=PriceContext();b=candles([(99,101,98,100),(100,105,99,101),(101,102,99,100)])
        for bar in b:p.record('999990',bar,bar['time']+timedelta(minutes=1))
        return p,b

    def test_unbroken_overhead_stays_a_cap_even_if_future_trade_wins(self):
        p,b=self.context();asof=START+timedelta(minutes=3)
        snap=p.snapshot('999990',SimpleNamespace(time=asof),100)
        self.assertEqual(snap['nearest_confirmed_peak']['price'],105)
        self.assertEqual(snap['cleared_peak_count'],0)

    def test_live_wick_cannot_clear_resistance_completed_close_can(self):
        p,b=self.context()
        wick=candles([(100,108,100,104)],START+timedelta(minutes=3))[0]
        self.assertTrue(p.record('999990',wick,START+timedelta(minutes=4)))
        self.assertEqual(p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=4)),100)['nearest_confirmed_peak']['price'],105)
        close=candles([(104,108,103,106)],START+timedelta(minutes=4))[0]
        self.assertFalse(p.record('999990',close,START+timedelta(minutes=4,seconds=50)))
        self.assertTrue(p.record('999990',close,START+timedelta(minutes=5)))
        snap=p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=5)),100)
        self.assertEqual(snap['cleared_peak_count'],1)
        self.assertNotEqual((snap['nearest_confirmed_peak'] or {}).get('price'),105)

    def test_reference_handoff_cannot_resurrect_cleared_peak_from_older_copy(self):
        p,b=self.context();older=copy.deepcopy(p)
        close=candles([(100,108,100,106)],START+timedelta(minutes=3))[0]
        p.record('999990',close,START+timedelta(minutes=4));p.transfer(older,'999990')
        self.assertEqual(p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=4)),100)['cleared_peak_count'],1)

    def test_reset_source_completed_close_clears_destination_peak_at_handoff(self):
        p,b=self.context();source=PriceContext()
        close=candles([(100,108,100,106)],START+timedelta(minutes=3))[0]
        self.assertTrue(source.record('999990',close,START+timedelta(minutes=4)))
        p.transfer(source,'999990')
        self.assertEqual(p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=4)),100)['cleared_peak_count'],1)
        # A historical snapshot before that close completes must still see it.
        self.assertEqual(p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=3,seconds=59)),100)['nearest_confirmed_peak']['price'],105)

    def test_snapshot_replay_retires_only_valid_predecision_close(self):
        p,b=self.context();asof=START+timedelta(minutes=4)
        close=candles([(100,108,100,106)],START+timedelta(minutes=3))[0]
        self.assertIsNone(confirmed_overhead(b+[close],asof,100))
        self.assertEqual(confirmed_overhead(b+[close],asof-timedelta(seconds=1),100)['price'],105)
        for mode in ('gap','invalid','wick'):
            altered=copy.deepcopy(close)
            if mode=='gap':altered['gap_reason']='MISSING_MINUTE'
            elif mode=='invalid':altered['low']=107
            else:altered['close']=104
            with self.subTest(mode=mode):self.assertEqual(confirmed_overhead(b+[altered],asof,100)['price'],105)

    def test_new_peak_after_clearance_still_becomes_resistance(self):
        p,b=self.context()
        extra=candles([(100,108,100,106),(106,110,104,108),(108,109,105,107)],START+timedelta(minutes=3))
        for bar in extra:p.record('999990',bar,bar['time']+timedelta(minutes=1))
        self.assertEqual(p.snapshot('999990',SimpleNamespace(time=START+timedelta(minutes=6)),100)['nearest_confirmed_peak']['price'],110)


class TimingTests(unittest.TestCase):
    def test_first_discovery_is_not_latest_cycle_and_risk_eligibility_is_separate(self):
        e=SimpleNamespace();d=SimpleNamespace(observation_start={'999990':START+timedelta(seconds=10)})
        row={'first_watch_at':START.isoformat(),'observed_at':(START+timedelta(minutes=8)).isoformat(),
            'discovery_context':{'first_raw_at':START.isoformat()}}
        s={'symbol':'999990','time':(START+timedelta(minutes=9)).isoformat()}
        annotate_signal(e,d,row,s);self.assertEqual(s['candidate_detected_at'],START.isoformat())
        self.assertEqual(s['latest_radar_cycle_at'],row['observed_at'])
        self.assertNotIn('first_risk_eligible_at',s['opportunity_timing'])
        mark_risk_eligible(e,s,START+timedelta(minutes=9,seconds=1))
        later={'symbol':'999990','time':(START+timedelta(minutes=12)).isoformat()}
        annotate_signal(e,d,row,later)
        self.assertEqual(later['opportunity_timing']['first_detector_signal_at'],s['time'])
        self.assertEqual(later['opportunity_timing']['first_risk_eligible_at'],(START+timedelta(minutes=9,seconds=1)).isoformat())

    def test_unknown_and_future_discovery_is_never_filled_from_latest_cycle(self):
        for first in (None,(START+timedelta(minutes=20)).isoformat(),START.replace(tzinfo=None).isoformat()):
            s={'symbol':'999990','time':(START+timedelta(minutes=9)).isoformat()}
            annotate_signal(SimpleNamespace(),SimpleNamespace(),{'observed_at':START.isoformat(),'first_raw_at':first},s)
            self.assertIsNone(s['candidate_detected_at'])

    def test_watch_refresh_retains_first_transition_and_day_boundary_resets(self):
        self.assertEqual(watch_started({}, {'first_watch_at':START.isoformat()},START+timedelta(minutes=1)),START.isoformat())
        tomorrow=START+timedelta(days=1)
        self.assertEqual(watch_started({}, {'first_watch_at':START.isoformat()},tomorrow),tomorrow.isoformat())
        self.assertIsNone(watch_started({}, {},'unknown'))

    def test_timing_does_not_reuse_yesterday_and_is_bounded(self):
        e=SimpleNamespace();d=SimpleNamespace()
        for i in range(520):annotate_signal(e,d,{}, {'symbol':str(i),'time':START.isoformat()})
        self.assertEqual(len(e._opportunity_timing['symbols']),512)
        s={'symbol':'519','time':(START+timedelta(days=1)).isoformat()};annotate_signal(e,d,{},s)
        self.assertEqual(len(e._opportunity_timing['symbols']),1)
        self.assertEqual(s['opportunity_timing']['first_detector_signal_at'],s['time'])


class ProtectionComparisonTests(unittest.TestCase):
    def position(self):
        return {'avg':1000,'stop':980,'initial_stop':980,'initial_r':20,'high':1040,
            'opened':(START+timedelta(seconds=1)).isoformat(),
            'protection_bars':candles([(1018,1030,1015,1020),(1020,1025,1010,1018),(1018,1035,1015,1030)],START+timedelta(minutes=1))}

    def test_confirmed_pivot_gives_normal_pullback_more_room_than_MFE_lock(self):
        cfg=Config();auto=AutomationConfig();p=self.position();asof=START+timedelta(minutes=4)
        _,new,evidence=confirmed_protection(cfg,auto,p,1030,asof)
        _,core=protective_stop(cfg,p,1030,1015)
        from core.util import floor_tick
        old=max(core,floor_tick(1000+(1040-1000)*(1-auto.unrealized_giveback_fraction)))
        self.assertGreater(new,p['initial_stop']);self.assertLess(new,old)
        self.assertGreater(old,1023);self.assertLess(new,1023)
        self.assertEqual(evidence['reason'],'CONFIRMED_HIGHER_LOW_RAISED_STOP')

    def test_new_protection_never_widens_initial_stop_and_cost_floor_matters(self):
        p=self.position();cfg=Config();auto=AutomationConfig()
        p['high']=1010;self.assertEqual(confirmed_protection(cfg,auto,p,1010,START+timedelta(minutes=4))[1],980)
        p['high']=1040
        costly=replace(auto,sell_tax=.05)
        self.assertEqual(confirmed_protection(cfg,costly,p,1030,START+timedelta(minutes=4))[1],980)

    def test_partial_entry_minute_and_unconfirmed_pullback_do_not_raise_stop(self):
        p=self.position();p['protection_bars'][0]['time']=START
        self.assertEqual(confirmed_protection(Config(),AutomationConfig(),p,1030,START+timedelta(minutes=4))[1],980)

    def test_old_pivot_cannot_raise_stop_through_latest_data_gap(self):
        p=self.position()
        self.assertEqual(confirmed_protection(Config(),AutomationConfig(),p,1030,START+timedelta(minutes=8))[1],980)
        gap=candles([(1020,1040,1010,1030)],START+timedelta(minutes=7))[0];gap['gap_reason']='MISSING_MINUTE'
        p['protection_bars'].append(gap)
        self.assertEqual(confirmed_protection(Config(),AutomationConfig(),p,1030,START+timedelta(minutes=8))[1],980)
        p=self.position();p['protection_bars'][2]['time']+=timedelta(seconds=1)
        self.assertEqual(confirmed_protection(Config(),AutomationConfig(),p,1030,START+timedelta(minutes=4))[1],980)


class FakeOrderBoundaryTests(unittest.TestCase):
    setUp=entry_fixture.SetupAccelerationTests.setUp
    feed=entry_fixture.SetupAccelerationTests.feed
    trial=entry_fixture.SetupAccelerationTests.trial

    def test_forged_RR_target_cannot_ignore_known_overhead_at_order_boundary(self):
        s=self.feed('C');self.assertIsNotNone(s)
        self.e.a=replace(self.e.a,entry_context_enabled=True)
        s['evaluation_gates']['entry_context']={'result':'PASS','values':{'enabled':True,'asof':s['time'],
            'target':s['target'],'nearest_confirmed_peak':{'price':s['limit']+10,'confirmed_at':s['time']}}}
        self.assertIsNone(self.e.enter(s,self.t));self.assertEqual(self.b.orders,[])
        self.assertEqual(self.e.entry_market_reason(s,1,self.t),'ENTRY_CONTEXT_TARGET_ABOVE_CONFIRMED_RESISTANCE')

    def test_exit_trigger_is_preserved_after_later_health_update(self):
        s=self.feed('C');o=self.e.enter(s,self.t);self.assertEqual(o['status'],'ACCEPTED')
        self.e.cumulative_fill(o,o['qty'],o['qty']*s['limit'])
        p=self.e.s['positions'][entry_fixture.SYM]
        p['data_health']={'reason_code':'POSITION_DATA_UNVERIFIED_TIMEOUT','emergency_exit':True}
        self.e.request_exit(entry_fixture.SYM,'시세 지연/연결 단절')
        trigger=copy.deepcopy(p['exit_trigger_evidence'])
        p['data_health'].update(reason_code='TRADE_QUIET_QUOTE_FRESH',emergency_exit=False)
        self.e.request_exit(entry_fixture.SYM,'later cause')
        self.assertEqual(p['exit_trigger_evidence'],trigger)
        self.assertTrue(trigger['data_health']['emergency_exit'])
        self.assertFalse(p['data_health']['emergency_exit'])
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][entry_fixture.SYM]
        self.assertEqual(stored['exit_trigger_evidence'],trigger)
        trade={'symbol':entry_fixture.SYM,'entry':p['avg'],'exit':p['avg'],'qty':p['qty'],
            'reason':'시세 지연/연결 단절','pnl_estimate':-self.e.buy_fee(p['avg']*p['qty'])-self.e.sell_fee(p['avg']*p['qty'])}
        self.e.decorate_trade(trade,p)
        self.assertEqual(trade['exit_trigger_evidence'],trigger)

    def test_missing_resistance_confirmation_cannot_supply_order_proof(self):
        s=self.feed('C');self.e.a=replace(self.e.a,entry_context_enabled=True)
        for stamp in (None,'unknown',START.replace(tzinfo=None).isoformat(),(self.t.time+timedelta(seconds=1)).isoformat()):
            s['evaluation_gates']['entry_context']={'result':'PASS','values':{'enabled':True,'asof':s['time'],
                'target':s['target'],'nearest_confirmed_peak':{'price':s['target']+10,'confirmed_at':stamp}}}
            with self.subTest(stamp=stamp):self.assertIsNone(self.e.enter(s,self.t))
        self.assertEqual(self.b.orders,[])

    def test_zero_risk_quantity_never_marks_engine_eligibility(self):
        s=self.feed('C');annotate_signal(self.e,self.d,{},s)
        self.e.s['daily_loss']=self.e.daily_risk_budget()-1
        self.assertIsNone(self.e.enter(s,self.t));self.assertEqual(self.b.orders,[])
        self.assertNotIn('first_risk_eligible_at',s['opportunity_timing'])

    def test_real_detector_completed_reversal_can_reach_fake_order(self):
        self.auto=replace(self.auto,confirmed_box_direction=True);self.d.a=self.auto;self.e.a=self.auto
        self.feed('C')
        # Completed recovery inside lower endpoints; preserve the existing box,
        # breakout, chase, economic and risk checks at the real detector/engine.
        rows=[(10050,10070,10030,10040),(10040,10050,9940,9970),
              (9970,9990,9930,9960),(9960,9980,9940,9970),(9970,9990,9950,9990)]
        start=self.t.time.replace(second=0,microsecond=0)-timedelta(minutes=5)
        from collections import deque
        self.d.history[entry_fixture.SYM]=deque(candles(rows,start),maxlen=30)
        self.t=replace(self.t,price=10080,ask=10080,bid=10070)
        self.d.last[entry_fixture.SYM]=self.t
        sig=self.d.signal(self.t,self.watch)
        self.assertIsNotNone(sig,repr(self.trial('C')));self.assertEqual(sig['pattern'],'C')
        self.assertEqual(self.trial('C')['gates']['box_direction']['values']['reason'],'CONFIRMED_REVERSAL_BASE')
        self.assertEqual(self.e.enter(sig,self.t)['status'],'ACCEPTED');self.assertEqual(len(self.b.orders),1)


class CurrentBarIntegrationTests(unittest.TestCase):
    setUp=quality_fixture.StructureProtectionTests.setUp
    sell_count=quality_fixture.StructureProtectionTests.sell_count
    tick=quality_fixture.StructureProtectionTests.tick
    pivot=quality_fixture.StructureProtectionTests.pivot

    def enable(self):
        quality_fixture.StructureProtectionTests.enable(self)


    test_current_completed_pivot_and_stop_persist=quality_fixture.StructureProtectionTests.test_completed_bars_and_raised_stop_persist_in_existing_journal
    test_current_completed_pivot_survives_mock_restart=quality_fixture.StructureProtectionTests.test_restart_with_same_approved_settings_preserves_structural_stop
    test_runner_current_completed_pivot_reaches_protection=quality_fixture.StructureProtectionTests.test_real_runner_delivers_completed_bars_before_tick_protection


class CurrentReferenceIntegrationTests(unittest.TestCase):
    setUp=context_fixture.ContextOrderTests.setUp
    feed=context_fixture.ContextOrderTests.feed
    trial=context_fixture.ContextOrderTests.trial

    def test_minute_aligned_context_blocks_real_patterns_before_order(self):
        from automation.patterns import PatternDetector
        self.auto=replace(self.auto,entry_context_enabled=True);self.d.a=self.auto
        for name in ('B','C'):
            self.d=PatternDetector(self.cfg,self.auto,self.e.costs);sig=self.feed(name)
            self.assertIsNotNone(sig);entry=sig['limit']
            stamp=self.t.time.replace(second=0,microsecond=0)-timedelta(minutes=3)
            self.d.price_context=PriceContext()
            for i,h in enumerate((entry,entry+20,entry+10)):
                b=candles([(entry-100,h,entry-100,entry-50)],stamp+timedelta(minutes=i))[0]
                self.assertTrue(self.d.price_context.record(self.t.symbol,b,self.t.time))
            self.assertIsNone(self.d.signal(self.t,self.watch));self.assertEqual(self.b.orders,[])
            self.assertIn('ENTRY_CONTEXT_FAIL',self.trial(name)['failed_reasons'])


class FrozenReplayBoundaries(unittest.TestCase):
    def test_future_previous_day_and_naive_discovery_are_unknown_in_replay(self):
        from tools.trade_quality_replay import discovery_times
        asof=START+timedelta(minutes=9)
        for invalid in ((START+timedelta(minutes=20)).isoformat(),
                (START-timedelta(days=1)).isoformat(),START.replace(tzinfo=None).isoformat(),'unknown'):
            signal={'symbol':'999990','discovery_context':{'first_raw_at':invalid}}
            earliest={'999990:raw':{'time':invalid},'999990:WATCH_TRANSITION':{'time':invalid}}
            with self.subTest(invalid=invalid):self.assertEqual(discovery_times(earliest,signal,asof),(None,None,None))
        signal['discovery_context']['first_raw_at']=START.isoformat()
        self.assertEqual(discovery_times(earliest,signal,asof),(START.isoformat(),None,START.isoformat()))

    def test_later_snapshot_cannot_clear_an_earlier_entry_resistance(self):
        from tools.trade_quality_replay import contextual_peak
        first={'symbol':'999990','time':(START+timedelta(minutes=3)).isoformat(),'limit':100,
            'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in candles([(99,101,98,100),(100,105,99,101),(101,102,99,100)])]}
        later={'symbol':'999990','time':(START+timedelta(minutes=5)).isoformat(),'limit':106,
            'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in candles([(100,108,100,106)],START+timedelta(minutes=3))]}
        peak,_=contextual_peak(first,[first,later],{})
        self.assertEqual(peak['price'],105)

    def test_frozen_reference_can_retire_only_when_clearance_was_known(self):
        from tools.trade_quality_replay import contextual_peak
        sig={'symbol':'999990','time':(START+timedelta(minutes=4)).isoformat(),'limit':100,
            'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in candles([(100,108,100,106)],START+timedelta(minutes=3))]}
        ref={'nearest_confirmed_peak':{'price':105,'time':(START+timedelta(minutes=1)).isoformat(),
            'confirmed_at':(START+timedelta(minutes=3)).isoformat()}}
        peak,coverage=contextual_peak(sig,[sig],ref)
        self.assertIsNone(peak);self.assertEqual(coverage['cleared_peak_count'],1)


class StandingBookBoundaries(unittest.TestCase):
    setUp=book_fixture.StandingBookTests.setUp
    advance=book_fixture.StandingBookTests.advance
    sell_count=book_fixture.StandingBookTests.sell_count
    prepare=book_fixture.StandingBookTests.prepare

    def test_old_receipt_cannot_be_reused_forever(self):
        self.prepare();self.assertTrue(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        original=self.book['time'];self.advance(64)
        self.assertFalse(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        self.assertEqual(self.h.quotes[book_fixture.SYM]['time'],original)

    def test_confirmed_book_does_not_override_real_disconnect_or_subscription_loss(self):
        self.prepare();self.assertTrue(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        self.e.connected=False
        self.assertTrue(self.h.should_exit(book_fixture.SYM,self.e.s['positions'][book_fixture.SYM],self.clock))
        self.e.connected=True;self.h.subscriptions.symbols.clear()
        self.assertFalse(self.h.confirm_unchanged_book(book_fixture.SYM,self.book,self.clock))

    def test_delayed_REST_response_cannot_move_confirmation_backwards(self):
        self.prepare();self.assertTrue(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        saved=self.h.quotes[book_fixture.SYM]['snapshot_confirmed_at']
        older={**self.book,'received_at':(self.clock-timedelta(seconds=10)).isoformat()}
        self.assertFalse(self.h.accept_book(book_fixture.SYM,older,self.clock,'ka10004'))
        self.assertEqual(self.h.quotes[book_fixture.SYM]['snapshot_confirmed_at'],saved)

    def test_conflicting_newer_stale_quote_invalidates_confirmation_without_immediate_sell(self):
        self.prepare();self.assertTrue(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        self.advance(40)
        self.feed.orderbooks[book_fixture.SYM]={**self.feed.orderbooks[book_fixture.SYM],
            'time':(book_fixture.BASE+timedelta(seconds=10)).isoformat(),'best_bid':7000,'best_ask':7010}
        p=self.e.s['positions'][book_fixture.SYM]
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        self.assertTrue(p['data_health']['standing_confirmation_invalidated'])
        self.assertEqual(p['data_health']['reason_code'],'POSITION_QUOTE_RECHECK_PENDING')
        self.assertNotIn('snapshot_confirmed_at',self.h.quotes[book_fixture.SYM])
        self.assertEqual(self.sell_count(),0)

    def test_reconstructed_health_keeps_existing_unverified_deadline(self):
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        first=p['data_health']['recheck_started_at']
        account=self.h.account;subs=self.h.subscriptions
        self.advance(44)
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        fresh=type(self.h)(self.e,self.feed,account,subs)
        self.advance(48)
        self.assertTrue(fresh.should_exit(book_fixture.SYM,p,self.clock))
        self.assertEqual(p['data_health']['recheck_started_at'],first)
        self.assertGreaterEqual(p['data_health']['recheck_seconds'],15)

    def test_probe_records_market_receipt_and_processing_times_separately(self):
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.h.should_exit(book_fixture.SYM,p,self.clock)
        self.h.probe_results({'books':{book_fixture.SYM:self.book}},self.clock)
        probe=self.h.probes[book_fixture.SYM]
        self.assertEqual(probe['quote_time'],self.book['time'])
        self.assertEqual(probe['received_at'],self.book['received_at'])
        self.assertEqual(probe['result_at'],self.clock.isoformat())
        self.assertEqual(probe['ask_qty'],self.book['ask_qty'])

    def test_unverified_deadline_uses_saved_journal_after_health_recreation(self):
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][book_fixture.SYM]
        original=stored['data_health']['recheck_started_at']
        self.advance(48)
        fresh=type(self.h)(self.e,self.feed,self.h.account,self.h.subscriptions)
        self.assertTrue(fresh.should_exit(book_fixture.SYM,stored,self.clock))
        self.assertEqual(stored['data_health']['recheck_started_at'],original)

    def test_resolved_interval_is_saved_and_cannot_revive_on_reconstruction(self):
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        old_start=p['data_health']['recheck_started_at']
        self.advance(44)
        self.feed.orderbooks[book_fixture.SYM]={**self.book,'time':self.clock.isoformat()}
        self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][book_fixture.SYM]
        self.assertEqual(stored['data_health']['reason_code'],'TRADE_QUIET_QUOTE_FRESH')
        self.assertNotIn('recheck_started_at',stored['data_health'])
        # Simulate no verifiable receiver book in a new health object. A new
        # interval starts now, rather than reviving the resolved old interval.
        self.feed.orderbooks.clear();self.advance(48)
        self.e.s['positions'][book_fixture.SYM]=stored
        fresh=type(self.h)(self.e,self.feed,self.h.account,self.h.subscriptions)
        self.assertFalse(fresh.should_exit(book_fixture.SYM,stored,self.clock))
        self.assertNotEqual(stored['data_health']['recheck_started_at'],old_start)
        self.assertEqual(stored['data_health']['recheck_seconds'],0)

    def test_REST_confirmed_resolution_is_saved_once_without_timer_write_spam(self):
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.h.should_exit(book_fixture.SYM,p,self.clock)
        self.assertTrue(self.h.accept_book(book_fixture.SYM,self.book,self.clock,'ka10004'))
        with unittest.mock.patch.object(self.e,'save',wraps=self.e.save) as save:
            self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
            self.assertFalse(self.h.should_exit(book_fixture.SYM,p,self.clock))
            self.assertEqual(save.call_count,1)
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][book_fixture.SYM]
        self.assertEqual(stored['data_health']['reason_code'],'TRADE_QUIET_REST_BOOK_CONFIRMED')
        self.assertNotIn('recheck_started_at',stored['data_health'])

    def test_fresh_trade_through_runner_resolves_old_deadline_before_next_quiet_period(self):
        from unittest.mock import patch
        from automation.runner import process_message
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.h.should_exit(book_fixture.SYM,p,self.clock)
        old_start=p['data_health']['recheck_started_at'];self.advance(35)
        detector=Mock();detector.completed={};detector.ingest.return_value=False
        packet={'trnm':'REAL','data':[{'type':'0B','item':book_fixture.SYM,'values':{
            '20':self.clock.strftime('%H%M%S'),'10':'7240','15':'0','13':'100','27':'7250','28':'7240','290':'2'}}]}
        with patch('automation.runner.now',return_value=self.clock):
            process_message(self.e,detector,packet,self.clock,{book_fixture.SYM},{},Mock(),self.e.prices,self.account,True,self.sub)
        stored=json.loads(self.e.path.read_text(encoding='utf-8'))['positions'][book_fixture.SYM]
        self.assertEqual(stored['data_health']['reason_code'],'POSITION_TRADE_FRESH')
        self.assertNotIn('recheck_started_at',stored['data_health'])
        self.assertNotIn(book_fixture.SYM,self.h.waiting);self.assertEqual(self.sell_count(),0)
        self.feed.orderbooks.clear();self.advance(51)
        self.e.s['positions'][book_fixture.SYM]=stored
        fresh=type(self.h)(self.e,self.feed,self.account,self.sub)
        self.assertFalse(fresh.should_exit(book_fixture.SYM,stored,self.clock))
        self.assertNotEqual(stored['data_health']['recheck_started_at'],old_start)
        self.assertEqual(stored['data_health']['recheck_seconds'],0)

    def test_stale_or_nonregular_trade_cannot_resolve_pending_interval(self):
        from core.strategy import Tick
        self.prepare();p=self.e.s['positions'][book_fixture.SYM]
        self.h.should_exit(book_fixture.SYM,p,self.clock)
        old=copy.deepcopy(p['data_health'])
        tick=Tick(self.clock,book_fixture.SYM,7240,1,7250,7240,True)
        for invalid in (replace(tick,time=book_fixture.BASE),replace(tick,regular=False),replace(tick,price=0),
                replace(tick,price=float('nan')),replace(tick,price=True),replace(tick,time=self.clock+timedelta(seconds=1))):
            with self.subTest(tick=invalid):
                self.h.trade_seen(book_fixture.SYM,invalid)
                self.assertEqual(p['data_health'],old);self.assertIn(book_fixture.SYM,self.h.waiting)


if __name__=='__main__':unittest.main()
