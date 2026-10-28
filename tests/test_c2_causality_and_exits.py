"""Causal, synthetic sealed-PAPER checks; never profitability evidence."""
import contextlib,io,json,unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from tests.test_c2_box_retest import AT,SYM,PREFIX,settings,populate,watch
from tests.test_strict_benchmark_pipeline import packet
from research.c2_validation import C2Replay,C2ObservedAuthority
from research.c2_exits import install_exit_experiment,POLICIES
from research.observed_paper import configured_c_reward_bound
from automation.trade_review_capture import TradeReviewCapture


class CausalC2Tests(unittest.TestCase):
    def setUp(self):
        self.cfg,self.auto=settings()
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__,None,None,None)

    def replay(self,policy='BASELINE',latency_ms=25):
        r=C2Replay(AT,'C2',self.cfg,self.auto,70420,latency_ms=latency_ms)
        self.addCleanup(r.close);install_exit_experiment(r,policy);return r

    def test_C_bound_is_not_a_signal_and_cannot_pass_the_unchanged_cost_profile(self):
        r=self.replay();b=configured_c_reward_bound(self.cfg,self.auto,r.account.e.costs)
        self.assertAlmostEqual(b['net_rr_upper_bound'],1.004645747741495)
        self.assertFalse(b['configured_intersection_possible'])
        self.assertEqual(b['required_net_rr'],1.5)

    def test_future_reference_availability_cannot_supply_a_prior_target(self):
        r=self.replay()
        original=r.detector._arm
        def later_reference(t,bars,w):
            r.detector.price_context.rows[SYM]['history_received_at']=t.time+timedelta(seconds=1)
            return original(t,bars,w)
        with patch.object(r.detector,'_arm',side_effect=later_reference):order,_=populate(r)
        self.assertIsNone(order)
        self.assertTrue(any(e['reason']=='C2_REFERENCE_NOT_YET_KNOWN' for e in r.detector.events))

    def test_near_ceiling_never_vanishes_after_decision_price_crosses_it(self):
        r=self.replay();order,f=populate(r,resistance=10100)
        self.assertIsNone(order);self.assertEqual(r.detector.boxes[SYM]['target'],10100)
        for i,price in enumerate((10080,10110),1):
            at=f.available_at+timedelta(seconds=i);r.qualify(SYM,watch(at,11000),at)
            self.assertIsNone(r.step(packet(at,price,f.tick.cumulative+5000*i,5000,symbol=SYM,depth=10000)))
            self.assertEqual(r.detector.boxes[SYM]['target'],10100)
        self.assertEqual(r.detector.boxes[SYM]['state'],'INVALID')

    def test_HARD_LOCK_is_not_overridden_by_a_C2_signal(self):
        r=self.replay();r.account.e.s['excluded'].append(SYM)
        order,_=populate(r)
        self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])

    def test_no_actual_context_is_not_an_orderable_market_signal(self):
        r=self.replay();a=C2ObservedAuthority(r.account)
        original=r.step
        def observed(f,**kw):a.frame=f;return original(f,**kw)
        with patch.object(r,'step',side_effect=observed):order,_=populate(r)
        self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])
        self.assertGreater(a.counts['OBSERVED_CONTEXT_UNKNOWN'],0)
        self.assertTrue(any(s.get('detector_ready') for s in r.detector.episode_summary.values()))

    def test_independent_research_gates_need_complete_raw_authority_not_a_legacy_C_signal(self):
        source=self.replay();e=source.account.e
        c=TradeReviewCapture(source.account.root/'trace',start=False,clock=e.clock)
        self.addCleanup(c.close);e.trade_review=c;c.set_scope({SYM:watch(AT)},{},AT)
        _,frame=populate(source)
        row=next(json.loads(x) for x in reversed(c.pending)
                 if json.loads(x).get('event')=='ENTRY_DECISION_EVIDENCE' and json.loads(x).get('final_decision')=='BUY')
        row['authority'].update(market_state='3',session='REGULAR',balance_refresh_uncertain=False,
            readiness=dict(known_at=frame.available_at.isoformat(),order_ready=True,reason_codes=[]))
        # A different hypothesis must compute its own endogenous gates; raw
        # exogenous LIVE/account/receipt facts remain mandatory.
        row['final_required_gate_results']={'pattern':'FAIL'};row['final_gate_results']={'pattern':'FAIL'}
        row['account']['quantity']=0;row['selected_setup']=None
        for missing in (None,'account','readiness','book'):
            r=self.replay();a=C2ObservedAuthority(r.account);test=deepcopy(row)
            if missing=='account':test['account']['snapshot_at']=None
            if missing=='readiness':test['authority']['readiness']={}
            if missing=='book':test['book_received_at']=None
            a.update(test);original=r.step
            def observed(f,**kw):a.frame=f;return original(f,**kw)
            with patch.object(r,'step',side_effect=observed):order,_=populate(r)
            if missing is None:self.assertIsNotNone(order,dict(a.counts))
            else:self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])

    def test_stale_book_and_missing_depth_do_not_create_ready_orders(self):
        for kind in ('book','depth'):
            r=self.replay();original=r.step
            def changed(f,**kw):
                if f.tick.time>=AT+timedelta(minutes=len(PREFIX)):
                    t=replace(f.tick,book_time=f.tick.time-timedelta(seconds=6)) if kind=='book' else replace(f.tick,depth_available=False,ask_qty=0)
                    f=replace(f,tick=t)
                return original(f,**kw)
            with patch.object(r,'step',side_effect=changed):order,_=populate(r)
            self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])

    def test_paired_exit_policies_keep_the_same_entry_and_price_gaps_are_not_filled_at_a_stop(self):
        signatures=[];outputs={}
        for policy in POLICIES:
            r=self.replay(policy);order,f=populate(r)
            self.assertIsNotNone(order)
            for sec,price in ((1,10050),(2,10400),(3,9890),(4,9890)):
                r.step(packet(f.available_at+timedelta(seconds=sec),price,f.tick.cumulative+sec*5000,5000,symbol=SYM,depth=10000))
            out=r.result();outputs[policy]=out
            signatures.append([(x['at'],x['price'],x['qty']) for x in out['fills'] if x['side']=='BUY'])
            sell=next(x for x in out['fills'] if x['side']=='SELL')
            self.assertLessEqual(sell['price'],9890)
            self.assertLess(out['net_pnl_closed'],0)
        self.assertTrue(all(s==signatures[0] for s in signatures))
        self.assertEqual(len(signatures[0]),1)

    def test_pivot_is_confirmed_by_the_right_bar_and_never_lowers_a_line(self):
        r=self.replay('CONFIRMED_PIVOT_TRAIL');_,f=populate(r)
        r.step(packet(f.available_at+timedelta(seconds=1),10050,f.tick.cumulative+5000,5000,symbol=SYM,depth=10000))
        e=r.account.e;p=e.s['positions'][SYM];first=p['stop']
        for i,low in enumerate((10200,10100),1):
            e.observe_protection_bar(SYM,dict(time=AT+timedelta(minutes=10+i),low=low,high=10400,close=10300))
        self.assertEqual(p['stop'],first)
        e.observe_protection_bar(SYM,dict(time=AT+timedelta(minutes=13),low=10200,high=10400,close=10300))
        self.assertEqual(p['stop'],10090)
        self.assertEqual(p['research_exit_activation']['confirmed_by'],(AT+timedelta(minutes=13)).isoformat())
        for i,low in enumerate((10000,9900,10000),14):
            e.observe_protection_bar(SYM,dict(time=AT+timedelta(minutes=i),low=low,high=10400,close=10300))
        self.assertEqual(p['stop'],10090)

    def test_exit_experiment_rejects_unsealed_engine(self):
        r=self.replay();r.account.e.paper_mode=False
        with self.assertRaisesRegex(ValueError,'SEALED_PAPER_REQUIRED'):install_exit_experiment(r,'INITIAL_STRUCTURE')

    def test_efficiency_rejection_is_not_reported_as_a_generated_signal(self):
        r=C2Replay(AT,'C2',self.cfg,replace(self.auto,min_efficiency_score=10000),70420)
        self.addCleanup(r.close);order,_=populate(r)
        self.assertIsNone(order)
        trial=r.detector.last_eval[SYM]['trials'][0]
        self.assertFalse(trial['entry_ready'])
        self.assertIn('C2_EXISTING_EFFICIENCY_REJECTED',trial['failed_reasons'])
        self.assertEqual(r.result()['c2_stage_counts']['detector_ready'],0)


if __name__=='__main__':unittest.main()
