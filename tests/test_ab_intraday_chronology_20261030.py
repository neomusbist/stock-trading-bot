"""Research-only causal boundaries; these fixtures are not market observations."""
import copy
import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from unittest.mock import patch

from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs
from core.settings import Config, KST
from core.strategy import Tick
from tests.test_structural_pullback import VIABLE_B, AT


class ABChronologyTests(unittest.TestCase):
    def setUp(self):
        self.d = PatternDetector(Config(), AutomationConfig(), Costs(.00015, .00015, .002, .002, .003))

    def tick(self, when, price, cumulative):
        return Tick('999990', when, price, 1, cumulative, price, price-1)

    def test_prior_session_high_is_frozen_within_decision_minute(self):
        start = datetime(2026, 10, 30, 10, 0, tzinfo=KST)
        samples = [(0, 1000), (1, 1010)] + [(s, 1005) for s in range(2, 60, 2)] + [(60, 1015), (61, 1020)]
        samples += [(s, 1015) for s in range(62, 120, 2)]
        for seconds, price in samples:
            tick = self.tick(start + timedelta(seconds=seconds), price, seconds+1)
            self.assertTrue(self.d.ingest(tick, tick.time))
            if seconds == 60:
                self.assertEqual(self.d.prior_high['999990'], 1010)
                self.assertGreater(tick.price, self.d.prior_high['999990'])
            if seconds == 61:
                self.assertEqual(self.d.prior_high['999990'], 1010)
        tick = self.tick(start + timedelta(seconds=120), 1020, 121)
        self.assertTrue(self.d.ingest(tick, tick.time))
        self.assertEqual(self.d.prior_high['999990'], 1020)
        self.assertFalse(tick.price > self.d.prior_high['999990'])

    def test_b_requires_impulse_then_pullback_before_recovery_proof(self):
        from automation.structural_pullback import structural_pullback
        setup, reason, evidence = self.d.pullback_analysis(VIABLE_B)
        self.assertIsNotNone(setup, reason)
        self.assertGreater(evidence['impulse_close_progress'], evidence['minimum_rise'])
        ok, proof = structural_pullback(VIABLE_B, AT, setup, self.d.cfg, 1051, 1050, 1051)
        self.assertTrue(ok, proof)
        self.assertLess(proof['impulse_bars'][-1], proof['support_touch_times'][-1])
        self.assertLess(proof['support_touch_times'][-1], proof['confirmation_bar'])
        raw = copy.deepcopy(VIABLE_B)
        raw[-1].update(low=setup['support']-1, close=setup['support'])
        revised = self.d.pullback(raw)
        self.assertIsNotNone(revised)
        ok, proof = structural_pullback(raw, AT, revised, self.d.cfg, 1051, 1050, 1051)
        self.assertFalse(ok, proof)
        self.assertEqual(proof['reason'], 'PULLBACK_SUPPORT_NOT_CONFIRMED')

    def test_a_breakout_requires_strictly_above_known_level(self):
        setup = {'pattern': 'A', 'breakout': 1000, 'support': 950, 'projection': 1100}
        watch = {'status': 'WATCH', 'resistance': 1200, 'source_details': {'a': {}, 'b': {}}}
        for price, expected in ((999, 'FAIL'), (1000, 'FAIL'), (1001, 'PASS')):
            tick = self.tick(AT, price, 1)
            gates = self.d.setup_gates(tick, watch, setup, [])
            self.assertEqual(gates['breakout']['result'], expected)

    def test_local_continuation_does_not_ignore_observed_recent_high(self):
        from automation.structural_breakout import confirmed_continuation
        from tests.test_structural_a_exit import CONTINUING, AT as CONTINUATION_AT
        d = PatternDetector(Config(), replace(AutomationConfig(), structural_breakout_enabled=True),
                            Costs(.00015, .00015, .002, .002, .003))
        for bar in CONTINUING:
            d.price_context.record('999990', bar, CONTINUATION_AT)
        d.prior_high['999990'] = 11000
        tick = self.tick(CONTINUATION_AT, 10020, 1)
        with patch('automation.structural_breakout.confirmed_continuation', wraps=confirmed_continuation) as confirm:
            setups = d.candidates(tick, CONTINUING)
        self.assertTrue(confirm.called)
        self.assertFalse(any(s['pattern'] == 'A' for s in setups))
        from tests.test_night_judgment_quality import SustainedLocalRiseTests
        common = SustainedLocalRiseTests().common()
        with patch('automation.structural_breakout.confirmed_continuation', wraps=confirmed_continuation) as confirm:
            trials = d.setup_trials(tick, {'status': 'WATCH', 'resistance': 12000}, common, _bars=CONTINUING)
        self.assertTrue(confirm.called)
        self.assertFalse(next(t for t in trials if t['setup']=='A')['candidate_generated'])


if __name__ == '__main__':
    unittest.main()
