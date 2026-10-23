"""Bounded recorder and final FAST order boundary; no network or actual orders."""
import contextlib
from dataclasses import replace
from datetime import datetime,timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation.trade_review_capture import TradeReviewCapture
from automation.engine import AutonomousEngine
from automation.fast_momentum import FastSignal
from automation.policy import AutomationConfig,Policy
from automation.profit_review import compare_trade,summarize,POLICIES
from core.realtime import Feed
from core.settings import Config,KST
from core.strategy import Tick
from tests.test_automation import FakeBroker

AT=datetime(2026,10,16,12,50,47,tzinfo=KST)

def tick(symbol='033160',at=AT,price=16910):
    return Tick(symbol,at,price,10,1000,price,price-10,True,
        ask_qty=100,bid_qty=100,depth_available=True,depth_source='0D',book_time=at)

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def capture(self,**kw):
        kw.setdefault('continuous_watch',False);kw.setdefault('min_free_bytes',0)
        c=TradeReviewCapture(self.root,start=False,clock=lambda:AT,**kw)
        self.addCleanup(c.close);return c

    def rows(self):
        return [json.loads(line) for p in sorted(self.root.glob('trade-review-*.jsonl'))
                for line in p.read_text(encoding='utf-8').splitlines()]

    def test_watch_tape_is_memory_only_until_real_candidate(self):
        c=self.capture();c.set_scope(['033160'],{},AT)
        c.tick(tick(),AT);c.book('033160',{'time':AT.isoformat(),'best_bid':16900},AT)
        c.flush();self.assertFalse([r for r in self.rows() if r['event']=='TRADE'])
        c.promote('033160',AT);c.flush()
        trade=[r for r in self.rows() if r['event']=='TRADE'][0]
        self.assertEqual(trade['exchange_at'],AT.isoformat())
        self.assertEqual(trade['received_at'],AT.isoformat())
        self.assertIn('BOUNDED_PARTIAL',next(r for r in self.rows() if r['event']=='PREENTRY_BUFFER')['coverage'])

    def test_near_candidate_rejection_retains_bounded_actual_tape_without_order(self):
        c=self.capture();c.set_scope(['033160'],{},AT)
        c.tick(tick(),AT);c.book('033160',{'time':AT.isoformat(),'best_bid':16900},AT)
        evaluation={'gates':{},'trials':[{'setup':'A','candidate_generated':True,'entry_ready':False,
            'failed_reasons':['RISK_REWARD_FAIL'],
            'gates':{'warmup':{'result':'PASS'},'risk_reward':{'result':'FAIL','values':{'calculated_rr':1.1}}}}]}
        c.evaluation(tick(),{'status':'WATCH'},evaluation,None,AT);c.flush()
        rows=self.rows()
        self.assertTrue(any(r['event']=='TRADE' for r in rows))
        self.assertTrue(any(r['event']=='BOOK' for r in rows))
        marker=next(r for r in rows if r['event']=='REJECTED_CANDIDATE_BUFFER')
        self.assertFalse(marker['actual_order_permission'])
        self.assertIn('BOUNDED_PARTIAL',marker['coverage'])
        self.assertEqual(c.promoted,{})

    def test_rejected_buffer_sampling_is_bounded_and_never_grants_order_permission(self):
        c=self.capture();c.set_scope(['033160'],{},AT)
        evaluation={'gates':{},'trials':[{'setup':'B','candidate_generated':True,'entry_ready':False,
            'gates':{'risk_reward':{'result':'FAIL'}}}]}
        for _ in range(100):
            c.tick(tick(),AT);c.evaluation(tick(),{'status':'WATCH'},evaluation,None,AT)
        c.flush()
        markers=[r for r in self.rows() if r['event']=='REJECTED_CANDIDATE_BUFFER']
        self.assertEqual(len(markers),1)
        self.assertLessEqual(c.stats()['rejected_buffer_bytes'],512*1024)
        self.assertFalse(c.stats()['orders_enabled'])

    def test_continuous_watch_retains_trade_and_book_without_signal(self):
        c=self.capture(continuous_watch=True);c.set_scope(['033160'],{},AT)
        c.tick(tick(),AT,AT+timedelta(milliseconds=20))
        c.book('033160',{'time':AT.isoformat(),'best_bid':16900},AT)
        c.flush();rows=self.rows()
        trade=next(r for r in rows if r['event']=='TRADE')
        self.assertEqual(trade['processed_at'],(AT+timedelta(milliseconds=20)).isoformat())
        self.assertEqual(trade['capture_point'],'STRATEGY_INGESTION')
        self.assertTrue(any(r['event']=='BOOK' for r in rows))
        self.assertEqual(c.promoted,{})

    def test_continuous_watch_reserves_queue_capacity_for_holdings(self):
        c=self.capture(continuous_watch=True,queue_rows=12,queue_bytes=100000)
        c.set_scope(['033160'],{'999990':{}},AT)
        for _ in range(30):c.tick(tick(),AT)
        c.tick(tick('999990'),AT);c.flush()
        self.assertTrue(any(r.get('symbol')=='999990' and r['event']=='TRADE' for r in self.rows()))
        self.assertGreater(c.stats()['capture_losses'],0)

    def test_disk_reserve_blocks_recording_only_and_preserves_existing_data(self):
        c=self.capture(continuous_watch=True,min_free_bytes=1000)
        c.flush();before={p.name:p.read_bytes() for p in self.root.glob('trade-review-*.jsonl')}
        c.set_scope(['033160'],{},AT);c.tick(tick(),AT)
        with patch('automation.trade_review_capture.shutil.disk_usage',return_value=SimpleNamespace(free=1000)):
            self.assertFalse(c.flush())
        self.assertEqual(c.stats()['storage_block_reason'],'CAPTURE_FREE_SPACE_RESERVE')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.glob('trade-review-*.jsonl')})

    def test_reference_capture_retains_available_prefix_without_future_or_warmup_claims(self):
        c=self.capture()
        bar={'time':AT.replace(second=0)-timedelta(minutes=1),'open':100,'high':102,'low':99,'close':101,'volume':50}
        future={**bar,'time':AT.replace(second=0)}
        c.reference_snapshot('033160',[bar,future],AT-timedelta(seconds=1),AT,'KRX');c.flush()
        record=next(r for r in self.rows() if r['event']=='REFERENCE_BARS_PART')
        self.assertEqual(len(record['rows']),1)
        self.assertEqual(record['known_at'],AT.isoformat())
        self.assertEqual(record['restored_signal_bars'],0)
        self.assertEqual(record['bar_label'],'MINUTE_START')

    def test_holding_keeps_receiver_capture_after_watch_and_shadow_removal(self):
        c=self.capture();p={'opened':AT.isoformat(),'avg':16910,'qty':4,'initial_stop':16840,
            'stop':16840,'emergency':16830,'protection_stop':17070}
        c.set_scope([],{'033160':p},AT)
        feed=Feed(SimpleNamespace(cfg=Config()),['033160'])
        feed.trade_review=c;feed.configure_observation([],[],None)
        frame={'trnm':'REAL','data':[{'type':'0B','item':'033160','values':{
            '20':'125047','10':'16910','15':'10','13':'1000','27':'16910','28':'16900','290':'2'}}]}
        with patch('core.realtime.now',return_value=AT):feed.emit(frame)
        self.assertEqual(feed.q.qsize(),0)  # receiver capture precedes queue routing
        c.flush();self.assertEqual(len([r for r in self.rows() if r['event']=='TRADE']),1)
        line=next(r for r in self.rows() if r['event']=='POSITION_LINES')
        self.assertEqual((line['stop'],line['emergency'],line['protection_stop']),(16840,16830,17070))

    def test_memory_limits_and_drop_evidence(self):
        c=self.capture(ring_rows=4,ring_bytes=1800,queue_rows=3,queue_bytes=1800)
        c.set_scope(['033160'],{},AT)
        for _ in range(100):c.tick(tick(),AT)
        self.assertLessEqual(c.stats()['ring_rows'],4)
        self.assertLessEqual(c.stats()['ring_bytes'],1800)
        c.promote('033160',AT)
        for _ in range(100):c.tick(tick(),AT)
        stats=c.stats();self.assertLessEqual(stats['queue_rows'],3)
        self.assertLessEqual(stats['queue_bytes'],1800);self.assertGreater(stats['capture_losses'],0)

    def test_disk_cap_never_deletes_any_file(self):
        unrelated=self.root/'old-evidence.jsonl';unrelated.write_text('preserve',encoding='utf-8')
        c=self.capture(file_bytes=1000,day_bytes=1100,total_bytes=1100)
        c.set_scope([],{'033160':{'opened':AT.isoformat(),'avg':16910,'qty':4}},AT)
        for _ in range(30):c.tick(tick(),AT);c.flush()
        self.assertLessEqual(sum(p.stat().st_size for p in self.root.glob('trade-review-*.jsonl')),1100)
        self.assertTrue(c.stats()['storage_blocked']);self.assertGreater(c.losses,0)
        self.assertEqual(unrelated.read_text(encoding='utf-8'),'preserve')

    def test_completed_bar_contains_real_confirmation_and_gap_status(self):
        c=self.capture();c.set_scope(['033160'],{},AT)
        bar={'time':AT.replace(second=0)-timedelta(minutes=1),'open':16800,'high':16900,
             'low':16790,'close':16890,'volume':100,'gap_reason':'MISSING_TICKS'}
        c.completed_bar('033160',bar,AT,AT+timedelta(seconds=1))
        c.completed_bar('033160',bar,AT,AT+timedelta(seconds=1))
        c.promote('033160',AT);c.flush()
        rows=[r for r in self.rows() if r['event']=='COMPLETED_BAR'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['bar']['gap_reason'],'MISSING_TICKS')
        self.assertFalse(rows[0]['reference_bars_used']);self.assertEqual(rows[0]['confirmed_at'],AT.isoformat())

    def test_gap_order_and_fee_records_refer_to_authoritative_log(self):
        c=self.capture();c.stream('DISCONNECTED',AT)
        c.event_ref('BROKER_PNL_RECONCILED',{'pnl':444,'fee':156},AT,{})
        c.flush();rows=self.rows()
        self.assertTrue(any(r['event']=='STREAM_STATE' for r in rows))
        ref=next(r for r in rows if r['event']=='EVENT_REF')
        self.assertNotIn('pnl',ref);self.assertNotIn('fee',ref);self.assertFalse(c.stats()['orders_enabled'])

    def test_file_lock_keeps_bounded_retry_buffer(self):
        c=self.capture();real_open=Path.open
        def denied(path,*a,**kw):
            if a and a[0] in ('ab','r+b'):raise PermissionError('locked')
            return real_open(path,*a,**kw)
        with patch.object(Path,'open',denied):self.assertFalse(c.flush())
        self.assertGreater(c.stats()['write_errors'],0);self.assertGreater(c.stats()['queue_rows'],0)
        self.assertTrue(c.flush());self.assertTrue(self.rows())

    def test_restart_creates_new_capture_session_without_restoring_prices(self):
        c=self.capture();c.flush();first=c.session
        other=TradeReviewCapture(self.root,start=False,clock=lambda:AT);self.addCleanup(other.close)
        other.flush();self.assertNotEqual(first,other.session)
        self.assertEqual(other.rings,{})
        self.assertEqual(len([r for r in self.rows() if r['event']=='CAPTURE_STARTED']),2)

    def test_post_exit_tail_is_bounded_and_marked_as_observation(self):
        c=self.capture();c.set_scope([],{'033160':{'opened':AT.isoformat(),'avg':16910,'qty':4}},AT)
        c.set_scope([],{},AT);c.tick(tick(),AT);c.flush()
        self.assertTrue(any(r['event']=='TRADE' for r in self.rows()))
        self.assertLessEqual(c.promoted['033160']-__import__('time').monotonic(),120.)
        with patch('automation.trade_review_capture.time.monotonic',return_value=c.promoted['033160']+1):
            before=c.stats()['queue_rows'];c.tick(tick(),AT)
        self.assertEqual(c.stats()['queue_rows'],before)

class FastBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=contextlib.redirect_stdout(io.StringIO());self.output.__enter__()
        self.addCleanup(self.output.__exit__,None,None,None)
        cfg=replace(Config(),live_mode=True)
        auto=replace(AutomationConfig(),fast_momentum_enabled=True)
        root=Path(self.temp.name);policy=Policy(root/'policy.json')
        self.e=AutonomousEngine(cfg,auto,policy,root/'book',FakeBroker(),lambda:AT)
        self.t=tick();self.book={'time':AT.isoformat(),'received_at':AT.isoformat(),
            'best_bid':16900,'best_ask':16910,'bid_qty':4,'ask_qty':4,'depth_available':True}
        self.e.entry_quote_provider=lambda symbol:(self.t,self.book)
        self.s={'symbol':'033160','pattern':'FAST','time':AT.isoformat(),
            'signal_detected_at':AT.isoformat(),'ttl_seconds':15.,'max_chase_price':16920,
            'limit':16910,'stop':16840,'target':17350}

    def reason(self):return self.e.entry_market_reason(self.s,4,phase='PRE_HTTP')

    def test_regular_fast_requires_current_executable_quantity(self):
        self.assertIsNone(self.reason())
        self.book['bid_qty']=3
        self.assertEqual(self.reason(),'ENTRY_DEPTH_BELOW_ACTUAL_QUANTITY')

    def test_regular_fast_rejects_mismatched_or_stale_books(self):
        self.book['best_ask']=16920
        self.assertEqual(self.reason(),'ENTRY_QUOTE_ABOVE_LIMIT')
        self.book['best_ask']=16910;self.book['time']=(AT-timedelta(seconds=6)).isoformat()
        self.book['received_at']=self.book['time']
        self.assertEqual(self.reason(),'ENTRY_DEPTH_UNAVAILABLE')

    def test_regular_fast_rejects_absent_or_invalid_depth(self):
        self.book={};self.t=replace(self.t,depth_available=False)
        self.assertEqual(self.reason(),'ENTRY_DEPTH_UNAVAILABLE')
        self.t=tick();self.book={'time':AT.isoformat(),'best_bid':16900,'best_ask':16910,
            'depth_available':True,'bid_qty':float('nan'),'ask_qty':100}
        self.assertEqual(self.reason(),'ENTRY_DEPTH_UNAVAILABLE')

    def test_own_ttl_and_chase_are_rechecked_at_pre_http(self):
        self.s['signal_detected_at']=(AT-timedelta(seconds=16)).isoformat()
        self.assertEqual(self.reason(),'FAST_SIGNAL_TTL_EXPIRED')
        self.s['signal_detected_at']=AT.isoformat();self.s['max_chase_price']=16900
        self.assertEqual(self.reason(),'FAST_MAX_CHASE_EXCEEDED')

    def test_no_new_rule_is_applied_to_legacy_regular_abc(self):
        self.s['pattern']='A';self.book={};self.t=replace(self.t,depth_available=False)
        self.assertIsNone(self.reason())

    def test_saved_quotes_and_synthetic_positive_depth_controls_stay_eligible(self):
        # Saved PRE_HTTP quote numbers. These are boundary checks, not full replays.
        for symbol,price,bid,bidq,askq,qty in (
            ('092870',48200,48150,586,896,1),('293490',11000,10990,14,688,6),
            ('452280',17180,17160,1920,690,4),('033160',16910,16900,41,7,4)):
            with self.subTest(symbol=symbol):
                self.t=tick(symbol,price=price)
                self.t=replace(self.t,bid=bid)
                self.book={'time':AT.isoformat(),'received_at':AT.isoformat(),
                    'best_bid':bid,'best_ask':price,'bid_qty':bidq,'ask_qty':askq,'depth_available':True}
                self.s.update(symbol=symbol,limit=price,stop=price-100,target=price+1000,max_chase_price=price*1.003)
                self.assertIsNone(self.e.entry_market_reason(self.s,qty,phase='PRE_HTTP'))

    def test_signal_preserves_actual_liquidity_and_structure_evidence(self):
        sig=FastSignal('033160','MK',16910,16840,17350,16830,4,2.,3.,200.,100.,'A','REGULAR',AT,
            extra={'metrics':{'turnover_10':6000000,'latest_time':AT},'geometry':{'structural_evidence':{'support':16860}}})
        out=sig.to_engine_signal()
        self.assertEqual(out['evaluation_gates']['liquidity']['values']['turnover'],6000000)
        self.assertEqual(out['fast_entry_evidence']['geometry']['structural_evidence']['support'],16860)
        json.dumps(out,allow_nan=False)  # saved INTENT must remain serializable
        out['fast_entry_evidence']['metrics']['turnover_10']=1
        self.assertEqual(sig.extra['metrics']['turnover_10'],6000000)

class ProfitReviewTests(unittest.TestCase):
    def setUp(self):
        self.trade={'trade_id':'test','symbol':'033160','opened':AT.isoformat(),'entry':16910,
            'qty':4,'initial_stop':16840,'pnl_estimate':444,'net_pnl':444,'pnl_source':'BROKER_QUERY',
            'signal':{'emergency':16830,'invalidation_policy':{'emergency_stop':16830}}}
        self.cfg=Config();self.auto=AutomationConfig()

    def row(self,kind,seconds,**values):
        stamp=(AT+timedelta(seconds=seconds)).isoformat()
        return {'event':kind,'symbol':'033160','received_at':stamp,'exchange_at':stamp,
            'capture_session':'test','capture_losses':0,**values}

    def test_missing_real_capture_has_no_invented_profit(self):
        result=compare_trade(self.cfg,self.auto,self.trade,[])
        self.assertTrue(all(r['status']=='UNKNOWN' for r in result['policies'].values()))
        self.assertEqual(result['actual_broker_net_pnl'],444)
        self.assertFalse(result['profitability_proven'])

    def test_bars_missing_never_becomes_successful_hold(self):
        rows=[self.row('TRADE',-.1,price=16910,volume=1,cumulative=1,bid=16900,ask=16910,regular=True),
              self.row('BOOK',1,best_bid=16900,best_ask=16910,bid_qty=100,depth_available=True)]
        result=compare_trade(self.cfg,self.auto,self.trade,rows)
        for n in POLICIES[1:]:
            self.assertEqual(result['policies'][n]['status'],'UNKNOWN')
            self.assertEqual(result['policies'][n]['reason'],'COMPLETE_POST_FILL_BARS_UNAVAILABLE')

    def test_actual_emergency_is_not_delayed_for_missing_bars(self):
        rows=[self.row('TRADE',-.1,price=16910,volume=1,cumulative=1,bid=16900,ask=16910,regular=True),
              self.row('BOOK',.1,best_bid=16820,best_ask=16830,bid_qty=100,depth_available=True),
              self.row('BOOK',.8,best_bid=16810,best_ask=16820,bid_qty=100,depth_available=True)]
        result=compare_trade(self.cfg,self.auto,self.trade,rows)
        for n in POLICIES[1:]:
            r=result['policies'][n]
            self.assertEqual(r['status'],'MODEL_COMPONENT_EXIT');self.assertEqual(r['reason'],'INITIAL_EMERGENCY')
            self.assertIsNone(r['actual_fill']);self.assertLess(r['modeled_net_pnl'],0)

    def test_queue_drop_invalidates_counterfactual_expectancy(self):
        result=compare_trade(self.cfg,self.auto,self.trade,[self.row('TRADE',0,capture_losses=1)])
        self.assertTrue(all(r['status']=='UNKNOWN' for r in result['policies'].values()))
        stats=summarize([result]);self.assertEqual(stats['common_model_cohort'],0)
        self.assertIsNone(stats['actual_expectancy'])

    def test_no_later_depth_cannot_be_called_a_fill(self):
        rows=[self.row('TRADE',-.1,price=16910,volume=1,cumulative=1,bid=16900,ask=16910,regular=True),
              self.row('BOOK',.1,best_bid=16820,best_ask=16830,bid_qty=100,depth_available=True)]
        result=compare_trade(self.cfg,self.auto,self.trade,rows,delay_seconds=1.)
        for n in POLICIES[1:]:
            self.assertEqual(result['policies'][n]['reason'],'POST_DELAY_EXECUTABLE_BID_UNAVAILABLE')
            self.assertNotIn('modeled_net_pnl',result['policies'][n])

class CapturedFastPipelineTests(unittest.TestCase):
    def test_real_fast_path_preserves_signal_time_and_basis_without_extra_order(self):
        from tests.test_r6_fast_live_pipeline import FastPipelineProductionLikeFlagsTests,SYM
        from automation.entry_timing import ObservationTimeline
        case=FastPipelineProductionLikeFlagsTests(methodName='runTest')
        case.setUp();self.addCleanup(case.doCleanups)
        capture=TradeReviewCapture(case.root/'capture',start=False,clock=lambda:case.clock[0])
        self.addCleanup(capture.close)
        case.e.trade_review=capture
        case.e.observation_timeline=ObservationTimeline(case.root/'timeline.json')
        capture.set_scope(case.watch,{},case.clock[0]);case.series();capture.flush()
        self.assertEqual(len(case.orders('BUY')),1)
        timeline=case.e.observation_timeline.rows[SYM]
        self.assertLessEqual(timeline['first_valid_signal_at'],timeline['first_risk_eligible_at'])
        rows=[json.loads(line) for p in (case.root/'capture').glob('trade-review-*.jsonl')
              for line in p.read_text(encoding='utf-8').splitlines()]
        basis=next(r for r in rows if r['event']=='ENTRY_BASIS')
        self.assertGreater(basis['fast_entry_evidence']['metrics']['turnover_10'],0)
        self.assertIsNotNone(basis['fast_entry_evidence']['geometry']['structural_evidence']['support_at'])
        self.assertTrue(basis['fast_context_observation']['diagnostic_only'])
        self.assertFalse(basis['fast_context_observation']['actual_order_permission'])
        self.assertIn('fast_reference_bar_snapshot',basis)

