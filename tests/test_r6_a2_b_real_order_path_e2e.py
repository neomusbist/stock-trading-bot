import copy
import json
import shutil
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from automation.policy import Policy, AutomationConfig
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.capital import Costs

AT = datetime(2026, 11, 2, 10, 0, 0, tzinfo=KST)


def make_bars(rows, end_at=AT):
    return [
        dict(
            time=end_at - timedelta(minutes=len(rows) - i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            first_second=0,
        )
        for i, (o, h, l, c, v) in enumerate(rows)
    ]


A2_VALID = make_bars([
    (10000, 10150, 9950, 10100, 300),
    (10100, 10200, 10050, 10180, 280),
    (10180, 10850, 10170, 10800, 900),
    (10240, 10250, 10200, 10220, 320),
    (10220, 10280, 10210, 10270, 450),
])


class MockRealBroker:
    def __init__(self, identity="mock_broker_001"):
        self.identity = identity
        self.orders = []
        self.cancels = []
        self.next_order_id = 1000

    def submit(self, side, symbol, qty, limit=0, venue='KRX', session='REGULAR'):
        self.next_order_id += 1
        ord_no = str(self.next_order_id)
        self.orders.append({'id': ord_no, 'side': side, 'symbol': symbol, 'qty': qty, 'limit': limit})
        return ord_no

    def cancel(self, symbol, order_id, venue='KRX'):
        self.cancels.append({'symbol': symbol, 'order_id': str(order_id)})
        return "C-" + str(order_id)

    def snapshot(self):
        return {'holdings': {}, 'orders': [], 'cash': 70420.8, 'equity': 70420.8}


class A2BRealOrderPathE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_engine(self, enable_a2_live=False, enable_modb_live=False):
        cfg = Config(
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_position_fraction=1.0,
            max_positions=1,
            paper_cash=70420.8,
            account_equity=70420.8,
            enable_a2_live=enable_a2_live,
            enable_modb_live=enable_modb_live,
        )
        auto = AutomationConfig(
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_breakout_enabled=True,
            structural_pullback_enabled=True,
            confirmed_invalidation_enabled=True,
            a2_breakout_retest_enabled=True,
            b_support_reclaim_entry_enabled=True,
            enable_a2_live=enable_a2_live,
            enable_modb_live=enable_modb_live,
            force_flatten_start="15:10:00",
        )

        broker = MockRealBroker()
        self.clock_time = AT
        clock_fn = lambda: self.clock_time

        state = {
            "version": 1,
            "day": "2026-11-02",
            "config_hash": cfg.fingerprint(),
            "identity": broker.identity,
            "positions": {},
            "orders": {},
            "entries": {},
            "daily_loss": 0.0,
            "realized": 0.0,
            "cash": 70420.8,
            "equity": 70420.8,
            "halt": "",
            "excluded": [],
            "trades": [],
            "sequence": 0,
            "automation": {
                "approved_cap": 81300,
                "allocated": False,
                "cash": 70420.8,
                "existing": {},
                "consecutive_losses": 0,
                "strategy": "IDLE",
                "last_exit": {},
                "config_hash": auto.fingerprint(),
            },
        }
        with open(self.folder / "state.json", "w", encoding="utf-8") as f:
            json.dump(state, f)

        policy_path = self.folder / "policy.json"
        with open(policy_path, "w", encoding="utf-8") as f:
            json.dump({"mode": "LIVE", "live_authorized": True, "PAPER_FIXTURE_ONLY": False}, f)
        policy = Policy(policy_path)

        engine = AutonomousEngine(cfg, auto, policy, self.folder, broker, clock_fn)
        engine.available_cash = 70420.8
        engine.account_ok = True
        engine.connected = True
        engine.last_account = time.monotonic()
        engine.market = "3"
        engine.market_status["047770"] = {"pass": True, "checked_at": AT.isoformat()}
        engine.source_qualification["047770"] = {"source_count": 3, "timestamp": AT.isoformat()}
        return engine, broker, cfg, auto

    def build_a2_signal_and_tick(self, cfg, auto):
        a2_entry = 10260
        det = PatternDetector(cfg, auto, self.costs)
        det.observation_start["047770"] = AT - timedelta(minutes=6)
        det.history["047770"] = deque(copy.deepcopy(A2_VALID))
        for s in reversed(range(10, 70)):
            det.buffers.setdefault("047770", deque()).append(
                Tick("047770", AT - timedelta(seconds=s), 10240, 200, 100000 - s * 10, 10240, 10230)
            )
        for s in reversed(range(1, 9)):
            det.buffers["047770"].append(
                Tick("047770", AT - timedelta(seconds=s), 10250, 500, 100000 - s, 10250, 10240)
            )
        t = Tick("047770", AT, a2_entry, 600, 100100, a2_entry, 10250)
        watch = {
            "status": "WATCH",
            "resistance": 11000,
            "resistance_room_pass": True,
            "source_details": {"a": {}, "b": {}},
            "market_status": {"pass": True},
        }
        sig = det.signal(t, watch)
        return sig, t

    def test_default_live_mode_routes_a2_to_shadow_without_broker_submit(self):
        """By default (enable_a2_live=False), A2 signals create SHADOW orders and NEVER call broker.submit."""
        engine, broker, cfg, auto = self._create_engine(enable_a2_live=False)
        sig, tick = self.build_a2_signal_and_tick(cfg, auto)

        o = engine.enter(sig, tick)
        self.assertIsNotNone(o)
        self.assertTrue(o['id'].startswith('SHADOW-'))
        self.assertEqual(o['execution'], 'SHADOW')
        self.assertEqual(len(broker.orders), 0, "Real broker must receive 0 orders when enable_a2_live is False")

    def test_authorized_live_mode_submits_real_broker_limit_buy(self):
        """When explicitly authorized (enable_a2_live=True), A2 signal submits REAL broker limit order."""
        engine, broker, cfg, auto = self._create_engine(enable_a2_live=True)
        sig, tick = self.build_a2_signal_and_tick(cfg, auto)

        o = engine.enter(sig, tick)
        self.assertIsNotNone(o)
        self.assertEqual(o['execution'], 'REAL')
        self.assertEqual(len(broker.orders), 1, "Broker must receive exactly 1 real order")
        sent = broker.orders[0]
        self.assertEqual(sent['side'], 'BUY')
        self.assertEqual(sent['symbol'], '047770')
        self.assertEqual(sent['limit'], 10260)
        self.assertEqual(o['id'], sent['id'])
        self.assertIn(o['local'], engine.active_limit_lifecycles)

    def test_authorized_live_order_cancels_on_broker_when_support_lost(self):
        """When real order loses support before fill, cancel() sends cancel request to broker."""
        engine, broker, cfg, auto = self._create_engine(enable_a2_live=True)
        sig, tick = self.build_a2_signal_and_tick(cfg, auto)

        o = engine.enter(sig, tick)
        ord_no = o['id']

        # Quote drops below structural stop 10180
        t_bad = AT + timedelta(seconds=10)
        bad_tick = Tick('047770', t_bad, 10170, 10, 10010, 10180, 10170)
        engine.on_tick(bad_tick)

        self.assertEqual(len(broker.cancels), 1, "Broker cancel must be invoked")
        self.assertEqual(broker.cancels[0]['order_id'], ord_no)
        self.assertEqual(o['status'], 'CANCEL_SENT')

    def test_authorized_live_order_cancels_on_broker_when_timeout(self):
        """When real order exceeds 120s without fill, timer() cancels order on broker."""
        engine, broker, cfg, auto = self._create_engine(enable_a2_live=True)
        sig, tick = self.build_a2_signal_and_tick(cfg, auto)

        o = engine.enter(sig, tick)
        ord_no = o['id']

        # 121 seconds pass
        t_timeout = AT + timedelta(seconds=121)
        self.clock_time = t_timeout
        engine.timer()

        self.assertEqual(len(broker.cancels), 1, "Broker cancel must be invoked upon 120s timeout")
        self.assertEqual(broker.cancels[0]['order_id'], ord_no)
        self.assertEqual(o['status'], 'CANCEL_SENT')

    def test_max_one_position_strictly_blocks_second_order(self):
        """While holding 1 position, subsequent A2 order is blocked at gate (0 broker calls)."""
        engine, broker, cfg, auto = self._create_engine(enable_a2_live=True)

        # Pre-seed 1 held position
        engine.s['positions']['005930'] = {
            'symbol': '005930', 'qty': 1, 'avg': 60000, 'ownership': 'BOT',
            'stop': 59000, 'target': 62000, 'entry': 60000
        }

        sig, tick = self.build_a2_signal_and_tick(cfg, auto)
        o = engine.enter(sig, tick)
        self.assertIsNone(o, "Order must be None because max concurrent positions is 1")
        self.assertEqual(len(broker.orders), 0, "Broker must not receive any orders")

    def _prepare_b_signal_and_tick(self, engine):
        from dataclasses import replace
        engine.a = replace(engine.a, structural_pullback_enabled=False)
        engine.source_qualification['200350'] = {'source_count': 3, 'timestamp': AT.isoformat()}
        engine.market_status['200350'] = {'pass': True, 'checked_at': AT.isoformat()}
        sig = {
            'pattern': 'B', 'setup_variant': 'MOD_B', 'symbol': '200350', 'limit': 3310, 'stop': 3225, 'structural_stop': 3260, 'target': 3530,
            'time': AT.isoformat(), 'market_state': '3', 'net_reward_risk': 2.1, 'breakout': 3300,
            'efficiency': {'decision': 'TRADE', 'score': 1.5},
        }
        tick = Tick("200350", AT, 3310, 100, 10000, 3310, 3300)
        return sig, tick

    def test_default_live_mode_routes_b_to_shadow_without_broker_submit(self):
        """By default (enable_modb_live=False), B signals create SHADOW orders and NEVER call broker.submit."""
        engine, broker, cfg, auto = self._create_engine(enable_modb_live=False)
        sig, tick = self._prepare_b_signal_and_tick(engine)
        o = engine.enter(sig, tick)
        self.assertIsNotNone(o)
        self.assertTrue(o['id'].startswith('SHADOW-'))
        self.assertEqual(o['execution'], 'SHADOW')
        self.assertEqual(len(broker.orders), 0, "Broker must receive 0 real orders when enable_modb_live is False")

    def test_authorized_live_mode_submits_real_broker_limit_buy_for_b(self):
        """When explicitly authorized (enable_modb_live=True), B signal submits REAL broker limit order."""
        engine, broker, cfg, auto = self._create_engine(enable_modb_live=True)
        sig, tick = self._prepare_b_signal_and_tick(engine)
        o = engine.enter(sig, tick)
        self.assertIsNotNone(o)
        self.assertEqual(o['execution'], 'REAL')
        self.assertEqual(len(broker.orders), 1, "Broker must receive exactly 1 real order for authorized B")
        self.assertEqual(broker.orders[0]['symbol'], '200350')


if __name__ == '__main__':
    unittest.main()
