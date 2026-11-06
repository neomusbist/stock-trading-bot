"""MK 033160: independent FAST trade protection and confirmed-bar structure.

Saved incident numbers are point-in-time evidence, not a complete tick replay.
Counterexamples explicitly use synthetic ticks/bars. All orders use FakeBroker.
"""
import contextlib
import copy
import io
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace

from automation.engine import AutonomousEngine
from automation.exit_candidates import observe_candidates
from automation.exit_plan import invalidation_lines,build_plan
from automation.policy import AutomationConfig,Policy,HARD_LOCKS
from automation.position_health import PositionDataHealth
from core.settings import Config,KST
from core.strategy import Tick
from tests.test_automation import FakeBroker

# Sanitized saved evidence is embedded so the source-only handoff is portable.
INCIDENT={'provenance': {'date': '2026-10-16',
                'symbol': '033160',
                'source': 'production ledger and bounded 12:50-12:55 events',
                'post_exit_prices_used_for_decision': False,
                'complete_received_tick_tape': False,
                'complete_post_fill_bars_available': False},
 'entry': {'opened': '2026-10-16T12:50:50.516471+09:00',
           'entry': 16910.0,
           'qty': 4,
           'initial_stop': 16840},
 'signal': {'symbol': '033160',
            'pattern': 'FAST',
            'limit': 16910.0,
            'stop': 16840,
            'emergency': 16830,
            'target': 17350,
            'time': '2026-10-16T12:50:47.535755+09:00',
            'invalidation_policy': {'policy_name': 'FAST_MICRO_STRUCTURE',
                                    'enabled': True,
                                    'structural_stop': 16840,
                                    'emergency_stop': 16830,
                                    'noise_band': 10.0,
                                    'breakout_level': 16880.0,
                                    'support_level': 16860.0,
                                    'origin_level': 16850.0}},
 'exit': {'closed': '2026-10-16T12:54:48.283159+09:00',
          'exit': 17060.0,
          'reason': '호가 급락/유동성 붕괴',
          'MFE': 190.0,
          'MAE': 0.0,
          'gross_pnl': 600.0,
          'fee_actual': 156.0,
          'net_pnl': 444.0,
          'holding_seconds': 237.766703},
 'trigger': {'requested_at': '2026-10-16T12:54:44.551725+09:00',
             'last_quote': {'time': '2026-10-16T12:54:44+09:00',
                            'bid': 17060.0,
                            'ask': 17080.0,
                            'source': '0D'},
             'trade_price': 17080.0,
             'trade_bid': 17080.0,
             'micro_stop': 17070,
             'legacy_emergency': 17060,
             'bar_protection_reason': 'CONFIRMED_PULLBACK_NOT_READY'},
 'raised_micro_lines': [{'at': '2026-10-16T12:54:22.747646+09:00',
                         'stop': 17020,
                         'current_price': 17070.0},
                        {'at': '2026-10-16T12:54:24.832781+09:00',
                         'stop': 17040,
                         'current_price': 17070.0},
                        {'at': '2026-10-16T12:54:33.247038+09:00',
                         'stop': 17060,
                         'current_price': 17070.0},
                        {'at': '2026-10-16T12:54:38.886079+09:00',
                         'stop': 17070,
                         'current_price': 17100.0}],
 'limits': {'broker_net_pnl_confirmed': True,
            'actual_sell_commission': 10,
            'actual_buy_commission': 10,
            'actual_sell_tax': 136,
            'post_exit_counterfactual_fill_unknown': True}}
SYM='033160'


class MKExitAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        output=contextlib.redirect_stdout(io.StringIO());output.__enter__()
        self.addCleanup(output.__exit__,None,None,None)
        self.root=Path(self.temp.name)
        self.at=datetime.fromisoformat(INCIDENT['trigger']['requested_at'])
        self.cfg=replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=81300,costs_confirmed=True,
            fast_momentum_enabled=True,confirmed_invalidation_enabled=True,confirmed_structure_protection=False)
        self.policy=Policy(self.root/'policy.json');self.policy.command('LIVE',confirmation='LIVE')
        self.broker=FakeBroker();self.e=self.new_engine()
        self.e.reconcile({'cash':81300,'equity':81300,'orders':[],'holdings':{}})
        self.e.connected=True;self.e.market='3';self.e.last_account=time.monotonic()
        entry=INCIDENT['entry'];signal=copy.deepcopy(INCIDENT['signal'])
        self.p={'symbol':SYM,'qty':entry['qty'],'avg':entry['entry'],'pattern':'FAST',
            'stop':entry['initial_stop'],'initial_stop':entry['initial_stop'],'initial_r':70,
            'emergency':signal['emergency'],'protection_stop':INCIDENT['trigger']['micro_stop'],
            'high':entry['entry']+INCIDENT['exit']['MFE'],'low':entry['entry'],
            'ownership':'BOT','qty_blocked':False,'opened':entry['opened'],'last_tick':self.at.isoformat(),
            'exit_requested':False,'exit_reason':'','exit_attempts':0,'signal':signal,
            'trade_id':'synthetic-mk','entry_qty':4,
            'stop_history':[{'time':entry['opened'],'stop':entry['initial_stop']}]}
        self.p['exit_plan']=build_plan(self.cfg,self.auto,signal,self.p['avg'],4,True)
        self.e.s['positions'][SYM]=self.p
        self.e.actual={SYM:{'qty':4,'available':4,'avg':self.p['avg']}}
        self.feed=SimpleNamespace(subscription_lock=threading.RLock(),orderbooks={},last_message_at=self.at)
        self.account=SimpleNamespace(position_symbols=frozenset(),request=threading.Event(),need_fills=False)
        self.sub=SimpleNamespace(symbols={SYM},waiting={})
        self.health=PositionDataHealth(self.e,self.feed,self.account,self.sub)
        self.cumulative=0

    def new_engine(self):
        return AutonomousEngine(self.cfg,self.auto,self.policy,self.root/'book',self.broker,lambda:self.at)

    def tick(self,price=17080,bid=None,ask=None):
        self.cumulative+=100
        return Tick(SYM,self.at.replace(microsecond=0),price,100,self.cumulative,
            price+10 if ask is None else ask,price if bid is None else bid,
            ask_qty=1000,bid_qty=1000,depth_available=True,book_time=self.at.replace(microsecond=0))

    def quote(self,bid=17060,age=0):
        book={'time':(self.at-timedelta(seconds=age)).isoformat(),'best_bid':bid,'best_ask':bid+20}
        return self.health.accept_book(SYM,book,self.at)

    def test_mk_hold_is_not_overridden_by_single_quote_at_micro_line(self):
        self.e.on_tick(self.tick())
        self.assertEqual(self.p['fast_exit_evidence']['reason'],'HOLD')
        self.assertEqual(self.p['protection_stop'],17070)
        self.assertEqual(self.p['stop'],16840)
        self.assertTrue(self.quote())
        self.assertFalse(self.p['exit_requested'])
        self.assertEqual(self.broker.orders,[])
        lines=invalidation_lines(self.cfg,self.auto,self.p)
        self.assertEqual((lines['active_structure'],lines['emergency']),(16840,16830))
        self.assertEqual(self.p['exit_state']['fast_exit']['micro_protection_stop'],17070)

    def test_original_copy_mechanism_reproduces_recorded_single_quote_exit(self):
        self.e.on_tick(self.tick())
        self.p['stop']=self.p['protection_stop']  # removed original assignment
        self.quote()
        self.assertEqual(self.p['exit_reason'],INCIDENT['exit']['reason'])
        self.assertEqual(invalidation_lines(self.cfg,self.auto,self.p)['emergency'],17060)

    def test_fast_micro_protection_is_still_armed_from_causal_ticks_without_structural_promotion(self):
        self.p.pop('protection_stop')
        for i,price in enumerate((17040,17070,17060,17080,17070,17100)):
            self.at=datetime(2026,10,16,12,54,20+i,tzinfo=KST)
            self.e.on_tick(self.tick(price))
        self.assertGreater(self.p['protection_stop'],16910)
        self.assertEqual(self.p['stop'],16840)
        self.assertFalse(self.p['exit_requested'])

    def test_two_independent_trade_breaches_still_take_fast_profit(self):
        self.p['fast_protection_history']=[{'stop':17070,'basis':'CONFIRMED_POST_FILL_HIGHER_LOW'}]
        self.e.on_tick(self.tick(17060))
        self.assertFalse(self.p['exit_requested'])
        self.at+=timedelta(seconds=1);self.e.on_tick(self.tick(17060))
        self.assertEqual(self.p['exit_reason'],'FAST_PROFIT_LOCK_EXIT')
        evidence=self.p['fast_exit_evidence']['evidence']
        self.assertEqual(evidence['confirmation_coverage'],'OBSERVED_ACCEPTED_PRINTS')
        self.assertEqual([x['price'] for x in evidence['confirmation_trades']],[17060,17060])
        self.assertLess(evidence['confirmation_trades'][0]['time'],evidence['confirmation_trades'][1]['time'])
        self.assertEqual(self.p['exit_trigger_evidence']['fast_protection_history'][0]['stop'],17070)
        self.assertEqual(self.p['stop'],16840)
        self.assertEqual(len(self.broker.orders),1)
        captured=copy.deepcopy(self.p['exit_trigger_evidence']['fast_exit'])
        self.at+=timedelta(seconds=1);self.e.on_tick(self.tick(17060))
        self.assertEqual(len(self.broker.orders),1)
        self.assertEqual(self.p['exit_trigger_evidence']['fast_exit'],captured)

    def test_emergency_has_no_45_second_or_two_tick_wait(self):
        self.at=datetime.fromisoformat(self.p['opened'])+timedelta(seconds=10)
        self.e.on_tick(self.tick(16830))
        self.assertEqual(self.p['exit_reason'],'FAST_EMERGENCY_PLUNGE_EXIT')
        self.assertEqual(len(self.broker.orders),1)

    def test_quote_at_genuine_emergency_exits_without_any_trade_confirmation(self):
        self.quote(16830)
        self.assertTrue(self.p['exit_requested'])
        self.e.drive_exits();self.e.drive_exits()
        self.assertEqual(len(self.broker.orders),1)

    def test_initial_structural_breach_still_requires_two_independent_trades(self):
        self.p.pop('protection_stop')
        self.at=datetime.fromisoformat(self.p['opened'])+timedelta(seconds=10)
        self.e.on_tick(self.tick(16840));self.assertFalse(self.p['exit_requested'])
        self.at+=timedelta(seconds=1);self.e.on_tick(self.tick(16840))
        self.assertEqual(self.p['exit_reason'],'FAST_INVALIDATION_STOP_EXIT')

    def test_target_touch_is_not_an_automatic_full_exit(self):
        self.e.on_tick(self.tick(17350))
        self.assertFalse(self.p['exit_requested'])
        self.assertEqual(self.p['target_touch']['price'],17350)

    def test_missing_confirmed_bars_cannot_promote_a_micro_pivot(self):
        self.p['protection_bars']=[]
        self.e.on_tick(self.tick())
        self.assertEqual(self.p['protection_evidence']['reason'],'CONFIRMED_PULLBACK_NOT_READY')
        self.assertEqual(self.p['stop'],16840)
        observed=observe_candidates(self.cfg,self.auto,self.p,self.at)
        self.assertEqual(observed['policies']['STRUCTURAL_TRAIL']['action'],'UNKNOWN')
        self.assertFalse(observed['experimental_orders_enabled'])

    def test_unexecutable_fast_quote_is_unknown_not_a_hold_assertion(self):
        self.e.on_tick(replace(self.tick(),depth_available=False))
        self.assertEqual(self.p['fast_exit_evidence']['decision'],'UNKNOWN')
        self.assertEqual(self.p['fast_exit_evidence']['reason'],'EXECUTABLE_DEPTH_UNKNOWN')
        self.assertEqual(self.p['stop'],16840)

    def test_real_confirmed_higher_low_can_still_raise_common_stop(self):
        # Synthetic counterexample, not invented missing historical candles.
        for minute,low,close in ((51,17060,17090),(52,17040,17080),(53,17070,17100)):
            bar={'time':datetime(2026,10,16,12,minute,tzinfo=KST),'open':low+10,
                 'high':17100,'low':low,'close':close,'volume':100}
            self.e.observe_protection_bar(SYM,bar)
        self.e.on_tick(self.tick(17100))
        self.assertEqual(self.p['stop'],17020)
        self.assertEqual(self.p['protection_evidence']['reason'],'CONFIRMED_HIGHER_LOW_RAISED_STOP')
        self.at=datetime(2026,10,16,12,55,1,tzinfo=KST)
        self.e.observe_protection_bar(SYM,{'time':self.at.replace(minute=54,second=0),
            'open':17060,'high':17070,'low':17010,'close':17010,'volume':100})
        self.assertEqual(self.p['exit_reason'],'확정봉 종가의 구조적 무효화')

    def test_stale_or_future_book_cannot_manufacture_an_exit(self):
        self.assertTrue(self.quote(16800,age=6))
        self.assertFalse(self.p['exit_requested'])
        self.assertFalse(self.quote(16800,age=-5))
        self.assertFalse(self.p['exit_requested'])

    def test_restart_preserves_separate_lines_and_existing_loss_ledger(self):
        self.e.on_tick(self.tick())
        self.e.s['daily_loss']=767.;self.e.s['entries']={SYM:1}
        self.e.s['trades']=[{'symbol':'synthetic','pnl_estimate':-767.}]
        self.e.s['automation']['existing']={s:{'qty':1,'policy':'HARD_LOCK'} for s in HARD_LOCKS}
        self.e.save();prior=copy.deepcopy(self.e.s)
        restored=self.new_engine()
        for key in ('daily_loss','entries','trades','orders'):
            self.assertEqual(restored.s[key],prior[key])
        self.assertEqual(restored.s['automation']['existing'],prior['automation']['existing'])
        p=restored.s['positions'][SYM]
        self.assertEqual((p['stop'],p['protection_stop'],p['emergency']),(16840,17070,16830))
        self.assertEqual(self.cfg.risk_fraction,.02)
        self.assertEqual(self.cfg.max_daily_loss_fraction,.04)

    def test_existing_raised_structural_stop_is_never_automatically_lowered(self):
        self.p['stop']=17070
        self.e.on_tick(self.tick())
        self.assertEqual(self.p['stop'],17070)

    def test_incident_cost_and_missing_data_are_explicit(self):
        actual=INCIDENT['exit']
        self.assertEqual(actual['gross_pnl']-actual['fee_actual'],actual['net_pnl'])
        self.assertEqual(actual['net_pnl'],444)
        self.assertFalse(INCIDENT['provenance']['complete_received_tick_tape'])
        self.assertFalse(INCIDENT['provenance']['post_exit_prices_used_for_decision'])


if __name__=='__main__':unittest.main()
