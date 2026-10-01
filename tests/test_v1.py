import contextlib
import io
import json
import os
from pathlib import Path
import random
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, date
from unittest.mock import patch

from core.settings import Config, KST, ROOT, load_config
from core.util import code, number, floor_tick, atomic_json, write_csv, ProcessLock
from core.strategy import Tick, Detector, size, protective_stop
from core.engine import Engine
from core.broker import Rest, Rejected, AmbiguousOrder
from core.realtime import parse_tick
from candidate_builder import structure, bars_from_csv, collect_universe, RANKS

DAY=date(2026,9,30)
START=datetime(2026,9,30,9,2,0,tzinfo=KST)

def bars(n=750):
    return [{'datetime':(DAY-timedelta(days=n-i)).isoformat(),'open':9900,'high':9950,'low':9850,'close':9900,'volume':2000000} for i in range(n)]

def tick(second=0,price=10000,cumulative=100,volume=100):
    return Tick('005930',START+timedelta(seconds=second),price,volume,cumulative,price+10,price-10)

class FakeBroker:
    identity='test-broker'
    def __init__(self): self.orders=[]; self.cancels=[]; self.error=None
    def submit(self,side,symbol,qty,limit):
        self.orders.append((side,symbol,qty,limit))
        if self.error: raise self.error
        return str(len(self.orders)).zfill(7)
    def cancel(self,symbol,order_id):
        self.cancels.append(order_id)
        return 'C'+order_id

class ConfigAndUtilityTests(unittest.TestCase):
    def test_default_dry_run(self):
        from dataclasses import asdict
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            defaults=Path(temp)/'config.json'; atomic_json(defaults,asdict(Config()))
            self.assertFalse(load_config(defaults).live_mode)
    def test_string_live_rejected(self):
        with self.assertRaises(ValueError): replace(Config(),live_mode='false').validate()
    def test_unknown_config_rejected(self):
        with self.assertRaises(TypeError): Config(risk_precent=1)
    def test_nonfinite_and_invalid_values(self):
        for field,value in [('risk_fraction',float('nan')),('account_equity',-1),('risk_fraction',1),('max_positions',2.5),('min_history_bars',5)]:
            with self.subTest(field=field),self.assertRaises(ValueError): replace(Config(),**{field:value}).validate()
    def test_invalid_market_times(self):
        with self.assertRaises(ValueError): replace(Config(),entry_start='08:00:00').validate()
    def test_codes_preserve_alphanumeric(self):
        self.assertEqual(code('A0035S0'),'0035S0')
        with self.assertRaises(ValueError): code('005930_NX')
    def test_numbers_do_not_silently_impute(self):
        self.assertEqual(number('-12,340'),12340)
        self.assertEqual(number('-100',signed=True),-100)
        for value in ('',None,'abc','nan'):
            with self.assertRaises(ValueError): number(value)
    def test_order_tick_boundaries(self):
        self.assertEqual(floor_tick(1999.8),1999)
        self.assertEqual(floor_tick(2004.8),2000)
        self.assertEqual(floor_tick(5008),5000)
        self.assertEqual(floor_tick(50050),50000)
    def test_second_process_lock_blocked(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            with ProcessLock(Path(temp)/'lock'):
                with self.assertRaises(RuntimeError):
                    with ProcessLock(Path(temp)/'lock'): pass

class CandidateTests(unittest.TestCase):
    def test_existing_250_day_data_unknown(self):
        actual=bars_from_csv(ROOT/'tests/fixtures/original_052260_daily.csv',DAY)
        result=structure(actual,None,Config(),DAY)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertLess(result['history_count'],500)
    def test_clean_long_structure_eligible(self):
        r=structure(bars(),10000,Config(),DAY)
        self.assertEqual(r['status'],'ELIGIBLE'); self.assertEqual(r['overhead_share_estimate'],0)
    def test_overhead_density_reject(self):
        data=bars()
        for b in data[:-20]: b.update(open=10500,high=11000,low=10000,close=10500)
        r=structure(data,10000,Config(),DAY)
        self.assertEqual(r['status'],'REJECT'); self.assertIn('상단',r['reason'])
    def test_resistance_room_reject(self):
        data=bars(); data[-30]['high']=10100
        r=structure(data,10000,Config(),DAY)
        self.assertEqual(r['resistance'],10100); self.assertIn('저항',r['reason'])
    def test_recent_structure_breakdown_reject(self):
        self.assertIn('저점',structure(bars(),9800,Config(),DAY)['reason'])
    def test_history_stale_unknown(self):
        self.assertEqual(structure(bars(),10000,Config(),DAY+timedelta(days=20))['status'],'UNKNOWN')
    def test_same_day_future_and_incomplete_excluded(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            path=Path(temp)/'bars.csv'; data=bars(10)
            data.append({**data[-1],'datetime':DAY.isoformat(),'high':999999})
            data[-2]['is_complete']=False
            for b in data: b.setdefault('is_complete',True)
            write_csv(path,data); clean=bars_from_csv(path,DAY)
            self.assertEqual(len(clean),9); self.assertEqual(max(b['high'] for b in clean),9950)
    def test_duplicate_dates_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            path=Path(temp)/'bars.csv'; b=bars(1)[0]; write_csv(path,[b,b])
            with self.assertRaises(ValueError): bars_from_csv(path,DAY)
    def test_zero_volume_unknown(self):
        data=bars()
        for b in data: b['volume']=0
        self.assertEqual(structure(data,10000,Config(),DAY)['status'],'UNKNOWN')

class SignalTests(unittest.TestCase):
    def warm(self,cfg=None):
        d=Detector(cfg or Config()); last=None
        for i in range(127):
            last=tick(i,10000,100*(i+1))
            d.ingest(last,last.time)
        return d,last
    def test_warmup_is_required(self):
        d=Detector(Config()); t=tick(); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900}))
    def test_price_without_money_is_not_signal(self):
        d,_=self.warm(); t=tick(127,10050,12800,100); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900}))
    def test_all_conditions_signal(self):
        d,_=self.warm(); t=tick(127,10050,15700,3000); d.ingest(t,t.time)
        signal=d.signal(t,{'previous_close':9900})
        self.assertIsNotNone(signal); self.assertEqual(signal['stop'],9990)
        self.assertLessEqual(signal['limit'],10080)
    def test_chase_blocked(self):
        d,_=self.warm(); t=tick(127,10200,15700,3000); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900}))
    def test_spread_blocked(self):
        d,_=self.warm(); t=replace(tick(127,10050,15700,3000),bid=9900,ask=10060); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900}))
    def test_gap_blocked(self):
        d,_=self.warm(); t=tick(127,10050,15700,3000); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':8000}))
    def test_resistance_reward_risk_blocked(self):
        d,_=self.warm(); t=tick(127,10050,15700,3000); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900,'resistance':10100}))
    def test_duplicate_out_of_order_stale_rejected(self):
        d=Detector(Config()); t=tick(); self.assertTrue(d.ingest(t,t.time))
        self.assertFalse(d.ingest(t,t.time))
        self.assertFalse(d.ingest(tick(-1,cumulative=101),t.time))
        self.assertFalse(d.ingest(tick(1,cumulative=102),t.time+timedelta(seconds=20)))
    def test_gap_and_reconnect_reset_warmup(self):
        d,_=self.warm(); t=tick(140,10050,15700,3000); d.ingest(t,t.time)
        self.assertIsNone(d.signal(t,{'previous_close':9900}))
        d.reset(); self.assertFalse(d.last)
    def test_offhours_not_ingested(self):
        d=Detector(Config()); t=replace(tick(),regular=False)
        self.assertFalse(d.ingest(t,t.time))
    def test_tick_contract_signed_price_and_volume(self):
        item={'item':'005930','values':{'20':'090205','10':'-10000','15':'-20','13':'2000','27':'+10010','28':'-9990','290':'2'}}
        t=parse_tick(item,START); self.assertEqual(t.volume,20); self.assertEqual(t.price,10000)
        self.assertTrue(t.regular)

class RiskTests(unittest.TestCase):
    def test_sizing_includes_cost_and_slippage(self):
        c=Config(); qty=size(c,10000,9900,1000000,1000000,10000)
        self.assertLessEqual(qty*(100+10000*(c.roundtrip_cost_fraction+c.exit_slippage_fraction)),3000)
    def test_wider_stop_reduces_size(self):
        c=replace(Config(),max_position_fraction=0.9)
        self.assertGreater(size(c,10000,9900,1000000,1000000,10000),size(c,10000,9700,1000000,1000000,10000))
    def test_zero_cash_and_exhausted_budget(self):
        self.assertEqual(size(Config(),10000,9900,1000000,0,10000),0)
        self.assertEqual(size(Config(),10000,9900,1000000,1000000,0),0)
    def test_protection_monotonic_on_random_path(self):
        p={'avg':10000,'stop':9900,'initial_r':100,'high':10000}
        rng=random.Random(55)
        for _ in range(2000):
            before=p['stop']; p['high'],p['stop']=protective_stop(Config(),p,rng.randint(9800,12000),rng.randint(9700,10500))
            self.assertGreaterEqual(p['stop'],before)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'tests'); self.clock=[START]
        self.output=io.StringIO(); self.redirect=contextlib.redirect_stdout(self.output); self.redirect.__enter__()
    def tearDown(self):
        self.redirect.__exit__(None,None,None); self.temp.cleanup()
    def engine(self,live=False,**kwargs):
        cfg=replace(Config(),live_mode=live,**kwargs); broker=FakeBroker() if live else None
        e=Engine(cfg,Path(self.temp.name)/str(len(list(Path(self.temp.name).iterdir()))),broker,lambda:self.clock[0]); e.connected=True
        if live: e.account_ok=True; e.last_account=time.monotonic()
        return e,broker
    def enter(self,e,symbol='005930'):
        return e.enter({'symbol':symbol,'limit':10000,'stop':9900,'breakout':9990,'time':START.isoformat()},replace(tick(),symbol=symbol))
    def snap(self,e,holdings=None,orders=None):
        if holdings is None: holdings={s:{'qty':p['qty'],'avg':p['avg'],'available':p['qty']} for s,p in e.s['positions'].items()}
        return {'holdings':holdings,'orders':orders or [],'cash':800000,'equity':1000000}
    def test_paper_no_broker_order(self):
        e,b=self.engine(); o=self.enter(e)
        self.assertEqual(o['status'],'FILLED'); self.assertIn('005930',e.s['positions']); self.assertIsNone(b)
    def test_live_ack_not_fill(self):
        e,b=self.engine(True); o=self.enter(e)
        self.assertEqual(o['status'],'ACCEPTED'); self.assertFalse(e.s['positions'])
    def test_partial_fill_and_duplicate_dedup(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,3,30000); e.cumulative_fill(o,3,30000)
        self.assertEqual(e.s['positions']['005930']['qty'],3)
        self.assertTrue(e.active_orders('005930'))
    def test_excess_fill_halts(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,999,9990000)
        self.assertTrue(e.s['halt']); self.assertFalse(e.s['positions'])
    def test_ws_cumulative_fill(self):
        e,b=self.engine(True); o=self.enter(e)
        v={'9203':o['id'],'9001':'005930','907':'2','913':'체결','900':str(o['qty']),'902':str(o['qty']-2),'903':'20000','905':'+매수'}
        e.on_order_event(v); e.on_order_event(v)
        self.assertEqual(e.s['positions']['005930']['qty'],2)
    def test_reentry_blocked_after_stop(self):
        e,b=self.engine(); self.enter(e); e.on_tick(tick(1,9800,200))
        self.assertFalse(e.s['positions']); self.assertIsNone(self.enter(e))
    def test_unknown_submit_not_retried_on_restart(self):
        e,b=self.engine(True); b.error=AmbiguousOrder('timeout'); self.enter(e)
        resumed=Engine(e.c,e.folder,b,lambda:self.clock[0]); resumed.connected=True
        self.assertIsNone(self.enter(resumed)); self.assertEqual(len(b.orders),1); self.assertTrue(resumed.s['halt'])
    def test_rejected_buy_does_not_make_position(self):
        e,b=self.engine(True); b.error=Rejected('rejected'); o=self.enter(e)
        self.assertEqual(o['status'],'REJECTED'); self.assertFalse(e.s['positions']); self.assertIsNone(self.enter(e))
    def test_pending_blocks_duplicate_and_reserves_slot(self):
        e,b=self.engine(True,max_positions=1); self.enter(e)
        self.assertIsNone(self.enter(e)); self.assertIsNone(self.enter(e,'000660')); self.assertEqual(len(b.orders),1)
    def test_restart_retains_stop_and_entry_count(self):
        e,b=self.engine(); self.enter(e); e.on_tick(tick(1,10300,200))
        before=e.s['positions']['005930']['stop']; resumed=Engine(e.c,e.folder,clock=lambda:self.clock[0])
        self.assertEqual(resumed.s['positions']['005930']['stop'],before); self.assertEqual(resumed.s['entries']['005930'],1)
    def test_same_day_config_change_refused(self):
        e,b=self.engine()
        with self.assertRaises(RuntimeError): Engine(replace(e.c,risk_fraction=0.005),e.folder,clock=lambda:self.clock[0])
    def test_existing_holdings_excluded(self):
        e,b=self.engine(True); e.reconcile(self.snap(e,{'005930':{'qty':13,'avg':5822,'available':13}}))
        self.assertIsNone(self.enter(e)); self.assertFalse(b.orders)
    def test_partial_buy_exit_cancels_before_sell(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,3,30000)
        e.request_exit('005930','stop'); e.drive_exits()
        self.assertEqual(b.cancels,[o['id']]); self.assertEqual(len(b.orders),1)
        e.reconcile(self.snap(e)); e.drive_exits()
        self.assertEqual(b.orders[-1][:3],('SELL','005930',3))
    def test_late_fill_during_cancel_preserved(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,2,20000); e.request_exit('005930','stop')
        e.cumulative_fill(o,4,40000); e.reconcile(self.snap(e)); e.drive_exits()
        self.assertEqual(b.orders[-1][2],4)
    def test_partial_sell_never_duplicate_sell(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,o['qty'],10000*o['qty'])
        e.request_exit('005930','stop'); e.drive_exits(); sell=e.active_orders(side='SELL')[0]
        e.cumulative_fill(sell,2,19800); e.drive_exits()
        self.assertEqual(len(b.orders),2); self.assertEqual(e.s['positions']['005930']['qty'],o['qty']-2)
    def test_stale_position_exit_without_ticks(self):
        e,b=self.engine(); self.enter(e); self.clock[0]+=timedelta(seconds=20); e.timer({'005930':10000})
        self.assertTrue(e.s['halt']); self.assertFalse(e.s['positions'])
    def test_flatten_time_without_ticks(self):
        e,b=self.engine(); self.enter(e); self.clock[0]=START.replace(hour=15,minute=15); e.timer({'005930':10000})
        self.assertFalse(e.s['positions']); self.assertEqual(e.s['trades'][0]['reason'],'당일 청산 시각')
    def test_external_mismatch_blocks_sell_then_recovers_protection(self):
        e,b=self.engine(True); o=self.enter(e); e.cumulative_fill(o,o['qty'],o['qty']*10000)
        e.reconcile(self.snap(e,{})); e.request_exit('005930','stop'); e.drive_exits()
        self.assertEqual(len(b.orders),1)
        e.reconcile(self.snap(e)); e.drive_exits(); self.assertEqual(len(b.orders),2)
    def test_daily_loss_stops_next_symbol(self):
        e,b=self.engine(); e.s['daily_loss']=e.c.account_equity*e.c.max_daily_loss_fraction
        self.assertIsNone(self.enter(e)); self.assertFalse(e.s['positions'])
    def test_disconnected_and_stale_account_block_entries(self):
        e,b=self.engine(True); e.disconnect(); self.assertIsNone(self.enter(e)); e.connected=True; e.last_account=0
        self.assertIsNone(self.enter(e))
    def test_rest_recovers_missed_fill(self):
        e,b=self.engine(True); o=self.enter(e)
        fills=[{'ord_no':o['id'],'ord_stt':'체결','cntr_qty':str(o['qty']),'cntr_pric':'10000'}]
        holdings={'005930':{'qty':o['qty'],'avg':10000,'available':o['qty']}}
        e.reconcile(self.snap(e,holdings),fills)
        self.assertEqual(e.s['positions']['005930']['qty'],o['qty']); self.assertFalse(e.s['halt'])
    def test_outside_entry_time_blocked_in_engine(self):
        e,b=self.engine(); self.clock[0]=START.replace(hour=8)
        self.assertIsNone(self.enter(e))
    def test_external_order_excludes_symbol_immediately(self):
        e,b=self.engine(True)
        e.on_order_event({'9203':'9999999','9001':'005930','913':'접수'})
        self.assertIsNone(self.enter(e)); self.assertIn('005930',e.s['excluded'])
    def test_next_day_flat_session_allows_new_config(self):
        e,b=self.engine(); self.clock[0]+=timedelta(days=1)
        cfg=replace(e.c,risk_fraction=0.004)
        resumed=Engine(cfg,e.folder,clock=lambda:self.clock[0])
        self.assertEqual(resumed.s['config_hash'],cfg.fingerprint())
    def test_prior_day_pending_order_requires_reconciliation(self):
        e,b=self.engine(True); self.enter(e); self.clock[0]+=timedelta(days=1)
        with self.assertRaises(RuntimeError): Engine(e.c,e.folder,b,lambda:self.clock[0])
    def test_paper_no_quote_does_not_consume_exit_attempt(self):
        e,b=self.engine(); self.enter(e); e.request_exit('005930','stop')
        for _ in range(10): e.drive_exits()
        self.assertEqual(e.s['positions']['005930']['exit_attempts'],0)
    def test_mismatched_event_order_quantity_halts(self):
        e,b=self.engine(True); o=self.enter(e)
        e.on_order_event({'9203':o['id'],'9001':'005930','907':'2','913':'체결','900':'999','902':'998','903':'10000','905':'+매수'})
        self.assertTrue(e.s['halt']); self.assertFalse(e.s['positions'])

class RunnerTests(unittest.TestCase):
    def test_report_lock_does_not_stop_manager(self):
        import main
        with patch.object(main,'export',side_effect=PermissionError()),contextlib.redirect_stdout(io.StringIO()):
            main.safe_report([],type('E',(),{'s':{}})())
    def test_credentials_not_copied_and_existing_environment_wins(self):
        import main
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as temp:
            p=Path(temp)/'.env'; p.write_text('KIWOOM_APP_KEY=FROMFILE\nKIWOOM_APP_SECRET=FAKE\nUNRELATED=bad',encoding='utf-8')
            with patch.dict(os.environ,{'KIWOOM_APP_KEY':'EXISTING'},clear=True):
                main.load_credentials(p); self.assertEqual(os.environ['KIWOOM_APP_KEY'],'EXISTING')
                self.assertEqual(os.environ['KIWOOM_APP_SECRET'],'FAKE'); self.assertNotIn('UNRELATED',os.environ)
    def test_ws_login_reg_ping_and_delivery(self):
        import websocket
        from core.realtime import Feed
        rest=type('R',(),{'cfg':Config(),'get_token':lambda self:'FAKE'})()
        feed=Feed(rest,['005930']); sent=[]
        class FakeSocket:
            def __init__(self,url,**handlers): self.handlers=handlers
            def send(self,s): sent.append(json.loads(s))
            def close(self): pass
            def run_forever(self,**kwargs):
                self.handlers['on_open'](self)
                for msg in [{'trnm':'LOGIN','return_code':0},{'trnm':'REG','return_code':0},{'trnm':'PING'}]:
                    self.handlers['on_message'](self,json.dumps(msg))
                feed.stop.set()
        with patch.object(websocket,'WebSocketApp',FakeSocket): feed.run()
        self.assertEqual([x['trnm'] for x in sent],['LOGIN','REG','PING'])
        self.assertEqual(feed.q.get_nowait()[1]['trnm'],'REG')


class BrokerContractTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'KIWOOM_APP_KEY':'FAKE','KIWOOM_APP_SECRET':'FAKE'}); self.env.start()
    def tearDown(self): self.env.stop()
    def rest(self,live=False):
        r=Rest(replace(Config(),live_mode=live)); r.get_token=lambda:'FAKE'; return r
    def test_dry_run_guard_before_network(self):
        r=self.rest(); r._http=lambda *a: self.fail('network called')
        with self.assertRaises(Rejected): r.submit('BUY','005930',1,10000)
        with self.assertRaises(Rejected): r.cancel('005930','0000001')
    def test_live_order_payload_matches_official_spec(self):
        r=self.rest(True); calls=[]
        r._http=lambda path,body,headers:(calls.append((path,body,headers)) or ({'return_code':0,'ord_no':'0000001'},{}))
        self.assertEqual(r.submit('BUY','005930',3,10000),'0000001')
        path,body,h=calls[0]; self.assertEqual(path,'/api/dostk/ordr'); self.assertEqual(h['api-id'],'kt10000'); self.assertEqual(body['trde_tp'],'0')
        spec=json.loads((ROOT/'reference/contract.json').read_text(encoding='utf-8'))
        required={x['element'] for x in spec['kt10000']['request']['body'] if x['required']=='Y'}
        self.assertFalse(required-set(body))
    def test_sell_is_market_and_cancel_has_original_id(self):
        r=self.rest(True); calls=[]
        r._http=lambda path,body,headers:(calls.append((body,headers)) or ({'return_code':0,'ord_no':'0000002'},{}))
        r.submit('SELL','005930',3); r.cancel('005930','0000001')
        self.assertEqual(calls[0][0]['trde_tp'],'3'); self.assertEqual(calls[1][0]['orig_ord_no'],'0000001')
    def test_order_timeout_never_retried(self):
        r=self.rest(True); calls=[]
        def fail(*args): calls.append(args); raise TimeoutError()
        r._http=fail
        with self.assertRaises(AmbiguousOrder): r.submit('BUY','005930',1,10000)
        self.assertEqual(len(calls),1)
    def test_missing_order_id_is_ambiguous(self):
        r=self.rest(True); r._http=lambda *args:({'return_code':0},{})
        with self.assertRaises(AmbiguousOrder): r.submit('BUY','005930',1,10000)
    def test_error_code_rejected(self):
        r=self.rest(True); r._http=lambda *args:({'return_code':20},{})
        with self.assertRaises(Rejected): r.submit('BUY','005930',1,10000)
    def test_query_pagination_not_truncated(self):
        r=self.rest(); sequence=iter([({'rows':[1]},{'cont-yn':'Y','next-key':'a'}),({'rows':[2]},{'cont-yn':'N'})]); r.call=lambda *args:next(sequence)
        self.assertEqual(r.pages('x',{},'rows')[0],[1,2])
    def test_repeated_pagination_key_rejected(self):
        r=self.rest(); r.call=lambda *args:({'rows':[1]},{'cont-yn':'Y','next-key':'a'})
        with self.assertRaises(ValueError): r.pages('x',{},'rows')
    def test_missing_account_list_is_not_empty_success(self):
        r=self.rest(); r.call=lambda *args:({'return_code':0},{})
        with self.assertRaises(ValueError): r.pages('x',{},'holdings')
    def test_rank_payload_required_fields_verified(self):
        spec=json.loads((ROOT/'reference/contract.json').read_text(encoding='utf-8'))
        for api,key,limit,payload in RANKS:
            with self.subTest(api=api):
                required={x['element'] for x in spec[api]['request']['body'] if x['required']=='Y'}
                self.assertFalse(required-set(payload))
                self.assertIn(key,{x['element'] for x in spec[api]['response']['body']})

if __name__=='__main__': unittest.main()
