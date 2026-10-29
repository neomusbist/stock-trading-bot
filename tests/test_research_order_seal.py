"""Safety contract for PAPER-only research at the actual engine boundaries."""
from unittest import TestCase
from unittest.mock import patch
from core.broker import Rejected
from tests import test_subscription_refresh as refresh_tests

SYM=refresh_tests.SYM


class ResearchOrderSealTests(TestCase):
    def setUp(self):
        self.h=refresh_tests.SubscriptionRefreshTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def signals(self):
        for pattern in ('D','E','E2'):
            yield {**self.h.signal(),'pattern':pattern,'paper_only':True}
        yield {**self.h.signal(),'pattern':'C','research_strategy':'C2'}

    def test_real_order_authority_rejects_each_research_signal_even_if_flags_change(self):
        e=self.h.e
        with patch('automation.storage_governor.check_disk_safety_gate',return_value={'order_blocked':False}):
            for original in self.signals():
                for flag in (False,True):
                    with self.subTest(pattern=original['pattern'],fake_flag=flag):
                        signal={**original,'paper_only':False}
                        e.paper_mode=flag
                        self.assertFalse(e.live_order_allowed('BUY',SYM,1,10020,signal))
        self.assertEqual(self.h.broker.orders,[])

    def test_transport_guard_rejects_research_even_if_intent_exists(self):
        e=self.h.e
        for signal in self.signals():
            with self.subTest(pattern=signal['pattern']):
                o=dict(local='test-local',id='',symbol=SYM,side='BUY',qty=1,limit=10020,status='INTENT',signal=signal)
                e.s['orders']={'test-local':o}
                with self.assertRaisesRegex(Rejected,'RESEARCH_PAPER_ONLY'):
                    e.order_guard('kt10000',{'stk_cd':SYM,'ord_qty':'1','ord_uv':'10020'})
        self.assertEqual(self.h.broker.orders,[])

    def test_submit_cannot_hand_research_signal_to_base_order_engine(self):
        e=self.h.e
        with patch.object(e,'new_entry_reason',return_value=None),patch('core.engine.Engine.submit',return_value={'status':'ACCEPTED','local':'mock-local'}) as submit:
            for signal in self.signals():
                with self.subTest(pattern=signal['pattern']):
                    e.entry_intent=signal
                    self.assertIsNone(e.submit('BUY',SYM,1,10020,signal))
            submit.assert_not_called()
        self.assertEqual(self.h.broker.orders,[])

    def test_enter_blocks_research_and_preserves_reason_before_any_order(self):
        e=self.h.e
        with patch.object(e.decisions,'emit') as emit:
            for signal in self.signals():
                self.assertIsNone(e.enter(signal,self.h.tick()))
                self.assertEqual(emit.call_args.args[3],'RESEARCH_PAPER_ONLY')
        self.assertEqual(self.h.broker.orders,[])

    def test_sealed_fixture_only_and_protective_sell_are_separate(self):
        e=self.h.e
        signal=next(self.signals())
        self.assertTrue(e.live_order_allowed('SELL',SYM,1,10020,signal))
        self.assertTrue(e.research_paper_allowed({**signal,'pattern':'A','paper_only':False}))
        sealed_broker=type('QuoteReplayBroker',(),{'__module__':'research.causal_account'})()
        e.broker=sealed_broker;e.paper_mode=True
        self.assertFalse(e.research_paper_allowed(signal))
        e.policy.state['PAPER_FIXTURE_ONLY']=True
        self.assertTrue(e.research_paper_allowed(signal))
        e.paper_mode=False
        self.assertFalse(e.research_paper_allowed(signal))

