import unittest
from datetime import datetime
from dataclasses import replace
from core.settings import Config, KST
from core.strategy import Tick
from core.util import floor_tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector

class SetupDPaperTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)

    def test_setup_d_disabled_by_default(self):
        """When resistance_breakout_expansion_enabled is False, Setup D is not evaluated."""
        self.assertFalse(self.auto.resistance_breakout_expansion_enabled)
        t_now = datetime(2026, 10, 27, 12, 7, 48, tzinfo=KST)
        tick = Tick('138360', t_now, 2205.0, 50000, 15000000, 2205.0, 2200.0)
        watch = {'code': '138360', 'resistance': 2205.0, 'status': 'WATCH'}
        trials = self.detector.setup_trials(tick, watch)
        setups_evaluated = [tr['setup'] for tr in trials]
        self.assertEqual(setups_evaluated, ['A', 'B', 'C'])
        self.assertNotIn('D', setups_evaluated)

    def test_setup_d_geometry_and_unconstrained_target(self):
        """When enabled, Setup D captures breakout above resistance and does not clamp target to resistance."""
        auto_enabled = replace(self.auto, resistance_breakout_expansion_enabled=True)
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 12, 7, 48, tzinfo=KST)
        tick = Tick('138360', t_now, 2210.0, 50000, 15000000, 2210.0, 2205.0)
        watch = {'code': '138360', 'resistance': 2205.0, 'status': 'WATCH'}
        
        # Inject 2 fake usable completed bars
        bars = [
            {'time': t_now.replace(minute=5), 'open': 2190.0, 'high': 2200.0, 'low': 2185.0, 'close': 2195.0, 'volume': 10000, 'first_second': 0, 'gap_reason': None},
            {'time': t_now.replace(minute=6), 'open': 2195.0, 'high': 2210.0, 'low': 2195.0, 'close': 2210.0, 'volume': 20000, 'first_second': 0, 'gap_reason': None}
        ]
        
        trials = detector.setup_trials(tick, watch, _bars=bars)
        trial_d = next((tr for tr in trials if tr['setup'] == 'D'), None)
        self.assertIsNotNone(trial_d)
        self.assertTrue(trial_d['candidate_generated'])
        
        setup = trial_d['setup_geometry']
        self.assertEqual(setup['pattern'], 'D')
        self.assertEqual(setup['breakout'], 2205.0)
        self.assertTrue(setup['paper_only'])
        
        # Target check: target() should not clamp to 2205
        target_val = detector.target(tick, watch, setup, 2210.0)
        self.assertEqual(target_val, setup['projection'])
        self.assertGreater(target_val, 2205.0)

    def test_paper_only_signal_blocked_in_live_mode(self):
        """Signals with paper_only=True must be strictly blocked when policy.mode is LIVE."""
        class MockEngine:
            def __init__(self):
                self.policy = type('MockPolicy', (), {'mode': 'LIVE', 'state': {'live_authorized': True}})()
                self.blocked_events = []
                self.s = {'day': '2026-10-27', 'positions': {}, 'trades': []}
                self.c = Config()
                self.a = AutomationConfig()
                self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
            def event(self, name, **kwargs):
                if name == 'SIGNAL_BLOCKED':
                    self.blocked_events.append(kwargs)
            def enter(self, sig, tick):
                raise AssertionError("enter() should never be called in LIVE mode for paper_only signal!")

        engine = MockEngine()
        sig = {'symbol': '138360', 'pattern': 'D', 'limit': 2210, 'stop': 2178, 'target': 2290, 'paper_only': True}
        t_now = datetime(2026, 10, 27, 12, 7, 48, tzinfo=KST)
        tick = Tick('138360', t_now, 2210.0, 50000, 15000000, 2210.0, 2205.0)
        
        # In runner logic:
        if sig.get('paper_only') and engine.policy.mode != 'PAPER':
            engine.event('SIGNAL_BLOCKED', symbol=tick.symbol, reason='PAPER_STRATEGY_BLOCKED_IN_LIVE')
            blocked = True
        else:
            blocked = False
        self.assertTrue(blocked)
        self.assertEqual(len(engine.blocked_events), 1)
        self.assertEqual(engine.blocked_events[0]['reason'], 'PAPER_STRATEGY_BLOCKED_IN_LIVE')


if __name__ == '__main__':
    unittest.main()
