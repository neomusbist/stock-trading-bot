"""Corrected integration: real engine entry/guard/fill/exit, isolated PAPER.

The previous AG test called broker.submit directly and manually inserted and
removed a position. It did not test the engine. The original remains in Git
8174b5d; these tests intentionally do not reproduce that invalid shortcut.
"""
import unittest
from dataclasses import replace
from datetime import timedelta
from tests.test_causal_replay import AT,SYM,bars,frame
from core.settings import Config
from automation.policy import AutomationConfig
from automation.local_breakout import LocalBreakoutStrategy,LocalBreakoutSignal
from research.causal_account import PaperAccount
from research.retest_strategy import ConfirmedRetestStrategy,paper_engine_signal

class LocalBreakoutE2EIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,
            max_position_fraction=1.,max_positions=1,max_entries_per_symbol=None,max_entries_per_day=None)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,confirmed_invalidation_enabled=True,costs_confirmed=True,
            exit_plan_reward_guard_enabled=True,max_consecutive_losses=None)
        self.a=PaperAccount(AT,initial_cash=71822,cfg=self.cfg,auto=self.auto)
        self.addCleanup(self.a.close)
        self.a.step(frame(),market_status={'pass':True,'checked_at':AT.isoformat()})
        self.source={'source_count':2,'timestamp':AT.isoformat()}

    def test_legacy_projected_target_must_still_pass_common_engine_reward_guard(self):
        bs=bars()[-10:]
        for b in bs:b.update(open=995,high=1000,low=982,close=997)
        strategy=LocalBreakoutStrategy(self.cfg,self.auto,self.a.e.costs)
        s,why=strategy.evaluate(frame().tick,bs,{},71822,1436.44,AT)
        self.assertIsNone(s)
        self.assertEqual(why['reason'],'COMMON_ENGINE_REWARD_GUARD')
        # Explicit pre-fix AG signal counterexample. Only the signal is supplied;
        # order intent, sizing, validation and any position must be engine-made.
        legacy=LocalBreakoutSignal(SYM,'LEGACY_TEST_COUNTEREXAMPLE',1004,981,1050,976,43,
            1.158,1.642,38.43,33.17,created_at=AT,max_chase_price=1005,
            extra={'SHADOW_ONLY':True,'actual_order_permission':False})
        signal=paper_engine_signal(legacy,self.cfg,self.auto,self.a.e.costs,source_evidence=self.source)
        self.assertIsNone(self.a.enter(signal))
        self.assertEqual(self.a.broker.submissions,[])
        self.assertEqual(self.a.e.s['positions'],{})
        log=(self.a.e.folder/'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('EXIT_PLAN_REWARD_UNKNOWN_OR_LOW',log)

    def test_confirmed_retest_reaches_pre_http_then_engine_fill_and_structural_exit(self):
        strategy=ConfirmedRetestStrategy(self.cfg,self.auto,self.a.e.costs)
        watch={'nearest_confirmed_peak':{'price':1100,'observation_source':'ACTUAL_RECEIVED',
            'available_at':(AT-timedelta(minutes=20)).isoformat(),'confirmed_at':(AT-timedelta(minutes=20)).isoformat()}}
        s,why=strategy.evaluate(frame().tick,bars(),watch,71822,1436.44,AT)
        self.assertIsNotNone(s,why)
        order=self.a.enter(paper_engine_signal(s,self.cfg,self.auto,self.a.e.costs,source_evidence=self.source))
        self.assertEqual(order['status'],'FILLED')
        p=self.a.e.s['positions'][SYM]
        self.assertLess(p['signal']['invalidation_policy']['emergency_stop'],s.stop)
        # A normal one-tick pullback is not a fabricated TARGET_HIT sell.
        self.a.step(frame(AT+timedelta(seconds=1),1003))
        self.assertIn(SYM,self.a.e.s['positions'])
        known=AT+timedelta(minutes=2)
        bar={'time':AT+timedelta(minutes=1),'open':1003,'high':1004,'low':998,
            'close':998,'volume':1000,'first_second':0,'observation_source':'ACTUAL_RECEIVED',
            'available_at':known.isoformat()}
        self.a.step(frame(known,998),bar_events=[(SYM,bar)])
        self.assertEqual([o['side'] for o in self.a.broker.submissions],['BUY','SELL'])
        self.assertNotIn(SYM,self.a.e.s['positions'])
        result=self.a.result()
        self.assertEqual(result['completed_count'],1)
        self.assertIn('구조적 무효화',result['completed_trades'][0]['reason'])
        self.assertFalse(result['actual_order_permission'])

if __name__=='__main__':unittest.main()
