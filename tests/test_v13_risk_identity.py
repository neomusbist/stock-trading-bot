"""V1.3 위험2% 실험: 기존 수량/청산 경계를 합성 브로커로 검증한다."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tests import test_automation as fixture
from automation.allocation import efficiency
from automation.decisions import BUILD_ID, STRATEGY_VERSION, STRATEGY_EXPERIMENT, gate


class V13RiskIdentityTests(unittest.TestCase):
    make_engine = fixture.AutomationTests.make_engine
    tick = fixture.AutomationTests.tick

    def setUp(self):
        fixture.AutomationTests.setUp(self)
        self.auto = replace(self.auto, AUTO_TRADING_CAP_KRW=20000, replacement_enabled=False)
        self.cfg = replace(self.cfg, risk_fraction=.02, max_daily_loss_fraction=.04,
                           max_position_fraction=1.0, max_positions=1, max_entries_per_day=3)
        self.engine = self.make_engine()
        self.snapshot = {'cash':20420, 'equity':20420, 'holdings':{}, 'orders':[]}
        self.engine.reconcile(self.snapshot)
        self.engine.connected = True
        self.engine.market = '3'

    def signal(self, stop=9750, symbol=fixture.SYMBOL):
        entry = 10000
        return {'symbol':symbol, 'limit':entry, 'stop':stop, 'pattern':'A',
                'time':self.clock[0].isoformat(),
                'efficiency':efficiency(entry, stop, 11200, .8, 10, self.engine.costs)}

    def events(self):
        return [json.loads(line) for line in
                (self.engine.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_identity_separates_signal_rules_and_risk_experiment(self):
        self.assertEqual(BUILD_ID, 'V1.5-risk2-watch12-bc20M-structuralAE-20261013')
        self.assertEqual(STRATEGY_VERSION, 'V1.5-structural-context-exit-20261013')
        self.assertIn('risk2', STRATEGY_EXPERIMENT)
        self.assertIn('daily4', STRATEGY_EXPERIMENT)
        self.assertIn('20261007', STRATEGY_EXPERIMENT)

    def test_cap20000_risk_budget400_daily800(self):
        self.assertEqual(self.engine.risk_equity(), 20000)
        self.assertEqual(self.engine.risk_equity()*self.cfg.risk_fraction, 400)
        self.assertEqual(self.engine.capital_context()['risk_remaining'], 800)
        self.assertEqual(self.engine.a.capital_limit, 20000)
        self.assertEqual(self.engine.auto_available_capital(), 20000)

    def test_structural_unit_risk_budget_boundaries(self):
        signal = self.signal()
        details = self.engine.quantity_details(signal,20000,20000,800)
        self.assertGreater(details['risk_per_share'], 200)
        self.assertLessEqual(details['risk_per_share'], 400)
        self.assertEqual(details['risk_budget'],400)
        self.assertEqual(details['quantity'],1)
        qty,gates = self.engine.entry_gates(signal,self.tick(10000))
        self.assertEqual(qty,1)
        self.assertTrue(all(g['result']=='PASS' for g in gates.values()), gates)
        order = self.engine.enter(signal,self.tick(10000))
        self.assertIsNotNone(order)
        self.assertEqual(order['status'],'ACCEPTED')
        self.assertEqual(self.broker.orders,[('BUY',fixture.SYMBOL,1,10000)])
        self.setUp()
        signal2 = self.signal(stop=9600)
        details2 = self.engine.quantity_details(signal2,20000,20000,800)
        self.assertGreater(details2['risk_per_share'],400)
        self.assertEqual(details2['quantity'],0)
        self.assertEqual(details2['reason_code'],'RISK_BUDGET_ZERO_QTY')
        self.assertIsNone(self.engine.enter(signal2,self.tick(10000)))
        self.assertEqual(self.broker.orders,[])

    def test_risk_fraction_changes_quantity_not_structural_invalidation(self):
        signal = self.signal()
        original_stop = signal['stop']
        prior = replace(self.cfg,risk_fraction=.01)
        self.engine.c = prior
        one_percent = self.engine.quantity_details(signal,20000,20000,800)
        self.engine.c = self.cfg
        two_percent = self.engine.quantity_details(signal,20000,20000,800)
        self.assertEqual(one_percent['quantity'],0)
        self.assertEqual(two_percent['quantity'],1)
        self.assertEqual(one_percent['risk_per_share'],two_percent['risk_per_share'])
        self.assertEqual(signal['stop'],original_stop)
        self.assertNotEqual(original_stop,signal['limit']*.98)

    def test_daily_four_percent_blocks_new_buy_and_keeps_exit_protection(self):
        signal = self.signal()
        order = self.engine.enter(signal,self.tick(10000))
        self.assertIsNotNone(order)
        self.engine.cumulative_fill(order,order['qty'],order['qty']*order['limit'])
        self.engine.actual = {fixture.SYMBOL:{'qty':order['qty'],'available':order['qty'],'avg':order['limit']}}
        self.engine.s['daily_loss'] = 800
        count = len(self.broker.orders)
        self.assertIsNone(self.engine.enter(self.signal(symbol='999980'),self.tick(10000,'999980')))
        self.assertEqual(len(self.broker.orders),count)
        self.engine.on_tick(self.tick(9700))
        self.assertEqual(self.broker.orders[-1][0],'SELL')
        self.assertEqual(sum(1 for o in self.broker.orders if o[0]=='BUY'),1)

    def test_entry_and_risk_gate_logs_carry_experiment_and_budget_invariants(self):
        signal = self.signal()
        qty,gates = self.engine.entry_gates(signal,self.tick(10000))
        self.engine.decisions.emit('ENTRY_EVAL',fixture.SYMBOL,'NO_BUY','SYNTHETIC_ONLY',
            gates,signal=signal,price=10000,extra={'quantity':qty},force=True)
        row = [r for r in self.events() if r['event']=='ENTRY_EVAL'][-1]
        required = {'risk_fraction':.02,'max_daily_loss_fraction':.04,
                    'allowed_risk_krw':400,'remaining_daily_risk_krw':800}
        for key,value in required.items():
            self.assertEqual(row[key],value)
            self.assertEqual(row['gates']['risk_quantity']['values'][key],value)
        self.assertEqual(row['build_id'],BUILD_ID)
        self.assertEqual(row['strategy_experiment'],STRATEGY_EXPERIMENT)

        self.setUp()
        order=self.engine.enter(self.signal(),self.tick(10000))
        self.assertIsNotNone(order)
        self.assertLess(self.engine.capital_context()['risk_remaining'],800)
        row2=[r for r in self.events() if r['event']=='ENTRY_EVAL'][-1]
        self.assertEqual(row2['decision'],'BUY')
        self.assertEqual(row2['allowed_risk_krw'],400)
        self.assertEqual(row2['remaining_daily_risk_krw'],800)
        self.assertEqual(row2['gates']['risk_quantity']['values']['remaining_daily_risk_krw'],800)

        self.setUp()
        self.engine.s['daily_loss']=700
        self.engine.decisions.emit('ENTRY_EVAL',fixture.SYMBOL,'NO_BUY','SYNTHETIC_ONLY',
            {'risk_quantity':gate(False,quantity=0)},signal=self.signal(),price=10000,force=True)
        row3=[r for r in self.events() if r['event']=='ENTRY_EVAL'][-1]
        self.assertEqual(row3['allowed_risk_krw'],400)
        self.assertEqual(row3['remaining_daily_risk_krw'],100)
        self.assertEqual(row3['approved_cap'],20000)

    def test_example_has_explicit_experiment_without_live_or_cap_approval(self):
        config=json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text(encoding='utf-8'))
        auto=json.loads((Path(__file__).resolve().parents[1]/'automation.example.json').read_text(encoding='utf-8'))
        self.assertEqual(config['risk_fraction'],.02)
        self.assertEqual(config['max_daily_loss_fraction'],.04)
        self.assertEqual(config['max_positions'],1)
        self.assertEqual(config['max_entries_per_day'],3)
        self.assertIs(config['live_mode'],False)
        self.assertEqual(auto['AUTO_TRADING_CAP_KRW'],0)
        self.assertIs(auto['costs_confirmed'],False)


if __name__ == '__main__':
    unittest.main()
