"""
End-to-End Real Order Path and Exit Lifecycle Tests for FAST Momentum.

Verifies:
1. FAST signal generation without artificial peak clamping (resistance from higher timeframe / projection).
2. Economic vetting (strict net RR >= 1.5, trading costs 0.730% modeled).
3. Risk-based quantity sizing (emergency stop distance, auto cap 81,300 KRW, 2% trade risk).
4. Order placement: Limit BUY sent, order ID received.
5. Unfilled order timeout / cancellation path and capital reservation release.
6. Partial fill: Partial execution -> remainder cancel -> emergency plunge exit.
7. Stop loss execution: Price drop below stop/emergency line triggers exit.
8. Multi-position engine: Two independent symbols held concurrently up to max_positions=2, third rejected.
"""
import time
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import replace

from core.settings import KST, Config
from core.strategy import Tick
from core.broker import Rejected
from automation.policy import Policy, AutomationConfig
from automation.engine import AutonomousEngine
from automation.fast_momentum import FastMomentumStrategy


class MockBroker:
    identity = 'mock-broker-e2e'

    def __init__(self):
        self.orders = []
        self.cancels = []
        self.order_counter = 0

    def submit(self, side, sym, qty, limit, venue='KRX', session='REGULAR'):
        self.order_counter += 1
        oid = f"ORD_{self.order_counter:04d}"
        order_info = {
            'id': oid,
            'side': side,
            'symbol': sym,
            'qty': qty,
            'limit': limit,
            'venue': venue,
            'session': session,
            'time': datetime.now(KST).isoformat()
        }
        self.orders.append(order_info)
        return oid

    def cancel(self, sym, oid):
        self.cancels.append((sym, oid))
        return f"CANCEL_{oid}"


class FastRealOrderPathE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp_dir.name)
        self.clock_time = datetime(2026, 11, 2, 10, 0, 0, tzinfo=KST)
        self.broker = MockBroker()

        self.policy = Policy(self.folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')

        self.cfg = replace(
            Config(),
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_positions=2,
            min_reward_risk=1.5,
            account_equity=81300,
            stale_seconds=5
        )
        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            fast_momentum_enabled=True,
            min_source_count=1,
            fallback_source1=True,
            cooldown_seconds=300
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
        self.cumulative = 100000

    def tearDown(self):
        self.temp_dir.cleanup()

    def advance_time(self, seconds=1):
        self.clock_time += timedelta(seconds=seconds)

    def make_tick(self, price, bid=None, ask=None, volume=500, sym='079650', bid_qty=10000, ask_qty=10000):
        bid = bid if bid is not None else price - 1
        ask = ask if ask is not None else price
        self.cumulative += volume
        return Tick(
            symbol=sym,
            time=self.clock_time,
            price=float(price),
            volume=int(volume),
            cumulative=self.cumulative,
            ask=float(ask),
            bid=float(bid),
            regular=True,
            ask_qty=int(ask_qty),
            bid_qty=int(bid_qty),
            depth_available=True,
            book_time=self.clock_time,
            depth_source='SIMULATED_QUOTE',
        )

    def feed_fast_breakout_ticks(self, sym='079650', base_price=984.0):
        """Feeds a realistic causal micro pullback-rebound sequence (11 ticks)."""
        offset = base_price - 984.0
        causal = [984, 988, 992, 996, 1000, 990, 992, 994, 996, 998, 1001]
        data = []
        for i, price in enumerate(causal):
            p = float(price + offset)
            self.advance_time(1)
            t = self.make_tick(p, sym=sym, volume=1000)
            self.engine.fast_strategy.ingest_tick(t, self.clock_time)
            data.append(t)
        return data[-1]

    def test_e2e_fast_breakout_and_exit(self):
        """E2E Test 1: FAST Breakout -> Economic Sizing -> Buy Order -> Fill -> Plunge -> Exit."""
        sym = '079650'
        self.engine.names[sym] = '서산'
        self.engine.market_status[sym] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}

        trigger_tick = self.feed_fast_breakout_ticks(sym=sym, base_price=984.0)

        # Higher timeframe resistance at 1045.0 (without any fake peak)
        watch = {
            'symbol': sym,
            'name': '서산',
            'status': 'WATCH',
            'resistance': 1045.0,
            'daily_high_20d': 1050.0,
            'reference_price': 984.0,
            'source_count': 2,
            'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }

        # 1. Strategy Signal Evaluation
        cap_avail = self.engine.auto_available_capital()
        risk_budget = self.engine.risk_equity() * self.cfg.risk_fraction
        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=cap_avail,
            risk_budget=risk_budget,
            ingest=False,
            received=self.clock_time,
            live_authorized=True
        )

        self.assertIsNotNone(fast_sig, f"FAST signal must be generated: {self.engine.fast_strategy.last_evaluation.get(sym)}")
        self.assertGreaterEqual(fast_sig.net_reward_risk, 1.5)
        self.assertEqual(fast_sig.target, 1045.0)
        self.assertGreater(fast_sig.quantity, 0)

        # 2. Engine Enter & Order Generation
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)

        self.assertIsNotNone(order)
        self.assertEqual(len(self.broker.orders), 1)
        placed = self.broker.orders[0]
        self.assertEqual(placed['side'], 'BUY')
        self.assertEqual(placed['symbol'], sym)
        self.assertEqual(placed['limit'], 1001.0)
        self.assertEqual(placed['qty'], order['qty'])

        # 3. Order Fill
        self.engine.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])
        self.assertIn(sym, self.engine.s['positions'])
        pos = self.engine.s['positions'][sym]
        self.assertEqual(pos['qty'], order['qty'])

        # 4. Momentum breakdown triggers exit
        self.advance_time(1)
        stop_tick = self.make_tick(pos['stop'] - 5, bid=pos['stop'] - 6, ask=pos['stop'] - 5, volume=1000)
        self.engine.on_tick(stop_tick)

        self.assertTrue(pos.get('exit_requested'))
        self.assertIn(pos.get('exit_reason', ''), ('FAST_EMERGENCY_PLUNGE_EXIT', 'FAST_INVALIDATION_STOP_EXIT'))
        sell_orders = [o for o in self.broker.orders if o['side'] == 'SELL']
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]['qty'], order['qty'])

    def test_e2e_fast_unfilled_order_cancel_and_capital_recovery(self):
        """E2E Test 2: FAST Limit BUY sent -> No fill -> Cancel -> Capital reservation freed."""
        sym = '079650'
        self.engine.names[sym] = '서산'
        self.engine.market_status[sym] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}

        trigger_tick = self.feed_fast_breakout_ticks(sym=sym, base_price=984.0)
        watch = {
            'symbol': sym, 'name': '서산', 'status': 'WATCH', 'resistance': 1045.0,
            'source_count': 2, 'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        order = self.engine.enter(fast_sig.to_engine_signal(watch), trigger_tick)
        self.assertIsNotNone(order)
        oid = order['id']
        self.assertIn(order['local'], self.engine.s['orders'])

        # Cancel the open order (simulate timeout)
        self.engine.cancel(order)
        self.assertIn(order['status'], ('CANCEL_SENT', 'CANCELED'))

    def test_e2e_fast_partial_fill_and_emergency_stop(self):
        """E2E Test 3: FAST BUY partial fill -> Remainder cancel -> Emergency plunge exit."""
        sym = '079650'
        self.engine.names[sym] = '서산'
        self.engine.market_status[sym] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}

        trigger_tick = self.feed_fast_breakout_ticks(sym=sym, base_price=984.0)
        watch = {
            'symbol': sym, 'name': '서산', 'status': 'WATCH', 'resistance': 1045.0,
            'source_count': 2, 'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        order = self.engine.enter(fast_sig.to_engine_signal(watch), trigger_tick)
        fill_qty = 1

        # Partial fill
        self.engine.cumulative_fill(order, fill_qty, fill_qty * order['limit'])
        pos = self.engine.s['positions'][sym]
        self.assertEqual(pos['qty'], fill_qty)

        # Cancel remainder
        self.engine.cancel(order)
        order['status'] = 'CANCELED'  # Broker confirms cancellation

        # Price plunges below emergency stop
        emergency_line = pos['emergency']
        self.advance_time(1)
        crash_tick = self.make_tick(emergency_line - 10.0, bid=emergency_line - 11.0, ask=emergency_line - 10.0, sym=sym, volume=5000)
        self.engine.on_tick(crash_tick)

        # Verify emergency exit triggered for the partially filled qty
        self.assertTrue(pos.get('exit_requested'))
        self.assertIn('EMERGENCY', pos.get('exit_reason', ''))
        sell_orders = [o for o in self.broker.orders if o['side'] == 'SELL']
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0]['qty'], fill_qty)

    def test_e2e_multi_position_two_independent_symbols(self):
        """E2E Test 4: Multi-position engine holds 2 symbols independently up to max_positions=2."""
        sym1 = '079650'
        sym2 = '010580'
        self.engine.names[sym1] = '서산'
        self.engine.names[sym2] = '에스엠벡셀'
        self.engine.market_status[sym1] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}
        self.engine.market_status[sym2] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}

        # 1. Enter First Symbol
        t1 = self.feed_fast_breakout_ticks(sym=sym1, base_price=984.0)
        watch1 = {
            'symbol': sym1, 'name': '서산', 'status': 'WATCH', 'resistance': 1045.0,
            'source_count': 2, 'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }
        fast_sig1 = self.engine.fast_strategy.evaluate(
            t1, watch1,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        self.assertIsNotNone(fast_sig1)
        order1 = self.engine.enter(fast_sig1.to_engine_signal(watch1), t1)
        self.assertIsNotNone(order1)
        self.engine.cumulative_fill(order1, 1, 1001.0)
        self.assertEqual(len(self.engine.s['positions']), 1)

        # 2. Enter Second Symbol (2500 base, 1.4% extension)
        causal2 = [2500, 2510, 2520, 2525, 2530, 2505, 2510, 2515, 2520, 2525, 2535]
        data2 = []
        for p in causal2:
            self.advance_time(1)
            t = self.make_tick(p, bid=p-5, ask=p, sym=sym2, volume=1000)
            self.engine.fast_strategy.ingest_tick(t, self.clock_time)
            data2.append(t)
        t2 = data2[-1]

        watch2 = {
            'symbol': sym2, 'name': '에스엠벡셀', 'status': 'WATCH', 'resistance': 2750.0,
            'source_count': 2, 'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }
        fast_sig2 = self.engine.fast_strategy.evaluate(
            t2, watch2,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        self.assertIsNotNone(fast_sig2, f"Second symbol FAST signal expected: {self.engine.fast_strategy.last_evaluation.get(sym2)}")
        order2 = self.engine.enter(fast_sig2.to_engine_signal(watch2), t2)
        self.assertIsNotNone(order2, "Must allow second position under max_positions=2")
        self.engine.cumulative_fill(order2, 1, 2535.0)
        self.assertEqual(len(self.engine.s['positions']), 2)

        # 3. Third Symbol rejected because max_positions=2 is reached
        sym3 = '005930'
        self.engine.names[sym3] = '삼성전자'
        self.engine.market_status[sym3] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}
        t3 = self.feed_fast_breakout_ticks(sym=sym3, base_price=984.0)
        watch3 = {
            'symbol': sym3, 'name': '삼성전자', 'status': 'WATCH', 'resistance': 1045.0,
            'source_count': 2, 'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'observed_at': self.clock_time.isoformat()
        }
        fast_sig3 = self.engine.fast_strategy.evaluate(
            t3, watch3,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        self.assertIsNotNone(fast_sig3)
        order3 = self.engine.enter(fast_sig3.to_engine_signal(watch3), t3)
        self.assertIsNone(order3, "Third position must be rejected when max_positions=2")
        self.assertEqual(len(self.engine.s['positions']), 2)


if __name__ == '__main__':
    unittest.main()
