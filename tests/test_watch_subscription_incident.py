"""2026-10-19 10:54 focus oscillation and 65/64 subscription incident."""
from datetime import datetime,timedelta
from dataclasses import replace
from types import SimpleNamespace
import unittest

from automation.focus_selection import select_focus,TIER_FRESH_WEAK,TIER_RANK_ONLY
from automation.observation_retention import ObservationRetention
from automation.radar import DynamicRadar
from automation.policy import AutomationConfig
from core.settings import Config,KST

AT=datetime(2026,10,19,10,54,36,tzinfo=KST)


class WatchSubscriptionIncidentTests(unittest.TestCase):
    def pair(self):
        return [dict(code=s,status='ELIGIBLE',source_count=1,discovery_score=score,
                     affordability={'entry_eligible':True})
                for s,score in [('046970',20.0588679245283),('336370',10.01923076923077)]]

    def info(self,tier=TIER_FRESH_WEAK,age=0,blocked=True):
        return {'tier':tier,'timestamp':(AT-timedelta(seconds=age)).isoformat(),'price_blocked':blocked}

    def test_1054_expired_negative_geometry_cannot_gain_a_promotion(self):
        rows=self.pair();current={rows[0]['code']:rows[0]}
        info={'046970':self.info(),'336370':self.info(age=16)}
        result=select_focus(current,rows,info,1,clock_iso=AT.isoformat())
        self.assertFalse(result.applied)
        self.assertEqual(result.next_watch,['046970'])

    def test_expiry_and_refresh_of_same_failure_do_not_flip_watch(self):
        rows=self.pair();current={rows[0]['code']:rows[0]};changes=0
        for age in (16,0,16,0,16,0):
            info={'046970':self.info(),'336370':self.info(age=age)}
            selected=select_focus(current,rows,info,1,clock_iso=AT.isoformat())
            changes+=selected.applied
            current={row['code']:row for row in rows if row['code'] in selected.next_watch}
        self.assertEqual(changes,0)

    def test_actual_new_strong_evidence_still_promotes_without_minimum_hold(self):
        rows=self.pair();current={rows[0]['code']:rows[0]}
        for tier in (0,1,2):
            info={'046970':self.info(),'336370':self.info(tier=tier,blocked=False)}
            with self.subTest(tier=tier):
                result=select_focus(current,rows,info,1,clock_iso=AT.isoformat())
                self.assertEqual(result.next_watch,['336370'])

    def test_expired_positive_readiness_is_still_rank_only(self):
        rows=self.pair();result=select_focus({rows[0]['code']:rows[0]},rows,
            {'336370':self.info(tier=0,age=16,blocked=False)},1,clock_iso=AT.isoformat())
        self.assertEqual(result.tier_map['336370'],TIER_RANK_ONLY)
        self.assertEqual(result.next_watch,['046970'])

    def test_previous_day_negative_does_not_become_an_indefinite_exclusion(self):
        rows=self.pair();info={'046970':self.info()}
        info['336370']={**self.info(),'timestamp':(AT-timedelta(days=1)).isoformat()}
        result=select_focus({rows[0]['code']:rows[0]},rows,info,1,clock_iso=AT.isoformat())
        self.assertEqual(result.priority_tuples['336370'][0],TIER_RANK_ONLY)

    def test_new_observation_without_the_old_geometry_retires_negative_evidence(self):
        rows=self.pair();result=select_focus({rows[0]['code']:rows[0]},rows,
            {'046970':self.info(),'336370':self.info(blocked=False)},1,clock_iso=AT.isoformat())
        self.assertEqual(result.next_watch,['336370'])

    def test_future_or_malformed_negative_is_not_observed_price_evidence(self):
        rows=self.pair()
        for value in ((AT+timedelta(seconds=1)).isoformat(),'invalid'):
            info={'046970':self.info(),'336370':{**self.info(),'timestamp':value}}
            with self.subTest(value=value):
                result=select_focus({rows[0]['code']:rows[0]},rows,info,1,clock_iso=AT.isoformat())
                self.assertEqual(result.priority_tuples['336370'][0],TIER_RANK_ONLY)

    def test_radar_and_focus_use_the_same_negative_evidence_rule(self):
        items=[]
        for sym,ranks in [('046970',[17,100]),('336370',[52])]:
            items.append({'code':sym,'reference_price':1000,
                'source_details':{str(i):{'observed_rank':rank,'values':{}} for i,rank in enumerate(ranks)}})
        radar=DynamicRadar(replace(Config(),max_watchlist=1),
            replace(AutomationConfig(),setup_aware_watch_priority=True,fallback_source1=True),
            SimpleNamespace(locked=lambda symbol:False))
        radar.available_capital=100000;radar.day=AT.date();radar.watch={'046970':items[0]}
        radar.momentum={'046970':self.info(),'336370':self.info(age=16)}
        _,chosen,_=radar.update(items,lambda row:{'status':'ELIGIBLE'},AT)
        self.assertEqual([r['code'] for r in chosen],['046970'])

    def test_critical_thirteen_plus_shadow_sixty_four_fits_sixty_four(self):
        watch={f'{i:06d}' for i in range(1,13)}
        shadow=[f'{i:06d}' for i in range(1,65)]
        critical=watch|{'052260'}
        selected,observed,grace=ObservationRetention().select(watch,critical,shadow,AT,360,64)
        self.assertEqual(len(selected),64)
        self.assertTrue(critical<=selected)
        self.assertNotIn('000064',selected)
        self.assertEqual(observed,selected-critical)

    def test_grace_uses_capacity_without_expanding_or_dropping_protection(self):
        retained=ObservationRetention();retained.select({'old'},{'held','order'},[],AT,360,4)
        selected,observed,grace=retained.select({'new'},{'held','order'},['s1','s2'],AT+timedelta(seconds=1),360,4)
        self.assertEqual(selected,{'new','held','order','old'})
        self.assertEqual(grace,{'old'})
        self.assertEqual(observed,{'old'})

    def test_real_critical_overflow_never_discards_positions_or_orders(self):
        critical={'p1','p2','order','external'}
        selected,observed,grace=ObservationRetention().select({'watch'},critical,['s1','s2'],AT,360,3)
        self.assertEqual(selected,critical|{'watch'})
        self.assertEqual(observed,set());self.assertEqual(grace,set())
