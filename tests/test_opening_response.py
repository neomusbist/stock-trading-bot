"""Opening discovery, real receiver and failure counterexamples; mock API only."""
import copy,json,queue,tempfile,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from automation.radar import DynamicRadar,RadarWorker
from automation.runner import apply_qualified_progress,process_message,SubscriptionState
from automation.entry_timing import ObservationTimeline
from automation.patterns import PatternDetector
from automation.session import control_supported,supported
from automation.opening_failure import observe_opening_failure
from automation.exit_candidates import observe_candidates
from automation.policy import AutomationConfig
from core.settings import Config,KST
from core.strategy import Tick
from tests import test_subscription_refresh as wire
from tests import test_structural_pullback as pullback
from tests import test_pipeline as pipeline

OPEN=datetime(2026,10,14,9,0,tzinfo=KST)
SYM=wire.SYM;OTHER=wire.OTHER


def row(sym=SYM,status='ELIGIBLE',score=40,stamp=OPEN):
    return dict(code=sym,name='Synthetic '+sym,status=status,source_count=2,reference_price=10000,
        discovery_score=score,source_details={s:{'observed_rank':1,'values':{}} for s in ('one','two')},
        market_status={'pass':True,'checked_at':stamp.isoformat()},
        affordability={'entry_eligible':True},first_raw_at=stamp.isoformat(),observed_at=stamp.isoformat())


class OpeningProgressTests(unittest.TestCase):
    setUp=wire.SubscriptionRefreshTests.setUp

    def prepare(self):
        self.clock=OPEN;self.cfg=replace(self.cfg,max_watchlist=1)
        self.e.observation_timeline=ObservationTimeline()
        old=row(OTHER,'WATCH',30);lookup={OTHER:old}
        return lookup,[old],dict(phase='QUALIFIED_PROGRESS',source_observed_at=OPEN.isoformat(),
            rows=[row()],raw_rows=[row(OTHER,'RAW',30),row(SYM,'RAW')],time=OPEN.isoformat())

    def apply(self,result,lookup,rows,stamp=OPEN,readiness=None):
        return apply_qualified_progress(self.e,lookup,rows,result,readiness or {},self.cfg,self.auto,stamp)

    def test_verified_candidate_reaches_existing_focus_before_unrelated_queries_finish(self):
        lookup,rows,packet=self.prepare();radar=DynamicRadar(self.cfg,self.auto,self.policy)
        lookup[OTHER]['discovery_score']=10;rows[0]['discovery_score']=10
        seen=[]
        def evaluate(item):
            if item['code']==OTHER:self.assertIn(SYM,lookup)
            return {'status':'ELIGIBLE','market_status':{'pass':True}}
        def progress(verified):
            packet['rows']=verified
            self.apply(packet,lookup,rows);seen.append(list(lookup))
        radar.available_capital=1000000
        radar.update([row(),row(OTHER,score=30)],evaluate,OPEN,progress=progress)
        self.assertEqual(seen[0],[SYM]);self.assertEqual(self.broker.orders,[])

    def test_partial_batch_cannot_evict_an_unprocessed_incumbent_for_no_reason(self):
        lookup,rows,packet=self.prepare();packet['rows'][0]['discovery_score']=31
        _,selection=self.apply(packet,lookup,[])  # incumbent absent from the partial packet
        self.assertEqual(selection.next_watch,[OTHER]);self.assertEqual(selection.demoted,[])

    def test_raw_unknown_status_unaffordable_or_rejected_never_fast_promote(self):
        for kind in ('RAW','UNKNOWN','REJECT','status','affordability'):
            lookup,rows,packet=self.prepare()
            if kind=='status':packet['rows'][0]['market_status']['pass']=None
            elif kind=='affordability':packet['rows'][0]['affordability']['entry_eligible']=False
            else:packet['rows'][0]['status']=kind
            _,selection=self.apply(packet,lookup,rows)
            self.assertIsNone(selection);self.assertEqual(list(lookup),[OTHER])
        self.assertEqual(self.broker.orders,[])

    def test_progress_never_bypasses_lock_or_source_and_same_score_hysteresis(self):
        lookup,rows,packet=self.prepare();packet['rows'][0]['reason']='HARD_LOCK'
        self.assertNotIn(SYM,self.apply(packet,lookup,rows)[1].next_watch)
        packet['rows'][0]['reason']='';packet['rows'][0]['source_count']=0
        self.assertNotIn(SYM,self.apply(packet,lookup,rows)[1].next_watch)
        packet['rows'][0]['source_count']=2;packet['rows'][0]['discovery_score']=32
        self.assertEqual(self.apply(packet,lookup,rows)[1].next_watch,[OTHER])

    def test_stale_future_wrong_day_and_outside_opening_window_packets_are_ignored(self):
        for source,at in [(OPEN-timedelta(seconds=181),OPEN),(OPEN+timedelta(seconds=2),OPEN),
                          (OPEN-timedelta(days=1),OPEN),(OPEN,OPEN-timedelta(seconds=1)),
                          (OPEN,OPEN+timedelta(minutes=10))]:
            lookup,rows,packet=self.prepare();packet['source_observed_at']=source.isoformat()
            self.assertIsNone(self.apply(packet,lookup,rows,at)[1]);self.assertEqual(list(lookup),[OTHER])

    def test_existing_signal_ready_focus_wins_over_a_new_rank_only_candidate(self):
        lookup,rows,packet=self.prepare();packet['rows'][0]['discovery_score']=90
        ready={OTHER:{'tier':0,'timestamp':OPEN.isoformat()}}
        self.assertEqual(self.apply(packet,lookup,rows,readiness=ready)[1].next_watch,[OTHER])

    def test_coalesced_raw_packet_keeps_full_source_lineage_and_unknown_not_permission(self):
        lookup,rows,packet=self.prepare();self.apply(packet,lookup,rows)
        self.assertEqual(set(self.e.source_qualification),{SYM,OTHER})
        self.assertEqual(self.e.source_qualification[SYM]['source_count'],2)
        self.assertEqual(self.e.observation_timeline.rows[SYM]['first_raw_at'],OPEN.isoformat())
        self.assertEqual(self.broker.orders,[])

    def test_worker_only_publishes_progress_in_first_ten_minutes_with_same_queries(self):
        for at,expected in [(OPEN-timedelta(seconds=1),False),(OPEN,True),(OPEN+timedelta(minutes=10),False)]:
            worker=RadarWorker(self.cfg,self.auto,self.policy,SimpleNamespace(profile={}),self.root)
            worker.available_capital=1000000;packets=[]
            def emit(packet):
                packets.append(packet)
                if 'performance' in packet:worker.stop.set();worker.request.set()
            worker.emit=emit
            with (patch('automation.radar.now',return_value=at),patch('automation.radar.collect',return_value=[row()]) as rank,
                 patch.object(worker,'evaluate',return_value={'status':'ELIGIBLE','market_status':{'pass':True}}) as status,
                 patch('automation.radar.atomic_json'),patch('automation.radar.write_csv'),patch('automation.radar.append_jsonl')):
                worker.run()
            self.assertEqual(any(p.get('phase')=='QUALIFIED_PROGRESS' for p in packets),expected)
            self.assertEqual(rank.call_count,1);self.assertEqual(status.call_count,1)
            self.assertEqual(packets[-1]['performance']['scan_wait_seconds'],60)

    def test_bounded_radar_queue_handles_concurrent_drain_without_losing_final_snapshot(self):
        worker=RadarWorker(self.cfg,self.auto,self.policy,SimpleNamespace(profile={}),self.root)
        for phase in ('RAW','QUALIFIED_PROGRESS','FINAL'):worker.emit({'phase':phase})
        self.assertEqual(worker.q.qsize(),2)
        self.assertEqual([worker.q.get_nowait()['phase'] for _ in range(2)],['QUALIFIED_PROGRESS','FINAL'])
        raced=Mock();raced.put_nowait.side_effect=[queue.Full,None];raced.get_nowait.side_effect=queue.Empty
        worker.q=raced;worker.emit({'phase':'FINAL'})
        self.assertEqual(raced.put_nowait.call_count,2)


class OpeningReceiverTests(unittest.TestCase):
    setUp=wire.SubscriptionRefreshTests.setUp
    frame=wire.SubscriptionRefreshTests.frame

    def send(self,t,watch=None):
        self.clock=t.time
        return process_message(self.e,self.d,self.frame(t),t.time,{SYM},watch or {},self.radar,
                               self.prices,self.account,True,self.sub)

    def test_preopen_subscription_gets_first_open_tick_without_reset_or_fake_bars(self):
        self.clock=OPEN-timedelta(seconds=10);self.e.observation_timeline=ObservationTimeline()
        self.sub=SubscriptionState();self.sub.refresh(self.e,self.d,self.feed,{SYM},self.clock)
        process_message(self.e,self.d,{'trnm':'REG','return_code':0},self.clock,{SYM},{},self.radar,
                        self.prices,self.account,True,self.sub)
        t=Tick(SYM,self.clock,10000,100,100,10010,9990)
        self.assertEqual(self.send(t),0);self.assertFalse(self.d.usable_bars(SYM))
        t=replace(t,time=OPEN,cumulative=200)
        self.assertEqual(self.send(t),1);self.assertNotIn(SYM,self.sub.waiting)
        self.assertEqual(self.d.observation_start[SYM],OPEN);self.assertEqual(self.broker.orders,[])
        self.assertTrue(control_supported(OPEN.replace(hour=8)));self.assertFalse(supported(OPEN.replace(hour=8)))

    def test_indicative_partial_and_old_ticks_cannot_create_early_signal(self):
        for sec in range(60):
            self.send(Tick(SYM,OPEN+timedelta(seconds=sec),10000+sec*10,100,100+sec,10610,10590,False),{SYM:self.watch})
        self.assertFalse(self.d.history);self.assertFalse(self.d.observation_start);self.assertEqual(self.broker.orders,[])
        self.clock=OPEN+timedelta(minutes=1)
        old=Tick(SYM,OPEN-timedelta(seconds=1),10000,100,9999,10010,9990)
        process_message(self.e,self.d,self.frame(old),self.clock,{SYM},{SYM:self.watch},self.radar,
                        self.prices,self.account,True,self.sub)
        self.assertFalse(self.d.history);self.assertEqual(self.broker.orders,[])

    def test_real_opening_A_B_C_remain_reachable_with_current_structure_checks(self):
        for pattern in ('A','B','C'):
            with self.subTest(pattern=pattern):
                self.setUp();self.clock=OPEN;self.e.s['day']=OPEN.date().isoformat()
                self.cfg=replace(self.cfg,risk_fraction=.02,max_daily_loss_fraction=.04,
                    max_positions=1,max_position_fraction=1,max_entries_per_day=None)
                self.e.c=self.cfg
                auto=replace(self.auto,AUTO_TRADING_CAP_KRW=81300,intraday_context_enabled=True,
                    structural_breakout_enabled=True,structural_pullback_enabled=True,
                    confirmed_invalidation_enabled=True,confirmed_box_direction=True)
                self.e.a=auto;self.d=PatternDetector(self.cfg,auto,self.e.costs);self.d.continuous_observation=True
                self.e.reset_detector=False;self.e.observation_timeline=ObservationTimeline()
                self.watch.update(resistance=20000,resistance_room_pass=True,venue='KRX',source_details={'a':{},'b':{}})
                self.radar.cache={(OPEN.date(),SYM):[dict(datetime=(OPEN-timedelta(days=500-i)).date().isoformat(),
                    open=9900,high=10000,low=9800,close=9950,volume=200000) for i in range(500)]}
                self.radar.status_cache={}
                if pattern=='A':
                    shapes=[(10000,10020,9990,10010),(10300,10340,10290,10310)]
                elif pattern=='B':shapes=[tuple(b[k]*10 for k in ('open','high','low','close')) for b in pullback.NORMAL]
                else:shapes=[(9860,10000,9860,9970)]*5
                cumulative=0
                for minute,shape in enumerate(shapes):
                    for sec in range(60):
                        price=shape[min(sec,3)]
                        volume=100 if pattern=='A' else pullback.NORMAL[minute]['volume']*6 if pattern=='B' else 600
                        cumulative+=volume
                        t=Tick(SYM,OPEN+timedelta(minutes=minute,seconds=sec),price,volume,cumulative,price,price-10)
                        self.send(t)  # receiver builds genuine post-09:00 candles
                baseline=shapes[-1][3];breakout=10410 if pattern=='B' else 10350 if pattern=='A' else 10010
                for sec in range(6):
                    px=breakout if sec==5 else baseline;volume=4000 if sec==5 else 100;cumulative+=volume
                    t=Tick(SYM,OPEN+timedelta(minutes=len(shapes),seconds=sec),px,volume,cumulative,px,px-10)
                    self.send(t,{SYM:self.watch} if sec==5 else None)
                signal=self.d.signal(self.d.last[SYM],self.watch)
                self.assertIsNotNone(signal,self.d.last_eval.get(SYM))
                self.assertEqual(signal['pattern'],pattern)
                diagnostics=[json.loads(s) for s in (self.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
                recent=[(r.get('event'),r.get('primary_reason_code'),r.get('reason'),r.get('gates',{}).get('risk_quantity')) for r in diagnostics if r.get('event') in {'ENTRY_EVAL','NO_TRADE','ENTRY_EXECUTION_RECHECK'}][-6:]
                self.assertEqual(len(self.broker.orders),1,recent)
                self.assertGreaterEqual(datetime.fromisoformat(signal['decision_bar_snapshot'][0]['time']),OPEN)

    def test_timeline_keeps_receiver_processing_completion_and_ack_scope_distinct(self):
        timeline=ObservationTimeline();raw=row(stamp=OPEN-timedelta(minutes=1))
        timeline.raw([raw],OPEN);timeline.watch(SYM,OPEN);timeline.subscription_request([SYM],OPEN)
        timeline.subscription_ack([SYM],OPEN+timedelta(milliseconds=200))
        timeline.tick(SYM,OPEN+timedelta(seconds=1),[],10000,
            received=OPEN+timedelta(seconds=1,milliseconds=100),processed=OPEN+timedelta(seconds=2))
        timeline.tick(SYM,OPEN+timedelta(minutes=1),[{'time':OPEN}],10010,
            received=OPEN+timedelta(minutes=1),processed=OPEN+timedelta(minutes=1,seconds=2))
        timeline.signal(SYM,OPEN+timedelta(minutes=2));timeline.risk_eligible(SYM,OPEN+timedelta(minutes=2,seconds=1))
        v=timeline.opening_snapshot(OPEN+timedelta(minutes=3))['symbols'][SYM]
        self.assertEqual(v['first_completed_bar_start_at'],OPEN.isoformat())
        self.assertEqual(v['first_completed_bar_known_at'],(OPEN+timedelta(minutes=1,seconds=2)).isoformat())
        self.assertEqual(v['latency_seconds']['receiver_to_processing'],.9)
        self.assertEqual(v['latency_seconds']['signal_to_risk_eligible'],1)
        self.assertEqual(v['subscription_ack_scope'],'GROUP_REG_ACK_NOT_INDIVIDUAL_SYMBOL')
        events=timeline.drain_events();self.assertTrue(events);self.assertFalse(timeline.drain_events())
        self.assertTrue(all(not e['actual_order_permission'] for e in events))

    def test_receiver_fresh_backlog_is_not_mislabeled_current_execution_freshness(self):
        timeline=ObservationTimeline()
        timeline.tick(SYM,OPEN,[],10000,received=OPEN,processed=OPEN+timedelta(seconds=10),execution_fresh=False)
        self.assertNotIn('first_execution_fresh_tick_at',timeline.rows[SYM])
        timeline.tick(SYM,OPEN+timedelta(seconds=11),[],10010)
        self.assertEqual(timeline.rows[SYM]['first_execution_fresh_tick_at'],(OPEN+timedelta(seconds=11)).isoformat())


FAILURE=[(10020,10180,10010,10040),(10040,10060,10010,10030),
         (10030,10090,10020,10070),(10070,10080,10000,10005)]

def opening_position(values=FAILURE):
    p=dict(avg=10000,qty=3,high=10180,initial_r=250,initial_stop=9750,stop=9750,
           ownership='BOT',opened=OPEN.isoformat(),exit_requested=False,
           signal={'pattern':'A','session':'REGULAR'},
           protection_bars=[dict(time=(OPEN+timedelta(minutes=i)).isoformat(),open=o,high=h,low=l,close=c,
               volume=1000,first_second=0) for i,(o,h,l,c) in enumerate(values)],
           last_quote={'time':(OPEN+timedelta(minutes=len(values))).isoformat(),'bid':10000,'ask':10010,'bid_qty':10})
    return p


class OpeningFailureObservationTests(unittest.TestCase):
    cfg=Config();auto=replace(AutomationConfig(),confirmed_invalidation_enabled=True)
    at=OPEN+timedelta(minutes=4)

    def test_pre_one_r_failure_proposes_exit_with_costs_but_cannot_send_an_order(self):
        p=opening_position();before=copy.deepcopy(p);v=observe_opening_failure(self.cfg,self.auto,p,self.at)
        self.assertEqual(v['action'],'EXIT_CANDIDATE');self.assertFalse(v['orders_enabled'])
        self.assertLess(v['modeled_net_pnl'],0);self.assertEqual(p,before)

    def test_one_upper_wick_or_first_pullback_does_not_trigger(self):
        p=opening_position(FAILURE[:2]);v=observe_opening_failure(self.cfg,self.auto,p,OPEN+timedelta(minutes=2))
        self.assertEqual(v['action'],'UNKNOWN')

    def test_normal_pullback_rebound_holds_when_its_floor_survives(self):
        p=opening_position(FAILURE[:-1]+[(10070,10100,10020,10090)])
        self.assertEqual(observe_opening_failure(self.cfg,self.auto,p,self.at)['action'],'HOLD')

    def test_recovered_old_peak_retires_failed_rebound_hypothesis(self):
        p=opening_position(FAILURE[:-1]+[(10070,10190,10020,10180)]);p['high']=10190
        self.assertEqual(observe_opening_failure(self.cfg,self.auto,p,self.at)['action'],'HOLD')

    def test_future_partial_gap_missing_volume_and_partial_entry_are_not_evidence(self):
        for kind in ('future','gap','volume','entry'):
            p=opening_position()
            if kind=='future':p['protection_bars'][-1]['time']=self.at.isoformat()
            elif kind=='gap':p['protection_bars'][1]['gap_reason']='LOCAL_DROP'
            elif kind=='volume':p['protection_bars'][1]['volume']=None
            else:p['opened']=(OPEN+timedelta(seconds=1)).isoformat()
            self.assertEqual(observe_opening_failure(self.cfg,self.auto,p,self.at)['action'],'UNKNOWN')

    def test_stale_crossed_or_thin_bid_cannot_assert_executable_net_profit(self):
        for kind in ('stale','crossed','thin','unknown_depth'):
            p=opening_position()
            if kind=='stale':p['last_quote']['time']=(self.at-timedelta(seconds=6)).isoformat()
            elif kind=='crossed':p['last_quote']['bid']=10020
            elif kind=='thin':p['last_quote']['bid_qty']=1
            else:p['last_quote'].pop('bid_qty')
            v=observe_opening_failure(self.cfg,self.auto,p,self.at)
            self.assertEqual(v['action'],'EXIT_CANDIDATE');self.assertIsNone(v['modeled_net_pnl'])

    def test_operating_AE3_and_existing_profit_candidates_are_unchanged(self):
        p=opening_position();v=observe_candidates(self.cfg,self.auto,p,self.at)
        self.assertEqual(v['active_policy'],'AE3');self.assertEqual(v['policies']['AE3']['action'],'HOLD')
        self.assertFalse(v['experimental_orders_enabled']);self.assertFalse(p['exit_requested'])
        self.assertEqual(v['opening_failure_observation']['action'],'EXIT_CANDIDATE')

    def test_non_opening_B_C_AFTER_and_post_one_r_use_existing_policy(self):
        for change in ('B','C','AFTER','late','one_r'):
            p=opening_position()
            if change in ('B','C'):p['signal']['pattern']=change
            elif change=='AFTER':p['signal']['session']='AFTER'
            elif change=='late':p['opened']=OPEN.replace(minute=10).isoformat()
            else:p['high']=10250
            self.assertEqual(observe_opening_failure(self.cfg,self.auto,p,self.at)['action'],'NOT_APPLICABLE')


if __name__=='__main__':unittest.main()
