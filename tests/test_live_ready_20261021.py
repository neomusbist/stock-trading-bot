"""LIVE-source overlay acceptance with synthetic WebSocket, real guards and FakeBroker.

Optional R6_LIVE_CONFIG_ROOT supplies private, unchanged operator settings.
No Rest object, socket, production policy, account file or LIVE process is used.
"""
from copy import deepcopy
from dataclasses import fields,replace
from datetime import datetime,timedelta
import json,os,shutil,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.settings import Config,KST
from core.realtime import parse_tick
from core.util import tick_size
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig,Policy
from automation.radar import DynamicRadar
from automation.runner import process_message
from candidate_builder import structure
from tests.test_automation import FakeBroker
from tests.test_structural_a_exit import NORMAL as A_BARS
from tests.test_structural_pullback import NORMAL as B_BARS
from tests.test_opening_live_repair import NORMAL as C_BARS
from tests.test_r6_fast_repair import CAUSAL

AT=datetime(2026,10,21,10,30,tzinfo=KST)
SYM='999990'
RESULTS=[]

def settings():
    root=os.environ.get('R6_LIVE_CONFIG_ROOT')
    if root:
        def read(name,kind):
            raw=json.loads((Path(root)/name).read_text(encoding='utf-8-sig'))
            return kind(**{k:v for k,v in raw.items() if k in {f.name for f in fields(kind)}}).validate()
        return read('config.json',Config),read('automation.json',AutomationConfig)
    return (replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04,
                    max_position_fraction=1.,max_positions=1,max_entries_per_day=None,max_entries_per_symbol=None),
            replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,
                    min_source_count=2,fast_momentum_enabled=True,structural_pullback_enabled=True,
                    structural_breakout_enabled=True,intraday_context_enabled=True,
                    fresh_box_basis_enabled=True,confirmed_box_direction=True,
                    confirmed_invalidation_enabled=True,after_momentum_failure_enabled=True))


class IntentCheckingBroker(FakeBroker):
    def __init__(self):super().__init__();self.intent_checks=[];self.engine=None
    def submit(self,side,sym,qty,limit,**kwargs):
        persisted=json.loads(self.engine.path.read_text(encoding='utf-8'))
        intents=[o for o in persisted['orders'].values() if o['symbol']==sym and o['side']==side
                 and o['status']=='INTENT' and o['qty']==qty and o['limit']==limit]
        if len(intents)!=1:raise AssertionError('durable exact INTENT must precede fake transport')
        self.intent_checks.append({'side':side,'durable_intent':True,'quantity':qty})
        return super().submit(side,sym,qty,limit)


class LiveReadyLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=AT
        self.cfg,self.auto=settings()
        self.assertEqual((self.cfg.risk_fraction,self.cfg.max_daily_loss_fraction,self.cfg.max_positions),(.02,.04,1))
        self.policy=Policy(self.root/'runtime/automation_policy.json')
        self.policy.command('LIVE',confirmation='LIVE')  # isolated test policy only
        self.broker=IntentCheckingBroker()
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.broker,lambda:self.clock)
        self.broker.engine=self.e;self.events=[]
        self.e.console.emit=lambda *a:None
        original=self.e.event
        def event(kind,**kw):self.events.append((kind,deepcopy(kw)));return original(kind,**kw)
        self.e.event=event
        self.initial_cash=float(os.environ.get('R6_PAPER_INITIAL_CASH','71822'))
        self.e.reconcile({'cash':self.initial_cash,'equity':self.initial_cash,'holdings':{},'orders':[]})
        self.e.connected=True;self.e.market='3'
        self.detector=PatternDetector(self.cfg,self.auto,self.e.costs)
        self.detector.continuous_observation=True;self.detector.fast_strategy=self.e.fast_strategy
        self.account=SimpleNamespace(need_fills=False,request=threading.Event())
        self.latest=None;self.book={};self.cumulative=0;self.lookup={};self.prices={}
        self.e.entry_quote_provider=lambda sym:(self.latest,self.book)
        self.radar=SimpleNamespace(cache={},status_cache={})
        self.dynamic=DynamicRadar(self.cfg,self.auto,self.policy)
        self.dynamic.available_capital=self.initial_cash
        self.initial_fingerprints=(self.cfg.fingerprint(),self.auto.fingerprint())

    def watch(self,name):
        close=900 if name in {'B','FAST'} else 9000
        high=1045 if name=='FAST' else 2000 if name=='B' else 20000
        history=[{'datetime':(AT.date()-timedelta(days=800-i)).isoformat(),
            'open':close,'high':close*1.05,'low':close*.98,'close':close,'volume':2000000} for i in range(800)]
        history[250]['high']=high
        status={'pass':True,'checked_at':AT.isoformat(),'reason_code':'SYNTHETIC_NORMAL_STATUS'}
        raw={'code':SYM,'name':'SYNTHETIC_ONLY','reference_price':{'A':10040,'B':1041,'C':10160,'FAST':1001}[name],
             'source_details':{'one':{'observed_rank':1,'values':{}},'two':{'observed_rank':1,'values':{}}},
             'market_status':status}
        rows,selected,changes=self.dynamic.update([raw],lambda r:structure(history,r['reference_price'],self.cfg,AT.date()),AT)
        self.assertEqual([r['code'] for r in selected],[SYM],rows)
        self.lookup={r['code']:r for r in selected}
        self.radar.cache[(AT.date(),SYM)]=history;self.radar.status_cache[SYM]=status
        self.assertEqual(self.lookup[SYM]['status'],'WATCH')

    def feed(self,at,price,volume=100,*,execute=True,stale_book=False,changed_quote=False):
        self.clock=at;self.cumulative+=volume
        values={'20':at.strftime('%H%M%S'),'10':str(price),'15':str(volume),'13':str(self.cumulative),
                '27':str(price),'28':str(price-tick_size(price)),'290':'2','61':'1000000','71':'1000000'}
        item={'type':'0B','item':SYM,'values':values}
        self.latest=parse_tick(item,at,book_stale_seconds=self.cfg.stale_seconds)
        self.book={'time':(at-timedelta(seconds=6) if stale_book else at).isoformat(),'received_at':at.isoformat(),
            'best_bid':self.latest.bid,'best_ask':self.latest.ask,'bid_qty':1000000,'ask_qty':1000000,'depth_available':True}
        if changed_quote:
            self.latest=replace(self.latest,price=price+10*tick_size(price),bid=price+9*tick_size(price),ask=price+10*tick_size(price))
            self.book.update(best_bid=self.latest.bid,best_ask=self.latest.ask)
        self.e.last_account=time.monotonic()
        with patch('automation.runner.now',return_value=at):
            return process_message(self.e,self.detector,{'trnm':'REAL','data':[item]},at,{SYM},
                self.lookup if execute else {},self.radar,self.prices,self.account,execute)

    def seed(self,name,*,block=None):
        self.watch(name)
        if name=='FAST':
            # Independent complete reference candles do not supply tick warmup.
            for i in range(10):
                base=960+i*3
                self.detector.price_context.record(SYM,{'time':AT-timedelta(minutes=10-i),
                    # Normal rising context. The former 1,200 spike is a
                    # confirmed 16% drawdown counter-bounce, not a positive
                    # FAST fixture under the Oct22 production policy.
                    'open':base,'high':base+10,'low':base-2,'close':base+2,
                    'volume':100,'first_second':0,'gap_reason':None,'venue':'KRX','observation_source':'SYNTHETIC_REFERENCE'},AT)
            for i,price in enumerate(CAUSAL[:-1]):self.feed(AT+timedelta(seconds=i),price,1000)
            stamp=AT+timedelta(seconds=len(CAUSAL)-1);last_price=CAUSAL[-1];last_volume=1000
        else:
            raw=deepcopy({'A':A_BARS,'B':B_BARS,'C':C_BARS}[name])
            if name=='B':
                shapes=[(980,985,980,984),(984,1030,984,1028),(1028,1040,1020,1038),
                        (1036,1036,1028,1030),(1030,1038,1029,1038)]
                for b,(o,h,l,c) in zip(raw,shapes):b.update(open=o,high=h,low=l,close=c)
            for i,b in enumerate(raw):
                start=AT-timedelta(minutes=len(raw)-i)
                for sec,price in ((0,b['open']),(15,b['high']),(30,b['low']),(59,b['close'])):
                    self.feed(start+timedelta(seconds=sec),price,int(b.get('volume',100)))
            self.feed(AT,9970 if name=='A' else raw[-1]['close'])
            stamp=AT+timedelta(seconds=9 if name=='A' else 5)
            last_price={'A':10040,'B':1041,'C':10160}[name];last_volume=100000
        self.assertEqual(self.broker.orders,[],'no invented early fills or premature positive signal')
        if block=='daily_loss':self.e.s['daily_loss']=self.e.daily_risk_budget()
        if block=='vi':self.lookup[SYM]['market_status']['vi_active']=True
        if block=='source':self.lookup[SYM].update(source_count=1,fallback_source1=False)
        self.feed(stamp,last_price,last_volume,stale_book=block=='stale_book',changed_quote=block=='changed_quote')

    def fill(self,order,total,price):
        self.clock+=timedelta(milliseconds=100)
        if order['side']=='BUY':self.e.actual[SYM]={'qty':total,'available':total,'avg':price}
        data={'9203':order['id'],'9001':SYM,'907':'2' if order['side']=='BUY' else '1','913':'체결',
            '905':'매수' if order['side']=='BUY' else '매도','900':str(order['qty']),
            '902':str(order['qty']-total),'903':str(total*price)}
        process_message(self.e,self.detector,{'trnm':'REAL','data':[{'type':'00','values':data}]},
                        self.clock,{SYM},self.lookup,self.radar,self.prices,self.account,True)

    def lifecycle(self,name):
        self.seed(name)
        buys=[o for o in self.e.s['orders'].values() if o['side']=='BUY']
        self.assertEqual(len(buys),1,[(k,v.get('primary_reason_code') or v.get('reason')) for k,v in self.events if k in {'ENTRY_EVAL','ORDER_REJECTED','NO_TRADE'}][-8:])
        buy=buys[0];self.assertEqual(buy['status'],'ACCEPTED',buy)
        self.assertEqual(buy['signal']['pattern'],name)
        self.assertNotIn(SYM,self.e.s['positions'],'acceptance is not a fill')
        self.assertEqual(self.e.s['entries'].get(SYM,0),0)
        qty=buy['qty'];self.assertGreater(qty,1)
        self.fill(buy,1,buy['limit']);self.assertEqual(self.e.s['positions'][SYM]['qty'],1)
        self.fill(buy,1,buy['limit']);self.assertEqual(self.e.s['positions'][SYM]['qty'],1)
        self.assertGreater(self.e.pending_buy_reservation(),0)
        self.fill(buy,qty,buy['limit']);self.assertEqual(self.e.s['positions'][SYM]['qty'],qty)
        self.assertEqual(self.e.s['entries'][SYM],1)
        self.assertEqual(self.e.pending_buy_reservation(),0)
        p=self.e.s['positions'][SYM];initial_stop=p['stop']
        self.feed(self.clock+timedelta(seconds=1),buy['limit']+tick_size(buy['limit']))
        self.assertGreaterEqual(p['high'],buy['limit']+tick_size(buy['limit']))
        self.assertGreaterEqual(p['stop'],initial_stop)
        self.assertEqual(len([o for o in self.broker.orders if o[0]=='BUY']),1)
        hard=(p['signal'].get('invalidation_policy') or {}).get('emergency_stop') or p.get('emergency') or p['stop']
        sell_price=hard-tick_size(hard)
        self.feed(self.clock+timedelta(seconds=1),sell_price)
        sells=[o for o in self.e.s['orders'].values() if o['side']=='SELL']
        self.assertEqual(len(sells),1,self.events[-8:]);sell=sells[0]
        self.assertEqual(sell['status'],'ACCEPTED',sell)
        self.fill(sell,qty,sell_price);self.e.actual.pop(SYM,None)
        self.assertNotIn(SYM,self.e.s['positions']);self.assertEqual(self.e.active_orders(),[])
        self.assertEqual(len(self.e.s['trades']),1)
        persisted=json.loads(self.e.path.read_text(encoding='utf-8'))
        self.assertEqual(persisted,self.e.s)
        self.assertEqual((self.cfg.fingerprint(),self.auto.fingerprint()),self.initial_fingerprints)
        self.assertEqual([r['side'] for r in self.broker.intent_checks],['BUY','SELL'])
        self.assertEqual(self.e.s['halt'],'')
        RESULTS.append({'strategy':name,'watch':'WATCH','buy_status':buy['status'],'buy_quantity':qty,
            'partial_and_duplicate_fill_checked':True,'entry_count':1,'managed':True,
            'sell_status':sell['status'],'position_closed':True,'durable_intents':self.broker.intent_checks,
            'final_ledger_cash':self.e.s['cash'],'net_pnl':self.e.s['trades'][0]['pnl_estimate'],
            'risk_fraction':self.cfg.risk_fraction,'daily_loss_fraction':self.cfg.max_daily_loss_fraction,
            'actual_orders':0})

    def test_A_full_receive_watch_order_fill_manage_exit(self):self.lifecycle('A')
    def test_B_full_receive_watch_order_fill_manage_exit(self):self.lifecycle('B')
    def test_C_full_receive_watch_order_fill_manage_exit(self):self.lifecycle('C')
    def test_FAST_full_receive_watch_order_fill_manage_exit(self):self.lifecycle('FAST')

    def test_daily_loss_limit_still_blocks_valid_signal(self):
        self.seed('FAST',block='daily_loss');self.assertEqual(self.broker.orders,[])
    def test_vi_still_blocks_valid_signal(self):
        self.seed('FAST',block='vi');self.assertEqual(self.broker.orders,[])
    def test_insufficient_sources_still_block_valid_signal(self):
        self.seed('FAST',block='source');self.assertEqual(self.broker.orders,[])
    def test_stale_depth_still_blocks_valid_signal(self):
        self.seed('FAST',block='stale_book');self.assertEqual(self.broker.orders,[])
    def test_newer_quote_above_original_chase_still_blocks(self):
        self.seed('FAST',block='changed_quote');self.assertEqual(self.broker.orders,[])
        reasons=[v.get('primary_reason_code') for k,v in self.events if k=='ENTRY_EVAL']
        self.assertIn('FAST_MAX_CHASE_EXCEEDED',reasons)


@unittest.skipUnless(os.environ.get('R6_LIVE_LEDGER_FILE'),'Private preserved LIVE ledger required')
class PreservedLedgerCompatibility(unittest.TestCase):
    def setUp(self):
        self.cfg,self.auto=settings()
        self.source=Path(os.environ['R6_LIVE_LEDGER_FILE'])
        self.original=json.loads(self.source.read_text(encoding='utf-8-sig'))
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.book=self.root/'runtime/automation-production'
        self.book.mkdir(parents=True);shutil.copy2(self.source,self.book/'state.json')

    def test_real_preserved_ledger_rolls_next_day_without_losing_trades_or_manual_holdings(self):
        broker=FakeBroker();broker.identity=self.original['identity']
        policy=Policy(self.root/'runtime/automation_policy.json')
        engine=AutonomousEngine(self.cfg,self.auto,policy,self.book,broker,lambda:AT.replace(hour=8,minute=50))
        engine.console.emit=lambda *a:None
        self.assertEqual(engine.s['day'],'2026-10-21')
        self.assertEqual(engine.s['trades'],self.original['trades'])
        self.assertEqual(engine.s['automation']['existing'],self.original['automation']['existing'])
        self.assertEqual(engine.s['positions'],{});self.assertEqual(engine.active_orders(),[])
        self.assertEqual(engine.s['entries'],{});self.assertEqual(engine.s['daily_loss'],0)
        self.assertFalse(engine.account_ok)
        existing=deepcopy(self.original['automation']['existing'])
        cash=self.original['automation']['cash']
        equity=cash+sum(h.get('qty',0)*h.get('avg',0) for h in existing.values())
        self.assertIsNot(engine.reconcile({'cash':cash,'equity':equity,'holdings':existing,'orders':[]}),False)
        self.assertTrue(engine.account_ok)
        self.assertEqual(engine.s['positions'],{})
        self.assertEqual(engine.s['automation']['existing'],existing)
        self.assertAlmostEqual(engine.s['automation']['cash'],cash)
        self.assertEqual(json.loads(engine.path.read_text(encoding='utf-8')),engine.s)
        self.assertEqual(broker.orders,[])

    def test_previous_day_live_approval_cannot_authorize_tomorrow(self):
        from automation.readiness import authorization_valid
        private=Path(os.environ['R6_LIVE_CONFIG_ROOT']).parent/'private/runtime'
        shutil.copy2(private/'operator_user_authorization.json',self.root/'runtime/operator_user_authorization.json')
        policy=json.loads((private/'automation_policy.json').read_text(encoding='utf-8-sig'))
        ok,reasons=authorization_valid(self.root,self.cfg,self.auto,policy,AT.replace(hour=8,minute=50))
        self.assertFalse(ok)
        self.assertIn('DAILY_LIVE_APPROVAL_EXPIRED',reasons)


if __name__=='__main__':unittest.main()
