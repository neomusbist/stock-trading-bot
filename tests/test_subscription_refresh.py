"""구독 갱신/실제 단절/관찰 인계를 실제 계좌 없이 합성 틱과 가짜 broker로 검증."""
import contextlib
import io
import queue
import tempfile
import time
import unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.strategy import Tick
from core.realtime import Feed
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.shadow_observation import ShadowObserver
from automation.runner import SubscriptionState,prewarm_handoff,process_message
from automation.allocation import efficiency
from operator_service import OperatorService
from tests.test_automation import FakeBroker
from tests.test_pipeline import replay_prices

START=datetime(2026,10,6,10,0,tzinfo=KST)
SYM='999990'
OTHER='999980'

class SubscriptionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=START
        self.output=contextlib.redirect_stdout(io.StringIO());self.output.__enter__();self.addCleanup(self.output.__exit__,None,None,None)
        self.cfg=replace(Config(),live_mode=True)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=1000000,costs_confirmed=True)
        atomic_json(self.root/'config.json',asdict(self.cfg));atomic_json(self.root/'automation.json',asdict(self.auto))
        self.policy=Policy(self.root/'runtime/automation_policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.broker=FakeBroker();self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'runtime/automation-production',self.broker,lambda:self.clock)
        self.snapshot={'cash':1000000,'equity':1000000,'orders':[],'holdings':{}}
        self.e.reconcile(self.snapshot);self.e.connected=True;self.e.market='3';self.e.reset_detector=True
        self.e.market_status={s:{'pass':True} for s in (SYM,OTHER)}
        self.d=PatternDetector(self.cfg,self.auto,self.e.costs);self.d.continuous_observation=True
        self.feed=Feed(Mock(),[SYM]);self.feed.logged_in=True;self.feed.socket=Mock()
        self.sub=SubscriptionState([SYM]);self.account=Mock();self.account.request=__import__('threading').Event()
        self.radar=Mock();self.radar.cache={};self.prices={}
        self.watch={'code':SYM,'name':'합성','status':'WATCH','source_count':2,'resistance':11000,'previous_close':10000,'market_status':{'pass':True}}
    def tick(self,sym=SYM,sec=0,cumulative=None,regular=True):
        return Tick(sym,START+timedelta(seconds=sec),10000,100,cumulative or 100+sec,10010,9990,regular)
    def frame(self,t):
        return {'trnm':'REAL','data':[{'type':'0B','item':t.symbol,'values':{'20':t.time.strftime('%H%M%S'),'10':str(t.price),'15':str(t.volume),'13':str(t.cumulative),'27':str(t.ask),'28':str(t.bid),'290':'2' if t.regular else '1'}}]}
    def send(self,msg,lookup=None):
        with patch('automation.runner.now',return_value=self.clock):
            return process_message(self.e,self.d,msg,self.clock,self.sub.symbols,lookup or {},self.radar,self.prices,self.account,True,self.sub)
    def refresh(self,desired):self.sub.refresh(self.e,self.d,self.feed,desired,self.clock)
    def signal(self):return {'symbol':SYM,'limit':10020,'stop':9800,'pattern':'B','time':self.clock.isoformat(),'efficiency':efficiency(10020,9800,10600,.8,30,self.e.costs)}
    def on(self):
        self.e.save();atomic_json(self.e.folder/'heartbeat.json',{'time':self.clock.isoformat(),'connected':self.e.connected,'account_ok':self.e.account_ok,'market':'3'})
        service=OperatorService(self.root,clock=lambda:self.clock,gateway=Mock())
        with patch.object(service,'process_info',return_value={'running':True,'managed':False}):return service.public_status()['trading_on']
    def warm(self):
        for sec in range(121):self.assertTrue(self.d.ingest(self.tick(sec=sec),START+timedelta(seconds=sec)))
        self.clock=START+timedelta(seconds=120)
    def test_repeated_watch_shadow_refresh_keeps_ui_on_and_history(self):
        self.warm();history=self.d.history[SYM];buf=self.d.buffers[SYM]
        for n in range(8):
            self.clock+=timedelta(milliseconds=100);self.refresh({SYM,OTHER} if n%2 else {SYM})
            self.assertTrue(self.e.connected);self.assertTrue(self.e.account_ok);self.assertTrue(self.on())
            self.send({'trnm':'REG','return_code':0})
            self.assertTrue(self.on());self.assertIs(history,self.d.history[SYM]);self.assertIs(buf,self.d.buffers[SYM])
        self.assertFalse(self.account.request.is_set());self.assertEqual(self.broker.orders,[])
    def test_disconnected_immediately_off_and_reconnect_requires_account(self):
        self.assertTrue(self.on());self.send({'trnm':'DISCONNECTED'})
        self.assertFalse(self.on());self.assertFalse(self.e.account_ok);self.assertEqual(self.d.last,{})
        self.send({'trnm':'REG','return_code':0});self.assertFalse(self.on());self.assertTrue(self.account.request.is_set())
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
        self.e.reconcile(self.snapshot);self.assertTrue(self.on())
    def test_new_subscription_rejects_pre_registration_nonregular_and_stale_ticks(self):
        self.clock=START+timedelta(seconds=2);self.refresh({SYM,OTHER})
        self.assertEqual(self.send(self.frame(self.tick(OTHER,sec=1))),0)
        self.assertEqual(self.send(self.frame(self.tick(OTHER,sec=2,regular=False))),0)
        self.clock=START+timedelta(seconds=20)
        self.assertEqual(self.send(self.frame(self.tick(OTHER,sec=2))),0)
        self.assertIn(OTHER,self.sub.waiting);self.assertNotIn(OTHER,self.d.last);self.assertEqual(self.broker.orders,[])
    def test_new_subscription_waits_for_accepted_fresh_regular_tick(self):
        self.clock=START+timedelta(seconds=1);self.refresh({SYM,OTHER})
        self.send({'trnm':'REG','return_code':0});self.assertIn(OTHER,self.sub.waiting)
        self.assertEqual(self.send(self.frame(self.tick(OTHER,sec=1))),1)
        self.assertNotIn(OTHER,self.sub.waiting);self.assertEqual(self.broker.orders,[])
    def test_existing_ticks_continue_while_reg_ack_is_pending(self):
        self.send(self.frame(self.tick()));self.clock+=timedelta(seconds=1);self.refresh({SYM,OTHER})
        self.assertTrue(self.sub.refresh_pending);self.assertEqual(self.send(self.frame(self.tick(sec=1))),1)
        self.assertEqual(len(self.d.buffers[SYM]),2);self.assertTrue(self.e.account_ok)
    def test_reg_rejection_still_halts_new_entry(self):
        self.refresh({SYM,OTHER});self.send({'trnm':'REG','return_code':-1})
        self.assertTrue(self.e.s['halt']);self.assertFalse(self.on());self.assertIsNone(self.e.enter(self.signal(),self.tick()))
    def test_refresh_does_not_make_bad_or_stale_account_valid(self):
        self.e.account_ok=False;self.refresh({SYM,OTHER});self.send({'trnm':'REG','return_code':0})
        self.assertFalse(self.e.account_ok);self.assertFalse(self.on())
        self.e.account_ok=True;self.e.last_account=time.monotonic()-self.cfg.reconcile_seconds*3-1
        _,gates=self.e.entry_gates(self.signal(),self.tick());self.assertEqual(gates['connection_account']['result'],'FAIL')
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
    def test_short_unsubscribe_rejoin_preserves_history_but_requires_new_tick(self):
        self.warm();bars=self.d.history[SYM];self.refresh(set());self.clock+=timedelta(seconds=1);self.refresh({SYM})
        self.assertIs(bars,self.d.history[SYM]);self.assertIn(SYM,self.sub.waiting)
        self.assertEqual(self.send(self.frame(self.tick(sec=120))),0)
        self.assertEqual(self.send(self.frame(self.tick(sec=121))),1);self.assertNotIn(SYM,self.sub.waiting)
    def test_stale_rejoin_resets_every_symbol_map(self):
        self.warm();self.d.last_eval[SYM]={'trials':[1]};self.refresh(set())
        self.clock+=timedelta(seconds=self.cfg.stale_seconds+1);self.refresh({SYM})
        for m in (self.d.buffers,self.d.minutes,self.d.completed,self.d.last,self.d.history,self.d.current,self.d.day_high,self.d.prior_high,self.d.last_eval):self.assertNotIn(SYM,m)
    def test_detached_cache_expires_even_without_new_subscription_change(self):
        self.warm();self.refresh(set());self.clock+=timedelta(seconds=self.cfg.stale_seconds+1);self.refresh(set())
        self.assertNotIn(SYM,self.d.last);self.assertNotIn(SYM,self.sub.detached)
    def test_rejoin_next_day_never_uses_previous_day_data(self):
        self.warm();self.refresh(set());self.clock+=timedelta(days=1);self.refresh({SYM});self.assertNotIn(SYM,self.d.history)
    def test_shadow_watch_handoff_ingests_queued_prewarm_before_real_tick(self):
        self.warm();history=self.d.history[SYM];self.feed.configure_observation(set(),{SYM},Mock())
        for sec in range(121,125):self.feed.observation_q.put((START+timedelta(seconds=sec),self.tick(sec=sec)))
        self.clock=START+timedelta(seconds=124)
        with patch('automation.runner.now',return_value=self.clock):
            drops,priority=prewarm_handoff(self.e,self.d,self.feed,{SYM},{SYM},set(),0,Mock())
        self.assertEqual(priority,{SYM});self.assertIs(history,self.d.history[SYM]);self.assertEqual(self.d.last[SYM].time,self.clock)
        self.clock+=timedelta(seconds=1);self.assertEqual(self.send(self.frame(self.tick(sec=125))),1)
        self.assertEqual(self.d.last[SYM].time,self.clock);self.assertEqual(self.broker.orders,[])
    def test_handoff_is_not_blocked_by_unrelated_shadow_backlog(self):
        for n in range(300):self.feed.observation_q.put((START,self.tick(OTHER,cumulative=n+1)))
        for n in range(100):self.feed.observation_q.put((START,self.tick(cumulative=n+1)))
        with patch('automation.runner.now',return_value=self.clock):
            _,priority=prewarm_handoff(self.e,self.d,self.feed,{SYM,OTHER},{SYM},set(),0,Mock())
        self.assertEqual(priority,{SYM});self.assertIn(SYM,self.feed.priority_symbols);self.assertEqual(self.d.last[SYM].cumulative,100)
        self.assertGreater(self.feed.observation_q.qsize(),0);self.assertEqual(self.broker.orders,[])
    def test_critical_position_always_stays_priority_during_handoff(self):
        self.e.s['positions'][SYM]={'qty':1};self.feed.observation_q.put((START,self.tick(OTHER)))
        with patch('automation.runner.now',return_value=self.clock):
            _,priority=prewarm_handoff(self.e,self.d,self.feed,{SYM,OTHER},{SYM},set(),0,Mock())
        self.assertIn(SYM,priority);self.assertIn(SYM,self.feed.priority_symbols);self.assertEqual(self.broker.orders,[])
    def test_observation_queue_gap_still_resets_prewarm(self):
        self.warm();self.feed.observation_drops=1
        with patch('automation.runner.now',return_value=self.clock):
            dropped,_=prewarm_handoff(self.e,self.d,self.feed,{SYM},set(),set(),0,Mock())
        self.assertEqual(dropped,1);self.assertNotIn(SYM,self.d.history)
    def test_independent_shadow_short_exclusion_retains_then_stale_resets(self):
        shadow=ShadowObserver(self.cfg,self.auto,self.e.costs,Mock());shadow.detector.continuous_observation=True
        row={'code':SYM,'status':'REJECT','source_count':1,'reference_price':10000,'affordability':{'entry_eligible':True},'market_status':{'pass':True}}
        shadow.update([row],START);shadow.detector.ingest(self.tick(),START);buf=shadow.detector.buffers[SYM]
        shadow.update([],START+timedelta(seconds=1));shadow.update([row],START+timedelta(seconds=2));self.assertIs(buf,shadow.detector.buffers[SYM])
        shadow.update([],START+timedelta(seconds=3));shadow.update([],START+timedelta(seconds=self.cfg.stale_seconds+1))
        self.assertNotIn(SYM,shadow.detector.last);self.assertEqual(self.broker.orders,[])
    def test_default_A_ticks_can_use_existing_fake_order_path_after_churn(self):
        cumulative=0;self.radar.cache={(START.date(),SYM):[{}]}
        with patch('automation.runner.structure',return_value={'status':'ELIGIBLE','resistance':11000}):
            for sec,price,volume in replay_prices('A'):
                self.clock=START+timedelta(seconds=sec);cumulative+=volume
                if sec%10==0:
                    self.refresh({SYM,OTHER} if sec%20==0 else {SYM});self.send({'trnm':'REG','return_code':0})
                tick=Tick(SYM,self.clock,price,volume,cumulative,price+10,price-10)
                self.send(self.frame(tick),{SYM:self.watch})
        self.assertEqual(len(self.broker.orders),1);self.assertEqual(self.broker.orders[0][0],'BUY')
    def test_source_one_still_cannot_buy_despite_preserved_prewarm(self):
        self.warm();self.clock+=timedelta(seconds=1)
        with patch.object(self.d,'signal',return_value=self.signal()) as signal:
            self.send(self.frame(self.tick(sec=121)),{SYM:{**self.watch,'source_count':1}})
        signal.assert_not_called();self.assertEqual(self.broker.orders,[])
    def test_zero_cap_still_cannot_buy_after_refresh(self):
        self.e.a=replace(self.auto,AUTO_TRADING_CAP_KRW=0);self.refresh({SYM,OTHER});self.send({'trnm':'REG','return_code':0})
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
    def test_feed_handoff_routes_new_ticks_to_real_without_race(self):
        import threading
        self.feed.configure_observation(set(),{SYM},Mock())
        self.feed.observation_q.put((START,self.tick()))
        started=threading.Event();done=threading.Event()
        def receive():
            started.set();self.feed.emit(self.frame(self.tick(sec=1)));done.set()
        self.clock=START+timedelta(seconds=1)
        with patch('core.realtime.now',return_value=self.clock),patch('automation.runner.now',return_value=self.clock):
            with self.feed.subscription_lock:
                child=threading.Thread(target=receive);child.start();self.assertTrue(started.wait(2))
                self.assertFalse(done.is_set())
                prewarm_handoff(self.e,self.d,self.feed,{SYM},{SYM},set(),0,Mock())
            child.join(2);self.assertFalse(child.is_alive());self.assertTrue(done.is_set())
        self.assertTrue(self.feed.observation_q.empty());received,msg=self.feed.q.get_nowait()
        self.assertEqual(msg['data'][0]['item'],SYM);self.assertEqual(self.send(msg),1)
        self.assertEqual(self.d.last[SYM].time,self.clock);self.assertEqual(self.broker.orders,[])
    def test_detached_freshness_exact_stale_boundary(self):
        self.warm();buf=self.d.buffers[SYM];self.refresh(set())
        self.clock+=timedelta(seconds=self.cfg.stale_seconds);self.refresh({SYM});self.assertIs(buf,self.d.buffers[SYM])
    def test_hard_locks_still_block_forced_sell_after_churn(self):
        self.refresh({SYM,OTHER});self.send({'trnm':'REG','return_code':0})
        for sym in ('005930','017900'):
            self.e.s['positions'][sym]={'ownership':'BOT','qty':1,'qty_blocked':False}
            for reason in ('STOP_FLAT','KILL','SESSION_FLATTEN','PROTECTION','FORCED_SELL'):
                self.assertIsNone(self.e.submit('SELL',sym,1,0,{'reason':reason}))
        self.assertEqual(self.broker.orders,[])

if __name__=='__main__':unittest.main()
