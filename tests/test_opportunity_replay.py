from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import tempfile,unittest

from automation.opportunity_replay_cli import replay_capture,main
from automation.policy import AutomationConfig
from core.settings import Config,KST
from tests import test_reclaim_research as fixtures

AT=datetime(2026,10,19,9,5,tzinfo=KST)


class OpportunityReplayTests(unittest.TestCase):
    def records(self):
        fixture=fixtures.ReclaimResearchTests();fixture.setUp()
        # A completed close clears the old impulse ceiling. A print alone does
        # not retire that resistance, as the separate counterexample verifies.
        fixture.bars[-1]['close']=1001
        records=[{'event':'COMPLETED_BAR','symbol':'999990','bar':{**bar,'time':bar['time'].isoformat()},
            'processed_at':(bar['time']+timedelta(minutes=1)).isoformat(),
            'source':'ACTUAL_RECEIVED_DETECTOR_HISTORY','reference_bars_used':False} for bar in fixture.bars]
        records.append({'event':'OPPORTUNITY_SAMPLE','symbol':'999990','asof':AT.isoformat(),
            'exchange_at':AT.isoformat(),'received_at':AT.isoformat(),'price':1003,'bid':1002,'ask':1003,
            'bid_qty':1000,'ask_qty':1000,'regular':True,'depth_available':True,'book_time':AT.isoformat(),
            'watch':{k:v for k,v in fixture.watch.items() if k!='market_status'},
            'market_status':fixture.watch['market_status'],'gates':fixture.gates,'trials':[]})
        return records

    def test_exact_saved_confirmations_form_one_research_episode_not_a_trade(self):
        rows=self.records();result=replay_capture(rows,Config(),AutomationConfig(),AT)
        self.assertEqual(result['reclaim_price_ready_episodes'],1)
        self.assertIsNone(result['actual_trades']);self.assertIsNone(result['net_pnl'])
        self.assertFalse(result['orders_enabled'])

    def test_late_bar_confirmation_and_future_sample_are_unavailable(self):
        rows=self.records();rows[-2]['processed_at']=(AT+timedelta(seconds=1)).isoformat()
        result=replay_capture(rows,Config(),AutomationConfig(),AT)
        self.assertEqual(result['reclaim_price_ready_episodes'],0)
        self.assertEqual(result['future_records_excluded'],1)

    def test_disconnect_retires_prior_structure_evidence(self):
        rows=self.records();rows.insert(-1,{'event':'STREAM_STATE','kind':'DISCONNECTED','received_at':AT.isoformat()})
        self.assertEqual(replay_capture(rows,Config(),AutomationConfig(),AT)['reclaim_price_ready_episodes'],0)

    def test_duplicate_samples_do_not_create_extra_trades(self):
        rows=self.records();rows.append(deepcopy(rows[-1]))
        self.assertEqual(replay_capture(rows,Config(),AutomationConfig(),AT)['reclaim_price_ready_episodes'],1)

    def test_uncleared_known_ceiling_is_not_ignored_for_more_candidates(self):
        rows=self.records();rows[-2]['bar']['close']=999
        result=replay_capture(rows,Config(),AutomationConfig(),AT)
        self.assertEqual(result['reclaim_price_ready_episodes'],0)
        self.assertEqual(result['reclaim_primary_rejection_samples'],{'RECLAIM_NET_REWARD_RISK_FAIL':1})

    def test_new_session_and_capture_loss_clear_incomplete_proof(self):
        for marker in ({'event':'CAPTURE_STARTED','at':AT.isoformat()},
                       {'event':'STATUS','at':AT.isoformat(),'capture_losses':1}):
            rows=self.records();rows.insert(-1,marker)
            with self.subTest(marker=marker):
                result=replay_capture(rows,Config(),AutomationConfig(),AT)
                self.assertEqual(result['reclaim_price_ready_episodes'],0)
                self.assertEqual(result['evidence_reset_events'],1)

    def test_missing_execution_data_remains_unknown(self):
        rows=self.records();rows[-1]['ask']=None
        result=replay_capture(rows,Config(),AutomationConfig(),AT)
        self.assertEqual(result['reclaim_price_ready_episodes'],0)
        self.assertEqual(result['reclaim_primary_rejection_samples'],{'RECLAIM_EXECUTABLE_PRICE_OR_TIME_UNKNOWN':1})

    def test_cli_replays_explicit_bounded_file_without_broker(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'capture.jsonl';output=root/'result.json'
            source.write_text('\n'.join(json.dumps(r,default=str) for r in self.records()),encoding='utf-8')
            repo=Path(__file__).resolve().parents[1]
            result=main(['--input',str(source),'--config',str(repo/'config.example.json'),
                '--automation',str(repo/'automation.example.json'),'--cutoff',AT.isoformat(),'--output',str(output)])
            self.assertEqual(result,0);self.assertFalse(json.loads(output.read_text(encoding='utf-8'))['orders_enabled'])
