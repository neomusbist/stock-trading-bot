"""R6 independent audit regression tests (fixes found by the 2026-10-14 independent audit)."""
import ast
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import Config, KST
from core.strategy import Tick
from automation.observation_worker import TickLogger, ObservationWorker
from automation.policy import AutomationConfig
from automation.capital import Costs
from tests import test_shadow_observation as shadow_fixtures

ROOT = Path(__file__).resolve().parents[1]


def load_replay():
    path = ROOT / 'audit/r6_claude_independent/independent_replay.py'
    spec = importlib.util.spec_from_file_location('independent_replay', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['independent_replay'] = module
    spec.loader.exec_module(module)
    return module


R = load_replay()
BASE = datetime(2026, 10, 14, 14, 30, tzinfo=KST)


def ev(line, sec, sym='012345', price=10000.0, bid=9990.0, ask=10000.0, gain=0.01, ratio=2.0, turnover=1e7, name='합성'):
    return {'line': line, 'dt': BASE + timedelta(seconds=sec), 'symbol': sym, 'name': name, 'price': price, 'bid': bid,
            'ask': ask, 'spread': 0.001 if bid is not None else None, 'gain': gain, 'ratio': ratio, 'turnover': turnover,
            'decision': 'NO_BUY'}


class FastTapeIngestionTests(unittest.TestCase):
    def test_every_tick_reaches_fast_micro_tape_despite_eval_throttle(self):
        f = shadow_fixtures.ShadowTests()
        f.setUp()
        f.s.update([f.row(2, 'WATCH', '001210')], f.start)
        for sec in range(60):
            f.s.tick(f.tick(sec), f.start + timedelta(seconds=sec))
        self.assertEqual(len(f.s.fast_strategy.get_tape('001210').ticks), 60,
                         'FAST tape must hold every accepted tick, not one per evaluation interval')


class TickLoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'shadow_ticks.jsonl'

    def tick(self, **kw):
        base = dict(symbol='999990', time=BASE, price=1000, volume=3, cumulative=300, ask=1001, bid=999,
                    ask_qty=11, bid_qty=22, venue='KRX', depth_available=True)
        base.update(kw)
        return Tick(**base)

    def test_row_has_receive_time_best_quote_quantities_and_session_fields(self):
        logger = TickLogger(self.path)
        received = BASE + timedelta(milliseconds=250)
        logger.log(self.tick(), received)
        self.assertTrue(logger.flush())
        row = json.loads(self.path.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual((row['ask_qty'], row['bid_qty'], row['cumulative'], row['venue'], row['regular']), (11, 22, 300, 'KRX', True))
        self.assertEqual(row['received'], received.isoformat())
        self.assertNotEqual(row['time'], row['received'])
        self.assertEqual(logger.stats()['rows_written'], 1)

    def test_rotation_never_deletes_older_rotated_files(self):
        logger = TickLogger(self.path, max_bytes=200)
        for _ in range(6):
            for _ in range(5):
                logger.log(self.tick(), BASE)
            logger.flush()
        rotated = sorted(self.path.parent.glob('shadow_ticks.jsonl.*'))
        self.assertGreaterEqual(len(rotated), 3)
        total = sum(len(p.read_text(encoding='utf-8').splitlines()) for p in [self.path, *rotated])
        self.assertEqual(total, 30, 'no row may disappear across rotations')
        self.assertEqual(logger.stats()['rotations'], len(rotated))

    def test_failed_write_keeps_rows_and_is_counted(self):
        missing = Path(self.tmp.name) / 'later' / 'shadow_ticks.jsonl'
        logger = TickLogger(missing)
        for _ in range(5):
            logger.log(self.tick(), BASE)
        self.assertFalse(logger.flush(force=True))
        self.assertEqual(len(logger.buffer), 5)
        self.assertEqual(logger.stats()['write_errors'], 1)
        missing.parent.mkdir()
        self.assertTrue(logger.flush(force=True))
        self.assertEqual(logger.stats()['rows_written'], 5)
        self.assertEqual(len(missing.read_text(encoding='utf-8').splitlines()), 5)

    def test_buffer_overflow_is_counted_not_silent(self):
        logger = TickLogger(Path(self.tmp.name) / 'nodir' / 'x.jsonl', max_buffer_rows=10)
        for _ in range(25):
            logger.log(self.tick(), BASE)
        self.assertEqual(logger.stats()['rows_dropped'], 15)
        self.assertEqual(len(logger.buffer), 10)

    def test_worker_writes_every_delivered_tick_and_close_flushes(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = ObservationWorker(Config(), AutomationConfig(), Costs(.00015, .00015, .002, .002, .003), Path(folder))
            worker.clock = lambda: BASE
            worker.start()
            for n in range(7):
                worker.deliver(self.tick(cumulative=300 + n), BASE)
            deadline = time.monotonic() + 5
            while worker.ticks.qsize() and time.monotonic() < deadline:
                time.sleep(0.05)
            worker.close()
            lines = (Path(folder) / 'runtime/shadow_ticks.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 7)
            self.assertEqual(json.loads(lines[0])['ask_qty'], 11)


class ShadowIsolationTests(unittest.TestCase):
    def test_observation_modules_expose_no_order_surface(self):
        for name in ('automation/observation_worker.py', 'automation/shadow_observation.py', 'automation/fast_momentum.py'):
            tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module, {'core.broker', 'automation.engine'}, name)
                if isinstance(node, ast.Attribute) and node.attr in {'submit', 'enter', 'place_order', 'send_order', 'cancel_order'}:
                    self.fail(f'{name}: order-like attribute {node.attr}')
        used = set()
        tree = ast.parse((ROOT / 'automation/observation_worker.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == 'rest' \
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == 'self':
                used.add(node.attr)
        self.assertEqual(used, {'stock_status'}, 'the observation worker may only use the read-only status query on its REST client')


class IndependentReplayTests(unittest.TestCase):
    P = R.Params()

    def test_open_position_at_tape_end_is_unresolved_not_force_liquidated(self):
        trades, _, counters = R.simulate([ev(1, 0), ev(2, 5, ask=10000.0, bid=9990.0)], self.P)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['status'], 'UNRESOLVED_AT_TAPE_END')
        self.assertEqual(R.metrics(trades, self.P)['closed_trades'], 0)
        self.assertEqual(counters['unresolved_at_tape_end'], 1)

    def test_legacy_mode_still_force_liquidates_to_reproduce_old_report(self):
        trades, _, _ = R.simulate([ev(1, 0), ev(2, 5)], R.Params(legacy=True))
        self.assertEqual(trades[0]['exit_reason'], 'END_OF_TAPE')

    def test_target_exit_and_cash_identity(self):
        events = [ev(1, 0), ev(2, 30, price=10400.0, bid=10390.0, ask=10410.0, gain=None, ratio=None), ev(3, 40, sym='999999', gain=0.0)]
        trades, equity, _ = R.simulate(events, self.P)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['exit_reason'], 'TARGET_REACHED')
        net = sum(t['net_pnl'] for t in trades if t['status'] == 'CLOSED')
        self.assertAlmostEqual(trades[0]['cash_after'] - self.P.capital, net, places=2)
        self.assertAlmostEqual(equity[-1]['cash'] - self.P.capital, net, places=1)
        t = trades[0]
        parts = t['gross_pnl_at_observed'] - t['commissions'] - t['tax'] - t['assumed_slippage'] - t['adverse_tick_cost']
        self.assertAlmostEqual(parts, t['net_pnl'], places=2)

    def test_missing_bid_exit_uses_one_tick_below_price_and_is_flagged(self):
        events = [ev(1, 0), ev(2, 20, price=9900.0, bid=None, ask=None, gain=None, ratio=None)]
        trades, _, _ = R.simulate(events, self.P)
        self.assertEqual(trades[0]['exit_reason'], 'STOP_LOSS')
        self.assertTrue(trades[0]['exit_reference_is_estimate'])
        self.assertEqual(trades[0]['exit_reference_price'], 9890.0)

    def test_daily_risk_cap_blocks_and_reduces_quantity(self):
        trades, _, counters = R.simulate([ev(1, 0)], R.Params(daily_risk_cap=100.0))
        self.assertEqual(trades, [])
        self.assertEqual(counters['entry_blocked_daily_risk_cap'], 1)
        trades, _, _ = R.simulate([ev(1, 0), ev(2, 5)], R.Params(daily_risk_cap=400.0))
        self.assertEqual(trades[0]['qty'], 2)

    def test_single_slot_never_overlaps(self):
        events = [ev(1, 0, sym='000001'), ev(2, 1, sym='000002'), ev(3, 2, sym='000002'),
                  ev(4, 40, sym='000001', price=10400.0, bid=10390.0, ask=10410.0, gain=None, ratio=None),
                  ev(5, 50, sym='000002')]
        trades, _, _ = R.simulate(events, self.P)
        spans = sorted((t['entry_fill_time'], t['exit_time'] or '9') for t in trades)
        for a, b in zip(spans, spans[1:]):
            self.assertLessEqual(a[1], b[0])

    def test_next_snapshot_fill_uses_later_event(self):
        events = [ev(1, 0), ev(2, 3, ask=10010.0, bid=10000.0, price=10005.0, gain=None, ratio=None),
                  ev(3, 60, price=10400.0, bid=10390.0, ask=10410.0, gain=None, ratio=None)]
        trades, _, _ = R.simulate(events, R.Params(fill_mode='NEXT_SNAPSHOT'))
        self.assertEqual((trades[0]['signal_line'], trades[0]['entry_fill_line']), (1, 2))
        self.assertEqual(trades[0]['entry_observed_ask'], 10010.0)

    def test_no_future_information_trade_uses_only_events_up_to_exit(self):
        events = [ev(1, 0), ev(2, 20, price=9900.0, bid=9890.0, ask=9900.0, gain=None, ratio=None),
                  ev(3, 30, price=12000.0, bid=11990.0, ask=12000.0, gain=None, ratio=None)]
        truncated, _, _ = R.simulate(events[:2], self.P)
        full, _, _ = R.simulate(events, self.P)
        self.assertEqual(truncated[0]['net_pnl'], full[0]['net_pnl'])

    def test_symbol_code_survives_as_string_and_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            rows = []
            for n, sec in enumerate((0, 10), 1):
                rows.append(json.dumps({'event': 'ENTRY_EVAL', 'time': (BASE + timedelta(seconds=sec)).isoformat(),
                    'symbol': '012345', 'name': '가나다', 'current_price': 10000.0,
                    'gates': {'spread': {'values': {'bid': 9990.0, 'ask': 10000.0, 'spread': 0.001}},
                              'acceleration': {'values': {'gain': 0.01, 'ratio': 2.0}},
                              'liquidity': {'values': {'window_turnover': 1e7}}}}, ensure_ascii=False))
            path.write_text('\n'.join(rows) + '\n', encoding='utf-8')
            events, _ = R.load_entry_evals(path)
            self.assertEqual({e['symbol'] for e in events}, {'012345'})
            trade = {'trade_id': 1, 'code': '012345', 'name': '가나다', 'signal_line': 1, 'entry_fill_line': 1,
                     'exit_fill_line': 2, 'entry_observed_ask': 10000.0}
            self.assertTrue(all(r['verdict'] == 'MATCH' for r in R.symbol_identity_audit(path, [trade], events)))
            bad = dict(trade, code='012346')
            self.assertTrue(any(r['verdict'] == 'MISMATCH' for r in R.symbol_identity_audit(path, [bad], events)))


if __name__ == '__main__':
    unittest.main()
