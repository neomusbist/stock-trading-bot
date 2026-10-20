"""Real Tick FIFO regressions; no order transport, invented fills or relaxed gates."""
from datetime import datetime, timedelta
from queue import Queue
from types import SimpleNamespace
import unittest

from automation import runner
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from core.settings import Config, KST
from core.strategy import Tick

AT = datetime(2026, 10, 20, 9, 2, 59, tzinfo=KST)
SYM = '999990'


class DeliveryFairnessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.engine = SimpleNamespace(c=self.cfg, s={'positions': {}, 'automation': {'existing': {}}},
            active_orders=lambda: [], clock=lambda: AT, fast_strategy=None, event=lambda *a, **k: None)
        self.detector = PatternDetector(self.cfg, AutomationConfig(), Costs(.00015,.00015,.002,.002,.003))
        self.detector.continuous_observation = True
        self.feed = SimpleNamespace(q=Queue(), observation_q=Queue(), observation_symbols=frozenset({SYM}),
            latest_ticks={}, observation_drops=0, transport_failed=False,
            overflow=SimpleNamespace(is_set=lambda: False), subscription_lock=None,
            configure_observation=lambda *a: None, _opportunity_pending={}, _opportunity_triggers={})

    def tick(self, cumulative=1000, stamp=AT):
        return Tick(SYM, stamp, 1000., 100, cumulative, 1000., 999.)

    def enqueue(self, tick):
        self.feed.latest_ticks[SYM] = tick
        self.feed.observation_q.put((tick.time, tick))

    def handoff(self, callback, priority=frozenset(), prior=frozenset(), shadow_detector=None):
        return runner.prewarm_handoff(self.engine,self.detector,self.feed,{SYM}-set(priority),
            set(priority),set(prior),0,on_opportunity=callback,shadow_detector=shadow_detector)

    def ordinary_frame(self):
        return (AT, {'trnm':'REAL','data':[{'type':'0B','item':'999980','values':{}}]})

    def test_ordinary_watch_backlog_does_not_starve_caught_up_shadow(self):
        self.feed.q.put(self.ordinary_frame()); self.enqueue(self.tick())
        seen=[]; self.handoff(lambda t,r: seen.append(t))
        self.assertEqual(seen,[self.tick()])
        self.assertEqual(self.feed.q.qsize(),1)

    def test_control_behind_ordinary_watch_still_precedes_history_and_evaluation(self):
        self.feed.q.put(self.ordinary_frame())
        self.feed.q.put((AT,{'trnm':'DISCONNECTED'}));self.enqueue(self.tick())
        seen=[];self.handoff(lambda t,r: seen.append(t))
        self.assertEqual(seen,[])
        self.assertNotIn(SYM,self.detector.last)
        self.assertEqual(self.feed.observation_q.qsize(),1)

    def test_position_tick_behind_ordinary_watch_has_priority(self):
        self.engine.s['positions']['999981']={'qty':1}
        self.feed.q.put(self.ordinary_frame())
        self.feed.q.put((AT,{'trnm':'REAL','data':[{'type':'0B','item':'999981'}]}))
        self.enqueue(self.tick());self.handoff(lambda *a:self.fail('protected frame pending'))
        self.assertNotIn(SYM,self.detector.last)

    def test_same_pending_frame_is_evaluated_only_once(self):
        tick=self.tick();self.enqueue(tick);seen=[]
        self.handoff(lambda t,r:seen.append(t))
        self.feed._opportunity_pending[SYM]=(AT,tick)
        runner.drain_observed_opportunities(self.engine,self.feed,lambda t,r:seen.append(t))
        self.assertEqual(seen,[tick])

    def test_position_created_by_first_callback_preempts_second_candidate(self):
        from dataclasses import replace
        first=self.tick();second=replace(first,symbol='999991')
        self.feed.latest_ticks={SYM:first,'999991':second}
        self.feed._opportunity_pending={SYM:(AT,first),'999991':(AT,second)}
        self.feed.q.put(self.ordinary_frame());seen=[]
        def evaluate(tick,received):
            seen.append(tick.symbol)
            self.engine.s['positions']['999980']={'qty':1}
        runner.drain_observed_opportunities(self.engine,self.feed,evaluate)
        self.assertEqual(seen,[SYM])
        self.assertIn('999991',self.feed._opportunity_pending)

    def test_promotion_hands_latest_fifo_tick_to_existing_callback_once(self):
        first=self.tick(1000,AT-timedelta(seconds=1));latest=self.tick(1100)
        self.enqueue(first);self.enqueue(latest);seen=[]
        self.handoff(lambda t,r:seen.append(t),priority={SYM})
        self.assertEqual(self.detector.last[SYM],latest)
        self.assertEqual(seen,[latest])
        self.handoff(lambda t,r:seen.append(t),priority={SYM},prior={SYM})
        self.assertEqual(seen,[latest])

    def test_stale_promotion_builds_history_without_current_evaluation(self):
        old=self.tick(1000,AT-timedelta(seconds=6));self.enqueue(old);seen=[]
        self.handoff(lambda t,r:seen.append(t),priority={SYM})
        self.assertEqual(seen,[])
        self.assertEqual(self.detector.last[SYM],old)

    def test_copied_shadow_history_still_delivers_its_queued_promotion_once(self):
        shadow=PatternDetector(self.cfg,AutomationConfig(),Costs(.00015,.00015,.002,.002,.003))
        shadow.continuous_observation=True
        first=self.tick(1000,AT-timedelta(seconds=1));latest=self.tick(1100)
        for tick in (first,latest):
            shadow.ingest(tick,tick.time);self.enqueue(tick)
        seen=[]
        self.handoff(lambda t,r:seen.append(t),priority={SYM},shadow_detector=shadow)
        self.assertEqual(self.detector.last[SYM],latest)
        self.assertEqual(seen,[latest])

    def test_newer_receiver_tick_cannot_evaluate_an_old_fifo_prefix(self):
        first=self.tick(1000);latest=self.tick(1100)
        self.feed._opportunity_pending[SYM]=(AT,first)
        self.enqueue(latest);seen=[]
        runner.drain_observed_opportunities(self.engine,self.feed,lambda t,r:seen.append(t))
        self.assertEqual(seen,[])
        self.handoff(lambda t,r:seen.append(t))
        self.assertEqual(seen,[latest])


if __name__=='__main__':unittest.main()
