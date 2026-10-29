"""Production path tests. All prices/packets here are labelled synthetic.

No detector, order engine, sizing, fills or exits are mocked. The L1 broker
owns a temporary journal and rejects submissions without engine INTENT.
"""
import json
import contextlib
import io
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import AutomationConfig
from research.causal_data import Frame
from research.strict_benchmark import (FullPathReplay, normalize_cache_row,
    missing_execution_evidence, valid_tick_price)
from tools.benchmark_all_strategies import main, run_events, read_settings

AT = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
SYMBOL = '089890'


def settings():
    cfg = replace(Config(), risk_fraction=.02, max_daily_loss_fraction=.04,
        max_position_fraction=1., max_positions=1, max_entries_per_day=None,
        max_entries_per_symbol=None)
    auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300,
        costs_confirmed=True, max_consecutive_losses=3, cooldown_seconds=900)
    return cfg.validate(), auto.validate()


def packet(at, price, cumulative, volume=20000, symbol=SYMBOL, depth=1000):
    step = 50 if price >= 20000 else 10
    t = Tick(symbol, at, price, volume, cumulative, price, price-step, True,
        ask_qty=depth, bid_qty=depth, depth_available=bool(depth),
        book_time=at, depth_source='0D', received_at=at.isoformat(),
        book_received_at=at.isoformat())
    return Frame(t, at, at, 'SYNTHETIC_TEST_QUOTE', at.isoformat())


def metadata(at, resistance=45000):
    return dict(code=SYMBOL, status='WATCH', source_count=2,
        source_details={'SYNTHETIC_1': {}, 'SYNTHETIC_2': {}}, resistance=resistance,
        market_status={'pass':True, 'checked_at':at.isoformat(),
                       'reason_code':'SYNTHETIC_STATUS_NORMAL'})


def populate(replay):
    start = AT-timedelta(minutes=5)
    daily = [dict(datetime=(start.date()-timedelta(days=i)).isoformat(),
        open=35000, high=38000, low=34000, close=37000, volume=500000)
        for i in range(600, 0, -1)]
    # One previously known resistance, not 600 identical high-volume bars
    # above the entry: those fail the production overhead supply gate.
    daily[250]['high'] = 45000
    replay.reference(SYMBOL, daily, start)
    # Construct candles solely by causal ingestion of explicitly synthetic prints.
    candles = [(37000,37300,37000,37300), (37300,38500,37200,38400),
               (38400,38400,37800,37900), (37900,38000,37600,37750),
               (37750,38100,37700,38050)]
    cumulative = 0
    for minute, (op, hi, lo, cl) in enumerate(candles):
        for sec, price in ((0,op),(15,hi),(30,lo),(59,cl)):
            at=start+timedelta(minutes=minute,seconds=sec)
            replay.qualify(SYMBOL,metadata(at),at)
            cumulative+=20000
            replay.step(packet(at,price,cumulative))
    return cumulative


class EvidenceContractTests(unittest.TestCase):
    def test_alias_is_restored_without_fabricating_other_fields(self):
        row=normalize_cache_row({'vol':4074,'price':17170},'253590')
        self.assertEqual(row['volume'],4074)
        self.assertNotIn('ask',row)
        self.assertIn('received_at',missing_execution_evidence(row))
        with self.assertRaisesRegex(ValueError,'VOLUME_ALIAS_CONFLICT'):
            normalize_cache_row({'vol':4074,'volume':1})

    def test_neosem_legacy_17214_is_not_a_valid_market_price(self):
        self.assertFalse(valid_tick_price(17214))
        self.assertTrue(valid_tick_price(17180))
        self.assertFalse(valid_tick_price(float('nan')))

    def test_missing_watch_never_acquires_default_order_permission(self):
        cfg,auto=settings(); replay=FullPathReplay(AT,'C',cfg,auto,70420)
        self.addCleanup(replay.close)
        replay.step(packet(AT,17180,20000,symbol='253590'))
        self.assertEqual(replay.reasons['POINT_IN_TIME_WATCH_UNKNOWN'],1)
        self.assertEqual(replay.account.broker.submissions,[])

    def test_incomplete_settings_and_future_reference_rejected(self):
        cfg,auto=settings(); replay=FullPathReplay(AT,'A',cfg,auto,70420)
        self.addCleanup(replay.close)
        with self.assertRaisesRegex(ValueError,'COMPLETED_PRIOR_DAILY'):
            replay.reference(SYMBOL,[{'date':AT.date().isoformat()}],AT)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'INCOMPLETE_EXPLICIT_SETTINGS'):
                read_settings(p,Config)

    def test_cache_audit_cannot_overwrite_input_or_claim_missing_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'cache.json';out=Path(tmp)/'audit.json'
            original=json.dumps({'253590':[{'time':AT.isoformat(),'price':17180,'vol':1}]}).encode()
            p.write_bytes(original)
            result=main(['--cache',str(p),'--output',str(out)])
            audit=result['cache_audits'][0]
            self.assertEqual(audit['counts']['volume_alias_restored'],1)
            self.assertIsNone(audit['actual_orderable_signals'])
            self.assertIsNone(audit['net_pnl'])
            with self.assertRaisesRegex(ValueError,'OVERWRITE_INPUT'):
                main(['--cache',str(p),'--output',str(p)])
            self.assertEqual(p.read_bytes(),original)

    def test_timeline_cannot_hide_missing_clock_or_reorder_evidence(self):
        cfg,auto=settings()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            path.write_text(json.dumps({'event':'TRADE','available_at':AT.isoformat(),
                'symbol':SYMBOL,'time':AT.isoformat(),'price':38100,'volume':1})+'\n')
            result=run_events(path,cfg,auto,70420,['C'])
            self.assertEqual(result['counts']['MISSING:received_at'],1)
            self.assertEqual(result['strategies']['C']['orders_submitted'],0)
            rows=[dict(event='WATCH_METADATA',symbol=SYMBOL,watch=metadata(t),known_at=t.isoformat())
                  for t in (AT,AT-timedelta(seconds=1))]
            path.write_text('\n'.join(map(json.dumps,rows))+'\n')
            with self.assertRaisesRegex(ValueError,'NONCAUSAL_INPUT_ORDER'):
                run_events(path,cfg,auto,70420,['C'])

    def test_missing_book_does_not_erase_an_actual_trade_from_detector(self):
        cfg,auto=settings()
        frame=packet(AT,38100,20000)
        row=asdict(frame.tick)
        row.update(event='TRADE',processed_at=AT.isoformat(),input_evidence_basis='SYNTHETIC_TEST_QUOTE',
                   ask_qty=None,bid_qty=None,depth_available=False,depth_source='BOOK_PRICE_MISMATCH')
        from research.causal_data import json_values
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl';path.write_text(json.dumps(json_values(row))+'\n')
            result=run_events(path,cfg,auto,70420,['C'])
            self.assertEqual(result['counts']['TRADE'],1)
            self.assertEqual(result['strategies']['C']['frames'],1)
            self.assertEqual(result['strategies']['C']['reasons']['MEASURED_DEPTH_UNKNOWN'],1)
            self.assertEqual(result['strategies']['C']['orders_submitted'],0)

    def test_quote_retry_requires_the_same_prior_observed_trade(self):
        from research.causal_data import json_values
        cfg,auto=settings();frame=packet(AT,38100,20000)
        trade={**json_values(asdict(frame.tick)), 'event':'TRADE',
               'processed_at':AT.isoformat(),'input_evidence_basis':'SYNTHETIC_TEST_QUOTE'}
        later=(AT+timedelta(seconds=1)).isoformat()
        retry={**trade,'event':'QUOTE_RETRY','processed_at':later,
               'book_time':later,'book_received_at':later,'ask_qty':1500}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            path.write_text(json.dumps(retry)+'\n')
            with self.assertRaisesRegex(ValueError,'QUOTE_RETRY_WITHOUT_SAME_OBSERVED_TRADE'):
                run_events(path,cfg,auto,70420,['C'])
            path.write_text(json.dumps(trade)+'\n'+json.dumps(retry)+'\n')
            result=run_events(path,cfg,auto,70420,['C'])
            self.assertEqual(result['counts']['TRADE'],1)
            self.assertEqual(result['counts']['QUOTE_RETRY'],1)
            self.assertEqual(result['strategies']['C']['orders_submitted'],0)


class FullProductionPathTests(unittest.TestCase):
    def setUp(self):
        self.output=io.StringIO()
        self.quiet=contextlib.redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__,None,None,None)

    def scenario(self):
        cfg,auto=settings()
        replay=FullPathReplay(AT-timedelta(minutes=5),'E2',cfg,auto,70420,latency_ms=25)
        self.addCleanup(replay.close)
        cumulative=populate(replay)
        replay.qualify(SYMBOL,metadata(AT),AT)
        return replay,cumulative

    def test_real_detector_runner_intent_fill_profit_protection_exit(self):
        replay,cum=self.scenario()
        order=replay.step(packet(AT,38100,cum+20000))
        self.assertIsNotNone(order,replay.result())
        self.assertEqual(order['side'],'BUY')
        self.assertEqual(order['qty'],1)
        self.assertEqual(replay.account.broker.fills,[])  # latency is respected
        replay.step(packet(AT+timedelta(seconds=1),38100,cum+40000))
        self.assertIn(SYMBOL,replay.account.e.s['positions'])
        replay.step(packet(AT+timedelta(seconds=2),40200,cum+60000))
        replay.step(packet(AT+timedelta(seconds=3),39500,cum+80000))
        replay.step(packet(AT+timedelta(seconds=4),39450,cum+100000))
        result=replay.result()
        self.assertEqual([f['side'] for f in result['fills']],['BUY','SELL'])
        self.assertEqual(result['completed_count'],1)
        self.assertGreater(result['net_pnl_closed'],0)
        self.assertFalse(result['forced_close_from_last_bar'])
        self.assertFalse(result['actual_order_permission'])
        self.assertTrue(all(f['input_evidence_basis']=='SYNTHETIC_TEST_QUOTE' for f in result['fills']))
        self.assertEqual(len([o for o in replay.account.broker.submissions if o['side']=='BUY']),1)
        journal=(replay.account.e.folder/'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('INTENT',journal)

    def test_real_engine_loss_exit_and_cash_conservation(self):
        replay,cum=self.scenario()
        self.assertIsNotNone(replay.step(packet(AT,38100,cum+20000)))
        replay.step(packet(AT+timedelta(seconds=1),38100,cum+40000))
        replay.step(packet(AT+timedelta(seconds=2),37000,cum+60000))
        replay.step(packet(AT+timedelta(seconds=3),37000,cum+80000))
        result=replay.result()
        self.assertEqual(result['completed_count'],1)
        self.assertLess(result['net_pnl_closed'],0)
        self.assertAlmostEqual(result['final_broker_cash']-70420,result['net_pnl_closed'])

    def test_pending_buy_cannot_fill_at_an_off_tick_quote(self):
        replay,cum=self.scenario()
        self.assertIsNotNone(replay.step(packet(AT,38100,cum+20000)))
        bad=packet(AT+timedelta(seconds=1),38100,cum+40000)
        bad=replace(bad,tick=replace(bad.tick,ask=38099,bid=38050))
        replay.step(bad)
        self.assertEqual(replay.account.broker.fills,[])
        self.assertEqual(replay.reasons['OFF_TICK_MARKET_PRICE'],1)

    def test_open_position_is_not_sold_at_invented_end_of_data_price(self):
        replay,cum=self.scenario()
        self.assertIsNotNone(replay.step(packet(AT,38100,cum+20000)))
        replay.step(packet(AT+timedelta(seconds=1),38100,cum+40000))
        result=replay.result()
        self.assertEqual(result['completed_count'],0)
        self.assertIn(SYMBOL,result['open_positions'])
        self.assertEqual(len(result['fills']),1)


if __name__=='__main__':unittest.main()
