"""Synthetic NHN-like FAST exit fixture (NOT market evidence).

The raw 2026-11-06 NHN ticks were never persisted (priority tape hit its
64 MiB daily cap at 13:45:48), so these prints only reproduce the *mechanics*
recorded in events.jsonl: entry 62,300, structural stop 61,600, emergency
61,400, high 63,500, micro protection stop 63,300, exit bid 63,000.
"""
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from core.settings import KST, Config
from core.strategy import Tick
from automation.policy import Policy, AutomationConfig
from automation.engine import AutonomousEngine
from automation.fast_momentum import FastSignal

SYM = '181710'


class _Broker:
    identity = 'mock-broker-nhn'

    def __init__(self):
        self.orders = []
        self.n = 0

    def submit(self, side, sym, qty, limit, venue='KRX', session='REGULAR'):
        self.n += 1
        oid = f'ORD_{self.n:04d}'
        self.orders.append({'id': oid, 'side': side, 'symbol': sym, 'qty': qty, 'limit': limit})
        return oid

    def cancel(self, sym, oid):
        return f'CANCEL_{oid}'


class NhnFastExitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        self.clock = datetime(2026, 11, 6, 14, 17, 40, tzinfo=KST)
        self.broker = _Broker()
        self.policy = Policy(folder / 'policy.json')
        self.policy.command('LIVE', confirmation='LIVE')
        cfg = replace(Config(), live_mode=True, risk_fraction=0.02, max_daily_loss_fraction=0.04,
                      max_positions=2, max_position_fraction=1.0, min_reward_risk=1.5, account_equity=81300, stale_seconds=5)
        auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300, costs_confirmed=True,
                       fast_momentum_enabled=True, min_source_count=1, fallback_source1=True,
                       cooldown_seconds=300)
        self.engine = AutonomousEngine(cfg, auto, self.policy, folder / 'book', self.broker, lambda: self.clock)
        self.engine.reconcile({'cash': 81300.0, 'equity': 81300.0, 'orders': [], 'holdings': {}})
        self.engine.connected = True
        self.engine.market = '3'
        self.engine.last_account = time.monotonic()
        self.engine.names[SYM] = 'NHN'
        self.engine.market_status[SYM] = {'pass': True, 'reason_code': 'MARKET_NORMAL'}
        self.cum = 1_000_000

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, price, bid, ask, volume=3):
        self.cum += volume
        return Tick(symbol=SYM, time=self.clock, price=float(price), volume=volume, cumulative=self.cum,
                    ask=float(ask), bid=float(bid), regular=True, ask_qty=500, bid_qty=500,
                    depth_available=True, book_time=self.clock, depth_source='SIMULATED_QUOTE')

    def step(self, price, bid=None, ask=None, seconds=1):
        self.clock += timedelta(seconds=seconds)
        bid = price - 100 if bid is None else bid
        ask = price if ask is None else ask
        t = self.tick(price, bid, ask)
        self.engine.on_tick(t)
        return t

    def open_position(self):
        """Fill 1 share at 62,300 via the real engine entry path."""
        for p in (62000, 62100, 62200, 62100, 62200):
            self.clock += timedelta(seconds=1)
            self.engine.fast_strategy.ingest_tick(self.tick(p, p - 100, p + 100), self.clock)
        self.clock += timedelta(seconds=1)
        trig = self.tick(62300, 62200, 62300)
        sig = FastSignal(symbol=SYM, name='NHN', limit=62300.0, stop=61600, target=64900,
                         emergency=61400, quantity=1, net_reward_risk=1.58, gross_reward_risk=2.89,
                         reward_net_per_share=2132.2, risk_net_per_share=1349.8, regime='A',
                         phase='REGULAR', created_at=self.clock,
                         extra={'geometry': {'structural_evidence': {'breakout': 62000.0, 'support': 61800.0, 'origin': 60300.0}, 'noise_band': 200.0}})
        order = self.engine.enter(sig.to_engine_signal({'symbol': SYM, 'name': 'NHN', 'status': 'WATCH'}), trig)
        self.assertIsNotNone(order, 'entry fixture must be accepted by the real engine path')
        self.engine.cumulative_fill(order, order['qty'], order['qty'] * order['limit'])
        self.assertIn(SYM, self.engine.s['positions'])
        return self.engine.s['positions'][SYM]

    def run_nhn_like_path(self):
        pos = self.open_position()
        # (price, bid, ask): rally past 1R (63,200), 1-tick swing low 63,400, high 63,500,
        # pullback through the micro line, two independent trades <= line with bid <= line.
        path = [(62200, 62100, 62300), (62300, 62200, 62300), (62500, 62400, 62500), (62800, 62700, 62800),
                (63000, 62900, 63000), (63300, 63200, 63300), (63500, 63400, 63500), (63400, 63400, 63500),
                (63500, 63400, 63500), (63400, 63300, 63400), (63300, 63200, 63300), (63100, 63000, 63200)]
        for price, bid, ask in path:
            self.step(price, bid, ask)
            if pos.get('exit_requested'):
                break
        return pos


