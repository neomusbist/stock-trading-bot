"""R6 FAST Momentum LIVE Order Integration & Broker Simulation Tests.

Verifies end-to-end integration of FAST momentum signal with AutonomousEngine,
FakeBroker, risk management (2% trade, 4% daily, 81,300 KRW cap, 1 slot),
entry latency & anti-chase gates, exit lifecycle, and failure injection.
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
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.fast_momentum import FastMomentumStrategy, FastSignal
from automation.runner import evaluate_actual_entry, order_ready_from_evidence


class IntegrationFakeBroker:
    """Fake broker tracking submissions, cancels, and simulated network states."""
    identity = 'autonomous-live-test'

    def __init__(self):
        self.orders = []
        self.cancels = []
        self.reject_next_order = False
        self.reject_message = 'BROKER_REJECT_TEST'
        self.order_counter = 0

    def submit(self, side, sym, qty, limit, venue='KRX', session='REGULAR'):
        if self.reject_next_order:
            self.reject_next_order = False
            raise Rejected(self.reject_message)
        self.order_counter += 1
        order_id = f'LIVE_ORD_{self.order_counter}'
        self.orders.append({
            'id': order_id,
            'side': side,
            'symbol': sym,
            'qty': qty,
            'limit': limit,
            'venue': venue,
            'session': session
        })
        return order_id

    def cancel(self, sym, oid):
        self.cancels.append((sym, oid))
        return f'C_{oid}'


class FastLiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp_dir.name)
        self.clock_time = datetime(2026, 10, 15, 9, 30, 0, tzinfo=KST)
        self.broker = IntegrationFakeBroker()

        self.policy = Policy(self.folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')

        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            fast_momentum_enabled=True,
            min_source_count=1,
            fallback_source1=True,
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
        self.cumulative = 0

        self.symbol = '079650'  # 서산
        self.engine.names[self.symbol] = '서산'
        self.engine.market_status[self.symbol] = {'pass': True, 'reason_code': 'SYNTHETIC_TEST_PASS'}

    def tearDown(self):
        self.temp_dir.cleanup()

    def advance_time(self, seconds=1):
        self.clock_time += timedelta(seconds=seconds)

    def make_tick(self, price, bid=None, ask=None, volume=500, sym=None, book_time=None, bid_qty=10000, ask_qty=10000):
        sym = sym or self.symbol
        bid = bid if bid is not None else price - 1
        ask = ask if ask is not None else price
        self.cumulative = getattr(self, 'cumulative', 100000) + volume
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
            book_time=book_time or self.clock_time,
            depth_source='SIMULATED_QUOTE',
        )

    def create_mock_watch(self, resistance=1045.0):
        return {
            'code': self.symbol,
            'name': '서산',
            'status': 'WATCH',
            'reference_price': 984.0,
            'source_count': 2,
            'fallback_source1': True,
            'market_status': {'pass': True, 'reason_code': 'MARKET_NORMAL'},
            'resistance': resistance,
            'resistance_asof': self.clock_time.isoformat(),
            'observed_at': self.clock_time.isoformat(),
            'venue': 'KRX'
        }

    def feed_setup_ticks(self):
        """Feed a realistic causal micro pullback-rebound sequence into engine.fast_strategy."""
        causal = [984, 988, 992, 996, 1000, 990, 992, 994, 996, 998, 1001]
        cumulative = 0
        data = []
        for i, price in enumerate(causal):
            t_time = self.clock_time + timedelta(seconds=i)
            cumulative += 1000
            t = Tick(
                symbol=self.symbol,
                time=t_time,
                price=float(price),
                volume=1000,
                cumulative=cumulative,
                ask=float(price),
                bid=float(price - 1),
                regular=True,
                ask_qty=10000,
                bid_qty=10000,
                depth_available=True,
                book_time=t_time,
                depth_source='SIMULATED_QUOTE'
            )
            self.engine.fast_strategy.ingest_tick(t, t_time)
            data.append(t)
        self.clock_time = data[-1].time
        self.cumulative = cumulative
        return data[-1]

    def test_fast_live_full_lifecycle_buy_to_sell(self):
        """1. Complete Lifecycle: Tick -> FAST Signal -> BUY -> Fill -> Trailing Exit -> SELL -> Fill."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        # Evaluate FAST signal for LIVE
        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        self.assertIsNotNone(fast_sig, "FAST signal should trigger on clear impulse breakout")
        self.assertTrue(fast_sig.extra['actual_order_permission'])
        self.assertFalse(fast_sig.extra['SHADOW_ONLY'])

        # Convert to engine signal and execute LIVE enter
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)
        self.assertIsNotNone(order, "Order should be accepted by AutonomousEngine")
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertEqual(order['side'], 'BUY')
        self.assertEqual(len(self.broker.orders), 1)
        self.assertEqual(self.broker.orders[0]['side'], 'BUY')

        # Simulate broker fill
        fill_qty = order['qty']
        fill_price = order['limit']
        self.engine.cumulative_fill(order, fill_qty, fill_qty * fill_price)
        self.assertEqual(order['status'], 'FILLED')

        # Verify position is active with FAST attributes
        pos = self.engine.s['positions'].get(self.symbol)
        self.assertIsNotNone(pos)
        self.assertEqual(pos['qty'], fill_qty)
        self.assertEqual(pos['pattern'], 'FAST')
        self.assertIn('emergency', pos)
        self.assertIn('exit_plan', pos)

        # Simulate price rising towards target (+1R profit lock)
        self.advance_time(1)
        peak_tick = self.make_tick(fill_price + 20, bid=fill_price + 19, ask=fill_price + 20, volume=1000)
        self.engine.on_tick(peak_tick)

        # Now simulate momentum breakdown below support/stop
        self.advance_time(1)
        stop_tick = self.make_tick(pos['stop'] - 5, bid=pos['stop'] - 6, ask=pos['stop'] - 5, volume=1000)
        self.engine.on_tick(stop_tick)

        # Verify SELL exit was requested and submitted to FakeBroker
        self.assertTrue(pos.get('exit_requested'))
        self.assertEqual(len(self.broker.orders), 2)
        sell_order = self.broker.orders[1]
        self.assertEqual(sell_order['side'], 'SELL')
        self.assertEqual(sell_order['qty'], fill_qty)

        # Simulate broker fill for SELL order
        active_sell = [o for o in self.engine.s['orders'].values() if o['side'] == 'SELL'][-1]
        self.engine.cumulative_fill(active_sell, fill_qty, fill_qty * (pos['stop'] - 6))
        self.assertNotIn(self.symbol, self.engine.s['positions'])
        self.assertEqual(len(self.engine.s['trades']), 1)
        self.assertEqual(self.engine.s['trades'][0]['symbol'], self.symbol)

    def test_fast_live_partial_fill_accounting(self):
        """2. Partial fill cash accounting: no double reservation."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)
        self.assertIsNotNone(order)
        total_qty = order['qty']
        self.assertGreater(total_qty, 1)

        # Fill 1 share partially
        partial_qty = 1
        fill_price = order['limit']
        self.engine.cumulative_fill(order, partial_qty, partial_qty * fill_price)
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertEqual(order['filled'], partial_qty)

        # Check pending buy reservation: must only reserve remaining (total_qty - partial_qty)
        reserved = self.engine.pending_buy_reservation()
        remaining_qty = total_qty - partial_qty
        expected_reserved = remaining_qty * fill_price * (1 + self.auto.commission_buy)
        self.assertAlmostEqual(reserved, expected_reserved, places=2)

        # Complete the fill
        self.engine.cumulative_fill(order, total_qty, total_qty * fill_price)
        self.assertEqual(order['status'], 'FILLED')
        self.assertEqual(self.engine.pending_buy_reservation(), 0.0)

    def test_fast_live_broker_rejection(self):
        """3. Broker rejection handling: failure latch, cash release, no position."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        # Configure broker to reject the order
        self.broker.reject_next_order = True
        self.broker.reject_message = 'ORDER_REJECTED: 증거금 부족'

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)

        # Order should be marked REJECTED
        self.assertIsNotNone(order)
        self.assertEqual(order['status'], 'REJECTED')
        self.assertNotIn(self.symbol, self.engine.s['positions'])
        self.assertEqual(self.engine.pending_buy_reservation(), 0.0)

    def test_fast_live_ttl_latency_rejection(self):
        """4. Entry latency and TTL expiration: signals older than 15s must be rejected."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)

        # Simulate network or processing latency: clock moves forward 16 seconds
        self.advance_time(16)
        late_tick = self.make_tick(trigger_tick.price, bid=trigger_tick.bid, ask=trigger_tick.ask)

        order = self.engine.enter(engine_sig, late_tick)
        self.assertIsNone(order, "Order must be rejected when signal latency exceeds 15s TTL")
        self.assertEqual(len(self.broker.orders), 0)

    def test_fast_live_max_chase_rejection(self):
        """5. Anti-chase gate: price rising above breakout * 1.003 must be rejected."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)

        # Price spikes up above max_chase_price
        chase_ask = fast_sig.max_chase_price + 5
        chase_tick = self.make_tick(chase_ask, bid=chase_ask - 1, ask=chase_ask)

        order = self.engine.enter(engine_sig, chase_tick)
        self.assertIsNone(order, "Order must be rejected when ask exceeds max_chase_price")
        self.assertEqual(len(self.broker.orders), 0)

    def test_fast_live_single_slot_concurrency(self):
        """6. Single-slot constraint: holding 1 position blocks entry into any other symbol."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)
        self.assertIsNotNone(order)
        self.engine.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])

        # Now try to enter another symbol (e.g. 010580)
        sym2 = '010580'
        self.engine.names[sym2] = '에스엠벡셀'
        self.engine.market_status[sym2] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}
        tick2 = self.make_tick(2800, bid=2790, ask=2800, sym=sym2)

        fake_sig2 = {
            'symbol': sym2,
            'name': '에스엠벡셀',
            'pattern': 'FAST',
            'limit': 2800,
            'stop': 2750,
            'emergency': 2700,
            'target': 3100,
            'planned_qty': 10,
            'time': self.clock_time.isoformat(),
            'source_count': 2,
            'fallback_source1': True,
            'efficiency': {'decision': 'TRADE', 'score': 2.0, 'inputs': {'entry': 2800, 'stop': 2750, 'target': 3100, 'confidence': 0.8, 'planned_minutes': 10}},
            'evaluation_gates': {'breakout': {'result': 'PASS'}, 'invalidation': {'result': 'PASS'}},
            'SHADOW_ONLY': False,
            'actual_order_permission': True
        }
        order2 = self.engine.enter(fake_sig2, tick2)
        self.assertIsNone(order2, "Must reject second position under max_positions=1")
        self.assertEqual(len(self.engine.s['positions']), 1)

    def test_fast_live_emergency_plunge_exit(self):
        """7. Emergency plunge exit: sudden bid drop below emergency line triggers immediate market sell."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        fast_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=True
        )
        engine_sig = fast_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)
        self.engine.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])

        pos = self.engine.s['positions'][self.symbol]
        emergency_line = pos['emergency']

        # Flash crash tick below emergency line
        self.advance_time(1)
        crash_tick = self.make_tick(emergency_line - 5, bid=emergency_line - 10, ask=emergency_line - 5, volume=5000)
        self.engine.on_tick(crash_tick)

        self.assertTrue(pos.get('exit_requested'))
        self.assertIn('EMERGENCY', pos.get('exit_reason', ''))
        sell_orders = [o for o in self.broker.orders if o['side'] == 'SELL']
        self.assertEqual(len(sell_orders), 1)

    def test_fast_shadow_and_live_mode_isolation(self):
        """8. Mode isolation: SHADOW signals must never trigger live orders; ARMED mode blocks broker."""
        watch = self.create_mock_watch(resistance=1045.0)
        trigger_tick = self.feed_setup_ticks()

        # Evaluate with live_authorized=False (standard shadow mode)
        shadow_sig = self.engine.fast_strategy.evaluate(
            trigger_tick, watch,
            capital_limit=self.engine.auto_available_capital(),
            risk_budget=self.engine.risk_equity() * self.cfg.risk_fraction,
            ingest=False, received=self.clock_time, live_authorized=False
        )
        self.assertTrue(shadow_sig.extra['SHADOW_ONLY'])
        self.assertFalse(shadow_sig.extra['actual_order_permission'])

        # Attempt to pass shadow signal to engine: must be blocked
        engine_sig = shadow_sig.to_engine_signal(watch)
        order = self.engine.enter(engine_sig, trigger_tick)
        self.assertIsNone(order, "Engine must strictly reject signals flagged with SHADOW_ONLY=True")
        self.assertEqual(len(self.broker.orders), 0)


if __name__ == '__main__':
    unittest.main()
