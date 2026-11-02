import copy
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from core.util import floor_tick
from automation.policy import Policy, AutomationConfig
from automation.engine import AutonomousEngine
from automation.capital import Costs
from automation.multi_timeframe import MultiTimeframeStrategy, resample_bars

AT = datetime(2026, 11, 2, 10, 0, 0, tzinfo=KST)


class MockRealBroker:
    def __init__(self, identity="mock_broker_multi_001"):
        self.identity = identity
        self.orders = []
        self.cancels = []
        self.next_order_id = 2000

    def submit(self, side, symbol, qty, limit=0, venue='KRX', session='REGULAR'):
        self.next_order_id += 1
        ord_no = str(self.next_order_id)
        self.orders.append({
            'id': ord_no,
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'limit': limit,
            'venue': venue,
            'session': session,
        })
        return ord_no

    def cancel(self, symbol, order_id, venue='KRX'):
        self.cancels.append({'symbol': symbol, 'order_id': str(order_id)})
        return "C-" + str(order_id)

    def snapshot(self):
        return {'holdings': {}, 'orders': [], 'cash': 70847.0, 'equity': 70847.0}


class MultiPositionEngineE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_engine(self, max_positions=2, enable_live=True, cash=70847.0):
        cfg = Config(
            live_mode=True,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            max_position_fraction=1.0,
            max_positions=max_positions,
            paper_cash=cash,
            account_equity=cash,
            enable_a2_live=enable_live,
            enable_mtf_live=enable_live,
        )
        auto = AutomationConfig(
            AUTO_TRADING_CAP_KRW=81300,
            costs_confirmed=True,
            structural_breakout_enabled=True,
            structural_pullback_enabled=True,
            confirmed_invalidation_enabled=True,
            a2_breakout_retest_enabled=True,
            enable_a2_live=enable_live,
            enable_mtf_live=enable_live,
            mtf_entry_enabled=True,
            max_positions=max_positions,
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
            "cash": cash,
            "equity": cash,
            "halt": "",
            "excluded": [],
            "trades": [],
            "sequence": 0,
            "automation": {
                "approved_cap": 81300,
                "allocated": False,
                "cash": cash,
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
        engine.available_cash = cash
        engine.account_ok = True
        engine.connected = True
        engine.last_account = time.monotonic()
        engine.market = "3"
        return engine, broker, cfg, auto

    def _make_signal(self, symbol, limit, emergency_stop, structural_stop, target, pattern="MTF"):
        econ = {
            'status': 'KNOWN',
            'entry_price': limit,
            'structural_stop': structural_stop,
            'emergency_stop': emergency_stop,
            'target': target,
            'buy_fee_per_share': limit * 0.00015,
            'entry_slippage_reserve': limit * 0.002,
            'target_sell_fee_per_share': target * 0.00215,
            'target_exit_slippage_reserve': target * 0.003,
            'emergency_sell_fee_per_share': emergency_stop * 0.00215,
            'emergency_exit_slippage_reserve': emergency_stop * 0.003,
            'one_share_cash': limit * 1.00215,
            'risk_per_share': (limit - emergency_stop) + limit * 0.00215 + emergency_stop * 0.00515,
            'reward_after_cost': (target - limit) - (limit * 0.00215 + target * 0.00515),
            'cost_per_share': limit * 0.00215 + target * 0.00515,
            'gross_rr': (target - limit) / (limit - emergency_stop),
            'net_rr': 2.10,
            'structural_gross_rr': (target - limit) / (limit - structural_stop),
            'minimum_net_rr': 1.5,
            'rr_pass': True,
            'max_entry_price': limit + 20,
            'max_entry_price_unrounded': limit + 25.0,
            'above_max_entry': False,
            'actual_order_permission': True,
            'costs': {'buy': 0.00015, 'sell': 0.00015, 'tax': 0.002, 'entry_slippage': 0.002, 'exit_slippage': 0.003},
        }
        return {
            'pattern': pattern,
            'setup_variant': 'MTF_3M_1M',
            'symbol': symbol,
            'limit': limit,
            'stop': emergency_stop,
            'structural_stop': structural_stop,
            'emergency': emergency_stop,
            'target': target,
            'net_reward_risk': 2.10,
            'invalidation_policy': {
                'enabled': True,
                'structural_stop': structural_stop,
                'emergency_stop': emergency_stop,
                'target_ceiling': target,
            },
            'use_limit_lifecycle': False,
            'entry_basis': 'CONFIRMED_MTF_SUPPORT_RECLAIM',
            'economics': econ,
            'efficiency': {'decision': 'TRADE', 'score': 1.5},
        }

    def test_concurrent_two_positions_hold_and_buy(self):
        """Holding Stock A while successfully buying Stock B under max_positions=2."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2)

        # 1. Enter Stock A (001210: 3 shares @ 7,760 KRW = 23,280 KRW)
        self.clock_time = AT
        engine.last_account = time.monotonic()
        engine.market_status["001210"] = {"pass": True, "checked_at": AT.isoformat()}
        engine.source_qualification["001210"] = {"source_count": 3, "timestamp": AT.isoformat()}
        sig_a = self._make_signal("001210", 7760, 7500, 7600, 8400)
        tick_a = Tick("001210", AT, 7760, 100, 10000, 7760, 7750)
        o_a = engine.enter(sig_a, tick_a)
        self.assertIsNotNone(o_a, "Stock A order must succeed")
        engine.cumulative_fill(o_a, o_a['qty'], o_a['qty'] * 7760)

        self.assertIn("001210", engine.s['positions'])
        self.assertEqual(len(engine.s['positions']), 1)
        cash_after_a = engine.s['cash']
        self.assertLess(cash_after_a, 70847.0)

        # 2. Enter Stock B (200350: 12 shares @ 3,310 KRW = 39,720 KRW)
        t_b = AT + timedelta(minutes=1)
        self.clock_time = t_b
        engine.last_account = time.monotonic()
        engine.market_status["200350"] = {"pass": True, "checked_at": t_b.isoformat()}
        engine.source_qualification["200350"] = {"source_count": 3, "timestamp": t_b.isoformat()}
        sig_b = self._make_signal("200350", 3310, 3225, 3260, 3530)
        tick_b = Tick("200350", t_b, 3310, 100, 10000, 3310, 3300)
        o_b = engine.enter(sig_b, tick_b)
        self.assertIsNotNone(o_b, "Stock B order must succeed concurrently")
        engine.cumulative_fill(o_b, o_b['qty'], o_b['qty'] * 3310)

        # Verify both positions exist concurrently in engine ledger
        self.assertEqual(len(engine.s['positions']), 2)
        self.assertIn("001210", engine.s['positions'])
        self.assertIn("200350", engine.s['positions'])
        self.assertEqual(len(broker.orders), 2)

    def test_stock_a_stop_loss_while_stock_b_processes(self):
        """Stock A executes stop loss while Stock B position is safely maintained."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2)

        # Seed 2 positions with full required keys
        engine.s['positions']["001210"] = {
            'symbol': "001210", 'qty': 3, 'avg': 7760, 'stop': 7500, 'target': 8400,
            'ownership': 'BOT', 'initial_stop': 7500, 'initial_r': 260, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 7760, 'low': 7760, 'opened': AT.isoformat(),
            'trade_id': 'T-001210-01', 'pattern': 'A2', 'entry_hour': '10', 'market_state': '3',
            'stop_history': [7500], 'signal': {}
        }
        engine.s['positions']["200350"] = {
            'symbol': "200350", 'qty': 12, 'avg': 3310, 'stop': 3225, 'target': 3530,
            'ownership': 'BOT', 'initial_stop': 3225, 'initial_r': 85, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 3310, 'low': 3310, 'opened': AT.isoformat(),
            'trade_id': 'T-200350-01', 'pattern': 'B', 'entry_hour': '10', 'market_state': '3',
            'stop_history': [3225], 'signal': {}
        }

        # Price drops on Stock A below stop (7500)
        t_drop_a = Tick("001210", AT + timedelta(seconds=10), 7480, 50, 10000, 7490, 7480)
        self.clock_time = t_drop_a.time
        engine.on_tick(t_drop_a)
        engine.prices["001210"] = 7480
        engine.prices["200350"] = 3320

        # Trigger exit drive
        engine.drive_exits(engine.prices)

        # Verify SELL was submitted for 001210 only
        sells = [o for o in broker.orders if o['side'] == 'SELL']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]['symbol'], "001210")

        # Simulate broker sell fill
        sell_order = next(o for o in engine.s['orders'].values() if o['symbol'] == "001210" and o['side'] == 'SELL')
        engine.cumulative_fill(sell_order, 3, 3 * 7480)

        # Stock A is now removed, Stock B remains active
        self.assertNotIn("001210", engine.s['positions'])
        self.assertIn("200350", engine.s['positions'])
        self.assertEqual(engine.s['positions']["200350"]['qty'], 12)
        self.assertGreater(engine.s['daily_loss'], 0.0)

    def test_stock_a_unexecuted_sell_while_stock_b_reconciles(self):
        """Stock A has an unexecuted SELL order; Stock B calculations remain unaffected."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2)

        # Seed Stock A and B
        engine.s['positions']["001210"] = {
            'symbol': "001210", 'qty': 3, 'avg': 7760, 'stop': 7500, 'target': 8400,
            'ownership': 'BOT', 'initial_stop': 7500, 'initial_r': 260, 'exit_requested': True,
            'exit_attempts': 1, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 7760, 'low': 7760, 'opened': AT.isoformat()
        }
        engine.s['positions']["200350"] = {
            'symbol': "200350", 'qty': 12, 'avg': 3310, 'stop': 3225, 'target': 3530,
            'ownership': 'BOT', 'initial_stop': 3225, 'initial_r': 85, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 3310, 'low': 3310, 'opened': AT.isoformat()
        }

        # Active SELL order for 001210
        engine.s['orders']['sell_local_a'] = {
            'local': 'sell_local_a', 'id': '2001', 'symbol': '001210', 'side': 'SELL',
            'qty': 3, 'filled': 0, 'amount': 0.0, 'limit': 7500, 'status': 'ACCEPTED'
        }

        # Verify active orders per symbol
        self.assertEqual(len(engine.active_orders('001210')), 1)
        self.assertEqual(len(engine.active_orders('200350')), 0)

        # Reconcile snapshot with proper broker fields
        snapshot = {
            'cash': 25000.0, 'equity': 65000.0,
            'holdings': {'001210': {'qty': 3}, '200350': {'qty': 12}},
            'orders': [{'ord_no': '2001', 'stk_cd': '001210', 'side': 'SELL', 'qty': 3, 'oso_qty': 3, 'cntr_tot_amt': 0}]
        }
        engine.reconcile(snapshot)
        self.assertEqual(engine.s['positions']['200350']['qty'], 12)
        self.assertEqual(engine.s['positions']['001210']['qty'], 3)

    def test_block_duplicate_buy_on_same_symbol(self):
        """Holding Stock A strictly blocks second buy on Stock A."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2)

        engine.s['positions']["001210"] = {
            'symbol': "001210", 'qty': 3, 'avg': 7760, 'stop': 7500, 'target': 8400,
            'ownership': 'BOT', 'initial_stop': 7500, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 7760, 'low': 7760, 'opened': AT.isoformat()
        }

        engine.market_status["001210"] = {"pass": True, "checked_at": AT.isoformat()}
        sig_a2 = self._make_signal("001210", 7760, 7500, 7600, 8400)
        tick_a2 = Tick("001210", AT + timedelta(minutes=2), 7760, 100, 10000, 7760, 7750)

        # Should be rejected at repeat_slots gate
        o = engine.enter(sig_a2, tick_a2)
        self.assertIsNone(o, "Duplicate buy on already held symbol must return None")
        self.assertEqual(len(broker.orders), 0)

    def test_cumulative_daily_loss_blocks_new_buys(self):
        """When daily loss reaches 4% daily budget, new buys are blocked on risk budget."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2, cash=70847.0)

        engine.s['daily_loss'] = 2850.0

        engine.market_status["200350"] = {"pass": True, "checked_at": AT.isoformat()}
        sig = self._make_signal("200350", 3310, 3225, 3260, 3530)
        tick = Tick("200350", AT, 3310, 100, 10000, 3310, 3300)

        qty, gates = engine.entry_gates(sig, tick)
        self.assertEqual(gates['risk_quantity']['result'], 'FAIL', "Exceeded daily loss must fail risk_quantity gate")
        o = engine.enter(sig, tick)
        self.assertIsNone(o, "Order must be blocked when daily risk budget exhausted")

    def test_multi_position_eod_force_flatten(self):
        """At 15:10:00, all positions across symbols are liquidated and pending buys cancelled."""
        engine, broker, cfg, auto = self._create_engine(max_positions=2)

        engine.s['positions']["001210"] = {
            'symbol': "001210", 'qty': 3, 'avg': 7760, 'stop': 7500, 'target': 8400,
            'ownership': 'BOT', 'initial_stop': 7500, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 7760, 'low': 7760, 'opened': AT.isoformat()
        }
        engine.s['positions']["200350"] = {
            'symbol': "200350", 'qty': 12, 'avg': 3310, 'stop': 3225, 'target': 3530,
            'ownership': 'BOT', 'initial_stop': 3225, 'exit_requested': False,
            'exit_attempts': 0, 'qty_blocked': False, 'last_tick': AT.isoformat(),
            'high': 3310, 'low': 3310, 'opened': AT.isoformat()
        }
        engine.s['orders']['buy_pending'] = {
            'local': 'buy_pending', 'id': '9999', 'symbol': '060310', 'side': 'BUY',
            'qty': 10, 'filled': 0, 'amount': 0.0, 'limit': 1320, 'status': 'ACCEPTED',
            'created': AT.isoformat(), 'time': AT.isoformat()
        }

        self.clock_time = datetime(2026, 11, 2, 15, 10, 5, tzinfo=KST)
        engine.prices = {"001210": 7600, "200350": 3365, "060310": 1330}
        engine.timer(engine.prices)

        self.assertIn(engine.s['orders']['buy_pending']['status'], ('CANCEL_SENT', 'CANCELED'))
        self.assertTrue(engine.s['positions']["001210"]['exit_requested'])
        self.assertTrue(engine.s['positions']["200350"]['exit_requested'])

    def test_cash_and_risk_constrain_before_max_positions(self):
        """Even with max_positions=5, cash and risk budgets reject 3rd buy when exhausted."""
        engine, broker, cfg, auto = self._create_engine(max_positions=5, cash=70847.0)

        engine.s['positions']["001210"] = {
            'symbol': "001210", 'qty': 3, 'avg': 7760, 'stop': 7500, 'target': 8400,
            'ownership': 'BOT', 'initial_stop': 7500, 'high': 7760, 'low': 7760, 'opened': AT.isoformat()
        }
        engine.s['positions']["200350"] = {
            'symbol': "200350", 'qty': 12, 'avg': 3310, 'stop': 3225, 'target': 3530,
            'ownership': 'BOT', 'initial_stop': 3225, 'high': 3310, 'low': 3310, 'opened': AT.isoformat()
        }
        engine.s['cash'] = 7847.0
        engine.available_cash = 7847.0

        engine.market_status["035420"] = {"pass": True, "checked_at": AT.isoformat()}
        sig_c = self._make_signal("035420", 15000, 14500, 14700, 16500)
        tick_c = Tick("035420", AT, 15000, 10, 10000, 15000, 14950)

        qty, gates = engine.entry_gates(sig_c, tick_c)
        self.assertEqual(gates['risk_quantity']['result'], 'FAIL')
        self.assertEqual(gates['risk_quantity']['values']['reason_code'], 'ONE_SHARE_UNAFFORDABLE')

        o_c = engine.enter(sig_c, tick_c)
        self.assertIsNone(o_c, "Third buy must be blocked by cash constraint before max_positions=5")

    def test_multi_timeframe_strategy_generates_valid_signal(self):
        """MTF Strategy detects 3m impulse-pullback structure and 1m reclaim trigger."""
        cfg = Config(live_mode=True, risk_fraction=0.02, max_positions=2)
        auto = AutomationConfig(mtf_entry_enabled=True, AUTO_TRADING_CAP_KRW=81300)
        strat = MultiTimeframeStrategy(cfg, auto, self.costs)

        start_time = datetime(2026, 11, 2, 9, 30, 0, tzinfo=KST)
        raw_bars = [
            # 9:30 - 9:32 (Bucket 0: Base)
            (10000, 10100, 9980, 10080, 1000),
            (10080, 10200, 10050, 10180, 1200),
            (10180, 10300, 10150, 10280, 1500),
            # 9:33 - 9:35 (Bucket 1: Impulse to 11,000)
            (10280, 10500, 10250, 10480, 2000),
            (10480, 10800, 10450, 10780, 2500),
            (10780, 11000, 10750, 10980, 3000),
            # 9:36 - 9:38 (Bucket 2: Pullback to 10,480: 52% retrace of 1000 rise)
            (10980, 10980, 10700, 10720, 1200),
            (10720, 10720, 10550, 10580, 900),
            (10580, 10600, 10480, 10500, 800),
            # 9:39 - 9:41 (Bucket 3: Support hold at 10,480)
            (10500, 10520, 10480, 10490, 600),
            (10490, 10520, 10480, 10500, 700),
            (10500, 10530, 10490, 10510, 900),
            # 9:42 (1m reclaim bar)
            (10500, 10530, 10490, 10510, 1200),
        ]
        bars_1m = [
            {'time': start_time + timedelta(minutes=i),
             'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}
            for i, (o, h, l, c, v) in enumerate(raw_bars)
        ]
        now = start_time + timedelta(minutes=len(raw_bars) - 1)
        tick = Tick("047770", now, 10510, 100, 10000, 10510, 10500)
        watch = {'code': '047770', 'resistance': 11000, 'status': 'WATCH'}

        sig = strat.evaluate(bars_1m, tick, watch, capital_limit=70847.0, risk_budget=1416.94)
        self.assertIsNotNone(sig, "MTF strategy must generate signal on confirmed 3m structure + 1m reclaim")
        self.assertEqual(sig['pattern'], 'MTF')
        self.assertGreaterEqual(sig['net_reward_risk'], 1.50)
        self.assertGreater(sig['planned_qty'], 0)
        self.assertEqual(sig['limit'], 10510)
        self.assertLess(sig['stop'], sig['limit'])


if __name__ == '__main__':
    unittest.main()
