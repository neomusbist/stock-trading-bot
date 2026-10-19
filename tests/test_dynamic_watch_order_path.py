"""
End-to-End Integration Tests for Dynamic WATCH Promotion and Live Order Path
Validates the complete execution pipeline:
1. Subscribed non-WATCH candidate -> Dynamic WATCH promotion -> Live strategy signal -> PRE_INTENT -> Mock BUY order -> Accepted -> Filled -> Exit.
2. Redesign A/B setups remain strictly SHADOW-only (emits SHADOW event, zero promotion, zero broker orders).
3. Safety gates enforce strict blocking: HARD_LOCK, daily risk limit, and unverified structure.
"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.settings import Config, KST
from core.strategy import Tick
from core.util import atomic_json
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import DynamicRadar
from automation.runner import (
    evaluate_shadow_opportunity,
    evaluate_actual_entry,
    process_message,
    select_focus,
    apply_focus_selection,
    focus_readiness
)
from tests.test_automation import FakeBroker


class DynamicWatchOrderPathIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        object.__setattr__(self.cfg, 'enable_redesign_trials', True)
        object.__setattr__(self.cfg, 'enable_redesign_live', False)  # Hard quarantine for live
        object.__setattr__(self.cfg, 'live_mode', True)
        object.__setattr__(self.cfg, 'max_watchlist', 4)
        object.__setattr__(self.cfg, 'stale_seconds', 10.0)
        
        self.auto = AutomationConfig(AUTO_TRADING_CAP_KRW=10_000_000)
        object.__setattr__(self.auto, 'fast_momentum_enabled', True)
        object.__setattr__(self.auto, 'enable_ask_1tick_tolerance', True)
        object.__setattr__(self.auto, 'costs_confirmed', True)
        object.__setattr__(self.auto, 'min_source_count', 1)
        object.__setattr__(self.auto, 'fallback_source1', True)
        
        self.costs = Costs(
            self.auto.commission_buy, self.auto.commission_sell, self.auto.sell_tax,
            self.cfg.entry_slippage_fraction, self.cfg.exit_slippage_fraction
        )
        
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp_dir.name)
        self.time = datetime(2026, 10, 19, 10, 30, 0, tzinfo=KST)
        
        atomic_json(self.folder / 'policy.json', {
            'mode': 'LIVE', 'live_authorized': True, 'releases': {}, 'revision': 1, 'commands': []
        })
        self.policy = Policy(self.folder / 'policy.json')
        self.broker = FakeBroker()
        self.engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder, self.broker, lambda: self.time)
        
        self.engine.connected = True
        self.engine.account_ok = True
        self.engine.last_account = time.monotonic()
        self.engine.s['cash'] = 10_000_000
        self.engine.available_cash = 10_000_000
        self.engine.s['daily_loss'] = 0
        self.engine.market = '3'
        self.engine.is_supported_now = MagicMock(return_value=True)
        self.engine.entry_window = MagicMock(return_value=True)
        
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)
        self.radar = DynamicRadar(self.cfg, self.auto, self.policy, fallback_source1=True)
        self.radar.cache = {}
        self.cum = {}

    def get_events(self):
        events_path = self.folder / 'events.jsonl'
        if not events_path.exists():
            return []
        import json
        evs = []
        with open(events_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    evs.append(json.loads(line))
        return evs

    def tearDown(self):
        self.temp_dir.cleanup()

    def _generate_bars(self, base_price=2000, n_bars=70):
        bars = []
        for i in range(n_bars):
            t_bar = self.time - timedelta(days=n_bars - i)
            bars.append({
                'datetime': t_bar.date().isoformat(),
                'time': t_bar,
                'open': base_price,
                'high': base_price + 100,
                'low': base_price - 100,
                'close': base_price + 50,
                'volume': 2000000
            })
        return bars

    def _make_frame(self, sym, price, vol=5000, ask=None, bid=None):
        self.cum[sym] = self.cum.get(sym, 0) + vol
        t = self.time
        values = {
            '20': t.strftime('%H%M%S'),
            '10': str(price),
            '15': str(vol),
            '13': str(self.cum[sym]),
            '27': str(price if ask is None else ask),
            '28': str(price - 5 if bid is None else bid),
            '290': '2',
            '61': '10000',
            '71': '10000'
        }
        return {'trnm': 'REAL', 'data': [{'type': '0B', 'item': sym, 'values': values}]}

    def test_e2e_non_watch_live_strategy_promotes_and_submits_buy_order(self):
        """End-to-End Test: Non-WATCH candidate with valid LIVE Setup A promotes dynamically to WATCH and submits BUY order."""
        sym = '018670'  # Synthetic signal fixture; no real-stock identity claim.
        bars = self._generate_bars(base_price=2400, n_bars=70)
        self.radar.cache[(self.time.date(), sym)] = bars
        
        self.detector.completed[sym] = list(bars)
        
        lookup = {}  # Non-WATCH
        by_symbol = {
            sym: {
                'code': sym, 'name': 'Synthetic order-path fixture', 'reference_price': 2400,
                'resistance': 2600, 'overhead_share': 0.02, 'source_count': 2,
                'market_status': {'pass': True, 'reason_code': 'PASS'},
                'fallback_source1': True, 'status': 'SHADOW'
            }
        }
        
        mock_signal = {
            'symbol': sym,
            'time': self.time.isoformat(),
            'pattern': 'A',
            'limit': 2450,
            'stop': 2380,
            'emergency': 2350,
            'target': 2580,
            'net_reward_risk': 1.65,
            'source_count': 2,
            'actual_order_permission': True,
            'SHADOW_ONLY': False,
            'efficiency': {'decision': 'TRADE', 'score': 1.0, 'inputs': {'confidence': 1.0, 'planned_minutes': 15}},
            'invalidation_policy': {'enabled': True, 'structural_stop': 2380, 'emergency_stop': 2350, 'noise_band': 20}
        }
        self.detector.signal = MagicMock(return_value=mock_signal)
        self.detector.last_eval[sym] = {'trials': [{'setup': 'A', 'entry_ready': True}], 'gates': {}}
        
        # A positive marketable BUY uses the exact persisted limit. The separate
        # final-boundary regression rejects ask=2455 with an unsent 2450 limit.
        message = self._make_frame(sym, 2450, vol=5000, ask=2450, bid=2445)
        prices = {sym: 2450}
        account = MagicMock(need_fills=False)
        
        with patch('automation.runner.now', return_value=self.time):
            accepted = process_message(
                self.engine, self.detector, message,
                received=self.time,
                symbols={sym},
                lookup=lookup,
                radar=self.radar,
                prices=prices,
                account=account,
                radar_ok=True,
                subscriptions=None,
                by_symbol=by_symbol
            )
        
        # Verification:
        # 1. Accepted tick
        self.assertEqual(accepted, 1)
        # 2. Promoted to WATCH
        self.assertIn(sym, lookup, "Symbol should be dynamically promoted to lookup/WATCH")
        self.assertEqual(lookup[sym]['status'], 'WATCH')
        # 3. Dynamic promotion event recorded
        promo_events = [e for e in self.get_events() if e.get('event') == 'WATCH_DYNAMIC_PROMOTION']
        self.assertEqual(len(promo_events), 1)
        self.assertEqual(promo_events[0]['symbol'], sym)
        self.assertEqual(promo_events[0]['pattern'], 'A')
        self.assertIn('latency_ms', promo_events[0])
        self.assertLess(promo_events[0]['latency_ms'], 100.0, "Dynamic promotion must complete in < 100ms")
        
        # 4. Mock BUY order submitted to FakeBroker
        self.assertEqual(len(self.broker.orders), 1, "Mock BUY order should be submitted to broker")
        order_tuple = self.broker.orders[0]
        self.assertEqual(order_tuple[0], 'BUY')
        self.assertEqual(order_tuple[1], sym)
        self.assertGreater(order_tuple[2], 0)
        self.assertEqual(order_tuple[3], 2450)
        
        # 5. Simulate Fill
        order_dict = next(o for o in self.engine.s['orders'].values() if o['side'] == 'BUY')
        fill_qty = order_dict['qty']
        fill_px = 2450
        self.engine.actual = {sym: {'qty': fill_qty, 'available': fill_qty, 'avg': fill_px}}
        self.engine.cumulative_fill(order_dict, fill_qty, fill_qty * fill_px)
        
        self.assertIn(sym, self.engine.s['positions'], "Position must exist after fill")
        pos = self.engine.s['positions'][sym]
        self.assertEqual(pos['qty'], fill_qty)
        self.assertIn('exit_plan', pos['signal'])
        
        # 6. Simulate Exit on Target Hit
        self.engine.request_exit(sym, 'TARGET_HIT')
        self.engine.drive_exits({sym: 2580})
        sell_orders = [o for o in self.broker.orders if o[0] == 'SELL']
        self.assertEqual(len(sell_orders), 1, "Exit SELL order should be submitted")
        self.assertEqual(sell_orders[0][1], sym)
        self.assertEqual(sell_orders[0][2], fill_qty)

    def test_redesign_strategy_quarantined_to_shadow_zero_broker_orders(self):
        """Negative Test: Redesign A/B setup generates SHADOW event but is strictly blocked from WATCH promotion and broker orders."""
        sym = '025950'  # 동신건설
        lookup = {}
        by_symbol = {
            sym: {
                'code': sym, 'name': '동신건설', 'reference_price': 15000,
                'resistance': 16000, 'overhead_share': 0.03, 'source_count': 2,
                'market_status': {'pass': True, 'reason_code': 'PASS'},
                'fallback_source1': True, 'status': 'SHADOW'
            }
        }
        
        self.detector.signal = MagicMock(return_value=None)
        self.detector.last_eval[sym] = {
            'trials': [{
                'setup': 'EarlyMomentumBreakout',
                'entry_ready': True,
                'price': 15200,
                'stop': 14800,
                'target': 15800,
                'net_reward_risk': 1.45
            }],
            'gates': {}
        }
        
        message = self._make_frame(sym, 15200, vol=3000, ask=15250, bid=15200)
        prices = {sym: 15200}
        account = MagicMock(need_fills=False)
        
        with patch('automation.runner.now', return_value=self.time):
            accepted = process_message(
                self.engine, self.detector, message,
                received=self.time,
                symbols={sym},
                lookup=lookup,
                radar=self.radar,
                prices=prices,
                account=account,
                radar_ok=True,
                subscriptions=None,
                by_symbol=by_symbol
            )
        
        # Verification:
        # 1. Tick processed
        self.assertEqual(accepted, 1)
        # 2. NOT promoted to WATCH
        self.assertNotIn(sym, lookup, "Redesign setup MUST NOT promote symbol to WATCH")
        # 3. Emitted SHADOW_OPPORTUNITY_DETECTED event
        shadow_events = [e for e in self.get_events() if e.get('event') == 'SHADOW_OPPORTUNITY_DETECTED']
        self.assertEqual(len(shadow_events), 1)
        self.assertEqual(shadow_events[0]['symbol'], sym)
        self.assertEqual(shadow_events[0]['pattern'], 'EarlyMomentumBreakout')
        self.assertFalse(shadow_events[0]['actual_order_permission'])
        self.assertTrue(shadow_events[0]['shadow_only'])
        # 4. ZERO broker orders submitted
        self.assertEqual(len(self.broker.orders), 0, "Zero broker orders must be submitted for redesign setups")

    def test_hard_lock_and_daily_risk_budget_blocks_order(self):
        """Negative Test: HARD_LOCK or exhausted daily risk budget blocks order submission."""
        sym = '018670'
        self.engine.s['daily_loss'] = 1_000_000
        self.engine.daily_risk_budget = MagicMock(return_value=400_000)
        
        signal = {
            'symbol': sym, 'pattern': 'A', 'limit': 2500, 'stop': 2400, 'emergency': 2350,
            'target': 2700, 'net_reward_risk': 1.6, 'actual_order_permission': True,
            'efficiency': {'decision': 'TRADE', 'score': 1.0, 'inputs': {'confidence': 1.0, 'planned_minutes': 15}}
        }
        tick = Tick(sym, self.time, 2500, 1000, 10000, 2505, 2500, True)
        
        res = self.engine.enter(signal, tick)
        self.assertIsNone(res, "Order must be blocked when daily risk budget is exhausted")
        self.assertEqual(len(self.broker.orders), 0)
        
        self.engine.s['daily_loss'] = 0
        from automation.policy import HARD_LOCKS
        HARD_LOCKS.add(sym)
        try:
            res_locked = self.engine.enter(signal, tick)
            self.assertIsNone(res_locked, "Order must be blocked for HARD_LOCK symbol")
            self.assertEqual(len(self.broker.orders), 0)
        finally:
            HARD_LOCKS.discard(sym)


if __name__ == '__main__':
    unittest.main()
