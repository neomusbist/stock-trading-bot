"""Synthetic closed-loop fixtures; not evidence of historical market trades."""
from dataclasses import asdict
from datetime import timedelta
import hashlib,json
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from automation.policy import AutomationConfig
from core.settings import Config
from research.evidence_collector import freeze_session
from research.initial_structure_replay import main
from tests import test_initial_structure as fixture
from tests import test_support_recovery as recovery_fixture
AT=fixture.AT

class InitialReplay(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.live=self.root/'input';self.live.mkdir()
        self.freeze=self.root/'frozen';self.output=self.root/'result'
        self.cfg=Config(max_position_fraction=1.,risk_fraction=.02,max_daily_loss_fraction=.04,min_reward_risk=1.5)
        self.auto=AutomationConfig(costs_confirmed=True,AUTO_TRADING_CAP_KRW=81300,
            confirmed_invalidation_enabled=True,fast_momentum_enabled=False,pullback_expansion_target_enabled=False)
        for name,value in [('config.json',asdict(self.cfg)),('automation.json',asdict(self.auto))]:
            (self.live/name).write_text(json.dumps(value,default=str),encoding='utf-8')
        h=fixture.InitialStructure();h.setUp();self.h=h
        self.rows=[]
        references=[{**b,'time':b['time'].isoformat()} for b in h.refs[:3]]
        raw=[[b['time']]+[b[k] for k in ('open','high','low','close')]+[b['volume']] for b in references]
        self.rows.append({'event':'REFERENCE_BARS_PART','symbol':'999990','parts':1,'part':0,'rows':references,
            'reference_sha256':hashlib.sha256(json.dumps(raw,separators=(',',':')).encode()).hexdigest(),
            'known_at':(AT-timedelta(minutes=3)).isoformat(),'venue':'KRX'})
        for b in h.base:
            self.rows.append({'event':'COMPLETED_BAR','symbol':'999990','bar':{**b,'time':b['time'].isoformat()},
                'source':'ACTUAL_RECEIVED_DETECTOR_HISTORY','known_at':b['available_at']})
        for offset,price,cum in [(-1,1004,100),(1,1006,200),(2,1007,300),(10,977,400)]:
            t=h.tick(offset,price,cum)
            self.rows.append({'event':'TRADE','capture_point':'STRATEGY_INGESTION','symbol':t.symbol,
                'exchange_at':t.time.isoformat(),'received_at':t.received_at,'processed_at':t.time.isoformat(),
                **{k:getattr(t,k) for k in ('price','volume','cumulative','ask','bid','ask_qty','bid_qty','regular','depth_available','depth_source','book_received_at')},
                'book_time':t.book_time.isoformat(),'receipt_evidence':{'trade_receipt_id':'SYNTHETIC_FIXTURE:'+str(cum)},
                'input_evidence_basis':'SYNTHETIC_TEST_QUOTE'})
        t=h.tick()
        self.rows.append({'event':'OPPORTUNITY_SAMPLE','symbol':t.symbol,'asof':(t.time+timedelta(milliseconds=100)).isoformat(),
            'processed_at':(t.time+timedelta(milliseconds=100)).isoformat(),'exchange_at':t.time.isoformat(),
            'received_at':t.received_at,'cumulative':t.cumulative,'price':t.price,
            **{k:getattr(t,k) for k in ('bid','ask','bid_qty','ask_qty','depth_available','depth_source','book_received_at')},
            'book_time':t.book_time.isoformat(),'watch':{'status':'WATCH','source_count':2},
            'market_status':{'pass':True,'checked_at':t.time.isoformat()},'trials':[]})
    def run_fixture(self,**kwargs):
        capture=self.live/'runtime/automation-production/trade-review/trade-review-20261023-0001.jsonl'
        capture.parent.mkdir(parents=True);capture.write_text(''.join(json.dumps(r,default=str)+'\n' for r in self.rows),encoding='utf-8')
        freeze_session(self.live,self.freeze,'2026-10-23')
        args=['--freeze',str(self.freeze),'--output',str(self.output)]
        if kwargs.get('cash',True):args+=['--scenario-cash','70000']
        if kwargs.get('paper'):args+=['--paper']
        if kwargs.get('hypothesis'):args+=['--hypothesis',kwargs['hypothesis']]
        with patch('builtins.print'):main(args)
        return json.loads((self.output/'RESULT.json').read_bytes())
    def test_each_actual_trade_is_evaluated_before_a_sample_exists(self):
        d=self.run_fixture()
        self.assertEqual(d['counts']['accepted_trades'],4)
        self.assertEqual(d['unique_L1_signal_structures'],1)
        r=d['signals'][0]
        self.assertLess(r['first_L1_at'],r['first_qualified_L1_at'])
        self.assertEqual(r['trigger_at'],self.h.tick().time.isoformat())
        self.assertFalse(r['signal']['actual_order_permission'])
    def test_no_trade_is_fabricated_from_a_qualification_sample(self):
        self.rows=[r for r in self.rows if r.get('event')!='TRADE']
        d=self.run_fixture();self.assertEqual(d['unique_L1_signal_structures'],0)
    def test_missing_cash_remains_unknown(self):
        d=self.run_fixture(cash=False)
        self.assertIsNone(d['signals'][0]['signal']['planned_qty'])
        self.assertIsNone(d['paper_account'])
    def test_real_engine_paper_intent_fill_and_emergency_exit_are_isolated(self):
        d=self.run_fixture(paper=True)
        self.assertIsNotNone(d['paper_account'])
        self.assertIsNotNone(d['signals'][0]['paper_order'],d['paper_account'])
        self.assertGreaterEqual(d['paper_account']['orders_submitted'],2)
        self.assertEqual(d['paper_account']['completed_count'],1)
        self.assertEqual(d['actual_orders'],0)
        self.assertTrue(all(f['input_evidence_basis']=='SYNTHETIC_TEST_QUOTE' for f in d['paper_account']['fills']))

    def test_recovery_signal_reaches_owned_paper_intent_and_protected_exit(self):
        f=recovery_fixture.Recovery();f.setUp()
        refs=[{**b,'time':b['time'].isoformat()} for b in f.refs[:3]]
        raw=[[b['time']]+[b[k] for k in ('open','high','low','close')]+[b['volume']] for b in refs]
        self.rows[0].update(rows=refs,reference_sha256=hashlib.sha256(json.dumps(raw,separators=(',',':')).encode()).hexdigest())
        for row,b in zip(self.rows[1:4],f.base):
            row.update(bar={**b,'time':b['time'].isoformat()},known_at=b['available_at'])
        trades=[r for r in self.rows if r['event']=='TRADE']
        for row,values in zip(trades,[(1,1015,100),(2,1012,200),(3,1016,300),(10,960,400)]):
            t=f.helper.tick(*values)
            row.update(exchange_at=t.time.isoformat(),received_at=t.received_at,processed_at=t.time.isoformat(),
                **{k:getattr(t,k) for k in ('price','cumulative','ask','bid','ask_qty','bid_qty','book_received_at')},
                book_time=t.book_time.isoformat())
        sample=self.rows[-1];t=f.helper.tick(3,1016,300)
        sample.update(asof=(t.time+timedelta(milliseconds=100)).isoformat(),
            processed_at=(t.time+timedelta(milliseconds=100)).isoformat(),exchange_at=t.time.isoformat(),
            received_at=t.received_at,cumulative=t.cumulative,price=t.price,
            bid=t.bid,ask=t.ask,book_time=t.book_time.isoformat(),book_received_at=t.book_received_at)
        d=self.run_fixture(paper=True,hypothesis='B');self.assertEqual(d['unique_L1_signal_structures'],1)
        self.assertIsNotNone(d['signals'][0]['paper_order'],d['paper_account'])
        self.assertEqual(d['paper_account']['completed_count'],1)
        self.assertTrue(all(f['input_evidence_basis']=='SYNTHETIC_TEST_QUOTE' for f in d['paper_account']['fills']))
        self.assertEqual(d['actual_orders'],0)

if __name__=='__main__':unittest.main()
