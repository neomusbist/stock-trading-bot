"""Dated absolute budget: real synthetic order path, expiry and preserved loss ledger."""
import copy,json,tempfile,time,unittest
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from core.settings import Config,KST
from core.util import atomic_json
from core.strategy import Tick
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.allocation import efficiency
from automation.readiness import authorization_valid
from operator_service import OperatorService
from tests import test_automation as fixture

class DailyRiskOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.stamp=[datetime(2026,10,12,14,40,tzinfo=KST)]
        self.cfg=replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04,
            max_position_fraction=1,max_positions=1,max_entries_per_day=None,max_entries_per_symbol=None)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,max_consecutive_losses=None)
        self.policy=Policy(self.root/'runtime/automation_policy.json');self.policy.command('LIVE',confirmation='LIVE')
        atomic_json(self.root/'config.json',asdict(self.cfg));atomic_json(self.root/'automation.json',asdict(self.auto))
        self.broker=fixture.FakeBroker();self.e=self.make(self.auto)
    def make(self,auto):
        e=AutonomousEngine(self.cfg,auto,self.policy,self.root/'runtime/automation-production',self.broker,lambda:self.stamp[0])
        e.reconcile({'cash':78536.,'equity':78536.,'holdings':{},'orders':[]});e.connected=True;e.market='3'
        e.market_status={fixture.SYMBOL:{'pass':True}}
        e.s['cash']=78123.018;e.s['automation']['cash']=78123.018;e.s['daily_loss']=2880.266;e.save()
        return e
    def override(self):
        return replace(self.auto,daily_risk_override_day='2026-10-12',daily_risk_override_budget_krw=3880.266).validate()
    def signal(self):
        return {'symbol':fixture.SYMBOL,'limit':10020,'stop':9400,'pattern':'C','time':self.stamp[0].isoformat(),
                'efficiency':efficiency(10020,9400,11500,.8,20,self.e.costs)}
    def tick(self):return Tick(fixture.SYMBOL,self.stamp[0],10020,100,100,10020,10010)
    def test_override_reaches_real_fake_broker_boundary_not_only_preview(self):
        sig=self.signal();self.assertLess(self.e.capital_context()['risk_remaining'],245)
        self.assertIsNone(self.e.enter(sig,self.tick()));self.assertEqual(self.broker.orders,[])
        self.e=self.make(self.override());self.assertAlmostEqual(self.e.capital_context()['risk_remaining'],1000)
        order=self.e.enter(sig,self.tick());self.assertIsNotNone(order);self.assertEqual(order['qty'],1)
        self.assertEqual(len(self.broker.orders),1);self.assertEqual(self.e.s['daily_loss'],2880.266)
    def test_budget_exhaustion_blocks_buy_and_protection_stays_available(self):
        self.e=self.make(self.override());order=self.e.enter(self.signal(),self.tick());self.assertIsNotNone(order)
        self.e.cumulative_fill(order,1,10020);self.e.actual={fixture.SYMBOL:{'qty':1,'available':1,'avg':10020}}
        self.e.s['daily_loss']=3880.266
        self.assertEqual(self.e.capital_context()['risk_remaining'],0)
        sold=self.e.submit('SELL',fixture.SYMBOL,1);self.assertIsNotNone(sold)
        self.assertEqual(self.broker.orders[-1][0],'SELL')
    def test_same_day_restart_preserves_loss_and_absolute_ceiling(self):
        self.e=self.make(self.override());before=copy.deepcopy(self.e.s)
        self.e=AutonomousEngine(self.cfg,self.override(),self.policy,self.e.folder,self.broker,lambda:self.stamp[0])
        self.assertEqual(self.e.s['daily_loss'],before['daily_loss']);self.assertEqual(self.e.s['trades'],before['trades'])
        self.assertEqual(self.e.daily_risk_budget(),3880.266)
    def test_expiry_in_running_engine_and_future_date_do_not_expand_budget(self):
        self.e=self.make(self.override());self.stamp[0]+=timedelta(days=1)
        self.assertAlmostEqual(self.e.daily_risk_budget(),self.e.risk_equity()*.04)
        self.assertFalse(self.e.capital_context()['daily_risk_override_active'])
        self.stamp[0]-=timedelta(days=2)
        self.assertAlmostEqual(self.e.daily_risk_budget(),self.e.risk_equity()*.04)
    def test_reserved_buy_risk_reduces_remaining_budget(self):
        self.e=self.make(self.override());self.assertIsNotNone(self.e.enter(self.signal(),self.tick()))
        self.assertLess(self.e.capital_context()['risk_remaining'],1000)
        self.assertGreater(self.e.capital_context()['risk_remaining'],0)
    def test_hard_lock_and_halt_are_not_overridden(self):
        self.e=self.make(self.override());self.e.s['halt']='UNKNOWN_ORDER'
        self.assertIsNone(self.e.enter(self.signal(),self.tick()));self.assertEqual(self.broker.orders,[])
        self.assertEqual(self.e.new_entry_reason('005930'),'HARD_LOCK')
    def test_malformed_or_unpaired_config_rejected(self):
        for changes in ({'daily_risk_override_day':'2026-10-12'},
            {'daily_risk_override_budget_krw':3880.266},
            {'daily_risk_override_day':'2026-10-12','daily_risk_override_budget_krw':float('nan')},
            {'daily_risk_override_day':'2026-10-12','daily_risk_override_budget_krw':True},
            {'daily_risk_override_day':'2026-10-12','daily_risk_override_budget_krw':81301},
            {'daily_risk_override_day':'2026-10-12','daily_risk_override_budget_krw':0}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):replace(self.auto,**changes).validate()
    def test_official_setting_path_preserves_ledger_and_receipt_no_network_or_start(self):
        self.policy.command('STOP');service=OperatorService(self.root,clock=lambda:self.stamp[0])
        receipt=self.root/'runtime/operator_user_authorization.json';atomic_json(receipt,{'sentinel':'DO_NOT_APPROVE'})
        prior_auto=json.loads((self.root/'automation.json').read_text(encoding='utf-8'))
        before={p:p.read_bytes() for p in (receipt,self.e.folder/'state.json',self.policy.path)}
        with patch.object(service.gateway,'snapshot') as snapshot,patch.object(service.gateway,'start') as start:
            result=service.set_daily_risk_remaining_today(1000)
        self.assertEqual(result['daily_risk_budget_krw'],3880.266);snapshot.assert_not_called();start.assert_not_called()
        self.assertTrue(all(p.read_bytes()==b for p,b in before.items()))
        loaded=service.settings()[1];self.assertEqual(loaded.daily_risk_override_day,'2026-10-12')
        current=json.loads((self.root/'automation.json').read_text(encoding='utf-8'))
        for key in ('daily_risk_override_day','daily_risk_override_budget_krw'):current[key]=prior_auto[key]
        self.assertEqual(current,prior_auto)
        _,reasons=authorization_valid(self.root,self.cfg,loaded,self.policy.state,self.stamp[0])
        self.assertIn('LIVE_APPROVAL_SETTINGS_MISMATCH',reasons)
    def test_official_setting_requires_stopped_idle_flat_and_no_halt(self):
        service=OperatorService(self.root,clock=lambda:self.stamp[0]);self.policy.command('STOP')
        with patch.object(service,'running',return_value=True),self.assertRaisesRegex(ValueError,'STOPPED'):service.set_daily_risk_remaining_today(1000)
        self.e.s['halt']='unsafe';self.e.save()
        with self.assertRaisesRegex(ValueError,'HALT'):service.set_daily_risk_remaining_today(1000)
    def test_budget_and_override_identity_are_logged(self):
        self.e=self.make(self.override());self.e.enter(self.signal(),self.tick())
        rows=[json.loads(s) for s in (self.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        row=next(r for r in reversed(rows) if r['event']=='ENTRY_EVAL')
        self.assertEqual(row['daily_risk_budget_krw'],3880.266);self.assertTrue(row['daily_risk_override_active'])
        self.assertEqual(row['max_daily_loss_fraction'],.04);self.assertEqual(row['risk_fraction'],.02)
    def test_actual_readonly_runner_clears_only_in_memory_override_with_zero_cap(self):
        from automation import runner
        atomic_json(self.root/'automation.json',asdict(self.override()))
        before=(self.root/'automation.json').read_bytes();seen=[]
        class BoundaryReached(Exception):pass
        def engine_boundary(cfg,auto,*args):
            auto.validate();self.assertFalse(cfg.live_mode);self.assertEqual(auto.capital_limit,0)
            self.assertIsNone(auto.daily_risk_override_day);self.assertIsNone(auto.daily_risk_override_budget_krw)
            seen.append(True);raise BoundaryReached()
        with patch.object(runner,'Rest'),patch.object(runner,'AutonomousEngine',side_effect=engine_boundary),\
             self.assertRaises(BoundaryReached):
            runner.run(self.cfg,self.root,recover_pilot=True,operational_pilot=True,readiness_check=True)
        self.assertEqual(seen,[True]);self.assertEqual((self.root/'automation.json').read_bytes(),before)

if __name__=='__main__':unittest.main()
