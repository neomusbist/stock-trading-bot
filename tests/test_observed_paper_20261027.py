"""Observed-context admission and actual production PAPER path, no network."""
import contextlib,io,json,tempfile,unittest
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from automation.trade_review_capture import TradeReviewCapture
from research.strict_benchmark import FullPathReplay
from research.observed_paper import ObservedAuthority,ObservedPaperSession,configured_c_reward_bound
from research.causal_data import json_values
from tests.test_strict_benchmark_pipeline import AT,SYMBOL,settings,populate,packet,metadata


class ObservedPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__,None,None,None)
        self.cfg,self.auto=settings()

    def replay(self,name='capture'):
        replay=FullPathReplay(AT-timedelta(minutes=5),'E2',self.cfg,self.auto,70420)
        self.addCleanup(replay.close)
        capture=TradeReviewCapture(self.root/name,start=False,clock=lambda:replay.account.at)
        self.addCleanup(capture.close)
        replay.account.e.trade_review=capture
        capture.set_scope({SYMBOL:metadata(AT-timedelta(minutes=5))},{},AT-timedelta(minutes=5))
        return replay,capture

    def source_evidence(self):
        r,c=self.replay('source');cum=populate(r);r.qualify(SYMBOL,metadata(AT),AT)
        self.assertIsNotNone(r.step(packet(AT,38100,cum+20000)))
        rows=[json.loads(x) for x in c.pending]
        row=next(x for x in reversed(rows) if x['event']=='ENTRY_DECISION_EVIDENCE' and x['final_decision']=='BUY')
        return row,cum,rows

    def test_evidence_has_price_account_final_gates_and_intent_link(self):
        row,cum,rows=self.source_evidence()
        self.assertEqual(row['cumulative'],cum+20000)
        self.assertTrue(row['receipt_evidence']['trade_receipt_id'])
        self.assertEqual(row['account']['quantity'],1)
        self.assertEqual(row['account']['slots'],0)  # pre-intent, not reserved afterward
        self.assertTrue(all(x=='PASS' for x in row['authority']['observed_final_gates'].values()))
        self.assertEqual(row['producer_execution'],'PAPER')
        self.assertEqual(row['input_evidence_basis'],'SYNTHETIC_TEST_QUOTE')
        self.assertEqual([s['setup'] for s in row['strategies']],['A','B','C','D','E2'])
        self.assertEqual(next(s for s in row['strategies'] if s['setup']=='D')['status'],'DISABLED')
        orders=[r for r in rows if r['event']=='ORDER_PATH_EVIDENCE']
        self.assertTrue(any(r['observed_intent_persisted'] for r in orders))
        self.assertTrue(any(r['broker_order_id'].startswith('PAPER-') for r in orders if r['broker_order_id']))

    def test_observed_engine_buy_fill_and_protective_exit_without_network(self):
        row,cum,_=self.source_evidence();r,c=self.replay('observed');populate(r)
        authority=ObservedAuthority(r.account);authority.update(row)
        r.qualify(SYMBOL,metadata(AT),AT)
        f=packet(AT,38100,cum+20000);authority.frame=f
        with patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK')):
            self.assertIsNotNone(r.step(f))
            for sec,price in enumerate((38100,40200,39500,39450),1):
                f=packet(AT+timedelta(seconds=sec),price,cum+20000*(sec+1));authority.frame=f;r.step(f)
        result=r.result()
        self.assertEqual([x['side'] for x in result['fills']],['BUY','SELL'])
        self.assertAlmostEqual(result['net_pnl_closed'],1209.575)
        self.assertEqual(result['completed_trades'][0]['MFE'],2100)
        self.assertEqual(result['completed_trades'][0]['MAE'],0)
        self.assertTrue(result['completed_trades'][0]['reason'])
        self.assertFalse(result['actual_order_permission'])

    def test_missing_authority_is_unknown_and_cannot_make_a_fake_buy(self):
        r,c=self.replay();cum=populate(r);authority=ObservedAuthority(r.account)
        r.qualify(SYMBOL,metadata(AT),AT);f=packet(AT,38100,cum+20000);authority.frame=f
        self.assertIsNone(r.step(f));self.assertEqual(r.account.broker.submissions,[])
        self.assertGreater(authority.counts['OBSERVED_CONTEXT_UNKNOWN'],0)

    def test_observed_cash_caps_quantity_even_if_paper_has_more_cash(self):
        row,cum,_=self.source_evidence();row['account']['actual_cash']=38000
        r,c=self.replay('small');populate(r);authority=ObservedAuthority(r.account);authority.update(row)
        r.qualify(SYMBOL,metadata(AT),AT);f=packet(AT,38100,cum+20000);authority.frame=f
        self.assertIsNone(r.step(f));self.assertEqual(r.account.broker.submissions,[])

    def test_receipt_mismatch_stale_account_and_paper_authority_are_blocked(self):
        source,cum,_=self.source_evidence()
        for mode in ('receipt','account','paper_output','policy','quantity','native_risk_gate','setup','book_receipt','signal_price'):
            with self.subTest(mode=mode):
                r,c=self.replay(mode);populate(r);authority=ObservedAuthority(r.account);row=deepcopy(source)
                if mode=='receipt':row['cumulative']-=1
                if mode=='account':row['account']['snapshot_at']=(AT-timedelta(seconds=16)).isoformat()
                if mode=='paper_output':row['input_evidence_basis']='MEASURED_PAIRED_TRADE_QUOTE'
                if mode=='policy':row['authority']['observed_final_gates']['policy']='UNKNOWN'
                if mode=='quantity':row['account']['quantity']=0
                if mode=='native_risk_gate':row['final_gate_results']['risk_quantity']='FAIL'
                if mode=='setup':row['selected_setup']='A'
                if mode=='book_receipt':row['book_received_at']=(AT-timedelta(seconds=1)).isoformat()
                if mode=='signal_price':row['order_limit']=38150
                authority.update(row);r.qualify(SYMBOL,metadata(AT),AT)
                f=packet(AT,38100,cum+20000);authority.frame=f
                self.assertIsNone(r.step(f));self.assertEqual(r.account.broker.submissions,[])

    def test_book_retry_does_not_ingest_same_trade_twice(self):
        s=ObservedPaperSession(AT,self.cfg,self.auto,70420,self.root/'session',start_writer=False)
        try:
            s.feed(dict(event='WATCH_METADATA',symbol=SYMBOL,active=True,watch=metadata(AT),known_at=AT.isoformat()))
            f=packet(AT,38100,20000);raw=json_values(asdict(f.tick))
            raw.update(event='TRADE',processed_at=AT.isoformat(),input_evidence_basis='SYNTHETIC_TEST_QUOTE')
            s.feed(raw,'trade')
            later=(AT+timedelta(seconds=1)).isoformat()
            s.feed(dict(event='BOOK',symbol=SYMBOL,exchange_at=AT.isoformat(),received_at=later,
                        best_bid=38050,best_ask=38100,bid_qty=1000,ask_qty=1000,depth_available=True,source='0D'),'book')
            self.assertEqual(len(s.replay.detector.buffers[SYMBOL]),1)
            self.assertEqual(s.counts['QUOTE_RETRY'],1)
            self.assertEqual(s.replay.account.broker.submissions,[])
        finally:s.close()
        self.assertTrue((self.root/'session/paper-book-final.json').is_file())

    def test_queue_limit_is_visible_without_synchronous_flush_or_order(self):
        r,c=self.replay();cum=populate(r);r.account.at=AT
        c.queue_rows=4;c.pending.clear();c.pending_size=0
        t=packet(AT,38100,cum+20000).tick;c.bind_entry_context(t,metadata(AT),r.detector,AT)
        before=len(r.account.broker.submissions)
        with patch.object(c,'flush',side_effect=AssertionError('NO_HOT_PATH_DISK_IO')):
            for i in range(20):c.decision_evidence(r.account.e,SYMBOL,'NO_BUY','REASON_'+str(i),{},None,{})
        self.assertLessEqual(len(c.pending),4);self.assertGreater(c.losses,0)
        self.assertEqual(len(r.account.broker.submissions),before)

    def test_no_buy_evidence_binds_the_quote_actually_used_after_merge(self):
        r,c=self.replay('merged_quote');cum=populate(r);r.qualify(SYMBOL,metadata(AT),AT)
        from automation.runner import current_entry_quote
        def widened_quote(e,t):
            return replace(current_entry_quote(e,t),ask=38600,ask_qty=777)
        with patch('automation.runner.current_entry_quote',side_effect=widened_quote):
            self.assertIsNone(r.step(packet(AT,38100,cum+20000)))
        rows=[json.loads(x) for x in c.pending]
        rows=[x for x in rows if x['event']=='ENTRY_DECISION_EVIDENCE']
        self.assertEqual(rows[-1]['ask_qty'],777)
        self.assertEqual(rows[-1]['ask'],38600)

    def test_old_gross_only_C_fixture_remains_a_cost_rejection(self):
        from tests import test_live_ready_20261021 as fixture
        h=fixture.LiveReadyLifecycle('runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.seed('C')
        self.assertEqual(h.broker.orders,[])
        trial=next(tr for tr in h.detector.last_eval[fixture.SYM]['trials'] if tr['setup']=='C')
        rr=trial['gates']['risk_reward']['values']
        self.assertGreaterEqual(rr['gross_rr'],1.5)
        self.assertLess(rr['net_rr'],1.5)

    def test_configured_C_cannot_reach_net_RR_even_before_buffers_and_noise(self):
        from core.settings import Config
        from automation.policy import AutomationConfig
        from automation.capital import entry_economics
        r,_=self.replay('bound')
        cfg=replace(Config(),min_reward_risk=1.5)
        auto=replace(AutomationConfig(),box_max_range=.015)
        costs=replace(r.account.e.costs,buy=.00015,sell=.00015,tax=.002,
                      entry_slippage=.002,exit_slippage=.003)
        bound=configured_c_reward_bound(cfg,auto,costs)
        self.assertAlmostEqual(bound['net_rr_upper_bound'],1.0046456737,places=6)
        self.assertFalse(bound['configured_intersection_possible'])
        for entry,stop,target in ((10150,10000,10450),(10160,9990,10450),(10150,10000,10400)):
            self.assertLessEqual(entry_economics(entry,stop,stop,target,costs,1.5)['net_rr'],bound['net_rr_upper_bound'])

    def test_follow_reference_versions_are_immutable_and_cannot_replace_earlier_evidence(self):
        from collections import Counter
        from datetime import datetime
        from types import SimpleNamespace
        from tools.observe_paper import follow,KST
        root=self.root/'source';out=self.root/'out';out.mkdir()
        tape=root/'runtime/automation-production/trade-review';tape.mkdir(parents=True)
        (tape/('trade-review-'+datetime.now(KST).strftime('%Y%m%d')+'-0001.jsonl')).touch()
        csv=root/'data/raw'/('999990_daily.csv');csv.parent.mkdir(parents=True);csv.write_bytes(b'earlier')
        class Session:
            def __init__(self,*a,**k):self.counts=Counter();self.capture=SimpleNamespace(storage_blocked=False,stats=lambda:{})
            def feed(self,*a):pass
            def result(self):return {}
            def close(self):pass
        class Tail:
            def __init__(self,*a,**k):self.stats={};self.offset=0
            def read(self,**k):return [dict(event='WATCH_METADATA',symbol='999990')]
        def advance(_):csv.write_bytes(b'later') if csv.read_bytes()==b'earlier' else None
        with patch('tools.observe_paper.ObservedPaperSession',Session),patch('tools.observe_paper.JsonTail',Tail),\
             patch('candidate_builder.bars_from_csv',return_value=[{}]*750),\
             patch('tools.observe_paper.time.monotonic',side_effect=[0,0,0,0,0,1]),\
             patch('tools.observe_paper.time.sleep',side_effect=advance),\
             patch('tools.observe_paper.shutil.disk_usage',return_value=SimpleNamespace(free=64*1024**3)):
            result=follow(root,out,self.cfg,self.auto,70420,.03)
        versions=list((out/'reference-inputs').glob('*.csv'))
        self.assertEqual({p.read_bytes() for p in versions},{b'earlier',b'later'})
        self.assertEqual(len(result['reference_observations']),2)

    def test_transition_evidence_precedes_display_throttle_and_keeps_same_geometry_episode(self):
        r,c=self.replay('transitions');cum=populate(r);r.account.at=AT
        t=packet(AT,38100,cum+20000).tick;c.bind_entry_context(t,metadata(AT),r.detector,AT)
        for reason in ('FIRST_GATE','SECOND_GATE'):
            r.account.e.decisions.emit('ENTRY_EVAL',SYMBOL,'NO_BUY',reason,{},price=t.price)
        rows=[json.loads(x) for x in c.pending]
        rows=[x for x in rows if x['event']=='ENTRY_DECISION_EVIDENCE']
        self.assertEqual([x['final_reason'] for x in rows[-2:]],['FIRST_GATE','SECOND_GATE'])
        tracer=c.entry_evidence;g=dict(pattern='A',breakout=38000,support=37500,projection=2000)
        first=tracer.prepare_episode(SYMBOL,'A',g,AT)
        c.decision_evidence(r.account.e,SYMBOL,'NO_BUY','NO_STRUCTURE',{},None,{})
        again=tracer.prepare_episode(SYMBOL,'A',g,AT+timedelta(seconds=1))
        self.assertEqual(first['id'],again['id'])


if __name__=='__main__':unittest.main()
