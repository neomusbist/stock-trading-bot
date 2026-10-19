"""Isolated verification for HTS 0624 external sell conflicts and bot exit safety."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import replace

from core.settings import Config, KST, ROOT
from core.strategy import Tick
from core.engine import Rejected
from automation.policy import AutomationConfig, Policy
from automation.engine import AutonomousEngine
from tests.test_v1 import FakeBroker

START = datetime(2026, 10, 15, 11, 0, 0, tzinfo=KST)

class Hts0624ConflictDefenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.clock = [START]
        
        p_path = self.folder / 'policy.json'
        self.policy = Policy(p_path)
        self.policy.command('LIVE', confirmation='LIVE')
        
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=100000, costs_confirmed=True)
        self.cfg = replace(Config(), live_mode=True, account_equity=1000000, max_position_fraction=1.0)
        self.broker = FakeBroker()
        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder / 'book', self.broker, lambda: self.clock[0])
        self.engine.connected = True
        self.engine.account_ok = True
        self.engine.market = '3'
        self.engine.last_account = 1000000.0
        self.engine.s['cash'] = 100000.0
        self.engine.available_cash = 100000.0
        self.engine.s['day'] = START.date().isoformat()
        self.engine.s['automation']['allocated'] = True
        self.engine.s['automation']['cash'] = 100000.0
        self.engine.s['automation']['existing'] = {}
        self.engine.market_status = {'090710': {'pass': True}}

    def create_position(self, sym='090710', qty=3, price=7580.0, stop=7170.0):
        sig = {'symbol': sym, 'stop': stop, 'limit': price, 'ownership': 'BOT', 'pattern': 'A'}
        self.engine.entry_intent = sig
        self.engine.entry_tick = Tick(sym, self.clock[0], price, 100, 1000, price, price)
        o = self.engine.submit('BUY', sym, qty, price, signal=sig)
        self.engine.entry_intent = None
        self.engine.entry_tick = None
        self.assertIsNotNone(o)
        self.assertEqual(o['status'], 'ACCEPTED')
        self.engine.cumulative_fill(o, qty, price * qty)
        self.assertIn(sym, self.engine.s['positions'])
        p = self.engine.s['positions'][sym]
        self.assertEqual(p['qty'], qty)
        self.broker.orders.clear()
        return p

    def test_scenario1a_hts_external_fill_before_bot_exit_halts_and_blocks_duplicate_sell(self):
        """Scenario 1A: HTS 0624 executes sell before bot exit.
        reconcile detects zero broker holding, HALTs, blocks qty, and prevents bot duplicate sell.
        """
        sym = '090710'
        p = self.create_position(sym, qty=3, price=7580.0)
        self.clock[0] += timedelta(minutes=1)
        
        # Broker snapshot shows 0 shares because HTS 0624 sold 3 shares on the exchange
        snapshot = {
            'query_started_at': self.clock[0].isoformat(),
            'holdings': {}, # 0 shares at broker
            'orders': [],
            'cash': 122740.0,
            'equity': 1000000.0
        }
        self.engine.reconcile(snapshot)
        
        # 1. Engine must latch HALT immediately
        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        # 2. Position qty must be blocked
        self.assertTrue(p['qty_blocked'])
        
        # 3. Now assume price crashes and bot tries to trigger exit
        self.engine.request_exit(sym, '구조/이익보호선 이탈')
        self.assertTrue(p['exit_requested'])
        self.engine.drive_exits({sym: 7000.0})
        
        # 4. Verification: No SELL order was sent to broker (duplicate sell strictly blocked)
        sells = [o for o in self.broker.orders if o[0] == 'SELL' and o[1] == sym]
        self.assertEqual(len(sells), 0, "Duplicate sell order must NOT be sent to broker")
        # 5. HALT remains latched
        self.assertTrue(self.engine.s['halt'])

    def test_scenario1b_hts_external_resting_open_order_halts_and_blocks_bot_exit(self):
        """Scenario 1B: HTS 0624 places an order which is currently resting (미체결).
        reconcile detects unowned open order on managed symbol, HALTs, and prevents bot exit.
        """
        sym = '090710'
        p = self.create_position(sym, qty=3, price=7580.0)
        self.clock[0] += timedelta(minutes=1)
        
        # Broker snapshot shows holding still 3, but unowned open sell order 'HTS-0624-999'
        snapshot = {
            'query_started_at': self.clock[0].isoformat(),
            'holdings': {sym: {'qty': 3, 'available': 0, 'avg': 7580.0}},
            'orders': [{'ord_no': 'HTS-0624-999', 'stk_cd': sym, 'oso_qty': '3', 'cntr_tot_amt': '0'}],
            'cash': 100000.0,
            'equity': 1000000.0
        }
        self.engine.reconcile(snapshot)
        
        # 1. Engine must latch HALT for external open order
        self.assertEqual(self.engine.s['halt'], '관리 종목 외부 미체결 주문')
        self.assertTrue(p['qty_blocked'])
        
        # 2. Bot exit attempt must be blocked
        self.engine.request_exit(sym, '호가 급락/유동성 붕괴')
        self.engine.drive_exits({sym: 7100.0})
        
        sells = [o for o in self.broker.orders if o[0] == 'SELL' and o[1] == sym]
        self.assertEqual(len(sells), 0, "Bot must not compete with external open order")

    def test_scenario2_concurrent_race_bot_sell_and_external_sell(self):
        """Scenario 2: Bot submits SELL order, but HTS 0624 concurrently filled on broker.
        Broker rejects bot order due to insufficient shares.
        Engine marks order rejected, reconcile confirms 0 shares, HALTs, and prevents resend.
        """
        sym = '090710'
        p = self.create_position(sym, qty=3, price=7580.0)
        self.clock[0] += timedelta(minutes=1)
        
        # Price drops below stop: bot requests exit
        self.engine.request_exit(sym, '구조/이익보호선 이탈')
        
        # Mock broker rejecting SELL order with insufficient balance (already sold by 0624)
        def reject_sell(side, symbol, qty, limit=0, **kwargs):
            if side == 'SELL':
                raise Rejected('ORDER_BOUNDARY_BLOCKED: 매도가능수량 부족 / 잔고 없음')
            return '0000001'
        self.broker.submit = reject_sell
        
        # Bot attempts to drive exit
        self.engine.drive_exits({sym: 7150.0})
        
        # Order should be rejected
        sell_orders = [o for o in self.engine.s['orders'].values() if o['side'] == 'SELL' and o['symbol'] == sym]
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]['status'], 'REJECTED')
        
        # Reconcile runs after broker execution
        snapshot = {
            'query_started_at': self.clock[0].isoformat(),
            'holdings': {}, # Broker has 0 shares
            'orders': [],
            'cash': 122740.0,
            'equity': 1000000.0
        }
        self.engine.reconcile(snapshot)
        
        # Verify HALT latched and qty blocked
        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.assertTrue(p['qty_blocked'])
        
        # Further exit calls do not resubmit
        self.engine.drive_exits({sym: 7100.0})
        self.assertEqual(len(self.engine.s['orders']), 2) # Only original BUY and single REJECTED SELL

    def test_scenario3_bot_crash_and_restart_reconciliation(self):
        """Scenario 3: Bot was off / crashed. During downtime, HTS 0624 executed stop-loss.
        When restarted, engine loads saved state, reconciles with broker, detects mismatch,
        and strictly enforces HALT without sending any new or duplicate orders.
        """
        sym = '090710'
        p = self.create_position(sym, qty=3, price=7580.0)
        self.engine.save()
        
        # Simulate bot crash / shutdown: create a fresh engine instance reading persisted book
        new_clock = [self.clock[0] + timedelta(minutes=10)]
        new_broker = FakeBroker()
        restarted_engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder / 'book', new_broker, lambda: new_clock[0])
        restarted_engine.connected = True
        restarted_engine.market = '3'
        
        # Verify recovered state has position
        self.assertIn(sym, restarted_engine.s['positions'])
        self.assertEqual(restarted_engine.s['positions'][sym]['qty'], 3)
        
        # Broker snapshot on boot: HTS 0624 had sold shares during downtime
        boot_snapshot = {
            'query_started_at': new_clock[0].isoformat(),
            'holdings': {}, # 0 shares at broker
            'orders': [],
            'cash': 122740.0,
            'equity': 1000000.0
        }
        restarted_engine.reconcile(boot_snapshot)
        
        # 1. Restarted engine must be in HALT
        self.assertEqual(restarted_engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        # 2. Position qty blocked
        self.assertTrue(restarted_engine.s['positions'][sym]['qty_blocked'])
        # 3. New buys are forbidden
        self.assertFalse(restarted_engine.is_supported_now() and not restarted_engine.s['halt'])
        # 4. Zero orders sent by restarted engine
        self.assertEqual(len(new_broker.orders), 0)

    def test_scenario4_unresolved_discrepancy_strictly_maintains_halt_and_refuses_trading(self):
        """Scenario 4: If discrepancy is not cleared, repeated reconciles and timers maintain HALT.
        New entries are strictly rejected, and no exit orders are submitted.
        """
        sym = '090710'
        p = self.create_position(sym, qty=3, price=7580.0)
        self.clock[0] += timedelta(minutes=1)
        
        # Snapshot with missing holdings (external sell happened)
        snapshot = {
            'query_started_at': self.clock[0].isoformat(),
            'holdings': {},
            'orders': [],
            'cash': 122740.0,
            'equity': 1000000.0
        }
        self.engine.reconcile(snapshot)
        self.assertTrue(self.engine.s['halt'])
        self.assertTrue(p['qty_blocked'])
        
        # 1. Repeated reconcile after another minute must NOT clear HALT
        self.clock[0] += timedelta(minutes=1)
        snapshot['query_started_at'] = self.clock[0].isoformat()
        self.engine.reconcile(snapshot)
        self.assertEqual(self.engine.s['halt'], '관리 잔고와 실제 잔고 불일치: 자동 매도 보류 필요')
        self.assertTrue(p['qty_blocked'])
        
        # 2. New buy attempts must be rejected by engine gates
        sig = {'symbol': '005930', 'limit': 60000, 'stop': 59000, 'ownership': 'BOT', 'pattern': 'A'}
        t = Tick('005930', self.clock[0], 60000, 100, 1000, 60000, 59900)
        entry_order = self.engine.enter(sig, t)
        self.assertIsNone(entry_order, "New buy order must be rejected when HALT is active")
        
        # 3. Timer drive exits must NOT submit duplicate sells
        self.engine.timer({sym: 7000.0})
        sells = [o for o in self.broker.orders if o[0] == 'SELL']
        self.assertEqual(len(sells), 0, "No exit orders permitted while discrepancy persists")

if __name__ == '__main__':
    unittest.main()

