import copy,unittest
from datetime import datetime,timedelta
from core.settings import Config,KST
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.entry_context_audit import confirmed_overhead,review_entry_context

class EntryContextTests(unittest.TestCase):
    def setUp(self):
        self.stamp=datetime(2026,10,12,12,42,tzinfo=KST);auto=AutomationConfig();cfg=Config()
        self.costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,cfg.entry_slippage_fraction,cfg.exit_slippage_fraction)
    def bars(self,highs,start=None):
        start=start or self.stamp-timedelta(minutes=len(highs))
        return [{'time':(start+timedelta(minutes=i)).isoformat(),'high':high} for i,high in enumerate(highs)]
    def signal(self):
        return {'time':self.stamp.isoformat(),'limit':7590,'stop':7460,'target':7800,
            'decision_bar_snapshot':self.bars([7590,7630,7610,7500,7510]),
            'efficiency':{'inputs':{'confidence':.8,'planned_minutes':20}}}
    def test_known_peak_caps_diagnostic_rr_without_changing_signal_or_stop(self):
        signal=self.signal();before=copy.deepcopy(signal)
        view=review_entry_context(signal,self.costs,1.5,.1)
        self.assertEqual(view['reviewed_target'],7630);self.assertAlmostEqual(view['reviewed_rr'],40/130)
        self.assertFalse(view['reviewed_rr_pass']);self.assertEqual(signal,before)
        self.assertFalse(view['live_gate_enabled']);self.assertFalse(view['actual_order_permission'])
    def test_future_partial_bar_cannot_confirm_peak_or_change_result(self):
        bars=self.bars([7590,7630],self.stamp-timedelta(minutes=2))
        bars+=self.bars([7610],self.stamp)
        self.assertIsNone(confirmed_overhead(bars,self.stamp,7590))
    def test_actual_time_gap_and_snapshot_endpoints_are_not_confirmed_peaks(self):
        bars=self.bars([7630,7620,7610]);self.assertIsNone(confirmed_overhead(bars,self.stamp,7590))
        bars=self.bars([7590,7630,7610]);bars[0]['time']=(self.stamp-timedelta(minutes=5)).isoformat()
        self.assertIsNone(confirmed_overhead(bars,self.stamp,7590))
    def test_crossed_peak_and_flat_plateau_are_not_overhead_resistance(self):
        self.assertIsNone(confirmed_overhead(self.bars([7500,7590,7580]),self.stamp,7590))
        self.assertIsNone(confirmed_overhead(self.bars([7630,7630,7630]),self.stamp,7590))
    def test_no_peak_does_not_claim_whole_day_has_no_resistance(self):
        signal=self.signal();signal['decision_bar_snapshot']=[]
        view=review_entry_context(signal,self.costs,1.5,.1)
        self.assertEqual(view['reviewed_target'],7800);self.assertIsNone(view['known_day_high'])
        self.assertEqual(view['status'],'NO_CONFIRMED_PEAK_IN_SNAPSHOT')
    def test_invalid_evidence_is_unknown_and_never_authorizes_order(self):
        view=review_entry_context({},self.costs,1.5,.1)
        self.assertEqual(view['status'],'UNKNOWN');self.assertFalse(view['actual_order_permission'])

if __name__=='__main__':unittest.main()
