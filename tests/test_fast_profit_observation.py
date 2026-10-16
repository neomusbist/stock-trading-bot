"""Pure observer tests. Synthetic candles are not the missing MK history."""
import copy
import unittest
from dataclasses import replace
from datetime import datetime,timedelta

from automation.fast_profit_observation import observe_fast_profit,POLICIES,REQUIRED_DATA
from automation.policy import AutomationConfig
from core.settings import Config,KST
from tests.test_mk_exit_authority import INCIDENT


def bar(minute,low,close,high=None):
    start=datetime(2026,10,16,12,minute,tzinfo=KST)
    return {'time':start.isoformat(),'open':low+10,'low':low,
            'high':high or max(close,low+20),'close':close,'volume':100,
            'complete':True,'received_at':(start+timedelta(minutes=1)).isoformat()}


class FastProfitObservationTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(Config(),risk_fraction=.02,max_daily_loss_fraction=.04)
        self.auto=AutomationConfig()
        self.at=datetime(2026,10,16,12,54,1,tzinfo=KST)
        self.p={'symbol':'033160','pattern':'FAST','avg':16910,'qty':4,
            'initial_stop':16840,'stop':17070,'protection_stop':17070,
            'emergency':16830,'initial_r':70,'high':17100,'low':16910,
            'opened':INCIDENT['entry']['opened'],'signal':copy.deepcopy(INCIDENT['signal']),
            'protection_bars':[]}
        self.quote(17060)

    def quote(self,bid):
        self.p['last_quote']={'time':self.at.isoformat(),'bid':bid,'ask':bid+10,'bid_qty':1000}

    def confirmed(self):
        self.p['protection_bars']=[bar(51,17060,17090,17100),
            bar(52,17040,17080,17100),bar(53,17070,17120,17130)]
        return observe_fast_profit(self.cfg,self.auto,self.p,self.at)

    def test_mk_missing_bars_is_unknown_not_successful_holding(self):
        self.at=datetime.fromisoformat(INCIDENT['trigger']['requested_at']);self.quote(17060)
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        for name in POLICIES[1:]:
            self.assertEqual(result['policies'][name]['action'],'UNKNOWN')
            self.assertIsNone(result['policies'][name]['profit_line'])
        self.assertEqual(result['shared_risk']['initial_emergency'],16830)
        self.assertFalse(result['profitability_proven'])

    def test_supplied_fast_decision_does_not_become_bar_trend_failure(self):
        self.p['fast_exit_evidence']={'checked_at':self.at.isoformat(),'decision':'EXIT',
            'reason':'PROFIT_LOCK_EXIT','micro_protection_stop':17070}
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertEqual(result['policies'][POLICIES[0]]['action'],'EXIT_ALL_OBSERVATION')
        self.assertEqual(result['policies'][POLICIES[1]]['action'],'UNKNOWN')
        self.assertFalse(result['orders_enabled'])

    def test_old_copied_stop_does_not_seed_either_candidate_line(self):
        result=self.confirmed()
        for name in POLICIES[1:]:self.assertEqual(result['policies'][name]['profit_line'],17020)
        self.assertEqual(self.p['stop'],17070)

    def test_normal_intraminute_breach_of_confirmed_line_is_not_a_completed_close(self):
        result=self.confirmed();self.at+=timedelta(seconds=20);self.quote(17000)
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at,result['state'])
        for name in POLICIES[1:]:self.assertEqual(result['policies'][name]['action'],'HOLD')

    def test_actual_completed_close_break_exits_before_searching_for_another_pivot(self):
        result=self.confirmed();self.at=datetime(2026,10,16,12,55,1,tzinfo=KST);self.quote(17000)
        self.p['protection_bars'].append(bar(54,16990,17000,17110))
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at,result['state'])
        for name in POLICIES[1:]:
            self.assertEqual(result['policies'][name]['action'],'EXIT_ALL_OBSERVATION')
            self.assertEqual(result['policies'][name]['reason'],'CONFIRMED_SUPPORT_CLOSE_FAILED')

    def test_one_valid_current_close_can_break_a_previously_confirmed_line(self):
        result=self.confirmed();self.at=datetime(2026,10,16,12,55,1,tzinfo=KST)
        self.p['protection_bars']=[bar(54,16990,17000,17110)]
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at,result['state'])
        for name in POLICIES[1:]:self.assertEqual(result['policies'][name]['action'],'EXIT_ALL_OBSERVATION')

    def test_genuine_emergency_is_immediate_even_with_no_complete_bar(self):
        self.at=datetime.fromisoformat(self.p['opened'])+timedelta(seconds=10);self.quote(16830)
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        for name in POLICIES[1:]:
            self.assertEqual(result['policies'][name]['action'],'EXIT_ALL_OBSERVATION')
            self.assertEqual(result['policies'][name]['reason'],'INITIAL_EMERGENCY')

    def test_initial_structure_failure_is_not_mislabeled_as_profit(self):
        self.at=datetime(2026,10,16,12,52,1,tzinfo=KST);self.quote(16850)
        self.p['protection_bars']=[bar(51,16820,16840,16900)]
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        for name in POLICIES[1:]:self.assertEqual(result['policies'][name]['reason'],'INITIAL_STRUCTURE_CLOSE_FAILED')

    def test_partial_future_received_late_and_gapped_bars_do_not_create_support(self):
        for kind in ('partial','future','late','gap','bad_volume','entry_minute'):
            with self.subTest(kind=kind):
                self.confirmed();bars=self.p['protection_bars']
                if kind=='partial':bars[-1]['complete']=False
                elif kind=='future':bars[-1]['time']=self.at.replace(second=0).isoformat()
                elif kind=='late':bars[-1]['received_at']=(self.at+timedelta(seconds=1)).isoformat()
                elif kind=='gap':bars[1]['gap_reason']='RECEIVER_GAP'
                elif kind=='bad_volume':bars[1]['volume']=None
                else:bars[0]['time']=datetime(2026,10,16,12,50,tzinfo=KST).isoformat()
                result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
                for name in POLICIES[1:]:
                    self.assertEqual(result['policies'][name]['action'],'UNKNOWN')
                    self.assertIsNone(result['policies'][name]['profit_line'])

    def test_observer_never_mutates_position_or_previous_state(self):
        result=self.confirmed();original=copy.deepcopy(self.p);previous=copy.deepcopy(result['state'])
        observe_fast_profit(self.cfg,self.auto,self.p,self.at,result['state'])
        self.assertEqual(self.p,original);self.assertEqual(result['state'],previous)

    def test_future_position_high_is_not_used_to_arm_profit_protection(self):
        self.p['high']=999999
        self.p['protection_bars']=[bar(51,16910,16920,16930),
            bar(52,16900,16920,16930),bar(53,16920,16940,16950)]
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertEqual(result['policies'][POLICIES[1]]['reason'],'PROTECTION_NOT_ARMED')
        self.assertIsNone(result['policies'][POLICIES[1]]['profit_line'])

    def test_C_requires_recovery_above_confirmed_pivot_high(self):
        self.p['protection_bars']=[bar(51,17060,17090,17100),
            bar(52,17040,17080,17130),bar(53,17070,17120,17130)]
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertEqual(result['policies'][POLICIES[1]]['profit_line'],17020)
        self.assertIsNone(result['policies'][POLICIES[2]]['profit_line'])

    def test_completed_high_remains_known_after_the_three_bar_window_rolls(self):
        self.p.update(avg=16000,initial_stop=15000,initial_r=1000,stop=15000,emergency=14990)
        self.p['signal']['invalidation_policy'].update(structural_stop=15000,emergency_stop=14990)
        self.p['protection_bars']=[bar(51,16100,16200,17100),
            bar(52,16110,16210,16250),bar(53,16120,16220,16260)]
        self.quote(16220)
        first=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertIsNone(first['policies'][POLICIES[1]]['profit_line'])
        self.at=datetime(2026,10,16,12,57,1,tzinfo=KST);self.quote(16300)
        self.p['protection_bars']=[bar(54,16180,16220,16250),
            bar(55,16160,16210,16230),bar(56,16200,16300,16310)]
        later=observe_fast_profit(self.cfg,self.auto,self.p,self.at,first['state'])
        self.assertEqual(later['state']['confirmed_high'],17100)
        self.assertGreater(later['policies'][POLICIES[1]]['profit_line'],16000)

    def test_C_can_confirm_profitable_structure_before_one_R_but_B_does_not(self):
        self.p.update(avg=16000,initial_stop=15000,initial_r=1000,stop=15000,emergency=14990)
        self.p['signal']['invalidation_policy'].update(structural_stop=15000,emergency_stop=14990)
        self.p['protection_bars']=[bar(51,16200,16220,16240),
            bar(52,16180,16200,16220),bar(53,16210,16250,16270)]
        self.quote(16250)
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertIsNone(result['policies'][POLICIES[1]]['profit_line'])
        self.assertGreater(result['policies'][POLICIES[2]]['profit_line'],16000)

    def test_new_position_does_not_inherit_old_profit_line(self):
        result=self.confirmed();self.p['opened']='2026-10-16T12:53:30+09:00'
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at,result['state'])
        for name in POLICIES[1:]:self.assertIsNone(result['policies'][name]['profit_line'])

    def test_time_cannot_move_backwards_inside_one_observation_stream(self):
        result=self.confirmed();earlier=self.at-timedelta(seconds=1)
        result=observe_fast_profit(self.cfg,self.auto,self.p,earlier,result['state'])
        for name in POLICIES:self.assertEqual(result['policies'][name]['reason'],'NON_MONOTONIC_OBSERVATION')

    def test_missing_data_fields_and_no_execution_authority_are_explicit(self):
        result=observe_fast_profit(self.cfg,self.auto,self.p,self.at)
        self.assertEqual(result['required_data'],list(REQUIRED_DATA))
        self.assertFalse(result['actual_order_permission'])
        self.assertIsNone(result['actual_fill']);self.assertIsNone(result['realized_net_pnl'])


if __name__=='__main__':unittest.main()
