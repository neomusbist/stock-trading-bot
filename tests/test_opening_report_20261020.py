"""Reported opening errors; no account, LIVE policy or strategy changes."""
from types import SimpleNamespace
import unittest
from automation.performance import summary
from automation.runner import record_opening_focus_change


class HoldingDurationReportTests(unittest.TestCase):
    def row(self,value):
        return {'holding_seconds':value,'pnl_estimate':10,'MFE':20,'MAE':1}

    def test_missing_duration_is_not_zero_or_reconciliation_duration(self):
        out=summary([self.row(None),self.row(120)])
        self.assertEqual(out['average_holding_seconds'],120)
        self.assertEqual(out['holding_seconds_known_count'],1)
        self.assertEqual(out['holding_seconds_unknown_count'],1)
        self.assertEqual(out['count'],2)

    def test_all_unknown_remains_unknown(self):
        out=summary([self.row(None)])
        self.assertIsNone(out['average_holding_seconds'])

    def test_real_zero_is_known(self):
        self.assertEqual(summary([self.row(0),self.row(10)])['average_holding_seconds'],5)

    def test_invalid_numeric_durations_are_not_averaged(self):
        out=summary([self.row(float('nan')),self.row(float('inf')),self.row(-1),self.row(True)])
        self.assertIsNone(out['average_holding_seconds'])
        self.assertEqual(out['holding_seconds_unknown_count'],4)

    def test_empty_report_still_works(self):
        self.assertEqual(summary([])['holding_seconds_known_count'],0)


class FocusEventTests(unittest.TestCase):
    def selection(self,applied):
        return SimpleNamespace(applied=applied,promoted=['X'] if applied else [],
            demoted=['Y'] if applied else [],next_watch=['X'],previous_watch=['Y'],
            primary_reason='TEST',focus_epoch='2026-10-20T09:01:00+09:00',tier_map={},priority_tuples={})

    def test_no_change_is_not_emitted_as_watch_change(self):
        calls=[];engine=SimpleNamespace(event=lambda *args,**kwargs:calls.append((args,kwargs)))
        self.assertFalse(record_opening_focus_change(engine,self.selection(False)))
        self.assertEqual(calls,[])

    def test_real_change_keeps_original_audit_evidence(self):
        calls=[];engine=SimpleNamespace(event=lambda *args,**kwargs:calls.append((args,kwargs)))
        self.assertTrue(record_opening_focus_change(engine,self.selection(True)))
        self.assertEqual(calls[0][0],('FOCUS_SELECTION_APPLIED',))
        self.assertEqual(calls[0][1]['promoted'],['X'])
        self.assertFalse(calls[0][1]['compression_batch_complete'])


if __name__=='__main__':unittest.main()
