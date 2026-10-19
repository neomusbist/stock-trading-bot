"""Offline receiver-to-file execution proof; synthetic fills are never profits evidence."""
import ast
import json
import queue
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request

from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.fast_momentum import FastSignal
from automation.observation_worker import TickLogger,ObservationWorker
from automation.shadow_fast_runtime import ShadowFastRuntime,VirtualAccount,MarketDataOnly,ensure_isolated,assert_source_stopped

BASE=datetime(2026,10,15,9,3,tzinfo=KST)
CFG=Config(risk_fraction=.02,max_daily_loss_fraction=.04,max_positions=1,max_position_fraction=1.)
AUTO=AutomationConfig(AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True)
COST=Costs(.00015,.00015,.002,.002,.003)

def tick(sec=0,price=1000,depth=True):
    at=BASE+timedelta(seconds=sec)
    return Tick('001210',at,price,100,100*(sec+1),price+1,price,
        ask_qty=500,bid_qty=500,depth_available=depth,book_time=at,depth_source='0D')

def row():
    return {'code':'001210','name':'Synthetic','status':'WATCH','source_count':2,'reference_price':1000,
        'observed_at':BASE.isoformat(),'affordability':{'entry_eligible':True},
        'market_status':{'pass':True},'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True},'resistance':1200,
        'resistance_asof':BASE.isoformat()}}

def wait(predicate):
    end=time.monotonic()+3
    while not predicate() and time.monotonic()<end:time.sleep(.01)
    if not predicate():raise AssertionError('Worker did not drain before deadline')


class ShadowRuntimeTests(unittest.TestCase):
    def test_missing_depth_zero_defaults_are_logged_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=TickLogger(Path(tmp)/'ticks.jsonl');t=replace(tick(),depth_available=False,ask_qty=0,bid_qty=0)
            log.log(t,BASE,processed=BASE+timedelta(milliseconds=5),sequence=7);log.flush()
            r=json.loads(log.path.read_text())
            self.assertIsNone(r['ask_qty']);self.assertIsNone(r['bid_qty']);self.assertEqual(r['transport_sequence'],7)
            self.assertNotEqual(r['receiver_at'],r['processed_at'])
            self.assertEqual(r['depth_status'],'UNKNOWN_NOT_PROVIDED_OR_UNUSABLE')

    def test_explicit_zero_depth_is_preserved_as_real_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            log=TickLogger(Path(tmp)/'ticks.jsonl');log.log(replace(tick(),ask_qty=0,bid_qty=0),BASE);log.flush()
            self.assertEqual(json.loads(log.path.read_text())['ask_qty'],0)

    def test_close_drains_queued_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=ObservationWorker(CFG,AUTO,COST,Path(tmp));w.clock=lambda:BASE
            for n in range(20):w.deliver(tick(0),BASE)
            w.start();result=w.close()
            self.assertEqual(result['tick_logger']['rows_written'],20)
            self.assertTrue(result['drained']);self.assertEqual(result['processed_sequence'],20)

    def test_close_flush_failure_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=ObservationWorker(CFG,AUTO,COST,Path(tmp));w.tick_logger.log(tick(),BASE)
            w.tick_logger.flush=Mock(return_value=False)
            with self.assertRaisesRegex(RuntimeError,'SHADOW_CLOSE_FLUSH_FAILED'):w.close()
            self.assertTrue(w.state()['last_error'])

    def test_queue_overflow_has_sequences_and_resets_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=ObservationWorker(CFG,AUTO,COST,Path(tmp));w.clock=lambda:BASE;w.ticks=queue.Queue(maxsize=1)
            w.observer.fast_strategy.reset=Mock()
            self.assertTrue(w.deliver(tick(),BASE));self.assertFalse(w.deliver(tick(1),BASE))
            w.start();wait(lambda:w.processed_sequence==1)
            self.assertTrue(w.deliver(tick(2),BASE));wait(lambda:w.processed_sequence==3)
            stats=w.close()
            self.assertEqual(stats['dropped'],1);self.assertEqual(stats['missing_sequence_ranges'],[[2,2]])
            self.assertTrue(w.observer.fast_strategy.reset.called)
            records=[json.loads(s) for s in (Path(tmp)/'runtime/shadow_events.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['event']=='SHADOW_SEQUENCE_GAP' and r['missing_sequence_range']==[2,2] for r in records))

    def test_day_and_expired_unsubscribe_clear_fast_tape(self):
        from automation.shadow_observation import ShadowObserver
        s=ShadowObserver(CFG,AUTO,COST,lambda *a,**k:None);s.update([row()],BASE)
        s.tick(tick(),BASE);self.assertEqual(len(s.fast_strategy.get_tape('001210').ticks),1)
        s.update([],BASE+timedelta(seconds=1));s.update([],BASE+timedelta(seconds=8))
        self.assertEqual(len(s.fast_strategy.get_tape('001210').ticks),0)
        s.update([row()],BASE+timedelta(days=1))
        self.assertEqual(len(s.fast_strategy.get_tape('001210').ticks),0)

    def test_mock_feed_worker_fast_virtual_files_integrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=[BASE];r=ShadowFastRuntime(CFG,AUTO,tmp,clock=lambda:clock[0])
            strategy=r.worker.observer.fast_strategy
            def evaluate(t,*args,**kw):
                if len(strategy.get_tape(t.symbol).ticks)<3:return None
                return FastSignal(symbol=t.symbol,name='Synthetic',limit=t.ask,stop=980,target=1200,
                    emergency=970,quantity=10,net_reward_risk=2.,gross_reward_risk=3.,reward_net_per_share=100,
                    risk_net_per_share=40,regime='A',phase='OPENING_BUILD',created_at=t.time)
            strategy.evaluate=Mock(side_effect=evaluate)
            strategy.evaluate_exit=Mock(return_value=(True,'SYNTHETIC_EXIT_FOR_WIRING_TEST',{}))
            strategy.record_exit=Mock();r.worker.start();r.candidates([row()],BASE)
            wait(lambda:'001210' in r.worker.observer.rows)
            for sec in range(8):
                clock[0]=BASE+timedelta(seconds=sec);price=1000 if sec<5 else 1060
                frame={'trnm':'REAL','data':[
                    {'type':'0D','item':'001210','values':{'21':clock[0].strftime('%H%M%S'),'41':str(price+1),'51':str(price),'61':'500','71':'500'}},
                    {'type':'0B','item':'001210','values':{'20':clock[0].strftime('%H%M%S'),'10':str(price),'15':'100','13':str((sec+1)*100),'27':str(price+1),'28':str(price),'290':'2'}}]}
                r.raw(frame,clock[0]);wait(lambda:r.worker.processed_sequence==r.worker.input_sequence)
            result=r.close();records=[json.loads(s) for s in (Path(tmp)/'runtime/shadow_ticks.jsonl').read_text().splitlines()]
            self.assertEqual(sum(x['event']=='TRADE' for x in records),8)
            self.assertEqual(sum(x['event']=='BOOK' for x in records),8)
            self.assertEqual(len(strategy.get_tape('001210').ticks),8)
            self.assertGreaterEqual(result['entries'],1);self.assertGreaterEqual(result['closed_round_trips'],1)
            self.assertEqual(result['actual_order_calls'],0);self.assertFalse(result['profitability_proven'])
            self.assertTrue(strategy.record_exit.called)
            ledger=[json.loads(x) for x in (Path(tmp)/'runtime/shadow_virtual_ledger.jsonl').read_text().splitlines()]
            self.assertTrue(all(x['evidence_class']=='MODELED_NOT_BROKER_FILL' and x['actual_order_permission'] is False for x in ledger))

    def test_tape_end_does_not_close_virtual_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy=Mock();a=VirtualAccount(CFG,AUTO,tmp,strategy)
            a.position={'symbol':'001210','qty':1,'avg':1000,'paid':1000.15};a.cash-=1000.15
            result=a.finish()
            self.assertEqual(result['closed_round_trips'],0);self.assertEqual(result['realized_net_pnl'],0)
            self.assertIsNotNone(result['open_position']);strategy.record_exit.assert_not_called()

    def test_actual_fast_algorithm_reaches_pending_entry_and_tape_end_remains_open(self):
        from tests.test_r6_fast_repair import ticks,watch
        with tempfile.TemporaryDirectory() as tmp:
            start=BASE.replace(minute=1);clock=[start];r=ShadowFastRuntime(CFG,AUTO,tmp,clock=lambda:clock[0])
            meta=watch(at=start);meta.update(code='999990',affordability={'entry_eligible':True},
                shadow_view={'status':'ELIGIBLE','market_status':{'pass':True},'resistance':1045,'resistance_asof':start.isoformat()})
            # The standalone metadata worker stores its actual structure in
            # shadow_view. FAST must receive that merged view, not a bare RAW row.
            meta.pop('resistance');meta.pop('resistance_asof')
            r.worker.observer.update([meta],start);r.worker.start()
            data=ticks(start=start)
            data.append(replace(data[-1],time=start+timedelta(seconds=11),cumulative=12000,
                book_time=start+timedelta(seconds=11)))
            for t in data:
                clock[0]=t.time;r.worker.deliver(t,t.time);wait(lambda:r.worker.processed_sequence==r.worker.input_sequence)
            result=r.close()
            self.assertEqual(result['entries'],1);self.assertEqual(result['closed_round_trips'],0)
            self.assertIsNotNone(result['open_position']);self.assertFalse(result['profitability_proven'])

    def test_sell_fill_does_not_require_buy_side_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            strategy=Mock();a=VirtualAccount(CFG,AUTO,tmp,strategy)
            a.position={'symbol':'001210','qty':10,'avg':1000,'paid':10001.5,'emergency':970}
            a.exit_pending={'timestamp':BASE.isoformat(),'reason':'TEST'}
            t=replace(tick(1),ask_qty=0)
            a.tick(t,t.time,t.time)
            self.assertEqual(a.closed,1);self.assertIsNone(a.position);strategy.record_exit.assert_called_once()

    def test_http_facade_denies_order_endpoints_before_network(self):
        market=MarketDataOnly(CFG)
        with patch('automation.shadow_fast_runtime.urlopen') as network:
            with self.assertRaisesRegex(RuntimeError,'ENDPOINT_DENIED'):
                market._open(Request('https://api.kiwoom.com/api/dostk/ordr',headers={'api-id':'kt10000'}))
            with self.assertRaisesRegex(RuntimeError,'READ_API_DENIED'):market.query('kt10000',{})
            network.assert_not_called()
        self.assertFalse(hasattr(market,'submit'));self.assertFalse(hasattr(market,'cancel'))

    def test_active_source_rejected_before_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            market=MarketDataOnly(CFG,source_root=tmp)
            with patch('operator_process.busy',return_value=True),patch.object(market,'_bootstrap') as auth:
                with self.assertRaisesRegex(RuntimeError,'LOCK_ACTIVE'):market.get_token()
                auth.assert_not_called()

    def test_token_file_cannot_bypass_live_collision_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            token=Path(tmp)/'private-source';token.write_text('synthetic-test-input')
            market=MarketDataOnly(CFG,token_file=token)
            with patch('automation.shadow_fast_runtime.urlopen') as network:
                with self.assertRaisesRegex(RuntimeError,'SOURCE_ROOT_REQUIRED'):market.get_token()
                network.assert_not_called()

    def test_cli_check_cannot_claim_collision_pass_without_source_root(self):
        from automation.shadow_fast_runtime import main
        with tempfile.TemporaryDirectory() as tmp,patch('automation.shadow_fast_runtime.urlopen') as network:
            with self.assertRaisesRegex(RuntimeError,'SOURCE_ROOT_REQUIRED'):main(['--check','--output',tmp])
            network.assert_not_called()

    def test_source_bot_pending_unknown_block_and_output_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'runtime/automation-production').mkdir(parents=True)
            (p/'runtime/automation-production/state.json').write_text(json.dumps({'orders':{'x':{'status':'UNKNOWN'}},'positions':{}}))
            with patch('operator_process.busy',return_value=False),patch('operator_process.process_identity',return_value=None):
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN_ORDER'):assert_source_stopped(p)
            with self.assertRaisesRegex(RuntimeError,'OUTSIDE_LIVE'):ensure_isolated(p/'shadow',p)

    def test_runtime_contains_no_order_or_live_engine_surface(self):
        p=Path(__file__).resolve().parents[1]/'automation/shadow_fast_runtime.py'
        tree=ast.parse(p.read_text(encoding='utf-8'))
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        self.assertNotIn('automation.engine',imports)
        self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr in {'send_order','cancel_order','place_order'} for n in ast.walk(tree)))
        # ObservationWorker.submit accepts candidate snapshots, not orders.
        self.assertTrue(all(isinstance(n.value,ast.Attribute) and n.value.attr=='worker'
            for n in ast.walk(tree) if isinstance(n,ast.Attribute) and n.attr=='submit'))

    def test_capture_metadata_and_subscription_use_same_ranked_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ShadowFastRuntime(CFG,AUTO,tmp)
            rows=[dict(row(),code=f'{n:06d}',source_count=1+n%3,
                source_details={'x':{'observed_rank':9-n}}) for n in range(8)]
            scope=r.capture_rows(rows,limit=3)
            self.assertEqual([x['code'] for x in scope],['000005','000002','000007'])
            self.assertEqual(len(scope),3)

    def test_unknown_quote_clock_never_uses_receiver_time_as_exchange_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ShadowFastRuntime(CFG,AUTO,tmp,clock=lambda:BASE);r.worker.start()
            r.raw({'trnm':'REAL','data':[{'type':'0D','item':'001210',
                'values':{'41':'1001','51':'1000','61':'500','71':'500'}}]},BASE)
            wait(lambda:r.worker.processed_sequence==1);r.close()
            record=json.loads((Path(tmp)/'runtime/shadow_ticks.jsonl').read_text(encoding='utf-8').splitlines()[0])
            self.assertIsNone(record['exchange_at']);self.assertEqual(record['receiver_at'],BASE.isoformat())
            self.assertEqual(record['quote_time_basis'],'UNKNOWN_NO_EXCHANGE_TIME')

    def test_evaluation_clock_is_independent_from_duplicate_log_suppression(self):
        from automation.shadow_observation import ShadowObserver
        s=ShadowObserver(CFG,AUTO,COST,lambda *a,**k:None);s.update([row()],BASE)
        s.fast_strategy.evaluate=Mock(return_value=None)
        for n in range(50):
            at=BASE+timedelta(milliseconds=n*100)
            t=replace(tick(),time=at,book_time=at,cumulative=100*(n+1))
            s.tick(t,at)
        self.assertEqual(len(s.fast_strategy.get_tape('001210').ticks),50)
        self.assertEqual(s.fast_strategy.evaluate.call_count,3)


if __name__=='__main__':unittest.main()
