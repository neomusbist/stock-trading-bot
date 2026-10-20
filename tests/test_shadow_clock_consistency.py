"""Residual SHADOW freshness path shares the proven receipt-time guard."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch
from automation.capital import Costs
from automation.policy import AutomationConfig
from automation.shadow_observation import ShadowObserver
from core.settings import Config,KST
from core.strategy import Tick

AT=datetime(2026,10,19,11,33,25,tzinfo=KST)


class ShadowClockTests(unittest.TestCase):
    def observer(self):
        events=[]
        s=ShadowObserver(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003),
                         lambda kind,**data:events.append((kind,data)))
        s.update([{'code':'079650','name':'synthetic clock control','source_count':2,
            'status':'WATCH','affordability':{'entry_eligible':True},
            'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True},'resistance':8600}}],
            AT-timedelta(seconds=1))
        return s,events

    def tick(self,received,counter=None):
        return Tick('079650',AT,7950,10,100,7950,7940,True,
                    ask_qty=100,bid_qty=100,depth_available=True,book_time=received,
                    received_at=received.isoformat(),received_monotonic=counter)

    def test_previously_fixed_small_exchange_leads_also_reach_shadow_evaluation(self):
        for lead in (.051,.293):
            s,events=self.observer();received=AT-timedelta(seconds=lead)
            s.tick(self.tick(received),received,transport_received=received)
            self.assertIn('079650',s.latest)
            self.assertTrue(any(k=='SHADOW_EVAL' for k,_ in events))
            self.assertTrue(all(v.get('actual_order_permission') is not True for _,v in events))

    def test_abnormal_future_or_real_stale_receive_is_still_rejected(self):
        for received in (AT-timedelta(seconds=1.001),AT+timedelta(seconds=6)):
            s,_=self.observer();s.tick(self.tick(received),received,transport_received=received)
            self.assertNotIn('079650',s.latest)
            self.assertNotIn('079650',s.detector.last)

    def test_valid_receiver_history_cannot_publish_current_readiness_after_queue_delay(self):
        received=AT+timedelta(seconds=.2);processed=AT+timedelta(seconds=6)
        s,_=self.observer();s.tick(self.tick(received),processed,transport_received=received)
        self.assertIn('079650',s.detector.last)
        self.assertNotIn('079650',s.latest)

    def test_expired_receive_counter_cannot_be_revived_by_wall_clock_correction(self):
        received=AT+timedelta(seconds=.2);s,_=self.observer()
        with patch('core.market_time.time.monotonic',return_value=106.2):
            s.tick(self.tick(received,100.2),received,transport_received=received)
        self.assertIn('079650',s.detector.last)
        self.assertNotIn('079650',s.latest)

    def test_coverage_gap_never_creates_ready_state(self):
        received=AT+timedelta(seconds=.2);s,_=self.observer()
        s.tick(self.tick(received),received,transport_received=received,coverage_complete=False)
        self.assertNotIn('079650',s.latest)
        self.assertFalse(hasattr(s,'enter'))

    def test_research_bar_knowledge_uses_processing_without_mutating_abc_history(self):
        s,_=self.observer();start=AT.replace(second=0)
        cumulative=0
        for second in range(0,61,5):
            stamp=start+timedelta(seconds=second);cumulative+=10
            t=Tick('079650',stamp,7950,10,cumulative,7950,7940,True,
                ask_qty=100,bid_qty=100,depth_available=True,depth_source='0D',book_time=stamp,
                received_at=stamp.isoformat(),book_received_at=stamp.isoformat())
            processed=stamp+timedelta(seconds=3)
            s.tick(t,processed,transport_received=stamp)
        self.assertEqual(s.research_bar_knowledge['079650'][start],
            (start+timedelta(seconds=63)).isoformat())
        self.assertNotIn('available_at',s.detector.history['079650'][-1])
        s.reset_stream('TEST_GAP',symbol='079650',stamp=processed)
        self.assertNotIn('079650',s.research_bar_knowledge)


if __name__=='__main__':unittest.main()
