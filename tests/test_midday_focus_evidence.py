"""12:30 routed WATCH evidence must survive its positive-readiness TTL.

The geometry failure is recorded evidence, never a buy permission. No live
runtime, broker, credentials, or future market data are used here.
"""
from datetime import datetime,timedelta
from threading import RLock
from types import SimpleNamespace
import unittest

from automation.focus_selection import select_focus,TIER_FRESH_WEAK,TIER_RANK_ONLY
from automation.runner import focus_readiness
from core.settings import KST

AT=datetime(2026,10,19,12,30,49,tzinfo=KST)
SYM='138080'


def detector(at,blocked=True):
    gates={k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status')}
    gates.update(acceleration={'result':'FAIL'},liquidity={'result':'FAIL'})
    trials=[{'setup':'C','candidate_generated':True,'acceleration_required':False,
             'gates':{'liquidity':{'result':'FAIL'},'box_structure':{'result':'FAIL' if blocked else 'PASS'},
                      'risk_reward':{'result':'FAIL' if blocked else 'PASS'}}}]
    return SimpleNamespace(_state_lock=RLock(),fast_strategy=None,
        last_eval={SYM:{'time':at,'gates':gates,'trials':trials}})


class MiddayFocusEvidenceTests(unittest.TestCase):
    def test_actual_expired_live_failure_reaches_priority_comparison(self):
        # 12:30:11 live evaluation was 38 seconds old at the 12:30:49 swap.
        ready=focus_readiness(detector(AT-timedelta(seconds=38)),{},AT)
        self.assertIn(SYM,ready)
        self.assertTrue(ready[SYM]['price_blocked'])
        rows=[{'code':'002710','status':'WATCH','source_count':2,'discovery_score':20.094164456233422},
              {'code':SYM,'status':'RESERVE','source_count':1,'discovery_score':10.025}]
        ready['002710']={'timestamp':AT.isoformat(),'tier':4,'price_blocked':True}
        chosen=select_focus({'002710':rows[0]},rows,ready,1,clock_iso=AT.isoformat())
        self.assertEqual(chosen.priority_tuples[SYM][0],TIER_FRESH_WEAK)
        self.assertFalse(chosen.applied)

    def test_expiry_does_not_change_negative_priority_without_new_market_evidence(self):
        d=detector(AT-timedelta(seconds=14))
        for delta in (0,2,10,60):
            asof=AT+timedelta(seconds=delta)
            ready=focus_readiness(d,{},asof)
            self.assertTrue(ready[SYM]['price_blocked'])
            self.assertEqual(ready[SYM]['timestamp'],d.last_eval[SYM]['time'].isoformat())

    def test_newer_shadow_observation_can_retire_old_negative_geometry(self):
        shadow={SYM:{'timestamp':(AT-timedelta(seconds=1)).isoformat(),
                     'tier':1,'price_blocked':False}}
        result=focus_readiness(detector(AT-timedelta(seconds=4)),shadow,AT)
        self.assertEqual(result[SYM],shadow[SYM])
        self.assertFalse(result[SYM]['price_blocked'])

    def test_fresh_live_evidence_supersedes_older_shadow(self):
        shadow={SYM:{'timestamp':(AT-timedelta(seconds=4)).isoformat(),
                     'tier':1,'price_blocked':False}}
        result=focus_readiness(detector(AT-timedelta(seconds=1)),shadow,AT)
        self.assertTrue(result[SYM]['price_blocked'])
        self.assertFalse(shadow[SYM]['price_blocked'])

    def test_positive_readiness_is_not_extended_past_fifteen_seconds(self):
        self.assertNotIn(SYM,focus_readiness(detector(AT-timedelta(seconds=16),blocked=False),{},AT))

    def test_previous_day_future_and_naive_live_evidence_are_not_carried(self):
        for at in (AT-timedelta(days=1),AT+timedelta(seconds=1),AT.replace(tzinfo=None)):
            with self.subTest(at=at):
                self.assertNotIn(SYM,focus_readiness(detector(at),{},AT))

    def test_malformed_or_future_shadow_cannot_erase_valid_live_failure(self):
        for stamp in ('invalid',(AT+timedelta(seconds=1)).isoformat()):
            shadow={SYM:{'timestamp':stamp,'tier':0,'price_blocked':False}}
            with self.subTest(stamp=stamp):
                self.assertTrue(focus_readiness(detector(AT-timedelta(seconds=2)),shadow,AT)[SYM]['price_blocked'])


if __name__=='__main__':unittest.main()
