"""Observation continuity and degraded radar recovery; no real account or orders."""
import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automation.observation_retention import ObservationRetention, observation_residence_seconds
from automation.policy import AutomationConfig
from automation.radar import collect, RadarWorker
from automation.observation_settings import ObservationSettings
from automation.runner import handle_radar_failure, verified_radar_cache_available
from core.settings import Config, KST
import tests.test_subscription_refresh as fixture

AT=datetime(2026,10,14,11,54,tzinfo=KST)

class RadarCacheTests(unittest.TestCase):
    def engine(self,stamp=AT):return SimpleNamespace(last_radar_at=stamp,event=Mock())
    def test_failed_refresh_preserves_only_existing_verified_ttl(self):
        e=self.engine()
        for seconds in (1,60,179,180):
            self.assertTrue(handle_radar_failure(e,{'error':'ValueError'},AT+timedelta(seconds=seconds),60))
            self.assertEqual(e.last_radar_at,AT)
        self.assertFalse(handle_radar_failure(e,{'error':'ValueError'},AT+timedelta(seconds=181),60))
    def test_no_verified_cycle_cannot_authorize_cache(self):
        self.assertFalse(verified_radar_cache_available(self.engine(None),AT,60))
    def test_future_and_previous_day_cannot_authorize_cache(self):
        for known in (AT+timedelta(seconds=1),AT-timedelta(days=1),AT.replace(tzinfo=None)):
            self.assertFalse(verified_radar_cache_available(self.engine(known),AT,60))
    def test_preopen_cycle_does_not_authorize_regular_orders(self):
        self.assertFalse(verified_radar_cache_available(self.engine(AT.replace(hour=8,minute=59)),AT.replace(hour=9,minute=0),60))
    def test_error_retains_its_stage_and_diagnostic_provenance(self):
        e=self.engine();r={'time':AT.isoformat(),'error':'ValueError','stage':'RANK_COLLECT','exception_message':'invalid row'}
        self.assertTrue(handle_radar_failure(e,r,AT,60))
        payload=e.event.call_args.kwargs
        self.assertEqual(payload['stage'],'RANK_COLLECT')
        self.assertEqual(payload['verified_cache_at'],AT)
        self.assertTrue(payload['cached_candidate_checks_unchanged'])

class RankingRowTests(unittest.TestCase):
    def rest(self,rows):
        return SimpleNamespace(pages=lambda *a,**k:(copy.deepcopy(rows),{}),
            radar_observation_settings=replace(ObservationSettings(),live_early_discovery_enabled=False))
    def test_one_invalid_identity_does_not_lose_all_other_sources(self):
        r=self.rest([{'stk_cd':'invalid','stk_nm':'bad'}, {'stk_cd':'028670','stk_nm':'팬오션','cur_prc':'6970'}])
        rows=collect(r)
        self.assertEqual([x['code'] for x in rows],['028670'])
        self.assertEqual(len(rows[0]['source_details']),5)
        self.assertEqual(len(r.radar_rejected_rows),5)
        self.assertEqual(rows[0]['source_details']['ka10032']['observed_rank'],2)
    def test_missing_identity_null_name_and_non_object_rows_are_rejected(self):
        r=self.rest([None,{'stk_nm':'missing code'},{'stk_cd':'028670','stk_nm':None}])
        self.assertEqual(collect(r),[])
        self.assertEqual(len(r.radar_rejected_rows),15)
    def test_unknown_exchange_suffix_is_rejected_not_silently_normalized(self):
        r=self.rest([{'stk_cd':'028670_ZZ','stk_nm':'팬오션'}])
        self.assertEqual(collect(r),[])
        self.assertTrue(r.radar_rejected_rows)
    def test_optional_early_source_preserves_supported_exchange_suffix(self):
        r=self.rest([{'stk_cd':'028670_NX','stk_nm':'팬오션','cur_prc':'6970'}])
        r.radar_observation_settings=replace(ObservationSettings(),live_early_discovery_enabled=True)
        rows=collect(r)
        self.assertEqual(rows[0]['code'],'028670')
        self.assertEqual(len(rows[0]['source_details']),6)
        self.assertIn('ka10023',rows[0]['source_details'])
    def test_source_failure_is_not_converted_to_verified_empty_data(self):
        def fail(*a,**k):raise ValueError('source field missing')
        with self.assertRaisesRegex(ValueError,'source field missing'):collect(SimpleNamespace(pages=fail))
    def test_worker_error_records_actual_message_trace_and_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            rest=SimpleNamespace(profile={})
            worker=RadarWorker(Config(),AutomationConfig(),SimpleNamespace(locked=lambda s:False),rest,Path(directory))
            def fail(*a,**k):
                worker.stop.set();worker.request.set();raise ValueError('source field missing')
            rest.pages=fail
            worker.run(); packet=worker.q.get_nowait()
            self.assertEqual(packet['error'],'ValueError')
            self.assertEqual(packet['stage'],'RANK_COLLECT')
            self.assertIn('source field missing',packet['exception_message'])
            self.assertIn('ValueError',packet['traceback'])

class ResidenceTests(unittest.TestCase):
    def test_residence_is_existing_longest_pattern_warmup(self):
        a=AutomationConfig()
        self.assertEqual(observation_residence_seconds(a),(a.box_min_bars+1)*60)
    def test_sixty_second_turnover_no_longer_discards_candidate_at_second_sixty_one(self):
        r=ObservationRetention();g=observation_residence_seconds(AutomationConfig())
        r.select({'old'},{'held'},[],AT,g,3)
        selected,_,grace=r.select({'new'},{'held'},['other'],AT+timedelta(seconds=61),g,3)
        self.assertIn('old',selected);self.assertIn('old',grace)
        selected,_,grace=r.select({'new'},{'held'},['other'],AT+timedelta(seconds=g+1),g,3)
        self.assertNotIn('old',selected);self.assertNotIn('old',grace)
    def test_full_pressure_never_expands_existing_footprint_or_drops_critical(self):
        r=ObservationRetention();g=observation_residence_seconds(AutomationConfig())
        r.select({'old'}, {'position','order'}, [], AT,g,4)
        r.last_watch.update({str(i):AT for i in range(30)})
        selected,_,_=r.select({'new'},{'position','order'},['s1','s2'],AT+timedelta(seconds=70),g,4)
        self.assertEqual(len(selected),4)
        self.assertTrue({'new','position','order'}<=selected)

class ReceivedHistoryTests(unittest.TestCase):
    setUp=fixture.SubscriptionRefreshTests.setUp
    tick=fixture.SubscriptionRefreshTests.tick
    warm=fixture.SubscriptionRefreshTests.warm
    refresh=fixture.SubscriptionRefreshTests.refresh
    def test_actual_two_minute_role_turnover_keeps_receipts_and_complete_bars(self):
        self.warm();start=self.d.observation_start[fixture.SYM]
        r=ObservationRetention();g=observation_residence_seconds(self.auto)
        r.select({fixture.SYM},{fixture.SYM},[],self.clock,g,2)
        for seconds in range(121,251):
            self.clock=fixture.START+timedelta(seconds=seconds)
            desired,_,_=r.select({fixture.OTHER},{fixture.OTHER},[],self.clock,g,2)
            self.refresh(desired)
            self.assertIn(fixture.SYM,self.sub.symbols)
            self.assertTrue(self.d.ingest(self.tick(sec=seconds),self.clock))
        self.assertEqual(self.d.observation_start[fixture.SYM],start)
        self.assertGreaterEqual(len(self.d.usable_bars(fixture.SYM)),4)
        self.assertEqual(self.broker.orders,[])
    def test_real_unsubscribed_gap_still_resets_history(self):
        self.warm();self.clock+=timedelta(seconds=1);self.refresh({fixture.OTHER})
        self.clock+=timedelta(seconds=6);self.refresh({fixture.OTHER})
        self.assertNotIn(fixture.SYM,self.d.last)
        self.assertNotIn(fixture.SYM,self.d.history)
        self.assertEqual(self.broker.orders,[])

if __name__=='__main__':unittest.main()
