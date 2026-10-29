import unittest
from datetime import datetime, date, timedelta
from dataclasses import replace
import tempfile
from pathlib import Path
from unittest.mock import patch

from core.settings import Config, KST
from core.strategy import Tick
from core.util import floor_tick
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.engine import AutonomousEngine
from automation.runner import evaluate_actual_entry

class FakeBroker:
    identity = 'fake-test-broker'
    def __init__(self):
        self.orders = []
        self.cancels = []
    def submit(self, side, sym, qty, limit):
        oid = f"ORD_{len(self.orders)+1}"
        self.orders.append((side, sym, qty, limit))
        return oid
    def cancel(self, sym, oid):
        self.cancels.append(oid)
        return 'C' + oid

class FakeRadar:
    def __init__(self):
        self.cache = {}
        self.root = None

class FakeAccount:
    def __init__(self, cash=1000000.0):
        self.cash = cash
        self.equity = cash
        self.need_fills = False

class SetupEPaperTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.auto = AutomationConfig()
        self.costs = Costs(0.00015, 0.00015, 0.002, 0.002, 0.003)
        self.detector = PatternDetector(self.cfg, self.auto, self.costs)

    def test_setup_e_disabled_by_default(self):
        """When support_rebound_reclaim_enabled is False, Setup E is never evaluated (LIVE default)."""
        self.assertFalse(self.auto.support_rebound_reclaim_enabled)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
        watch = {'code': '089890', 'resistance': 45000.0, 'status': 'WATCH'}
        trials = self.detector.setup_trials(tick, watch)
        setups_evaluated = [tr['setup'] for tr in trials]
        self.assertEqual(setups_evaluated, ['A', 'B', 'C'])
        self.assertNotIn('E', setups_evaluated)

    def test_setup_e_geometry_and_net_rr_pass(self):
        """When enabled and clean impulse-pullback-rebound occurs with room to resistance, Net RR >= 1.50 passes."""
        auto_enabled = replace(self.auto, support_rebound_reclaim_enabled=True)
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        
        # 5 completed 1m bars:
        # Bar 1: 37,000 -> 37,300 (impulse start)
        # Bar 2: 37,300 -> 38,500 (impulse peak, +4.05% from base 37,000)
        # Bar 3: 38,400 -> 37,700 (pullback 1, low 37,600)
        # Bar 4: 37,700 -> 37,750 (pullback 2, support low 37,300 touched earlier or low 37,650)
        # Bar 5: 37,750 -> 38,050 (rebound close)
        t_base = t_now - timedelta(minutes=6)
        bars = [
            {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
            {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
            {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
            {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
            {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 38100.0, 'low': 37700.0, 'close': 38050.0, 'volume': 25000},
        ]
        
        # Test support_rebound method directly
        tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
        watch = {'code': '089890', 'resistance': 45000.0, 'status': 'WATCH'}
        
        setup_e = detector.support_rebound(bars, tick, watch)
        self.assertIsNotNone(setup_e)
        self.assertEqual(setup_e['pattern'], 'E')
        self.assertTrue(setup_e['paper_only'])
        self.assertEqual(setup_e['peak'], 38500.0)
        self.assertEqual(setup_e['base_low'], 37000.0)
        self.assertAlmostEqual(setup_e['retrace'], (38500.0 - 37600.0)/(38500.0 - 37000.0), places=3)
        self.assertGreaterEqual(setup_e['projection'], 38100.0 * 1.04)
        self.assertEqual(setup_e['projection'], watch['resistance'])
        self.assertLessEqual(setup_e['invalidation_policy']['emergency_stop'], setup_e['invalidation_policy']['structural_stop'])
        self.assertIsNone(detector.support_rebound(bars, tick, {'status': 'WATCH'}))
        self.assertIsNone(detector.support_rebound(bars, tick, {**watch, 'resistance': 38100.0}))

        # Gates evaluation
        gates = detector.setup_gates(tick, watch, setup_e, bars)
        self.assertTrue(gates['risk_reward']['result'] == 'PASS')
        self.assertGreaterEqual(gates['risk_reward']['values']['net_rr'], 1.50)

    def test_setup_e_falling_knife_rejected(self):
        """When price is still collapsing (retrace > 75%), Setup E rejects entry."""
        auto_enabled = replace(self.auto, support_rebound_reclaim_enabled=True)
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        
        # Retrace over 80%
        t_base = t_now - timedelta(minutes=6)
        bars = [
            {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
            {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
            {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37250.0, 'close': 37300.0, 'volume': 40000},
            {'time': t_base + timedelta(minutes=4), 'open': 37300.0, 'high': 37350.0, 'low': 37100.0, 'close': 37150.0, 'volume': 30000},
        ]
        tick = Tick('089890', t_now, 37150.0, 50000, 15000000, 37150.0, 37100.0)
        watch = {'code': '089890', 'resistance': 45000.0, 'status': 'WATCH'}
        setup_e = detector.support_rebound(bars, tick, watch)
        self.assertIsNone(setup_e)

    def test_setup_e_tight_resistance_compresses_rr_and_fails(self):
        """When confirmed overhead resistance is immediately above entry, Net RR < 1.50 fails."""
        auto_enabled = replace(self.auto, support_rebound_reclaim_enabled=True)
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        
        t_base = t_now - timedelta(minutes=6)
        bars = [
            {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
            {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
            {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
            {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
            {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 38100.0, 'low': 37700.0, 'close': 38050.0, 'volume': 25000},
        ]
        tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
        # Tight resistance at 38,700 (reward room is only 600 KRW, risk is ~850 KRW)
        watch = {'code': '089890', 'resistance': 38700.0, 'status': 'WATCH'}
        
        trials = detector.setup_trials(tick, watch, _bars=bars)
        e_trial = next(tr for tr in trials if tr['setup'] == 'E')
        self.assertFalse(e_trial['gates']['risk_reward']['result'] == 'PASS')
        self.assertFalse(e_trial['entry_ready'])

    def test_setup_e_generic_fakebroker_cannot_bypass_research_seal(self):
        """A paper_mode flag alone cannot authorize a research order."""
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []}):
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                policy = Policy(folder / 'policy.json')
                policy.command('LIVE', confirmation='LIVE')
                
                t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
                auto = replace(self.auto, support_rebound_reclaim_enabled=True, AUTO_TRADING_CAP_KRW=1000000, costs_confirmed=True)
                cfg = replace(self.cfg, live_mode=True, min_window_turnover=1000, min_history_bars=20)
                broker = FakeBroker()
                engine = AutonomousEngine(cfg, auto, policy, folder / 'book', broker, lambda: t_now)
                engine.paper_mode = True  # Paper simulation mode allows paper strategy execution
                
                snapshot = {'cash': 1000000.0, 'equity': 1000000.0, 'orders': [], 'holdings': {}}
                engine.reconcile(snapshot)
                engine.connected = True
                engine.account_ok = True
                engine.market = '3'
                engine.is_supported_now = lambda: True
                engine.market_status['089890'] = {'pass': True, 'reason_code': 'STATUS_NORMAL'}
                
                account = FakeAccount(1000000.0)
                radar = FakeRadar()
                d_bars = [{'datetime': (t_now.date() - timedelta(days=i)).isoformat(),
                           'open': 35000.0, 'high': 45000.0, 'low': 34000.0, 'close': 37000.0, 'volume': 500000}
                          for i in range(25, 0, -1)]
                radar.cache[(t_now.date(), '089890')] = d_bars
                
                prices = {'089890': 38100.0}
                watch = {'code': '089890', 'name': '코세스', 'resistance': 45000.0, 'status': 'WATCH',
                         'source_details': {'ka10027': {}, 'ka10032': {}}, 'source_count': 2,
                         'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}}
                tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
                
                t_base = t_now - timedelta(minutes=6)
                bars = [
                    {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
                    {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
                    {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
                    {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
                    {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 38100.0, 'low': 37700.0, 'close': 38050.0, 'volume': 25000},
                ]
                
                detector = PatternDetector(cfg, auto, self.costs)
                for b in bars:
                    detector.history.setdefault('089890', []).append(b)
                detector.observation_start['089890'] = t_now - timedelta(minutes=10)
                detector.buffers['089890'] = [
                    Tick('089890', t_now - timedelta(seconds=80), 37800.0, 1000, 100000000, 37850.0, 37800.0),
                    Tick('089890', t_now - timedelta(seconds=20), 38050.0, 1000, 104000000, 38050.0, 38000.0),
                    Tick('089890', t_now - timedelta(seconds=5), 38100.0, 1000, 105000000, 38100.0, 38050.0),
                    Tick('089890', t_now - timedelta(seconds=1), 38100.0, 1000, 106000000, 38100.0, 38050.0)
                ]
                
                sig = detector.signal(tick, watch)
                self.assertIsNotNone(sig)
                self.assertEqual(sig['pattern'], 'E')
                self.assertTrue(sig['paper_only'])
                
                # A generic FakeBroker is not the isolated, sealed replay broker.
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account, {})
                self.assertIsNone(order)
                self.assertEqual(broker.orders, [])

    def test_setup_e_live_mode_blocked_paper_guard(self):
        """End-to-End: In LIVE mode, Setup E is strictly blocked by PAPER_STRATEGY_BLOCKED_IN_LIVE guard."""
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []}):
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                policy = Policy(folder / 'policy.json')
                policy.command('LIVE', confirmation='LIVE')
                self.assertEqual(policy.mode, 'LIVE')
                
                t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
                auto = replace(self.auto, support_rebound_reclaim_enabled=True, AUTO_TRADING_CAP_KRW=1000000, costs_confirmed=True)
                cfg = replace(self.cfg, live_mode=True, min_window_turnover=1000, min_history_bars=20)
                broker = FakeBroker()
                engine = AutonomousEngine(cfg, auto, policy, folder / 'book', broker, lambda: t_now)
                # engine.paper_mode is False (default LIVE production setting)
                self.assertFalse(getattr(engine, 'paper_mode', False))
                
                snapshot = {'cash': 1000000.0, 'equity': 1000000.0, 'orders': [], 'holdings': {}}
                engine.reconcile(snapshot)
                engine.connected = True
                engine.account_ok = True
                engine.market = '3'
                engine.is_supported_now = lambda: True
                engine.market_status['089890'] = {'pass': True, 'reason_code': 'STATUS_NORMAL'}
                
                account = FakeAccount(1000000.0)
                radar = FakeRadar()
                d_bars = [{'datetime': (t_now.date() - timedelta(days=i)).isoformat(),
                           'open': 35000.0, 'high': 45000.0, 'low': 34000.0, 'close': 37000.0, 'volume': 500000}
                          for i in range(25, 0, -1)]
                radar.cache[(t_now.date(), '089890')] = d_bars
                
                prices = {'089890': 38100.0}
                watch = {'code': '089890', 'name': '코세스', 'resistance': 45000.0, 'status': 'WATCH',
                         'source_details': {'ka10027': {}, 'ka10032': {}}, 'source_count': 2,
                         'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}}
                tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
                
                t_base = t_now - timedelta(minutes=6)
                bars = [
                    {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
                    {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
                    {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
                    {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
                    {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 38100.0, 'low': 37700.0, 'close': 38050.0, 'volume': 25000},
                ]
                
                detector = PatternDetector(cfg, auto, self.costs)
                for b in bars:
                    detector.history.setdefault('089890', []).append(b)
                detector.observation_start['089890'] = t_now - timedelta(minutes=10)
                detector.buffers['089890'] = [
                    Tick('089890', t_now - timedelta(seconds=80), 37800.0, 1000, 100000000, 37850.0, 37800.0),
                    Tick('089890', t_now - timedelta(seconds=20), 38050.0, 1000, 104000000, 38050.0, 38000.0),
                    Tick('089890', t_now - timedelta(seconds=5), 38100.0, 1000, 105000000, 38100.0, 38050.0),
                    Tick('089890', t_now - timedelta(seconds=1), 38100.0, 1000, 106000000, 38100.0, 38050.0)
                ]
                
                # Execute in LIVE mode
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account, {})
                # Must return None (blocked)
                self.assertIsNone(order)
                # Broker must receive ZERO orders
                self.assertEqual(len(broker.orders), 0)

    def test_setup_e_70420_live_capital_2pct_risk_rejection_no_max1_floor(self):
        """With 70,420 KRW capital (2% risk = 1,408.40 KRW), a 1-share risk > 1,408.40 KRW is rejected (RISK_BUDGET_ZERO_QTY)."""
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []}):
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                policy = Policy(folder / 'policy.json')
                policy.command('LIVE', confirmation='LIVE')
                t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
                auto = replace(self.auto, support_rebound_reclaim_enabled=True, AUTO_TRADING_CAP_KRW=70420, costs_confirmed=True)
                cfg = replace(self.cfg, live_mode=True, account_equity=70420.0, paper_cash=70420.0,
                              risk_fraction=0.02, max_daily_loss_fraction=0.04, max_position_fraction=1.0, max_positions=1,
                              min_window_turnover=1000, min_bc_window_turnover=1000, min_history_bars=20)
                broker = FakeBroker()
                engine = AutonomousEngine(cfg, auto, policy, folder / 'book', broker, lambda: t_now)
                engine.paper_mode = True
                engine.reconcile({'cash': 70420.0, 'equity': 70420.0, 'orders': [], 'holdings': {}})
                engine.connected = True
                engine.account_ok = True
                engine.market = '3'
                engine.is_supported_now = lambda: True

                # 60,000 KRW stock with stop at 58,200 KRW -> 1 share risk (~2,230 KRW) > 1,408.40 KRW (2% of 70,420)
                sig = {'pattern': 'E', 'symbol': '089890', 'limit': 60000.0, 'stop': 58200.0, 'target': 64500.0, 'paper_only': True}
                rem = engine.daily_risk_budget(70420.0) - engine.s['daily_loss']
                qd = engine.quantity_details(sig, 70420.0, 70420.0, rem)
                self.assertEqual(qd['quantity'], 0)
                self.assertEqual(qd['reason_code'], 'RISK_BUDGET_ZERO_QTY')

    def test_setup_e_daily_loss_limit_4pct_exhaustion_blocks_new_entry(self):
        """End-to-End: On Oct 27, after prior realized losses of 2,749.23 KRW, Trade 3 (357880) is blocked by 4% daily loss limit."""
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []}):
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                policy = Policy(folder / 'policy.json')
                policy.command('LIVE', confirmation='LIVE')
                t_now = datetime(2026, 10, 27, 12, 43, 48, tzinfo=KST)
                auto = replace(self.auto, support_rebound_reclaim_enabled=True, AUTO_TRADING_CAP_KRW=70420, costs_confirmed=True)
                cfg = replace(self.cfg, live_mode=True, account_equity=70420.0, paper_cash=70420.0,
                              risk_fraction=0.02, max_daily_loss_fraction=0.04, max_position_fraction=1.0, max_positions=1,
                              min_window_turnover=1000, min_bc_window_turnover=1000, min_history_bars=20)
                broker = FakeBroker()
                engine = AutonomousEngine(cfg, auto, policy, folder / 'book', broker, lambda: t_now)
                engine.paper_mode = True
                engine.reconcile({'cash': 67670.77, 'equity': 67670.77, 'orders': [], 'holdings': {}})
                engine.s['daily_loss'] = 2749.23
                engine.connected = True
                engine.account_ok = True
                engine.market = '3'
                engine.is_supported_now = lambda: True
                engine.market_status['357880'] = {'pass': True, 'reason_code': 'STATUS_NORMAL'}

                account = FakeAccount(67670.77)
                radar = FakeRadar()
                d_bars = [{'datetime': (t_now.date() - timedelta(days=i)).isoformat(),
                           'open': 1850.0, 'high': 2100.0, 'low': 1800.0, 'close': 1900.0, 'volume': 2000000}
                          for i in range(25, 0, -1)]
                radar.cache[(t_now.date(), '357880')] = d_bars
                prices = {'357880': 1945.0}
                watch = {'code': '357880', 'name': '비트나인', 'resistance': 2300.0, 'status': 'WATCH',
                         'source_details': {'ka10027': {}, 'ka10032': {}}, 'source_count': 2,
                         'market_status': {'pass': True, 'reason_code': 'STATUS_NO_KNOWN_SPECIAL_FLAG'}}
                tick = Tick('357880', t_now, 1945.0, 50000, 15000000, 1945.0, 1942.0)

                t_base = t_now - timedelta(minutes=6)
                bars = [
                    {'time': t_base + timedelta(minutes=1), 'open': 1900.0, 'high': 1920.0, 'low': 1900.0, 'close': 1915.0, 'volume': 20000},
                    {'time': t_base + timedelta(minutes=2), 'open': 1915.0, 'high': 1980.0, 'low': 1910.0, 'close': 1975.0, 'volume': 50000},
                    {'time': t_base + timedelta(minutes=3), 'open': 1975.0, 'high': 1975.0, 'low': 1935.0, 'close': 1940.0, 'volume': 15000},
                    {'time': t_base + timedelta(minutes=4), 'open': 1940.0, 'high': 1940.0, 'low': 1925.0, 'close': 1930.0, 'volume': 10000},
                    {'time': t_base + timedelta(minutes=5), 'open': 1932.0, 'high': 1948.0, 'low': 1930.0, 'close': 1945.0, 'volume': 25000},
                ]
                detector = PatternDetector(cfg, auto, self.costs)
                for b in bars:
                    detector.history.setdefault('357880', []).append(b)
                detector.observation_start['357880'] = t_now - timedelta(minutes=10)
                detector.buffers['357880'] = [
                    Tick('357880', t_now - timedelta(seconds=80), 1930.0, 10000, 100000000, 1932.0, 1930.0),
                    Tick('357880', t_now - timedelta(seconds=20), 1940.0, 10000, 104000000, 1942.0, 1940.0),
                    Tick('357880', t_now - timedelta(seconds=5), 1945.0, 10000, 105000000, 1945.0, 1942.0),
                    Tick('357880', t_now - timedelta(seconds=1), 1945.0, 10000, 106000000, 1945.0, 1942.0),
                ]

                sig = detector.signal(tick, watch)
                self.assertIsNotNone(sig)
                self.assertEqual(sig['pattern'], 'E')

                # Because daily_loss (2,749.23) exceeds 4% of current equity (2,706.83), remaining daily risk is <= 0
                order = evaluate_actual_entry(engine, detector, tick, watch, radar, prices, account, {})
                self.assertIsNone(order)
                self.assertEqual(len(broker.orders), 0)

    def test_setup_e_cash_flow_risk_sizing_includes_fees_and_slippage(self):
        """Setup E sizing via AutonomousEngine.quantity_details includes roundtrip fees and slippage in risk_per_share."""
        with patch('automation.release_integrity.check_runtime_integrity', return_value={'valid': True, 'reasons': []}):
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                policy = Policy(folder / 'policy.json')
                policy.command('LIVE', confirmation='LIVE')
                t_now = datetime(2026, 10, 27, 10, 54, 19, tzinfo=KST)
                auto = replace(self.auto, support_rebound_reclaim_enabled=True, AUTO_TRADING_CAP_KRW=70420, costs_confirmed=True)
                cfg = replace(self.cfg, live_mode=True, account_equity=70420.0, paper_cash=70420.0,
                              risk_fraction=0.02, max_daily_loss_fraction=0.04, max_position_fraction=1.0, max_positions=1)
                broker = FakeBroker()
                engine = AutonomousEngine(cfg, auto, policy, folder / 'book', broker, lambda: t_now)
                engine.paper_mode = True
                engine.reconcile({'cash': 69397.67, 'equity': 69397.67, 'orders': [], 'holdings': {}})
                engine.s['daily_loss'] = 1022.33
                engine.connected = True
                engine.account_ok = True

                # Oct 27 Trade 2 (001510): limit 2,090, stop 2,050
                rem = engine.daily_risk_budget(69397.67) - engine.s['daily_loss']
                # Price-only risk is 40 KRW, which would naively give 34 shares (or 31 at 2094),
                # but true cash-flow risk_per_share is ~55.02 KRW, giving exactly 25 shares (<= 1,387.95 KRW budget).
                for pattern in ('D', 'E', 'E2'):
                    with self.subTest(pattern=pattern):
                        sig = {'pattern': pattern, 'symbol': '001510', 'limit': 2090.0, 'stop': 2050.0, 'target': 2190.0, 'paper_only': True}
                        qd = engine.quantity_details(sig, 69397.67, 69397.67, rem)
                        self.assertEqual(qd['quantity'], 25)
                        self.assertLessEqual(qd['quantity'] * qd['risk_per_share'], qd['risk_budget'])

    def test_setup_e2_reclaim_high_rejection_when_below_support_bar_high(self):
        """E2 Rule: When price is above support but still below the high of the support-setting candle, entry is REJECTED."""
        auto_enabled = replace(self.auto, support_rebound_reclaim_enabled=True, setup_e_reclaim_mode='PULLBACK_HIGH_RECLAIM')
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        t_base = t_now - timedelta(minutes=6)
        # Pullback candle Bar 4 has low 37,600 and high 38,000
        bars = [
            {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
            {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
            {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
            {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
            {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 37900.0, 'low': 37700.0, 'close': 37850.0, 'volume': 25000},
        ]
        # Price 37,850 is +0.66% above support (37,600), but below Bar 4 high (38,000)
        tick = Tick('089890', t_now, 37850.0, 50000, 15000000, 37850.0, 37800.0)
        watch = {'code': '089890', 'resistance': 45000.0, 'status': 'WATCH'}
        setup_e2 = detector.support_rebound(bars, tick, watch)
        self.assertIsNone(setup_e2, "Price below support candle high must be rejected")

    def test_setup_e2_reclaim_high_approval_when_price_reclaims_support_bar_high(self):
        """E2 Rule: When price reclaims above the high of the support-setting candle, entry is APPROVED."""
        auto_enabled = replace(self.auto, support_rebound_reclaim_enabled=True, setup_e_reclaim_mode='PULLBACK_HIGH_RECLAIM')
        detector = PatternDetector(self.cfg, auto_enabled, self.costs)
        t_now = datetime(2026, 10, 27, 10, 30, tzinfo=KST)
        t_base = t_now - timedelta(minutes=6)
        bars = [
            {'time': t_base + timedelta(minutes=1), 'open': 37000.0, 'high': 37300.0, 'low': 37000.0, 'close': 37300.0, 'volume': 20000},
            {'time': t_base + timedelta(minutes=2), 'open': 37300.0, 'high': 38500.0, 'low': 37200.0, 'close': 38400.0, 'volume': 50000},
            {'time': t_base + timedelta(minutes=3), 'open': 38400.0, 'high': 38400.0, 'low': 37800.0, 'close': 37900.0, 'volume': 15000},
            {'time': t_base + timedelta(minutes=4), 'open': 37900.0, 'high': 38000.0, 'low': 37600.0, 'close': 37750.0, 'volume': 10000},
            {'time': t_base + timedelta(minutes=5), 'open': 37750.0, 'high': 38100.0, 'low': 37700.0, 'close': 38050.0, 'volume': 25000},
        ]
        # Price 38,100 reclaims Bar 4 high (38,000)
        tick = Tick('089890', t_now, 38100.0, 50000, 15000000, 38100.0, 38050.0)
        watch = {'code': '089890', 'resistance': 45000.0, 'status': 'WATCH'}
        setup_e2 = detector.support_rebound(bars, tick, watch)
        self.assertIsNotNone(setup_e2)
        self.assertEqual(setup_e2['breakout'], 38000.0)
        self.assertEqual(setup_e2['entry_basis'], 'CONFIRMED_SUPPORT_REBOUND_RECLAIM')
        self.assertTrue(setup_e2['paper_only'])

if __name__ == '__main__':
    unittest.main()
