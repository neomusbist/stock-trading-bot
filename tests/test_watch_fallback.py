import unittest
import tempfile
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

from core.settings import Config, KST, ROOT
from core.strategy import Tick
from automation.policy import AutomationConfig, Policy
from automation.capital import Costs
from automation.engine import AutonomousEngine
from automation.radar import DynamicRadar, RadarWorker
from automation.runner import qualify_raw
from automation.patterns import PatternDetector
from automation.allocation import efficiency
try:
    from test_automation import FakeBroker
except ImportError:
    from tests.test_automation import FakeBroker

START = datetime(2026, 10, 6, 9, 30, tzinfo=KST)

class WatchFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.policy = Policy(self.folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        self.auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=20000, costs_confirmed=True)
        self.cfg = replace(Config(), live_mode=True, max_watchlist=5)
        self.costs = Costs(.00015, .00015, .002, .002, .003)

    def make_radar(self, fallback_source1=True):
        radar = DynamicRadar(self.cfg, self.auto, self.policy, fallback_source1=fallback_source1)
        radar.available_capital = 20000
        return radar

    def primary_item(self, sym, price=2000):
        return {
            'code': sym, 'name': f'종목_{sym}', 'reference_price': price,
            'source_details': {
                'src_a': {'observed_rank': 1, 'values': {'flu_rt': 5.0}, 'raw': {}},
                'src_b': {'observed_rank': 2, 'values': {'flu_rt': 4.0}, 'raw': {}},
            }
        }

    def single_source_item(self, sym, price=2000):
        return {
            'code': sym, 'name': f'종목_{sym}', 'reference_price': price,
            'source_details': {
                'src_a': {'observed_rank': 1, 'values': {'flu_rt': 3.0}, 'raw': {}}
            }
        }

    def evaluate(self, item):
        return {'status': 'ELIGIBLE', 'overhead_share_estimate': 0.01, 'previous_close': item['reference_price'], 'resistance': item['reference_price'] * 1.1}

    def test_primary_vs_fallback_slot_allocation_matrix(self):
        radar = self.make_radar(fallback_source1=True)
        primaries = [self.primary_item(f'10000{i}') for i in range(6)]
        fallbacks = [self.single_source_item(f'20000{i}') for i in range(3)]
        rows, watch, changes = radar.update(primaries + fallbacks, self.evaluate, START)
        self.assertEqual(len(watch), 5)
        self.assertTrue(all(w['source_count'] >= 2 for w in watch))
        self.assertFalse(any(w.get('fallback_source1') for w in watch))
        self.assertEqual(len(changes['added']), 5)
        for fb in fallbacks:
            row = next(r for r in rows if r['code'] == fb['code'])
            self.assertEqual(row['status'], 'REJECT')
            self.assertEqual(row['reason'], '소스 교집합 부족')

        radar2 = self.make_radar(fallback_source1=True)
        p3 = [self.primary_item(f'10000{i}') for i in range(3)]
        f4 = [self.single_source_item(f'20000{i}') for i in range(4)]
        rows2, watch2, _ = radar2.update(p3 + f4, self.evaluate, START)
        self.assertEqual(len(watch2), 5)
        primary_watch = [w for w in watch2 if not w.get('fallback_source1')]
        fallback_watch = [w for w in watch2 if w.get('fallback_source1')]
        self.assertEqual(len(primary_watch), 3)
        self.assertEqual(len(fallback_watch), 2)

        radar3 = self.make_radar(fallback_source1=True)
        f7 = [self.single_source_item(f'20000{i}') for i in range(7)]
        _, watch3, _ = radar3.update(f7, self.evaluate, START)
        self.assertEqual(len(watch3), 5)
        self.assertTrue(all(w.get('fallback_source1') for w in watch3))

    def test_4_unaffordable_fallback_not_in_watch(self):
        """4. fallback 후보도 1주 매수 불가능하면 WATCH 불가."""
        radar = self.make_radar(fallback_source1=True)
        affordable_fb = [self.single_source_item('200001', price=5000), self.single_source_item('200002', price=10000)]
        unaffordable_fb = [self.single_source_item('200003', price=25000), self.single_source_item('200004', price=50000)]
        rows, watch, changes = radar.update(affordable_fb + unaffordable_fb, self.evaluate, START)
        self.assertEqual(len(watch), 2)
        watch_symbols = [w['code'] for w in watch]
        self.assertIn('200001', watch_symbols)
        self.assertIn('200002', watch_symbols)
        self.assertNotIn('200003', watch_symbols)
        self.assertNotIn('200004', watch_symbols)
        r3 = next(r for r in rows if r['code'] == '200003')
        self.assertEqual(r3['status'], 'REJECT')
        self.assertEqual(r3['reason'], 'ONE_SHARE_UNAFFORDABLE')

    def test_5_primary_priority_displacement(self):
        """5. primary 후보가 새로 등장하면 fallback 후보보다 우선권을 가짐."""
        radar = self.make_radar(fallback_source1=True)
        cycle1_primary = [self.primary_item('100001')]
        fallbacks = [self.single_source_item(f'20000{i}') for i in range(4)]
        _, watch1, _ = radar.update(cycle1_primary + fallbacks, self.evaluate, START)
        self.assertEqual(len(watch1), 5)
        self.assertEqual(len([w for w in watch1 if w.get('fallback_source1')]), 4)

        cycle2_primary = [self.primary_item('100001'), self.primary_item('100002'), self.primary_item('100003')]
        _, watch2, changes2 = radar.update(cycle2_primary + fallbacks, self.evaluate, START + timedelta(minutes=1))
        self.assertEqual(len(watch2), 5)
        p_watch = [w for w in watch2 if not w.get('fallback_source1')]
        fb_watch = [w for w in watch2 if w.get('fallback_source1')]
        self.assertEqual(len(p_watch), 3)
        self.assertEqual(len(fb_watch), 2)
        self.assertEqual(len(changes2['removed']), 2)

    def test_6_watch_total_never_exceeds_max_watchlist(self):
        """6. WATCH 총 개수는 절대 max_watchlist 초과 금지."""
        radar = self.make_radar(fallback_source1=True)
        for i in range(10):
            primaries = [self.primary_item(f'10000{j}') for j in range(i % 6)]
            fallbacks = [self.single_source_item(f'20000{j}') for j in range(8)]
            _, watch, _ = radar.update(primaries + fallbacks, self.evaluate, START + timedelta(minutes=i))
            self.assertLessEqual(len(watch), self.cfg.max_watchlist)
            self.assertEqual(len(watch), min(self.cfg.max_watchlist, len(primaries) + len(fallbacks)))

    def test_fallback_watch_engine_entry_and_riskgate_invariants(self):
        """source1 WATCH 승격만으로 order 발생 안 함 및 RiskGate 보존."""
        broker = FakeBroker()
        engine = AutonomousEngine(self.cfg, self.auto, self.policy, self.folder / 'book', broker, lambda: START)
        engine.connected = True
        engine.market = '3'
        engine.reconcile({'cash': 20000, 'equity': 20000, 'orders': [], 'holdings': {}})
        engine.market_status = {'200001': {'pass': True, 'reason_code': 'SYNTHETIC'}}

        qualify_raw(engine, [{'code': '200001', 'source_count': 1, 'fallback_source1': True,
                              'affordability': {'entry_eligible': True, 'reason_code': 'ONE_SHARE_AFFORDABLE'}}], START.isoformat())

        tick = Tick('200001', START, 2000, 100, 100, 2010, 1990)
        invalid_signal = {'symbol': '200001', 'time': START.isoformat(), 'limit': 2010, 'stop': 2050,
                          'source_count': 1, 'fallback_source1': True}
        order = engine.enter(invalid_signal, tick)
        self.assertIsNone(order)
        self.assertEqual(broker.orders, [])

        valid_signal = {'symbol': '200001', 'time': START.isoformat(), 'limit': 2005, 'stop': 1990,
                        'pattern': 'A', 'source_count': 1, 'fallback_source1': True,
                        'efficiency': efficiency(2005, 1990, 2100, 0.8, 10, engine.costs)}
        order2 = engine.enter(valid_signal, tick)
        self.assertIsNotNone(order2)
        self.assertEqual(len(broker.orders), 1)

        # RiskGate intact: wide stop rejected
        tick2 = Tick('200001', START, 10000, 100, 100, 10010, 9990)
        sig_wide_stop = {'symbol': '200001', 'time': START.isoformat(), 'limit': 10000, 'stop': 8000,
                         'pattern': 'A', 'source_count': 1, 'fallback_source1': True,
                         'efficiency': efficiency(10000, 8000, 12000, 0.8, 10, engine.costs)}
        self.assertIsNone(engine.enter(sig_wide_stop, tick2))
        self.assertEqual(len(broker.orders), 1)

    def test_9_subscription_refresh_and_observation_intact(self):
        """9. fallback 종목도 subscription lifecycle 및 observation_worker와 호환."""
        from automation.runner import SubscriptionState
        sub = SubscriptionState()
        feed = Mock()
        engine = Mock()
        detector = Mock()
        detector.last = {}
        detector.history = {}
        detector.usable_bars = Mock(return_value=[])
        detector.observation_fresh.return_value = True

        sub.refresh(engine, detector, feed, ['200001', '200002'], START)
        self.assertEqual(sub.symbols, {'200001', '200002'})

        sub.refresh(engine, detector, feed, ['200001', '100001'], START + timedelta(seconds=1))
        self.assertEqual(sub.symbols, {'200001', '100001'})
        self.assertIn('200002', sub.detached)

    def test_10_baseline_mode_without_fallback(self):
        """10. fallback_source1=False 기본 모드에서는 단일 소스 종목이 WATCH에 절대 진입하지 않음."""
        radar = self.make_radar(fallback_source1=False)
        item = self.single_source_item('200001')
        rows, watch, changes = radar.update([item], self.evaluate, START)
        self.assertEqual(watch, [])
        self.assertEqual(rows[0]['status'], 'REJECT')
        self.assertEqual(rows[0]['reason'], '소스 교집합 부족')

if __name__ == '__main__':
    unittest.main()
