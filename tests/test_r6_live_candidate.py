"""Operational-candidate regressions; causal synthetic input unless labelled saved."""
import copy
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tests import test_r6_execution_integrity as fixture
from tests.test_r6_fast_repair import CAUSAL
from automation import runner
SYM=fixture.SYM


class ShortSignalQueueTests(fixture.AccountFixture):
    seed_receiver=fixture.ReceiverPromotionTests.seed_receiver
    def test_brief_signal_invalidated_by_already_received_prints_is_not_replayed_as_order(self):
        self.seed_receiver()
        start=self.at
        # The receiver has already queued 6 neutral frames. Ask/bid, depth and
        # the original trigger's <5s age remain valid. Signal lifetime is not extended.
        for i in range(6):
            self.at=start+timedelta(seconds=(i+1)*.05)
            fields={'20':self.at.strftime('%H%M%S'),'10':'1001','15':'1000',
                '13':str(12000+i*1000),'27':'1001','28':'1000','290':'2',
                '61':'10000','71':'10000'}
            with patch('core.realtime.now',return_value=self.at):
                self.feed.emit({'trnm':'REAL','data':[{'type':'0B','item':SYM,'values':fields}]})
        lookup={};by_symbol={SYM:self.row}
        callback=lambda t,r:runner.evaluate_observed_entry(self.e,self.detector,t,r,lookup,
            self.radar,self.prices,self.account,by_symbol)
        probe=lambda t,r:runner.evaluate_shadow_opportunity(self.e,self.detector,t,
            {**by_symbol[t.symbol],'status':'SHADOW'},self.radar,self.prices,self.account,probe_only=True)
        for _ in range(12):
            runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
                on_opportunity=callback,opportunity_probe=probe)
            if self.broker.orders:break
        self.assertEqual(self.broker.orders,[])
        events=[json.loads(line) for line in (self.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e.get('event')=='OBSERVATION_SIGNAL_QUEUED' for e in events))
        decision=next(e for e in events if e.get('event')=='OBSERVATION_OPPORTUNITY_REEVALUATED')
        self.assertFalse(decision['order_created'])
        # Exchange HHMMSS is second precision; receipt times distinguish the
        # later prints within that same second.
        self.assertGreater(decision['latest_received_at'],decision['trigger_received_at'])

    def test_still_valid_latest_signal_reaches_all_order_guards(self):
        self.seed_receiver();lookup={};rows={SYM:self.row}
        callback=lambda t,r:runner.evaluate_observed_entry(self.e,self.detector,t,r,lookup,
            self.radar,self.prices,self.account,rows)
        probe=lambda t,r:runner.evaluate_shadow_opportunity(self.e,self.detector,t,self.row,
            self.radar,self.prices,self.account,probe_only=True)
        for _ in range(12):
            runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
                on_opportunity=callback,opportunity_probe=probe)
            if self.broker.orders:break
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(self.broker.orders[0],('BUY',SYM,67,1001))

    def test_priority_control_during_prefix_processing_preempts_new_entries(self):
        self.seed_receiver();calls=[]
        def probe(t,r):
            self.feed.q.put((r,{'trnm':'DISCONNECTED'}))
            return True
        runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
            on_opportunity=lambda *args:calls.append(args),opportunity_probe=probe)
        self.assertEqual(calls,[])
        self.assertGreater(self.feed.observation_q.qsize(),0)
        self.assertEqual(self.broker.orders,[])

    def test_unread_newer_frames_prevent_order_evaluation_of_a_prefix(self):
        self.seed_receiver();calls=[]
        runner.prewarm_handoff(self.e,self.detector,self.feed,{SYM},set(),set(),0,
            on_opportunity=lambda t,r:calls.append(t.cumulative),opportunity_probe=lambda *args:True)
        self.assertLessEqual(len(calls),1)
        if calls:self.assertEqual(calls,[11000])
        self.assertEqual(self.broker.orders,[])

    def test_sixty_four_symbols_receive_bounded_fair_prefix_evaluations(self):
        self.seed_receiver()
        while not self.feed.observation_q.empty():
            self.feed.observation_q.get_nowait();self.feed.observation_q.task_done()
        symbols={str(900000+i) for i in range(64)}
        last=self.feed.latest_ticks[SYM]
        for sym in sorted(symbols):
            self.feed.observation_q.put((self.at,replace(last,symbol=sym)))
        calls=[];passes=0;started=time.perf_counter()
        while not self.feed.observation_q.empty() or getattr(self.feed,'_opportunity_pending',{}):
            count=len(calls)
            runner.prewarm_handoff(self.e,self.detector,self.feed,symbols,set(),set(),0,
                on_opportunity=lambda t,r:calls.append(t.symbol),opportunity_probe=lambda *args:True)
            self.assertLessEqual(len(calls)-count,2)
            passes+=1;self.assertLess(passes,256)
        self.assertEqual(calls,sorted(symbols))
        # This tests dispatch capacity only, not 64 valid strategy signals/orders.
        self.assertLess(time.perf_counter()-started,3.)
        self.assertEqual(self.broker.orders,[])

    def test_expired_and_adverse_quotes_are_not_revived_by_prefix_dispatch(self):
        for reason in ('EXPIRED','ADVERSE_QUOTE'):
            with self.subTest(reason=reason):
                case=ShortSignalQueueTests();case.setUp()
                try:
                    case.seed_receiver()
                    if reason=='EXPIRED':case.at+=timedelta(seconds=20)
                    else:
                        t=case.feed.latest_ticks[SYM]
                        case.feed.latest_ticks[SYM]=replace(t,ask=1002)
                    lookup={};by_symbol={SYM:case.row}
                    callback=lambda t,r:runner.evaluate_observed_entry(case.e,case.detector,t,r,lookup,
                        case.radar,case.prices,case.account,by_symbol)
                    probe=lambda t,r:runner.evaluate_shadow_opportunity(case.e,case.detector,t,case.row,
                        case.radar,case.prices,case.account,probe_only=True)
                    for _ in range(12):
                        runner.prewarm_handoff(case.e,case.detector,case.feed,{SYM},set(),set(),0,
                            on_opportunity=callback,opportunity_probe=probe)
                    self.assertEqual(case.broker.orders,[])
                finally:case.doCleanups()


class CandidateManifestTests(unittest.TestCase):
    def setUp(self):
        self.source=Path(__file__).resolve().parents[1]

    def capsule(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        manifest=json.loads((self.source/'runtime/RELEASE_IDENTITY.json').read_text(encoding='utf-8'))
        for rel in manifest['files']:
            dst=root/rel;dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(self.source/rel,dst)
        (root/'runtime').mkdir(exist_ok=True)
        (root/'runtime/RELEASE_IDENTITY.json').write_text(json.dumps(manifest),encoding='utf-8')
        return root,manifest

    def save_manifest(self,root,manifest):
        (root/'runtime/RELEASE_IDENTITY.json').write_text(json.dumps(manifest),encoding='utf-8')

    def test_manifest_matches_every_operational_source(self):
        from automation.release_integrity import check_runtime_integrity
        result=check_runtime_integrity(Path(__file__).resolve().parents[1])
        self.assertTrue(result['valid'],result['reasons'])
        self.assertTrue(result['manifest_verified'])

    def test_changed_engine_and_observation_worker_are_both_detected(self):
        from automation.release_integrity import check_runtime_integrity
        root,_=self.capsule()
        for rel in ('automation/engine.py','automation/observation_worker.py'):
            with self.subTest(path=rel):
                p=root/rel;original=p.read_bytes();p.write_bytes(original+b'\n# unapproved\n')
                result=check_runtime_integrity(root)
                self.assertFalse(result['valid'])
                self.assertIn('MANIFEST_HASH_MISMATCH: '+rel,result['reasons'])
                p.write_bytes(original)

    def test_integrated_build_cannot_run_without_manifest(self):
        from automation.release_integrity import check_runtime_integrity
        root,_=self.capsule();(root/'runtime/RELEASE_IDENTITY.json').unlink()
        result=check_runtime_integrity(root)
        self.assertFalse(result['valid'])
        self.assertIn('RELEASE_MANIFEST_REQUIRED_FOR_BUILD',result['reasons'])

    def test_manifest_cannot_omit_a_patch_dependency(self):
        from automation.release_integrity import check_runtime_integrity
        root,manifest=self.capsule();manifest['files'].pop('automation/patterns.py')
        self.save_manifest(root,manifest)
        self.assertIn('MANIFEST_REQUIRED_FILE_MISSING: automation/patterns.py',check_runtime_integrity(root)['reasons'])

    def test_build_identity_mismatch_is_not_accepted(self):
        from automation.release_integrity import check_runtime_integrity
        root,manifest=self.capsule();manifest['build_id']='OLD_APPROVED_BUILD'
        self.save_manifest(root,manifest)
        self.assertIn('MANIFEST_BUILD_ID_MISMATCH',check_runtime_integrity(root)['reasons'])

    def test_manifest_cannot_read_outside_the_release_root(self):
        from automation.release_integrity import check_runtime_integrity
        root,manifest=self.capsule();manifest['files']['../outside.py']='0'*64
        self.save_manifest(root,manifest)
        result=check_runtime_integrity(root)
        self.assertFalse(result['valid'])
        self.assertTrue(any('MANIFEST_PATH_OUTSIDE_ROOT' in x for x in result['reasons']))

    def test_original_execution_and_risk_settings_have_no_new_options(self):
        from automation.policy import AutomationConfig
        from core.settings import Config
        cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        auto=AutomationConfig()
        self.assertEqual(cfg.risk_fraction,.02)
        self.assertEqual(cfg.max_daily_loss_fraction,.04)
        self.assertFalse(hasattr(auto,'enable_ask_1tick_tolerance'))
        self.assertFalse(hasattr(auto,'early_momentum_breakout_enabled'))

    def test_previous_build_authorization_cannot_authorize_candidate(self):
        from automation.readiness import authorization_valid,policy_hash,digest
        from automation.policy import AutomationConfig
        from core.settings import Config
        from types import SimpleNamespace
        cfg=Config();auto=AutomationConfig();policy=SimpleNamespace(state={})
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'runtime').mkdir()
            receipt={'authorization_kind':'DAILY_PREOPEN_V13','trading_day':fixture.AT.date().isoformat(),
                'build_id':'V1.6-risk2-daily4-fast-live-r6-20261015',
                'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),
                'policy_hash':policy_hash(policy),'security_state_hash':digest([])}
            (root/'runtime/operator_user_authorization.json').write_text(json.dumps(receipt),encoding='utf-8')
            valid,reasons=authorization_valid(root,cfg,auto,policy,fixture.AT)
            self.assertFalse(valid)
            self.assertIn('LIVE_APPROVAL_BUILD_MISMATCH',reasons)



