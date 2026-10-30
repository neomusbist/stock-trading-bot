import unittest
import tempfile
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta

from core.settings import Config, KST, ROOT
from core.strategy import Tick
from automation.policy import AutomationConfig, Policy
from automation.engine import AutonomousEngine
from tests.test_automation import FakeBroker

START = datetime(2026, 10, 6, 12, 19, 19, tzinfo=KST)

class ReconciliationRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.clock = [START]
        self.policy = Policy(self.folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=20000, costs_confirmed=True, commission_buy=0.00015, commission_sell=0.00015, sell_tax=0.002)
        self.cfg = replace(Config(), live_mode=True, account_equity=1000000, max_position_fraction=1.0, risk_fraction=0.01)
        self.broker = FakeBroker()
        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder / 'book', self.broker, lambda: self.clock[0])
        self.engine.connected = True
        self.engine.account_ok = True
        self.engine.market = '3'
        self.engine.s['cash'] = 20000.0
        self.engine.available_cash = 20000.0
        self.engine.s['day'] = START.date().isoformat()
        self.engine.s['automation']['allocated'] = True
        self.engine.s['automation']['cash'] = 20000.0
        self.engine.s['automation']['existing'] = {}
        self.engine.market_status = {'069540': {'pass': True}}
        # Use the engine's normal reconciliation to establish a fresh synthetic
        # account snapshot; an arbitrary monotonic value eventually becomes stale.
        self.engine.reconcile({'cash':20000,'equity':20000,'holdings':{},'orders':[]})
        self.assertTrue(self.engine.capital_account_valid())

    def create_position(self, sym='069540', qty=2, price=4960.0, limit=4965, stop=4905):
        sig = {'symbol': sym, 'stop': stop, 'limit': limit, 'ownership': 'BOT', 'pattern': 'A',
               # Transport/reconciliation fixture, not a detector signal. A/B
               # order guards require an explicit cost-feasible target now.
               'target':limit+4*(limit-stop),'time': self.clock[0].isoformat()}
        self.engine.entry_intent = sig
        # The final BUY guard now needs a current quote even in this offline fixture.
        self.engine.entry_tick=Tick(sym,self.clock[0],price,100,1000,limit,price)
        o = self.engine.submit('BUY', sym, qty, limit, signal=sig)
        self.engine.entry_intent = None
        self.engine.entry_tick = None
        self.assertIsNotNone(o)
        self.assertEqual(o['status'], 'ACCEPTED')
        self.engine.cumulative_fill(o, qty, price * qty)
        self.assertIn(sym, self.engine.s['positions'])
        p = self.engine.s['positions'][sym]
        self.assertEqual(p['qty'], qty)
        self.assertEqual(p['avg'], price)
        return p

    def test_stale_snapshot_before_fill_does_not_halt(self):
        """1. stale snapshot: query started before entry fill -> no HALT, qty_blocked is False."""
        fill_time = self.clock[0]
        p = self.create_position('069540', 2, 4960.0)
        self.assertEqual(self.engine.s['halt'], '')
        self.assertFalse(p.get('qty_blocked'))

        # Snapshot query started 1 second BEFORE the fill
        stale_query_time = fill_time - timedelta(seconds=1)
        stale_snapshot = {
            'holdings': {}, # Broker has 0 shares because query started before order
            'orders': [],
            'cash': 20420.0,
            'equity': 20000.0,
            'query_started_at': stale_query_time.isoformat()
        }
        self.engine.reconcile(stale_snapshot)

        # Mismatch must be ignored due to stale query timestamp
        self.assertEqual(self.engine.s['halt'], '')
        self.assertFalse(p.get('qty_blocked'))

    def test_subsequent_fresh_snapshot_matches_and_keeps_normal_state(self):
        """2. fresh snapshot: query started after fill with matching holdings -> normal state kept."""
        fill_time = self.clock[0]
        p = self.create_position('069540', 2, 4960.0)

        # 1st: Stale snapshot arrives (ignored)
        stale_snapshot = {
            'holdings': {},
            'orders': [],
            'cash': 20420.0,
            'equity': 20000.0,
            'query_started_at': (fill_time - timedelta(seconds=1)).isoformat()
        }
        self.engine.reconcile(stale_snapshot)
        self.assertEqual(self.engine.s['halt'], '')

        # 2nd: Fresh snapshot arrives (started 1 second after fill)
        fresh_query_time = fill_time + timedelta(seconds=1)
        self.clock[0] = fresh_query_time + timedelta(milliseconds=500)
        fresh_snapshot = {
            'holdings': {'069540': {'qty': 2, 'available': 2, 'avg': 4960.0}},
            'orders': [],
            'cash': 10500.0,
            'equity': 20000.0,
            'query_started_at': fresh_query_time.isoformat()
        }
        self.engine.reconcile(fresh_snapshot)

        self.assertEqual(self.engine.s['halt'], '')
        self.assertFalse(p.get('qty_blocked'))
        self.assertTrue(self.engine.account_ok)

    def test_fresh_snapshot_persistent_mismatch_triggers_halt(self):
        """3. fresh snapshot genuine mismatch: query started after fill but qty differs -> HALT triggered."""
        fill_time = self.clock[0]
        p = self.create_position('069540', 2, 4960.0)

        # Fresh snapshot (started after fill), but broker only shows 1 share
        fresh_query_time = fill_time + timedelta(seconds=1)
        self.clock[0] = fresh_query_time + timedelta(milliseconds=500)
        mismatch_snapshot = {
            'holdings': {'069540': {'qty': 1, 'available': 1, 'avg': 4960.0}},
            'orders': [],
            'cash': 10500.0,
            'equity': 20000.0,
            'query_started_at': fresh_query_time.isoformat()
        }
        self.engine.reconcile(mismatch_snapshot)

        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.assertTrue(p['qty_blocked'])

    def test_snapshot_without_query_started_at_preserves_strict_mismatch_halt(self):
        """4. snapshot without query_started_at preserves strict existing behavior (immediate HALT)."""
        p = self.create_position('069540', 2, 4960.0)

        legacy_snapshot = {
            'holdings': {}, # Missing
            'orders': [],
            'cash': 20420.0,
            'equity': 20000.0
            # No query_started_at
        }
        self.engine.reconcile(legacy_snapshot)

        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.assertTrue(p['qty_blocked'])

    def test_protection_sell_submitted_even_under_halt(self):
        """5. Position protection: stop loss exit is submitted to broker even if HALT is latched."""
        p = self.create_position('069540', 2, 4960.0, stop=4905)
        # Force HALT latch
        self.engine.halt('관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')

        # When fresh reconcile has cleared qty_blocked:
        p['qty_blocked'] = False
        self.engine.actual = {'069540': {'qty': 2, 'available': 2, 'avg': 4960.0}}

        # Price falls below stop: exit requested
        self.engine.request_exit('069540', '구조/이익보호선 이탈')
        self.assertTrue(p['exit_requested'])

        # drive_exits
        self.engine.drive_exits({'069540': 4900.0})

        # Broker should have received SELL order for 2 shares
        sells = [o for o in self.broker.orders if o[0] == 'SELL' and o[1] == '069540']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0][2], 2)

    def test_1510_flatten_submitted_even_under_halt(self):
        """6. 15:10 Flatten: timer triggers exit and submits sell order even if HALT is latched."""
        p = self.create_position('069540', 2, 4960.0)
        self.engine.halt('관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        p['qty_blocked'] = False
        self.engine.actual = {'069540': {'qty': 2, 'available': 2, 'avg': 4960.0}}

        # Advance clock to 15:10:00 (flatten time)
        self.clock[0] = datetime(2026, 10, 6, 15, 10, 1, tzinfo=KST)
        self.assertTrue(self.engine.session_ending())

        self.engine.timer({'069540': 4950.0})
        self.assertTrue(p['exit_requested'])
        self.assertEqual(p['exit_reason'], '당일 청산 시각')

        sells = [o for o in self.broker.orders if o[0] == 'SELL' and o[1] == '069540']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0][2], 2)

if __name__ == '__main__':
    unittest.main()
