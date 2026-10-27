import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import Mock
from core.settings import Config,KST,ROOT
from core.strategy import Tick
from core.broker import Rejected
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.allocation import efficiency,comparison,empirical_adjustment
from automation.performance import report
from automation.radar import DynamicRadar
from automation.patterns import PatternDetector

START=datetime(2026,9,30,14,40,tzinfo=KST)
SYMBOL='999990'

class FakeBroker:
    identity='autonomous-test'
    def __init__(self): self.orders=[]; self.cancels=[]
    def submit(self,side,sym,qty,limit):
        self.order_guard('kt10000' if side=='BUY' else 'kt10001',{'stk_cd':sym,'ord_qty':str(qty),'ord_uv':str(limit) if limit else ''})
        self.orders.append((side,sym,qty,limit)); return 'ORDER'+str(len(self.orders))
    def cancel(self,sym,oid):
        self.order_guard('kt10003',{'stk_cd':sym,'orig_ord_no':oid})
        self.cancels.append(oid); return 'C'+oid

class AutomationTests(unittest.TestCase):
    def setUp(self):
        # 이전 시험매도 계약은 고립된 역사 재현 fixture에서만 유지한다. 운영 기본은 비활성.
        from unittest.mock import patch
        legacy=patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',True); legacy.start(); self.addCleanup(legacy.stop)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name); self.clock=[START]; self.broker=FakeBroker()
        self.policy=Policy(self.folder/'policy.json'); self.policy.command('LIVE',confirmation='LIVE')
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=1000000,costs_confirmed=True,replacement_enabled=True)
        self.cfg=replace(Config(),live_mode=True)
        self.out=contextlib.redirect_stdout(io.StringIO()); self.out.__enter__(); self.addCleanup(self.out.__exit__,None,None,None)
        self.engine=self.make_engine(); self.snapshot={'cash':1000000,'equity':999999999,'orders':[],'holdings':{}}
        self.engine.reconcile(self.snapshot); self.engine.connected=True; self.engine.market='3'
    def make_engine(self):
        e=AutonomousEngine(self.cfg,self.auto,self.policy,self.folder/'book',self.broker,lambda:self.clock[0])
        e.market_status={s:{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'} for s in ('999990','999980')}
        return e
    def tick(self,price=10000,sym=SYMBOL): return Tick(sym,self.clock[0],price,100,100,price+10,price-10)
    def signal(self,sym=SYMBOL):
        cost=self.engine.costs
        # The target was already assumed by the efficiency fixture. Supply it
        # explicitly; a score object must never manufacture entry authority.
        return {'symbol':sym,'limit':10020,'stop':9800,'target':10600,'pattern':'B','time':self.clock[0].isoformat(),
                'efficiency':efficiency(10020,9800,10600,.8,30,cost)}
    def buy(self):
        o=self.engine.enter(self.signal(),self.tick()); self.assertIsNotNone(o)
        self.engine.cumulative_fill(o,o['qty'],o['qty']*o['limit'])
        self.engine.actual={SYMBOL:{'qty':o['qty'],'available':o['qty'],'avg':o['limit']}}
        return o
    def test_zero_cap_live_never_orders_even_with_cash(self):
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=0); self.engine=self.make_engine(); self.engine.reconcile(self.snapshot)
        self.engine.connected=True; self.engine.market='3'; self.engine.enter(self.signal(),self.tick())
        self.assertEqual(self.broker.orders,[]); self.assertEqual(self.engine.s['cash'],0)
    def test_default_armed_and_generates_no_order(self):
        self.assertEqual(Policy(self.folder/'other.json').mode,'ARMED')
        self.policy.command('ARMED'); self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.engine.s['orders'])
    def test_hard_locked_symbols_forbid_forced_orders(self):
        self.engine.enter(self.signal('017900'),self.tick(sym='017900')); self.engine.submit('BUY','017900',1,10020,self.signal('017900'))
        self.engine.s['positions']['005930']={'ownership':'BOT','qty':10,'qty_blocked':False}
        self.assertIsNone(self.engine.submit('SELL','005930',10)); self.assertEqual(self.broker.orders,[])
    def test_lock_requires_explicit_release(self):
        self.policy.command('ARMED'); self.policy.refresh(); self.assertTrue(self.policy.locked('005930'))
        with self.assertRaises(ValueError): self.policy.command('RELEASE','005930')
        self.policy.command('RELEASE','005930','RELEASE'); self.assertFalse(self.policy.locked('005930')); self.assertTrue(self.policy.locked('017900'))
    def test_released_existing_holding_still_no_averaging(self):
        self.snapshot['holdings']={'005930':{'qty':100,'available':100,'avg':10000}}; self.engine.reconcile(self.snapshot)
        self.policy.command('RELEASE','005930','RELEASE'); self.engine.enter(self.signal('005930'),self.tick(sym='005930'))
        self.assertFalse(self.broker.orders)
    def test_direct_rest_guard_blocks_without_intent(self):
        with self.assertRaises(Rejected): self.engine.order_guard('kt10000',{'stk_cd':SYMBOL,'ord_qty':'10','ord_uv':'10020'})
    def test_existing_valuation_excluded(self):
        self.snapshot['holdings']={'052260':{'qty':90000,'available':90000,'avg':5822},'005930':{'qty':10000,'available':10000,'avg':60000}}
        self.engine.reconcile(self.snapshot); self.assertEqual(self.engine.risk_equity(),1000000)
        self.assertIsNone(self.engine.submit('SELL','052260',1))
    def test_cash_reconciliation_bounds_available_capital(self):
        self.snapshot['cash']=9000000; self.engine.reconcile(self.snapshot)
        self.assertEqual(self.engine.s['cash'],1000000); self.assertEqual(self.engine.a.capital_limit,1000000)
        self.snapshot['cash']=100; self.engine.reconcile(self.snapshot); self.engine.enter(self.signal(),self.tick())
        self.assertFalse(self.broker.orders)
    def test_no_arbitrary_14_cutoff(self): self.assertIsNotNone(self.engine.enter(self.signal(),self.tick()))
    def test_unknown_or_auction_market_blocks_buy(self):
        for market in ('UNKNOWN','2','4','9'):
            self.engine.market=market; self.assertIsNone(self.engine.enter(self.signal(),self.tick()))
        self.assertFalse(self.broker.orders)
    def test_stop_blocks_and_cancels_new_buys(self):
        self.engine.enter(self.signal(),self.tick()); self.policy.command('STOP'); self.engine.controls()
        self.assertEqual(len(self.broker.cancels),1); count=len(self.broker.orders)
        self.engine.enter(self.signal('999980'),self.tick(sym='999980')); self.assertEqual(len(self.broker.orders),count)
    def test_stop_and_stop_flat_manage_bot_exits(self):
        # 1. stop protection still triggers stop loss
        self.buy(); self.policy.command('STOP'); self.engine.controls(); self.engine.on_tick(self.tick(9750))
        self.assertEqual(self.broker.orders[-1][0],'SELL')
        # 2. stop_flat requests immediate flat
        self.setUp()
        self.buy(); self.policy.command('STOP_FLAT'); self.engine.controls()
        self.assertTrue(self.engine.s['positions'][SYMBOL]['exit_requested']); self.assertEqual(self.broker.orders[-1][0],'SELL')
    def test_kill_waits_fresh_reconcile(self):
        self.buy(); self.policy.command('KILL'); self.engine.controls(); self.assertEqual(len(self.broker.orders),1)
        self.snapshot['holdings']=dict(self.engine.actual); self.engine.reconcile(self.snapshot); self.engine.controls()
        self.assertEqual(self.broker.orders[-1][0],'SELL')
    def test_connection_and_reconcile_gate_for_entries(self):
        self.engine.disconnect(); self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)
        self.engine.account_ok=False; self.engine.connected=True; self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)
        self.engine.reconcile(self.snapshot); self.assertIsNotNone(self.engine.enter(self.signal(),self.tick()))
    def test_restart_restores_position_and_requires_account(self):
        self.buy(); restored=self.make_engine(); self.assertEqual(restored.s['positions'][SYMBOL]['ownership'],'BOT')
        self.assertFalse(restored.account_ok); restored.market='3'
        self.snapshot['holdings']=dict(self.engine.actual); restored.reconcile(self.snapshot)
        self.assertFalse(restored.s['positions'][SYMBOL]['qty_blocked'])
    def test_quantity_mismatch_blocks_sell(self):
        self.buy(); self.snapshot['holdings']={}; self.engine.reconcile(self.snapshot); self.engine.request_exit(SYMBOL,'forced'); self.engine.drive_exits()
        self.assertEqual(len(self.broker.orders),1)
    def test_loss_limits_block_entries(self):
        self.engine.s['daily_loss']=self.auto.capital_limit*self.cfg.max_daily_loss_fraction
        self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)
        self.engine.s['daily_loss']=0
        self.engine.s['automation']['consecutive_losses']=self.auto.max_consecutive_losses
        self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)
    def test_symbol_reentry_and_cooldown(self):
        self.buy(); self.engine.s['positions']={}; self.engine.s['automation']['last_exit'][SYMBOL]=self.clock[0].isoformat()
        self.engine.enter(self.signal(),self.tick()); self.assertEqual(len(self.broker.orders),1)
        self.clock[0]+=timedelta(hours=1); self.engine.last_account=time.monotonic()
        self.engine.enter(self.signal(),self.tick()); self.assertEqual(len(self.broker.orders),1)
    def test_unconfirmed_costs_no_live(self):
        self.auto=replace(self.auto,costs_confirmed=False); self.engine=self.make_engine(); self.engine.reconcile(self.snapshot)
        self.engine.connected=True; self.engine.market='3'; self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)
    def test_profit_5822_6240_drop_protection(self):
        p={'symbol':SYMBOL,'ownership':'BOT','qty':10,'avg':5822,'initial_r':122,'initial_stop':5700,'stop':5700,'high':5822,'low':5822,
           'stop_history':[],'opened':self.clock[0].isoformat(),'last_tick':self.clock[0].isoformat(),'exit_requested':False,'exit_reason':'','exit_attempts':0,'qty_blocked':False}
        self.engine.s['positions'][SYMBOL]=p; self.engine.actual={SYMBOL:{'qty':10,'available':10,'avg':5822}}
        stops=[]
        for px in (5900,6100,6240,6200,6150,6100,6050,5900,5200):
            self.engine.on_tick(self.tick(px)); stops.append(p['stop'])
            if self.broker.orders: break
        self.assertEqual(stops,sorted(stops)); self.assertGreater(p['stop'],5822)
        self.assertEqual(self.broker.orders[-1][0],'SELL'); self.assertGreater(px,5822)
    def test_protective_stop_cannot_lower(self):
        p={'high':6240,'avg':5822,'stop':6200,'initial_r':122,'stop_history':[]}
        _,stop=self.engine.protection(p,5200,5100); self.assertGreaterEqual(stop,6200)
    def test_negative_efficiency_no_trade(self):
        bad=efficiency(10000,9900,10010,.8,30,self.engine.costs)
        self.assertEqual(bad['decision'],'NO_TRADE')
        sig=self.signal(); sig['efficiency']=bad; self.engine.enter(sig,self.tick()); self.assertFalse(self.broker.orders)
    def test_legacy_cost_comparison(self):
        held={'price':6000,'support':5700,'resistance':6050,'confidence':.4,'planned_minutes':60,'holding_minutes':30,'trend':'WEAK'}
        candidate={'pattern':'B','efficiency':efficiency(10000,9900,11000,.8,20,self.engine.costs)}
        result=comparison(held,candidate,self.auto,self.engine.costs,'3')
        self.assertEqual(result['decision'],'REPLACE'); self.assertIn('empirical',result)
        held['support']=None; self.assertEqual(comparison(held,candidate,self.auto,self.engine.costs,'3')['decision'],'HOLD')
    def test_legacy_zero_cap_no_actual_exit(self):
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=0); self.engine=self.make_engine()
        self.snapshot['holdings']={'052260':{'qty':100,'available':100,'avg':5822}}; self.engine.reconcile(self.snapshot); self.engine.market='3'
        self.assertIsNone(self.engine.replace_legacy({'decision':'REPLACE'})); self.assertFalse(self.broker.orders)
    def test_completed_trade_partial_exit_not_double_counted(self):
        o=self.buy(); self.engine.request_exit(SYMBOL,'test'); self.engine.drive_exits(); sell=list(self.engine.s['orders'].values())[-1]
        n=sell['qty']; self.engine.cumulative_fill(sell,1,10100)
        self.assertEqual(report(self.engine.s['trades'])['all']['count'],0)
        self.engine.cumulative_fill(sell,n,n*10100)
        stats=report(self.engine.s['trades']); self.assertEqual(stats['all']['count'],1)
        self.assertEqual(stats['completed_trades'][0]['qty'],n)
    def test_unsupported_fees_nan_rejected(self):
        with self.assertRaises(ValueError): replace(self.auto,sell_tax=float('nan')).validate()

class RadarAndPatternTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.addCleanup(self.temp.cleanup)
        self.policy=Policy(Path(self.temp.name)/'policy.json'); self.auto=AutomationConfig(); self.cfg=Config()
        self.radar=DynamicRadar(self.cfg,self.auto,self.policy)
        self.costs=Costs(.00015,.00015,.002,.002,.003)
    def item(self,sym=SYMBOL,flu=9):
        return {'code':sym,'name':'합성','source_details':{s:{'observed_rank':1,'values':{'flu_rt':flu},'raw':{}} for s in ('ka10029','ka10032')},'reference_price':10000}
    def evaluate(self,item): return {'status':'ELIGIBLE','overhead_share_estimate':.01,'previous_close':10000,'resistance':11000}
    def test_zero_candidates_no_trade_all_day(self):
        for minute in range(390):
            rows,watch,changes=self.radar.update([],self.evaluate,START+timedelta(minutes=minute)); self.assertFalse(watch)
    def test_dynamic_new_symbol_added(self):
        _,_,changes=self.radar.update([self.item()],self.evaluate,START); self.assertEqual(changes['added'],[SYMBOL])
        _,watch,changes=self.radar.update([self.item(),self.item('999980')],self.evaluate,START+timedelta(minutes=1))
        self.assertIn('999980',changes['added']); self.assertEqual(len(watch),2)
    def test_9_to_3_weakening_removed_and_recovery(self):
        self.radar.update([self.item(flu=9)],self.evaluate,START)
        rows,watch,changes=self.radar.update([self.item(flu=3)],self.evaluate,START+timedelta(minutes=1))
        self.assertFalse(watch); self.assertEqual(rows[0]['source_deltas']['ka10029']['values']['flu_rt'],-6)
        self.assertEqual(changes['removed'],[SYMBOL])
        _,watch,_=self.radar.update([self.item(flu=4)],self.evaluate,START+timedelta(minutes=2)); self.assertTrue(watch)
    def test_missing_rank_decays(self):
        self.radar.update([self.item()],self.evaluate,START)
        self.assertTrue(self.radar.update([],self.evaluate,START+timedelta(minutes=1))[1])
        self.assertFalse(self.radar.update([],self.evaluate,START+timedelta(minutes=2))[1])
    def test_single_source_or_unknown_structure_not_watched(self):
        item=self.item(); del item['source_details']['ka10029']
        self.assertFalse(self.radar.update([item],self.evaluate,START)[1])
        self.assertFalse(self.radar.update([self.item()],lambda x:{'status':'UNKNOWN','reason':'missing'},START)[1])
    def test_pattern_b_pullback_requires_low_volume_and_support(self):
        d=PatternDetector(self.cfg,self.auto,self.costs)
        bars=[{'open':10000,'low':10000,'high':10050,'close':10040,'volume':1000},
              {'open':10040,'low':10040,'high':10200,'close':10190,'volume':2000},
              {'open':10170,'low':10100,'high':10180,'close':10140,'volume':300},
              {'open':10140,'low':10110,'high':10160,'close':10150,'volume':200}]
        self.assertEqual(d.pullback(bars)['pattern'],'B')
        bars[-1]['volume']=10000; self.assertIsNone(d.pullback(bars))
        bars[-1].update(volume=200,low=9900); self.assertIsNone(d.pullback(bars))
    def test_pattern_c_box_width_required(self):
        d=PatternDetector(self.cfg,self.auto,self.costs)
        bars=[{'high':10100,'low':10000} for _ in range(5)]
        self.assertEqual(d.box(bars)['pattern'],'C'); bars[0]['high']=12000; self.assertIsNone(d.box(bars))
    def test_duplicate_tick_not_pattern_volume(self):
        d=PatternDetector(self.cfg,self.auto,self.costs); t=Tick(SYMBOL,START,10000,100,100,10010,9990)
        self.assertTrue(d.ingest(t,START)); self.assertFalse(d.ingest(t,START)); self.assertEqual(d.current[SYMBOL]['volume'],100)
    def test_empirical_missing_not_invented(self):
        self.assertEqual(empirical_adjustment([],'B',50)['basis'],'INSUFFICIENT_SAMPLE')

if __name__=='__main__': unittest.main()
