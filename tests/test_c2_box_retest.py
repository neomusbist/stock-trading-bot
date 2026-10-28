"""Synthetic causal input only: no market/profitability claims, no network."""
import contextlib,io,json,tempfile,time,unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST
from automation.policy import AutomationConfig
from automation.trade_review_capture import TradeReviewCapture
from automation.runner import account_query_failed,error_record
from core.broker import Rest,Rejected
from research.c2_validation import C2Replay
from research.strict_benchmark import FullPathReplay
from tests.test_strict_benchmark_pipeline import packet

AT=datetime(2026,10,27,10,0,tzinfo=KST);SYM='999990'
BOX=[(9950,10000,9920,9970),(9970,9990,9930,9980),(9980,10000,9940,9970),
     (9970,9990,9940,9980),(9980,10000,9930,9980)]
PREFIX=[(10300,10400,10250,10300),(10300,10500,10270,10300),(10300,10400,10000,10020),*BOX,
        (10020,10080,10010,10040),(10030,10040,10000,10020)]

def settings():
    cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04,max_position_fraction=1.,max_positions=1,
        max_entries_per_day=None,max_entries_per_symbol=None)
    auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,
        confirmed_invalidation_enabled=True,fast_momentum_enabled=False,pullback_expansion_target_enabled=False)
    return cfg,auto

def watch(at,resistance=11000):
    return dict(code=SYM,status='WATCH',source_count=2,resistance=resistance,
        source_details={'SYNTHETIC_1':{},'SYNTHETIC_2':{}},
        market_status={'pass':True,'checked_at':at.isoformat(),'reason_code':'SYNTHETIC_NORMAL'})

def populate(r,*,bars=PREFIX,resistance=11000,final=10050,symbol=SYM):
    daily=[dict(datetime=(AT.date()-timedelta(days=800-i)).isoformat(),open=9000,high=9450,low=8800,close=9000,volume=2000000) for i in range(800)]
    daily[250]['high']=11000;r.reference(symbol,daily,AT)
    cumulative=0
    for minute,bar in enumerate(bars):
        for sec,price in zip((0,15,30,59),bar):
            at=AT+timedelta(minutes=minute,seconds=sec);w={**watch(at,resistance),'code':symbol};r.qualify(symbol,w,at)
            cumulative+=5000;r.step(packet(at,price,cumulative,5000,symbol=symbol,depth=10000))
    at=AT+timedelta(minutes=len(bars));r.qualify(symbol,{**watch(at,resistance),'code':symbol},at)
    cumulative+=5000;frame=packet(at,final,cumulative,5000,symbol=symbol,depth=10000)
    return r.step(frame),frame


class C2Tests(unittest.TestCase):
    def setUp(self):
        self.quiet=contextlib.redirect_stdout(io.StringIO());self.quiet.__enter__();self.addCleanup(self.quiet.__exit__,None,None,None)
        self.cfg,self.auto=settings()

    def replay(self,cash=70420):
        r=C2Replay(AT,'C2',self.cfg,self.auto,cash);self.addCleanup(r.close);return r

    def test_real_detector_engine_positive_quantity_intent_fill_and_protective_exit(self):
        r=self.replay()
        with patch('socket.socket.connect',side_effect=AssertionError('NO_NETWORK')):
            order,f=populate(r)
            self.assertIsNotNone(order,r.result()['c2_reason_counts']);self.assertEqual(order['qty'],6)
            s=order['signal'];self.assertEqual(s['research_strategy'],'C2');self.assertEqual(s['target'],10500)
            self.assertEqual(s['target_evidence']['time'],(AT+timedelta(minutes=1)).isoformat())
            self.assertGreaterEqual(s['entry_economics']['net_rr'],1.5)
            self.assertLessEqual(order['qty']*s['entry_economics']['risk_per_share'],70420*.02)
            self.assertGreaterEqual(s['c2_retest_confirmed_at'],(AT+timedelta(minutes=10)).isoformat())
            saved=json.loads(r.account.e.path.read_text(encoding='utf-8'));self.assertEqual(saved['orders'][order['local']]['status'],'ACCEPTED')
            for second,price in ((1,10050),(2,10400),(3,9890),(4,9890)):
                r.step(packet(f.available_at+timedelta(seconds=second),price,f.tick.cumulative+second*5000,5000,symbol=SYM,depth=10000))
        out=r.result();self.assertEqual([x['side'] for x in out['fills']],['BUY','SELL'])
        self.assertEqual(out['completed_count'],1);self.assertEqual(out['completed_trades'][0]['MFE'],350)
        self.assertLess(out['net_pnl_closed'],0)  # a favorable excursion is NOT a guaranteed profitable exit
        self.assertFalse(out['actual_order_permission'])

    def test_frozen_C_and_C2_are_separate_and_C2_does_not_buy_the_initial_break(self):
        r=self.replay();order,_=populate(r,bars=PREFIX[:8],final=10020)
        self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])
        self.assertEqual(r.detector.boxes[SYM]['state'],'WAIT_RETEST')
        old=FullPathReplay(AT,'C',self.cfg,self.auto,70420);self.addCleanup(old.close)
        old_order,_=populate(old);self.assertIsNone(old_order);self.assertEqual(old.account.broker.submissions,[])

    def test_near_known_overhead_is_not_discarded_or_replaced_with_a_higher_target(self):
        r=self.replay();order,_=populate(r,resistance=10100)
        self.assertIsNone(order)
        self.assertIn('C2_NET_RR_REJECTED',r.detector.last_eval[SYM]['trials'][0]['failed_reasons'])
        self.assertEqual(r.detector.last_eval[SYM]['trials'][0]['gates']['risk_reward']['values']['target'],10100)

    def test_one_share_unaffordable_is_not_a_ready_order(self):
        r=self.replay(5000);order,_=populate(r)
        self.assertIsNone(order);self.assertEqual(r.account.broker.submissions,[])
        self.assertTrue(r.detector.last_eval[SYM]['trials'][0]['entry_ready'])

    def test_failed_retest_and_missing_reference_are_explicit_rejections(self):
        r=self.replay();bars=[*PREFIX[:-1],(10020,10040,9950,9980)]
        order,_=populate(r,bars=bars)
        self.assertIsNone(order);self.assertEqual(r.detector.boxes[SYM]['state'],'INVALID')
        r2=self.replay();order,_=populate(r2,bars=[*BOX,*BOX],final=10050)
        self.assertIsNone(order);self.assertTrue(any(e['reason']=='C2_PREBREAK_OVERHEAD_UNKNOWN' for e in r2.detector.events))

    def test_C2_live_mode_and_direct_transport_paths_are_blocked_even_if_flag_is_forged(self):
        r=self.replay();order,f=populate(r);signal=deepcopy(order['signal'])
        r2=self.replay();e=r2.account.e;e.paper_mode=False;wire=Mock();e.broker=wire
        self.assertIsNone(e.enter(signal,f.tick));self.assertIsNone(e.submit('BUY',SYM,1,10050,signal))
        e.paper_mode=True  # flag alone cannot turn a real/unknown broker into the sealed fake broker
        self.assertIsNone(e.enter(signal,f.tick));wire.submit.assert_not_called()
        e.s['orders']['C2-FAKE']=dict(local='C2-FAKE',id='',symbol=SYM,side='BUY',qty=1,limit=10050,status='INTENT',signal=signal)
        with self.assertRaisesRegex(Rejected,'C2_PAPER_ONLY'):e.order_guard('kt10000',{'stk_cd':SYM,'ord_qty':'1','ord_uv':'10050'})

    def test_stale_account_keeps_exit_intent_and_records_protection_block_without_zeroing_account(self):
        r=self.replay();order,f=populate(r);r.step(packet(f.available_at+timedelta(seconds=1),10050,f.tick.cumulative+5000,5000,symbol=SYM,depth=10000))
        with contextlib.nullcontext(self.enterContext(tempfile.TemporaryDirectory())) as tmp:
            c=TradeReviewCapture(Path(tmp),start=False,clock=lambda:r.account.at);self.addCleanup(c.close)
            r.account.e.trade_review=c;c.set_scope({SYM:watch(f.available_at)},r.account.e.s['positions'],r.account.at)
            e=r.account.e;old_cash=e.s['cash'];old_actual=deepcopy(e.actual);e.last_account=time.monotonic()-16
            account_query_failed(e,{**error_record(Rest.balance_rejection(1700,{})),'started':time.monotonic()})
            e.request_exit(SYM,'C2_TEST_EMERGENCY');e.drive_exits({SYM:9900})
            c.account_protection_evidence(e,'ACCOUNT_UNAVAILABLE',e.clock())
            self.assertTrue(e.s['positions'][SYM]['exit_requested']);self.assertFalse(e.capital_account_valid())
            self.assertEqual((e.s['cash'],e.actual),(old_cash,old_actual))
            self.assertFalse(any(o['side']=='SELL' for o in r.account.broker.submissions))
            rows=[json.loads(x) for x in c.pending];self.assertTrue(any(x.get('protection_status')=='BLOCKED_ACCOUNT_UNCERTAIN' for x in rows))
            persisted=json.loads(e.path.read_text(encoding='utf-8'));self.assertTrue(persisted['positions'][SYM]['exit_requested'])


if __name__=='__main__':unittest.main()
