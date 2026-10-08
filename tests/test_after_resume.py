"""Automatic AFTER handover with existing approval; fake gateway/broker only."""
import unittest
from dataclasses import asdict,replace
from datetime import timedelta
from unittest.mock import patch
from core.util import atomic_json
from automation.decisions import BUILD_ID
from automation.readiness import policy_hash,digest,security_state
from tests import test_continuous as fixtures
import operator_runner

class AfterResumeTests(unittest.TestCase):
    setUp=fixtures.ContinuousTests.setUp
    live=fixtures.ContinuousTests.live
    record=fixtures.ContinuousTests.record
    engine=fixtures.ContinuousTests.engine
    signal=fixtures.ContinuousTests.signal
    tick=fixtures.ContinuousTests.tick
    def ready(self):
        self.live();cfg,auto,data=self.s.settings()
        atomic_json(self.root/'config.json',asdict(replace(cfg,after_enabled=True)))
        self.policy.refresh();self.policy.state['after_live_authorized']=True
        atomic_json(self.policy.path,self.policy.state)
        self.record('STOPPED');self.s.stop('STOP');self.clock[0]=self.clock[0].replace(hour=15,minute=45)
        cfg,auto,data=self.s.settings()
        receipt={'authorization_kind':'DAILY_PREOPEN_V13','trading_day':self.clock[0].date().isoformat(),
            'build_id':BUILD_ID,'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),
            'policy_hash':policy_hash(data),'security_state_hash':digest(security_state(self.root))}
        atomic_json(self.root/'runtime/operator_user_authorization.json',receipt)
    def test_preparation_resume_keeps_receipt_config_cap_and_approval(self):
        self.ready();files=['config.json','automation.json','runtime/operator_user_authorization.json']
        before=[(self.root/f).read_bytes() for f in files]
        self.assertEqual(self.s.resume_live_continuous(True)['verdict'],'STARTED')
        self.assertEqual(self.gateway.starts,1);self.assertEqual(before,[(self.root/f).read_bytes() for f in files])
        self.assertTrue(self.policy.refresh()['after_live_authorized'])
    def test_after_resume_missing_after_approval_never_creates_one(self):
        self.ready();self.policy.refresh();self.policy.state['after_live_authorized']=False;atomic_json(self.policy.path,self.policy.state)
        with self.assertRaises(ValueError):self.s.resume_live_continuous(True)
        self.assertEqual(self.gateway.starts,0);self.assertEqual(self.policy.refresh()['mode'],'STOP')
        self.assertFalse(self.policy.state['after_live_authorized'])
    def test_changed_config_or_expired_receipt_blocks_resume(self):
        self.ready();self.clock[0]+=timedelta(days=1)
        with self.assertRaises(ValueError):self.s.resume_live_continuous(True)
        self.clock[0]-=timedelta(days=1);cfg,auto,data=self.s.settings()
        atomic_json(self.root/'config.json',asdict(replace(cfg,max_spread=.003)))
        with self.assertRaises(ValueError):self.s.resume_live_continuous(True)
        self.assertEqual(self.gateway.starts,0)
    def test_halt_unknown_blocks_resume_and_gap_wait(self):
        self.ready();atomic_json(self.root/'runtime/automation-production/state.json',{'halt':'test HALT','positions':{},'orders':{'x':{'status':'UNKNOWN'}}})
        with self.assertRaises(ValueError):self.s.resume_live_continuous(True)
        self.policy.command('LIVE',confirmation='LIVE');self.clock[0]=self.clock[0].replace(hour=15,minute=25)
        self.assertTrue(operator_runner.session_end(self.root,lambda:self.clock[0]))
        self.assertEqual(self.policy.refresh()['mode'],'STOP');self.assertEqual(self.gateway.starts,0)
    def test_authorized_gap_waits_and_1540_rejoins_without_policy_rewrite(self):
        self.ready();self.policy.command('LIVE',confirmation='LIVE')
        before=self.policy.path.read_bytes()
        for minute in (20,25,39,40,59):
            self.clock[0]=self.clock[0].replace(hour=15,minute=minute)
            self.assertFalse(operator_runner.session_end(self.root,lambda:self.clock[0]))
        self.assertEqual(self.policy.path.read_bytes(),before)
    def test_explicit_stop_kill_never_auto_restored_during_gap(self):
        self.ready();self.clock[0]=self.clock[0].replace(hour=15,minute=25)
        self.assertTrue(operator_runner.session_end(self.root,lambda:self.clock[0]))
        self.assertEqual(self.policy.refresh()['mode'],'STOP')
        self.policy.command('KILL');self.assertTrue(operator_runner.session_end(self.root,lambda:self.clock[0]))
        self.assertEqual(self.policy.refresh()['mode'],'KILL')
    def test_after_preparation_blocks_buy_and_1600_changes_existing_session_gate(self):
        self.ready();self.policy.command('LIVE',confirmation='LIVE');engine,broker=self.engine()
        self.clock[0]=self.clock[0].replace(hour=15,minute=59,second=59)
        self.assertFalse(engine.entry_window());self.assertIsNone(engine.enter(self.signal(engine),self.tick()))
        self.assertEqual(broker.orders,[])
        self.clock[0]=self.clock[0].replace(hour=16,minute=0,second=0)
        self.assertTrue(engine.entry_window())
        # A time-window change alone does not supply fresh ticks/readiness.
        engine.connected=False;self.assertIsNone(engine.enter(self.signal(engine),self.tick()));self.assertEqual(broker.orders,[])
    def test_failed_launch_restores_stop_instead_of_leaving_live(self):
        self.ready()
        with patch.object(self.s,'start_continuous',return_value={'verdict':'FAIL','reasons':['test launch failure']}):
            with self.assertRaises(ValueError):self.s.resume_live_continuous(True)
        self.assertEqual(self.policy.refresh()['mode'],'STOP')

if __name__=='__main__':unittest.main()
