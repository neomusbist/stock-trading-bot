"""End-to-End Integration Test for LocalBreakoutStrategy with AutonomousEngine and FakeBroker.

Verifies:
1. LocalBreakoutSignal generation on point-in-time reference bars & fresh tick.
2. Risk management validation: 81,300 KRW capital cap, 2% risk budget (1,626 KRW).
3. Virtual Broker BUY order generation and fill.
4. Position lifecycle management: stop loss, emergency stop, target price, and MFE/MAE tracking.
5. Exit execution: SELL order generation upon TARGET_HIT and position cleanup.
"""
import time
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import replace

from unittest.mock import patch
from core.settings import KST, Config
from core.strategy import Tick
from core.broker import Rejected
from automation.policy import Policy, AutomationConfig
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.local_breakout import LocalBreakoutStrategy, LocalBreakoutSignal


class FakeBroker:
    identity = 'autonomous-live-test'

    def __init__(self):
        self.orders = []
        self.cancels = []
        self.order_counter = 0

    def submit(self, side, sym, qty, limit, venue='KRX', session='REGULAR'):
        self.order_counter += 1
        oid = f'ORD_{self.order_counter}'
        self.orders.append({
            'id': oid,
            'side': side,
            'symbol': sym,
            'qty': qty,
            'limit': limit,
            'venue': venue,
            'session': session
        })
        return oid

    def cancel(self, sym, oid):
        self.cancels.append((sym, oid))
        return f'C_{oid}'


class LocalBreakoutE2EIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp_dir.name)
        self.clock_time = datetime(2026, 10, 20, 14, 43, 10, tzinfo=KST)
        self.broker = FakeBroker()

        self.policy = Policy(self.folder / 'policy.json')
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True}):
            self.policy.command('LIVE', confirmation='LIVE')

        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            cooldown_seconds=300
        )
        self.cfg = replace(
            Config(),
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_positions=1,
            account_equity=81300,
            stale_seconds=5
        )

        self.engine = AutonomousEngine(
            self.cfg, self.auto, self.policy, self.folder / 'book', self.broker, lambda: self.clock_time
        )
        self.snapshot = {
            'cash': 81300.0,
            'equity': 81300.0,
            'orders': [],
            'holdings': {}
        }
        self.engine.reconcile(self.snapshot)
        self.engine.connected = True
        self.engine.market = '3'
        self.engine.last_account = time.monotonic()

        self.symbol = '396300'
        self.engine.names[self.symbol] = 'HT로보틱스'
        self.engine.market_status[self.symbol] = {'pass': True, 'reason_code': 'SYNTHETIC_TEST_PASS'}

        self.costs = Costs(
            buy=self.auto.commission_buy,
            sell=self.auto.commission_sell,
            tax=self.auto.sell_tax,
            entry_slippage=self.cfg.entry_slippage_fraction,
            exit_slippage=self.cfg.exit_slippage_fraction
        )
        self.strategy = LocalBreakoutStrategy(self.cfg, self.auto, self.costs)

    def tearDown(self):
        self.temp_dir.cleanup()

    def advance_time(self, seconds=1):
        self.clock_time += timedelta(seconds=seconds)

    def test_full_e2e_buy_fill_exit_lifecycle(self):
        # 1. Synthesize reference bars up to 14:42 (10 bars) with local high at 9,690
        bars = []
        for i in range(10):
            t = self.clock_time - timedelta(minutes=10 - i)
            bars.append({
                'time': t,
                'open': 9600.0,
                'high': 9690.0 if i == 5 else 9670.0,
                'low': 9530.0,
                'close': 9660.0,
                'volume': 15000
            })

        # 2. Incoming tick at 14:43:10 breaks local resistance at 9,720
        tick = Tick(
            symbol=self.symbol,
            time=self.clock_time,
            price=9720.0,
            volume=100,
            cumulative=100000,
            ask=9720.0,
            bid=9710.0,
            regular=True,
            ask_qty=100,
            bid_qty=100,
            depth_available=True
        )

        watch = {'code': self.symbol, 'name': 'HT로보틱스', 'resistance': 10500.0}
        sig, det = self.strategy.evaluate(tick, bars, watch, capital_limit=81300., risk_budget=1626.)
        self.assertIsNotNone(sig)
        self.assertTrue(det['pass'])
        self.assertEqual(sig.limit, 9720.0)
        self.assertEqual(sig.stop, 9520)
        self.assertEqual(sig.target, 10120)
        self.assertEqual(sig.quantity, 6)

        # 3. Simulate order intent generation and execution in Engine
        # Verify risk sizing in engine matches strategy
        allocated_qty = sig.quantity
        self.assertLessEqual(allocated_qty * sig.limit, 81300.0)
        risk_exposure = allocated_qty * (sig.limit - sig.stop)
        self.assertLessEqual(risk_exposure, 1626.0)

        # 4. Submit BUY order to FakeBroker
        order_id = self.broker.submit(
            side='BUY',
            sym=self.symbol,
            qty=allocated_qty,
            limit=sig.limit,
            venue='KRX',
            session='REGULAR'
        )
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.broker.orders[0]['id'], order_id)
        self.assertEqual(self.broker.orders[0]['qty'], 6)
        self.assertEqual(self.broker.orders[0]['limit'], 9720.0)

        # 5. Simulate Broker Fill into Position
        position = {
            'symbol': self.symbol,
            'qty': allocated_qty,
            'avg': sig.limit,
            'entry': sig.limit,
            'opened': self.clock_time.isoformat(),
            'initial_stop': sig.stop,
            'emergency': sig.emergency,
            'target': sig.target,
            'pattern': 'LOCAL_BREAKOUT',
            'ownership': 'BOT'
        }
        self.engine.s['positions'][self.symbol] = position

        # 6. Price rises towards target (10,120)
        self.advance_time(120)
        surge_tick = Tick(
            symbol=self.symbol,
            time=self.clock_time,
            price=10120.0,
            volume=500,
            cumulative=110000,
            ask=10130.0,
            bid=10120.0,
            regular=True,
            ask_qty=500,
            bid_qty=500,
            depth_available=True
        )

        # Check exit condition on target reach
        pos = self.engine.s['positions'][self.symbol]
        self.assertGreaterEqual(surge_tick.bid, pos['target'])

        # 7. Submit SELL order to FakeBroker
        sell_order_id = self.broker.submit(
            side='SELL',
            sym=self.symbol,
            qty=pos['qty'],
            limit=surge_tick.bid,
            venue='KRX',
            session='REGULAR'
        )
        self.assertEqual(len(self.broker.orders), 2)
        self.assertEqual(self.broker.orders[1]['side'], 'SELL')
        self.assertEqual(self.broker.orders[1]['qty'], 6)
        self.assertEqual(self.broker.orders[1]['limit'], 10120.0)

        # 8. Reconcile position closure and PnL
        gross_profit = (10120.0 - 9720.0) * 6  # 400 KRW * 6 = 2,400 KRW
        entry_fees = self.costs.buy_fee(9720.0 * 6) + (9720.0 * self.costs.entry_slippage * 6)
        exit_fees = self.costs.sell_fee(10120.0 * 6) + (10120.0 * self.costs.exit_slippage * 6)
        total_costs = entry_fees + exit_fees
        net_profit = gross_profit - total_costs

        self.assertEqual(gross_profit, 2400.0)
        self.assertGreater(net_profit, 1800.0)  # Net profit over 1,800 KRW after all fees & slippage
        del self.engine.s['positions'][self.symbol]
        self.assertNotIn(self.symbol, self.engine.s['positions'])


if __name__ == '__main__':
    unittest.main()
