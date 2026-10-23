"""Prevent missing/reset/idle evidence from becoming a false capacity claim."""
from pathlib import Path
import tempfile
import unittest
from tools.measure_runtime_budget import counter_rate, distribution, heartbeat_summary, collect


class RuntimeBudgetTests(unittest.TestCase):
    def test_counter_reset_is_unknown_not_negative_throughput(self):
        self.assertIsNone(counter_rate(100,2,1))

    def test_missing_count_or_nonpositive_duration_is_unknown(self):
        for args in ((None,2,1),(0,None,1),(1,2,0),(1,2,-1)):
            self.assertIsNone(counter_rate(*args))

    def test_rate_uses_actual_interval(self):
        self.assertEqual(counter_rate(10,50,2),20)

    def test_zero_is_a_valid_observation(self):
        self.assertEqual(distribution([0,0])['median'],0)

    def test_unknown_and_nonfinite_are_not_zero(self):
        self.assertEqual(distribution([None,float('nan'),float('inf')])['n'],0)
        self.assertIsNone(distribution([])['p95'])

    def test_idle_missing_heartbeat_does_not_claim_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            row=heartbeat_summary(Path(tmp))
        self.assertIsNone(row['order_ready'])
        self.assertIsNone(row['heartbeat_age_seconds'])
        self.assertIsNone(row['runner_status'])

    def test_live_output_tree_is_rejected_before_windows_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaisesRegex(ValueError,'OUTPUT_MUST_BE_OUTSIDE_LIVE'):
                collect(root,root/'runtime/new-data',1,1)

    def test_sensitive_runner_tokens_are_not_exported(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'runtime').mkdir()
            (root/'runtime/operator_runner.json').write_text(json.dumps({'token':'SECRET','identity':'PRIVATE','pid':123,'status':'STOPPED'}))
            row=heartbeat_summary(root)
        self.assertNotIn('SECRET',str(row));self.assertNotIn('PRIVATE',str(row))
        self.assertEqual(row['runner_status'],'STOPPED')


if __name__=='__main__':unittest.main()
