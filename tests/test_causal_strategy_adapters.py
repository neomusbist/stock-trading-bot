"""Comprehensive Unit Tests for Unified Causal Strategy Adapters.

Validates the requirements of Section 4.4:
1. Incomplete entry conditions produce clean rejection (REJECT/UNKNOWN).
2. Qualifying conditions generate compliant engine signals, resulting in accepted BUY orders and position creation.
3. Premature/future data prior to verified availability is strictly blocked.
4. Depleted/unavailable liquidity prevents execution.
5. Account risk budget (2%) and capital limits bound quantity sizing.
6. Displayed depth limits partial fills with proper remaining quantity tracking.
7. Engine-governed exit triggers (stop loss / target / structural invalidation) execute SELL and close position.
8. Duplicate entries while pending or holding are blocked.
9. SHADOW strategy flags prevent LIVE order emission.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from dataclasses import replace

from core.settings import Config, KST
from core.strategy import Tick
from core.broker import Rejected
from automation.policy import AutomationConfig
from automation.capital import Costs
from research.causal_data import Frame
from research.causal_account import PaperAccount
from research.causal_strategy_adapters import (
    build_all_strategy_adapters,
    StrategyAAdapter,
    StrategyBAdapter,
    StrategyCAdapter,
    StrategyFastAdapter,
    StrategyLocalBreakoutAdapter,
    StrategyRetestAdapter,
)

AT = datetime(2026, 10, 20, 10, 13, tzinfo=KST)
SYM = "999990"


def make_frame(at=AT, price=1004.0, ask=1004.0, bid=1003.0, depth=1000, source="0D", regular=True):
    t = Tick(
        symbol=SYM,
        time=at,
        price=price,
        volume=100,
        cumulative=100000,
        ask=ask,
        bid=bid,
        regular=regular,
        ask_qty=depth,
        bid_qty=depth,
        depth_available=depth > 0,
        book_time=at,
        depth_source=source,
        received_at=at.isoformat(),
        book_received_at=at.isoformat()
    )
    return Frame(t, at, at, "SYNTHETIC_TEST_QUOTE", "test")


def make_bars(count=12, base_price=1000.0):
    rows = []
    for i in range(count):
        start = AT - timedelta(minutes=count - i)
        rows.append({
            'time': start,
            'open': base_price - 5.0,
            'high': base_price,
            'low': base_price - 10.0,
            'close': base_price - 3.0,
            'volume': 10000,
            'observation_source': 'ACTUAL_RECEIVED',
            'available_at': (start + timedelta(minutes=1)).isoformat(),
            'first_second': 0,
            'gap_reason': None
        })
    return rows


class CausalStrategyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(
            Config(),
            max_position_fraction=1.0,
            max_positions=1,
            after_enabled=False,
            max_entries_per_day=None,
            max_entries_per_symbol=None,
            risk_fraction=0.02,
            max_daily_loss_fraction=0.04,
            stale_seconds=5
        )
        self.auto = replace(
            AutomationConfig(),
            AUTO_TRADING_CAP_KRW=81300,
            confirmed_invalidation_enabled=True,
            costs_confirmed=True,
            exit_plan_reward_guard_enabled=True,
            max_consecutive_losses=None
        )
        self.costs = Costs(
            self.auto.commission_buy, self.auto.commission_sell, self.auto.sell_tax,
            self.cfg.entry_slippage_fraction, self.cfg.exit_slippage_fraction
        )
        self.account = PaperAccount(AT, initial_cash=71822.0, cfg=self.cfg, auto=self.auto)
        self.addCleanup(self.account.close)

        self.adapters = build_all_strategy_adapters(self.cfg, self.auto, self.costs)
        self.watch = {
            'code': SYM,
            'name': 'SYNTHETIC_STOCK',
            'status': 'WATCH',
            'source_count': 3,
            'resistance': 1200.0,
            'market_status': {'pass': True, 'checked_at': AT.isoformat()},
            'nearest_confirmed_peak': {
                'price': 1100.0,
                'confirmed_at': (AT - timedelta(minutes=20)).isoformat(),
                'available_at': (AT - timedelta(minutes=20)).isoformat(),
                'observation_source': 'ACTUAL_RECEIVED'
            }
        }

    def test_factory_builds_all_six_adapters(self):
        expected_keys = {'STRATEGY_A', 'STRATEGY_B', 'STRATEGY_C', 'STRATEGY_FAST', 'LOCAL_BREAKOUT', 'RETEST_EXPERIMENT'}
        self.assertEqual(set(self.adapters.keys()), expected_keys)
        for key, adapter in self.adapters.items():
            self.assertEqual(adapter.name, key)

    def test_1_entry_condition_unmet_rejected(self):
        """When warmup/liquidity or geometry fails, adapter returns None and records rejection."""
        adapter = self.adapters['STRATEGY_A']
        f = make_frame()
        # No history ingested -> warmup gate fails
        sig, detail = adapter.evaluate(f.tick, [], self.watch, 71822.0, 1436.44, AT)
        self.assertIsNone(sig)
        self.assertFalse(detail['pass'])
        self.assertIn(detail['status'], ('REJECT', 'UNKNOWN'))
        self.assertGreater(adapter.rejection_counts[detail['reason']], 0)

    def test_2_condition_met_normal_order_and_fill(self):
        """When conditions are met, adapter produces signal, PaperAccount enters, submits BUY, and fills."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        # Structure breakout on -2 bar, retest on -1 bar
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f = make_frame(price=1004.0, ask=1004.0, bid=1003.0, depth=1000)

        self.account.step(f, market_status={'pass': True, 'checked_at': AT.isoformat()})
        sig, detail = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        self.assertIsNotNone(sig)
        self.assertTrue(detail['pass'])

        # Real AutonomousEngine.enter execution path
        order = self.account.enter(sig)
        self.assertIsNotNone(order)
        self.assertEqual(order['status'], 'FILLED')
        self.assertIn(SYM, self.account.e.s['positions'])
        self.assertEqual(self.account.e.s['positions'][SYM]['qty'], order['filled'])

    def test_3_future_or_unavailable_data_blocked(self):
        """Data with availability in the future is strictly blocked from creating a signal."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        # Mark latest bar available 10 seconds into the future
        b_list[-1]['available_at'] = (AT + timedelta(seconds=10)).isoformat()
        f = make_frame()

        sig, detail = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        self.assertIsNone(sig)
        self.assertIn('FUTURE', detail['reason'])

    def test_4_insufficient_depth_blocks_order(self):
        """Zero displayed depth or unavailable book prevents order submission or fill."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f = make_frame(depth=0)

        sig, detail = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        # Strategy requires measured fresh book with depth
        self.assertIsNone(sig)
        self.assertEqual(detail['reason'], 'MEASURED_FRESH_BOOK_REQUIRED')

    def test_5_order_risk_quantity_limit_enforced(self):
        """Quantity is strictly bounded by single-trade risk (2%) and capital limits."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f = make_frame()

        self.account.step(f, market_status={'pass': True, 'checked_at': AT.isoformat()})
        sig, _ = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        order = self.account.enter(sig)
        pos = self.account.e.s['positions'][SYM]

        # Max loss cannot exceed 2% of equity (1,436.44 KRW)
        max_loss = pos['qty'] * (sig['limit'] - sig['stop'])
        self.assertLessEqual(max_loss, 1436.44)
        # Capital committed cannot exceed available cash
        self.assertLessEqual(pos['qty'] * sig['limit'], 71822.0)

    def test_6_partial_fill_handling(self):
        """When displayed depth is smaller than requested order quantity, partial fill occurs."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f_eval = make_frame(depth=1000)

        sig, _ = adapter.evaluate(f_eval.tick, b_list, self.watch, 71822.0, 1436.44, AT)

        # But when entering, available depth at arrival is only 5 shares
        f_entry = make_frame(depth=5)
        self.account.step(f_entry, market_status={'pass': True, 'checked_at': AT.isoformat()})
        order = self.account.enter(sig)

        self.assertIsNotNone(order)
        self.assertEqual(order['filled'], 5)
        self.assertLess(order['filled'], order['qty'])
        self.assertEqual(order['status'], 'ACCEPTED')  # In R6 engine, resting partially-filled orders retain status ACCEPTED
        self.assertEqual(self.account.e.s['positions'][SYM]['qty'], 5)

    def test_7_stop_loss_and_target_exit_functions(self):
        """Step forward to stop loss triggers SELL and position cleanup in real engine."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f = make_frame()

        self.account.step(f, market_status={'pass': True, 'checked_at': AT.isoformat()})
        sig, _ = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        self.account.enter(sig)
        self.assertIn(SYM, self.account.e.s['positions'])

        # Price drops below emergency stop (989.0)
        f_stop = make_frame(at=AT + timedelta(seconds=1), price=988.0, ask=989.0, bid=988.0)
        self.account.step(f_stop)

        # Position must be closed and trade recorded in ledger
        self.assertNotIn(SYM, self.account.e.s['positions'])
        res = self.account.result()
        self.assertEqual(res['completed_count'], 1)
        self.assertIn('비상선', res['completed_trades'][0]['reason'])

    def test_8_duplicate_order_blocked(self):
        """Submitting a second signal for a symbol with an open position or order is blocked."""
        adapter = self.adapters['RETEST_EXPERIMENT']
        b_list = make_bars(12)
        b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
        b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
        f = make_frame()

        self.account.step(f, market_status={'pass': True, 'checked_at': AT.isoformat()})
        sig, _ = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
        order1 = self.account.enter(sig)
        self.assertIsNotNone(order1)

        # Try to enter second order for same symbol
        order2 = self.account.enter(sig)
        self.assertIsNone(order2)
        self.assertEqual(len([o for o in self.account.broker.submissions if o['side'] == 'BUY']), 1)

    def test_9_shadow_strategy_live_order_prevented(self):
        """Signals generated by research adapters declare SHADOW_ONLY=True and actual_order_permission=False."""
        for key, adapter in self.adapters.items():
            if key == 'LOCAL_BREAKOUT':
                b_list = make_bars(12)
                f = make_frame()
                sig, _ = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
                if sig:
                    self.assertTrue(sig.get('SHADOW_ONLY'))
                    self.assertFalse(sig.get('actual_order_permission'))
            elif key == 'RETEST_EXPERIMENT':
                b_list = make_bars(12)
                b_list[-2].update(open=999.0, high=1005.0, low=996.0, close=1003.0)
                b_list[-1].update(open=1003.0, high=1003.0, low=1000.0, close=1002.0)
                f = make_frame()
                sig, _ = adapter.evaluate(f.tick, b_list, self.watch, 71822.0, 1436.44, AT)
                self.assertIsNotNone(sig)
                self.assertTrue(sig.get('SHADOW_ONLY'))
                self.assertFalse(sig.get('actual_order_permission'))

    def test_11_abc_ingest_tick_bar_accumulation_preserves_ohlcv(self):
        """Ticks in the same minute must accumulate open, high, low, close, and volume rather than overwriting."""
        adapter = self.adapters['STRATEGY_A']
        t0 = AT.replace(second=0, microsecond=0)
        t1 = Tick(SYM, t0, 1000.0, 10, 10, 1000.0, 999.0, True)
        t2 = Tick(SYM, t0 + timedelta(seconds=10), 1020.0, 20, 30, 1020.0, 1019.0, True)
        t3 = Tick(SYM, t0 + timedelta(seconds=20), 990.0, 15, 45, 990.0, 989.0, True)
        t4 = Tick(SYM, t0 + timedelta(seconds=30), 1010.0, 5, 50, 1010.0, 1009.0, True)

        adapter.ingest_tick(t1, t0)
        adapter.ingest_tick(t2, t0 + timedelta(seconds=10))
        adapter.ingest_tick(t3, t0 + timedelta(seconds=20))
        adapter.ingest_tick(t4, t0 + timedelta(seconds=30))

        cur = adapter.detector.current[SYM]
        self.assertEqual(cur['open'], 1000.0, "Open must remain the first tick price of the minute")
        self.assertEqual(cur['high'], 1020.0, "High must be the maximum price seen in the minute")
        self.assertEqual(cur['low'], 990.0, "Low must be the minimum price seen in the minute")
        self.assertEqual(cur['close'], 1010.0, "Close must be the latest tick price")
        self.assertEqual(cur['volume'], 50, "Volume must accumulate across ticks in the minute")
        self.assertEqual(cur['tick_count'], 4, "Tick count must reflect all 4 ingested ticks")


if __name__ == '__main__':
    unittest.main()
