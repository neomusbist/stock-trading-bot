"""Actual receiver / fake broker regressions for continuation proof parity."""
import copy,unittest
from datetime import timedelta
from tests import test_night_judgment_quality as night
from tests.test_subscription_refresh import SYM
from automation.decisions import gate
from automation.structural_breakout import structural_breakout

class ContinuationBoundaryTests(unittest.TestCase):
    setUp=night.LocalRiseReceiverTests.setUp
    observed=night.LocalRiseReceiverTests.observed
    frame=night.LocalRiseReceiverTests.frame
    send=night.LocalRiseReceiverTests.send

    def signal(self):
        t=self.observed();self.assertTrue(self.d.ingest(t,t.time))
        signal=self.d.signal(t,self.watch);self.assertIsNotNone(signal)
        self.assertEqual(signal['entry_basis'],'LOCAL_CONFIRMED_CONTINUATION')
        return signal,t

    def test_matching_received_and_reference_bars_reach_the_fake_broker(self):
        t=self.observed();self.send(self.frame(t),{SYM:self.watch})
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(next(iter(self.e.s['orders'].values()))['signal']['entry_basis'],'LOCAL_CONFIRMED_CONTINUATION')

    def reference_revision_reaches_broker(self,field,delta):
        t=self.observed()
        refs=self.d.price_context.reference_bars(SYM,t.time,'KRX')+[copy.deepcopy(self.d.current[SYM])]
        refs[-1][field]+=delta  # valid REST/live aggregation or OHLC revision
        self.d.price_context.merge_history(SYM,refs,t.time,'KRX')
        self.send(self.frame(t),{SYM:self.watch})
        self.assertEqual(len(self.broker.orders),1)
        sig=next(iter(self.e.s['orders'].values()))['signal']
        self.assertNotEqual(sig['decision_bar_snapshot'][-1][field],sig['reference_bar_snapshot'][-1][field])

    def test_reference_volume_revision_does_not_replace_received_proof(self):
        self.reference_revision_reaches_broker('volume',1)

    def test_reference_ohlc_revision_does_not_replace_received_proof(self):
        self.reference_revision_reaches_broker('close',-1)

    def test_missing_or_stale_structure_reference_remains_blocked(self):
        signal,t=self.signal()
        for refs in ([],signal['reference_bar_snapshot'][:-3]):
            s=copy.deepcopy(signal);s['reference_bar_snapshot']=refs
            self.assertEqual(self.e.entry_market_reason(s,1,t),'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID')
            self.assertIsNone(self.e.enter(s,t))
        self.assertEqual(self.broker.orders,[])

    def test_old_rest_receive_time_with_recent_complete_bars_is_not_a_stale_quote(self):
        signal,t=self.signal();signal['reference_received_at']=(t.time-timedelta(minutes=30)).isoformat()
        self.assertIsNone(self.e.entry_market_reason(signal,1,t))

    def test_incomplete_received_bars_cannot_be_replaced_by_good_references(self):
        signal,t=self.signal()
        for kind in ('missing','volume','gap','partial'):
            s=copy.deepcopy(signal)
            if kind=='missing':s['decision_bar_snapshot']=[]
            elif kind=='volume':s['decision_bar_snapshot'][-1]['volume']=None
            elif kind=='gap':s['decision_bar_snapshot'][-1]['gap_reason']='LOCAL_DROP'
            else:s['decision_bar_snapshot'][-1]['time']=t.time.isoformat()
            self.assertEqual(self.e.entry_market_reason(s,1,t),'CONTINUATION_PROOF_MISSING_OR_INVALID')
            self.assertIsNone(self.e.enter(s,t))
        self.assertEqual(self.broker.orders,[])

    def test_future_reference_receipt_and_expired_live_quote_still_block(self):
        signal,t=self.signal();s=copy.deepcopy(signal)
        s['reference_received_at']=(t.time+timedelta(seconds=1)).isoformat()
        self.assertEqual(self.e.entry_market_reason(s,1,t),'BREAKOUT_REFERENCE_NOT_KNOWN_AT_SIGNAL')
        self.clock+=timedelta(seconds=self.cfg.stale_seconds+1)
        self.assertEqual(self.e.entry_market_reason(signal,1,t),'ENTRY_MARKET_DATA_STALE')
        self.assertEqual(self.broker.orders,[])

    def test_near_session_high_is_rejected_in_the_actual_receiver(self):
        t=self.observed();self.d.day_high[SYM]=10060
        self.send(self.frame(t),{SYM:self.watch})
        self.assertEqual(self.broker.orders,[])
        a=self.d.last_eval[SYM]['trials'][0]
        self.assertEqual(a['gates']['risk_reward']['values']['target'],10060)
        self.assertEqual(a['gates']['risk_reward']['result'],'FAIL')

    def test_final_boundary_rejects_target_above_known_ceiling_or_insufficient_rr(self):
        signal,t=self.signal();signal['prior_session_high']=10060
        q=signal['evaluation_gates']['spread']['values']
        passed,ev=structural_breakout(signal['reference_bar_snapshot'],t.time,signal,self.cfg,signal['limit'],q['bid'],q['ask'])
        self.assertTrue(passed);self.assertEqual(ev['target_ceiling'],10060)
        signal['evaluation_gates']['breakout_structure']=gate(True,enabled=True,**ev)
        signal['invalidation_policy']={'enabled':True,**ev}
        for target in (10330,10060):
            s=copy.deepcopy(signal);s['target']=target
            self.assertEqual(self.e.entry_market_reason(s,1,t,phase='SEND'),'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID')
            self.assertIsNone(self.e.enter(s,t))
        self.assertEqual(self.broker.orders,[])

if __name__=='__main__':unittest.main()
