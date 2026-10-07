"""Observation continuity regression; synthetic ticks and a fake broker only."""
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock,patch
from tests import test_subscription_refresh as fixtures
START=fixtures.START;SYM=fixtures.SYM;OTHER=fixtures.OTHER
from automation.runner import resolve_radar_focus,SubscriptionState,prewarm_handoff
from automation.focus_selection import select_focus
from automation.shadow_observation import ShadowObserver

class ObservationContinuityTests(unittest.TestCase):
    setUp=fixtures.SubscriptionRefreshTests.setUp
    tick=fixtures.SubscriptionRefreshTests.tick
    warm=fixtures.SubscriptionRefreshTests.warm
    refresh=fixtures.SubscriptionRefreshTests.refresh
    frame=fixtures.SubscriptionRefreshTests.frame
    send=fixtures.SubscriptionRefreshTests.send
    signal=fixtures.SubscriptionRefreshTests.signal
    def row(self,sym=SYM,status='RESERVE',score=10,sources=1):
        return {'code':sym,'status':status,'source_count':sources,'discovery_score':score,
            'affordability':{'entry_eligible':True},'market_status':{'pass':True},'name':'synthetic'}
    def observed(self):
        return (self.d.buffers[SYM],self.d.history[SYM],self.d.current[SYM],self.d.day_high[SYM])
    def assert_retained(self,snapshot):
        self.assertIs(self.d.buffers[SYM],snapshot[0]);self.assertIs(self.d.history[SYM],snapshot[1])
        self.assertIs(self.d.current[SYM],snapshot[2]);self.assertEqual(self.d.day_high[SYM],snapshot[3])
        self.assertGreaterEqual(self.d.base_gates(self.d.last[SYM],self.watch)['warmup']['values']['elapsed_seconds'],70)
        self.assertEqual(self.broker.orders,[])
    def test_reserve_promotion_preserves_warmup_and_bars(self):
        self.warm();saved=self.observed();lookup={OTHER:self.row(OTHER,'WATCH',20,2)}
        self.sub=SubscriptionState({SYM,OTHER})
        selection=resolve_radar_focus(self.e,lookup,[self.row(),self.row(OTHER,'WATCH',20,2)],[],
            {SYM:{'tier':0},OTHER:{'tier':4}},replace(self.cfg,max_watchlist=1),self.auto,self.clock)
        self.assertEqual(selection.promoted,[SYM]);self.refresh(set(lookup)|{SYM,OTHER})
        self.assert_retained(saved);self.assertNotIn(SYM,self.sub.waiting)
    def test_watch_reserve_watch_active_universe_never_releases(self):
        self.warm();saved=self.observed();self.sub=SubscriptionState({SYM,OTHER});seen=[]
        self.e.event=Mock(side_effect=lambda kind,**kw:seen.append((kind,kw)))
        for watch in ({OTHER},{SYM},{OTHER},{SYM}):
            self.clock+=timedelta(milliseconds=100);self.refresh(set(watch)|{SYM,OTHER})
        self.assert_retained(saved)
        self.assertFalse(any(k in {'OBSERVATION_RELEASED','OBSERVATION_REJOINED'} for k,_ in seen))
    def test_quiet_trade_age_does_not_expire_short_real_subscription_gap(self):
        self.warm();saved=self.observed();self.clock+=timedelta(seconds=20)
        self.refresh(set());self.clock+=timedelta(milliseconds=100);self.refresh(set())
        self.clock+=timedelta(milliseconds=100);self.refresh({SYM})
        self.assert_retained(saved);self.assertIn(SYM,self.sub.waiting)
        self.assertEqual(self.send(self.frame(self.tick(sec=120))),0)
        self.assertEqual(self.broker.orders,[])
    def test_same_cycle_final_focus_before_subscription_has_no_release_rejoin(self):
        self.warm();saved=self.observed();self.sub=SubscriptionState({SYM,OTHER});seen=[]
        self.e.event=Mock(side_effect=lambda kind,**kw:seen.append((kind,kw)))
        lookup={SYM:self.row(SYM,'WATCH',10)}
        # Background ranks prefer OTHER. Actual current focus has the higher tier.
        resolve_radar_focus(self.e,lookup,[self.row(),self.row(OTHER,'WATCH',30,3)],
            [self.row(OTHER,'WATCH',30,3)],{SYM:{'tier':0},OTHER:{'tier':4}},
            replace(self.cfg,max_watchlist=1),self.auto,self.clock)
        self.refresh(set(lookup)|{SYM,OTHER})
        self.assertEqual(set(lookup),{SYM});self.assert_retained(saved)
        self.assertFalse(any(k in {'OBSERVATION_RELEASED','OBSERVATION_REJOINED'} for k,_ in seen))
    def test_real_detached_gap_resets_all_history(self):
        self.warm();self.refresh(set());self.clock+=timedelta(seconds=self.cfg.stale_seconds+1)
        self.refresh({SYM})
        for mapping in (self.d.buffers,self.d.history,self.d.current,self.d.last,self.d.day_high):self.assertNotIn(SYM,mapping)
        self.assertIn(SYM,self.sub.waiting);self.assertEqual(self.broker.orders,[])
    def test_long_data_gap_but_continued_ticks_while_detached_preserves(self):
        self.warm();saved=self.observed();self.refresh(set());self.clock+=timedelta(seconds=6)
        self.d.ingest(self.tick(sec=126),self.clock);self.refresh({SYM})
        self.assertIs(self.d.history[SYM],saved[1]);self.assertIn(SYM,self.sub.waiting)
    def test_disconnection_still_resets_and_reconcile_blocks(self):
        self.warm();self.send({'trnm':'DISCONNECTED'})
        self.assertEqual(self.d.last,{});self.assertEqual(self.d.history,{})
        self.assertFalse(self.e.connected);self.assertFalse(self.e.account_ok)
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
    def test_shadow_role_changes_preserve_quiet_observation(self):
        self.warm();shadow=ShadowObserver(self.cfg,self.auto,self.e.costs,Mock())
        shadow.detector=self.d;shadow.update([self.row(status='WATCH')],self.clock)
        # First update starts the observer epoch, so build real history after it.
        self.warm();saved=self.observed();self.clock+=timedelta(seconds=20)
        for status in ('RESERVE','SHADOW','WATCH'):
            shadow.update([self.row(status=status)],self.clock);self.clock+=timedelta(milliseconds=100)
        self.assert_retained(saved)
    def test_shadow_reselected_before_gap_threshold_is_not_cleaned_first(self):
        shadow=ShadowObserver(self.cfg,self.auto,self.e.costs,Mock());shadow.detector=self.d
        shadow.update([self.row()],self.clock);self.warm();saved=self.observed()
        self.clock+=timedelta(seconds=20);shadow.update([],self.clock)
        self.clock+=timedelta(milliseconds=100);shadow.update([self.row(status='WATCH')],self.clock)
        self.assert_retained(saved)
    def test_shadow_true_detached_gap_resets(self):
        shadow=ShadowObserver(self.cfg,self.auto,self.e.costs,Mock());shadow.detector=self.d
        shadow.update([self.row()],self.clock);self.warm();shadow.update([],self.clock)
        self.clock+=timedelta(seconds=6);shadow.update([self.row(status='WATCH')],self.clock)
        self.assertNotIn(SYM,self.d.last);self.assertNotIn(SYM,self.d.history)
    def test_current_focus_incumbent_bonus_survives_background_rank_result(self):
        lookup={SYM:self.row(SYM,'WATCH',20,2)}
        resolve_radar_focus(self.e,lookup,[self.row(SYM,'RESERVE',20,2),self.row(OTHER,'WATCH',21,2)],
            [self.row(OTHER,'WATCH',21,2)],{},replace(self.cfg,max_watchlist=1),self.auto,self.clock)
        self.assertEqual(set(lookup),{SYM});self.assertEqual(self.broker.orders,[])
    def test_exact_two_point_hysteresis_tie_preserves_incumbent(self):
        old={SYM:{**self.row(SYM,'WATCH',20,2),'overhead_share_estimate':.5}}
        challenger={**self.row(OTHER,'RESERVE',22,2),'overhead_share_estimate':0}
        selection=select_focus(old,list(old.values())+[challenger],{},max_watchlist=1)
        self.assertFalse(selection.applied);self.assertEqual(selection.next_watch,[SYM])
    def test_clear_higher_tier_can_rotate_all_eight_without_arbitrary_cap(self):
        old={f'10000{i}':self.row(f'10000{i}','WATCH',30,3) for i in range(8)}
        new=[self.row(f'20000{i}','RESERVE',10,1) for i in range(8)]
        info={s:{'tier':4} for s in old};info.update({r['code']:{'tier':0} for r in new})
        selection=select_focus(old,list(old.values())+new,info,max_watchlist=8)
        self.assertEqual(len(selection.next_watch),8);self.assertEqual(len(selection.promoted),8)
        self.assertEqual(len(selection.demoted),8)
    def test_stale_readiness_cannot_displace_incumbent(self):
        old={SYM:self.row(SYM,'WATCH',20,2)}
        info={OTHER:{'tier':0,'timestamp':(self.clock-timedelta(seconds=16)).isoformat()}}
        selection=select_focus(old,list(old.values())+[self.row(OTHER)],info,max_watchlist=1,clock_iso=self.clock.isoformat())
        self.assertFalse(selection.applied)
    def test_hard_invalid_incumbent_is_removed_even_with_bonus(self):
        old={SYM:self.row(SYM,'WATCH',200,2)}
        selection=select_focus(old,[{**old[SYM],'status':'REJECT','reason':'HARD_LOCK'},self.row(OTHER)],{},max_watchlist=1)
        self.assertEqual(selection.next_watch,[OTHER]);self.assertEqual(selection.demoted,[SYM])
    def test_missing_rank_after_existing_grace_is_not_retained_forever(self):
        lookup={SYM:self.row(SYM,'WATCH',200,2)}
        selection=resolve_radar_focus(self.e,lookup,[self.row(OTHER)],[],{},
            replace(self.cfg,max_watchlist=1),self.auto,self.clock)
        self.assertEqual(set(lookup),{OTHER});self.assertEqual(selection.demoted,[SYM])
    def test_existing_radar_grace_is_respected(self):
        lookup={SYM:self.row(SYM,'WATCH',200,2)}
        selection=resolve_radar_focus(self.e,lookup,[self.row(OTHER)],list(lookup.values()),{},
            replace(self.cfg,max_watchlist=1),self.auto,self.clock)
        self.assertEqual(set(lookup),{SYM});self.assertFalse(selection.applied)
    def test_missing_source_metadata_is_not_inferred_from_watch_label(self):
        row=self.row(status='WATCH');row.pop('source_count')
        lookup={}
        selection=resolve_radar_focus(self.e,lookup,[row],[row],{},self.cfg,self.auto,self.clock)
        self.assertEqual(selection.next_watch,[]);self.assertEqual(lookup,{})
        self.assertEqual(self.broker.orders,[])
    def test_source1_promotion_does_not_grant_buy_by_itself(self):
        self.e.a=replace(self.auto,min_source_count=2)
        self.warm();self.refresh({SYM,OTHER});self.send({'trnm':'REG','return_code':0})
        self.clock+=timedelta(seconds=1)
        with patch.object(self.d,'signal',return_value=self.signal()) as signal:
            self.send(self.frame(self.tick(sec=121)),{SYM:{**self.watch,'source_count':1,'fallback_source1':False}})
        signal.assert_not_called();self.assertEqual(self.broker.orders,[])

if __name__=='__main__':unittest.main()
