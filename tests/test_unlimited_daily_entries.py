"""일일 횟수만 해제하며 실제 체결 카운터와 위험/중복/보호 경계를 보존한다."""
import copy
import unittest
from dataclasses import replace
from core.settings import Config
from core.engine import Engine
from automation.engine import AutonomousEngine
from core.util import atomic_json
from tests import test_v1 as v1
from tests.test_audit_followup import AuditFixture

class UnlimitedDailyEntriesTests(unittest.TestCase):
    def test_null_is_explicit_opt_out_but_invalid_limits_still_rejected(self):
        self.assertIsNone(replace(Config(),max_entries_per_day=None).validate().max_entries_per_day)
        for value in (0,-1,False,True,3.5,'unlimited'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                replace(Config(),max_entries_per_day=value).validate()
        with self.assertRaises(ValueError):replace(Config(),max_positions=None).validate()

    def test_fourth_and_fifth_real_order_boundaries_after_first_fills(self):
        fixture=v1.EngineTests();fixture.setUp()
        try:
            e,b=fixture.engine(True,max_positions=1,max_entries_per_day=None)
            for sym in ('111110','222220','333330','444440','555550'):
                order=fixture.enter(e,sym);self.assertEqual(order['status'],'ACCEPTED')
                e.cumulative_fill(order,order['qty'],order['qty']*10000)
                e.request_exit(sym,'synthetic');e.drive_exits()
                sell=e.active_orders(sym,'SELL')[0]
                e.cumulative_fill(sell,sell['qty'],sell['qty']*10010)
            self.assertEqual(sum(e.s['entries'].values()),5)
            self.assertEqual(len([o for o in b.orders if o[0]=='BUY']),5)
            before=copy.deepcopy(e.s)
            resumed=Engine(e.c,e.folder,b,lambda:fixture.clock[0])
            for key in ('entries','daily_loss','realized','trades','entry_lifecycles'):
                self.assertEqual(resumed.s[key],before[key])
        finally:fixture.tearDown()

class UnlimitedAutonomousTests(AuditFixture):
    def unlimited(self):
        self.cfg=replace(self.cfg,max_entries_per_day=None,risk_fraction=.02,max_daily_loss_fraction=.04)
        self.e=AutonomousEngine(self.cfg,self.auto,self.policy,self.e.folder,self.broker,lambda:self.clock[0])
        self.e.reconcile(self.snap);self.e.connected=True;self.e.market='3'
        self.e.market_status={'999990':{'pass':True}}
        self.e.s['entries']={'111110':1,'222220':1,'333330':1}
        self.e.save()

    def test_fourth_autonomous_order_preserves_risk_and_count(self):
        self.unlimited()
        _,gates=self.e.entry_gates(self.signal(),self.tick())
        self.assertEqual(gates['repeat_slots']['result'],'PASS')
        self.assertIsNone(gates['repeat_slots']['values']['daily_entry_limit'])
        order=self.e.enter(self.signal(),self.tick());self.assertIsNotNone(order)
        self.e.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
        self.assertEqual(sum(self.e.s['entries'].values()),4)

    def test_remaining_daily_risk_zero_prevents_fourth_buy(self):
        self.unlimited();self.e.s['daily_loss']=self.e.s['equity']*.04
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])

    def test_cash_zero_prevents_fourth_buy(self):
        self.unlimited();self.e.available_cash=0
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])

    def test_halt_and_hard_locks_still_prevent_buy(self):
        self.unlimited();self.e.s['halt']='synthetic UNKNOWN'
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.e.s['halt']=''
        for sym in ('005930','017900'):
            self.e.market_status[sym]={'pass':True}
            self.assertIsNone(self.e.enter({**self.signal(),'symbol':sym},replace(self.tick(),symbol=sym)))
        self.assertEqual(self.broker.orders,[])

    def test_same_symbol_daily_limit_and_consecutive_losses_unchanged(self):
        self.unlimited();self.e.s['entries']['999990']=1
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        del self.e.s['entries']['999990'];self.e.s['automation']['consecutive_losses']=3
        self.assertIsNone(self.e.enter(self.signal(),self.tick()))
        self.assertEqual(self.broker.orders,[])

    def test_flat_same_day_config_migration_preserves_loss_risk_and_records(self):
        before=copy.deepcopy(self.e.s)
        before.update(entries={'111110':1,'222220':1,'333330':1},daily_loss=125,realized=-125)
        atomic_json(self.e.path,before)
        self.unlimited()
        for key in ('entries','daily_loss','realized','trades','orders'):
            self.assertEqual(self.e.s[key],before[key])
        _,gates=self.e.entry_gates(self.signal(),self.tick())
        self.assertGreater(gates['risk_quantity']['values']['remaining_risk'],0)
        self.assertEqual(self.e.s['config_hash'],self.cfg.fingerprint())

if __name__=='__main__':unittest.main()
