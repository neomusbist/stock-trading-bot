"""Execution provenance, exit explanation and complete rotated evidence."""
import copy,json,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from core.broker import Rest,Rejected
from core.settings import Config
from operator_ui_service import context_description
from tools.session_audit import audit
from tests import test_position_data_health as fixture

class ReportingTests(unittest.TestCase):
    setUp=fixture.PositionHealthTests.setUp
    def test_existing_one_R_giveback_rule_is_explained_without_changing_stop(self):
        p=copy.deepcopy(self.e.s['positions'][fixture.SYM])
        p.update(avg=4360,high=4360,stop=4295,initial_stop=4295,initial_r=65)
        high,stop=self.e.protection(p,4440,None)
        self.assertEqual((high,stop),(4440,4405))
        self.assertTrue(p['protection_evidence']['profit_protection_active'])
        self.assertEqual(p['protection_evidence']['giveback_stop'],4405)
        self.assertEqual(p['protection_evidence']['initial_invalidation'],4295)
    def test_trade_uses_entry_version_and_keeps_exit_build_separate(self):
        p=self.e.s['positions'][fixture.SYM]
        p['signal'].update(entry_strategy_version='entry-old',entry_build_id='old-build')
        p.update(trade_id='synthetic-fill',pattern='C',entry_hour=13,market_state='3',stop_history=[])
        trade=self.e.decorate_trade({'entry':7210,'exit':7170,'qty':9,'symbol':fixture.SYM,
            'pnl_estimate':-508,'reason':'시세 지연/연결 단절'},p)
        self.assertEqual(trade['strategy_version'],'entry-old');self.assertEqual(trade['entry_build_id'],'old-build')
        self.assertNotEqual(trade['exit_build_id'],'old-build')
    def test_broker_rejection_is_still_latched(self):
        o={'symbol':fixture.SYM,'local':'one','side':'BUY','qty':1}
        error=Rejected('broker rejection');error.retryable_market_recheck=True
        self.e.reject_order(o,error,broker_response=True)
        self.assertIsNotNone(self.e.entry_failure(fixture.SYM));self.assertFalse(o['fresh_signal_required'])
    def test_missing_diagnostic_adapter_never_becomes_a_control_error(self):
        self.feed.context_snapshot=Mock(return_value=Mock())
        self.assertEqual(self.h.market_context(self.clock)['status'],'UNKNOWN')
        self.assertFalse(self.e.s['halt']);self.assertEqual(self.b.orders,[])
    def test_context_text_is_diagnostic_and_supports_new_intraday_candidates(self):
        text=context_description({'market_context':{'trend_5m':{'status':'UP'},'trend_15m':{'status':'DOWN'},'market_regime':'DOWN'},
            'discovery_context':{'preopen_prepared':False,'first_raw_at':'2026-10-08T13:00:00+09:00'}})
        self.assertIn('종목 5분 상승',text);self.assertIn('시장 하락',text);self.assertIn('장중 발견 후보',text)

class CompleteEvidenceTests(unittest.TestCase):
    def test_signal_records_only_completed_decision_bars_for_later_audit(self):
        from datetime import datetime,timedelta
        from tests import test_audit_followup as setup_fixture
        f=setup_fixture.AuditFixture();f.setUp();self.addCleanup(f.doCleanups)
        for name in ('A','B','C'):
            with self.subTest(setup=name):
                detector,tick,watch=setup_fixture.SetupReachabilityTests.build(f,name)
                detector.ingest(tick,tick.time);signal=detector.signal(tick,watch)
                self.assertIsNotNone(signal)
                saved=signal['decision_bar_snapshot'];self.assertLessEqual(len(saved),12)
                self.assertGreaterEqual(len(saved),{'A':2,'B':4,'C':5}[name])
                self.assertTrue(all(datetime.fromisoformat(b['time'])+timedelta(minutes=1)<=tick.time for b in saved))
                self.assertTrue(all('close' in b and 'volume' in b for b in saved))

    def test_rotation_does_not_omit_earlier_candidates_or_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runtime=root/'runtime';(runtime/'archive').mkdir(parents=True)
            earlier={'time':'2026-10-08T09:05:00+09:00','rows':[{'code':'999990','name':'test','status':'WATCH','reference_price':100,'affordability':{'entry_eligible':True}}]}
            later={**earlier,'time':'2026-10-08T13:05:00+09:00'}
            (runtime/'archive/radar_history.jsonl.001').write_text(json.dumps(earlier)+'\n')
            (runtime/'radar_history.jsonl').write_text(json.dumps(later)+'\n')
            events=runtime/'automation-production';(events/'archive').mkdir(parents=True)
            (events/'archive/events.jsonl.001').write_text(json.dumps({'event':'ORDER_ACCEPTED','time':earlier['time'],'symbol':'999990','execution':'REAL'})+'\n')
            (events/'events.jsonl').write_text(json.dumps({'event':'ENTRY_EVAL','time':later['time'],'symbol':'999990',
                'gates':{'acceleration':{'result':'FAIL','required':False},'risk_quantity':{'result':'PASS','values':{'risk_quantity':1}}},
                'setup_trials':[{'setup':'B','evaluated':True,'entry_ready':True}]})+'\n')
            result=audit(root,'2026-10-08')
            self.assertEqual(result['counts']['cycles'],2);self.assertEqual(result['counts']['real_orders_accepted'],1)
            self.assertEqual(result['unique_raw'],1)
            self.assertEqual(result['counts']['final_order_eligible_evaluations'],1)
            self.assertEqual(result['counts']['setup_B_entry_ready'],1)
    def test_after_enabled_production_reconciliation_queries_both_exchanges(self):
        rest=object.__new__(Rest);rest.cfg=replace(Config(),after_enabled=True,broker_environment='production')
        rest.pages=Mock(return_value=([],{}))
        rest.fills();self.assertEqual(rest.pages.call_args.args[1]['stex_tp'],'0')
        rest.cfg=replace(rest.cfg,broker_environment='mock');rest.fills()
        self.assertEqual(rest.pages.call_args.args[1]['stex_tp'],'1')
