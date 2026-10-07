"""저장소로 복사할 공개 회귀검사. 실제 인증정보/네트워크를 사용하지 않는다."""
import copy
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from tests import test_audit_followup as fixture
from tests import test_v1
from automation.engine import AutonomousEngine
from automation.radar import DynamicRadar
from automation.runner import (apply_focus_selection, evaluate_actual_entry, qualify_raw,
                               readiness_snapshot)
from automation.focus_selection import select_focus
from automation.readiness import authorization_valid, digest, policy_hash, security_state
from automation.decisions import BUILD_ID
from automation.policy import AutomationConfig
from core.settings import Config
from core.util import atomic_json
from core.broker import Rejected, Rest


class DiscoveryResumeTests(fixture.AuditFixture):
    build = fixture.SetupReachabilityTests.build

    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, risk_fraction=.02, max_daily_loss_fraction=.04,
                           max_positions=1, max_entries_per_day=3)
        # 기존 운영 설정 min_source_count=2에서도 관찰 입구가 열려야 한다.
        self.auto = replace(self.auto, min_source_count=2)
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy, self.root/'book',
                                  self.broker, lambda: self.clock[0])
        self.e.reconcile(self.snap)
        self.e.connected = True
        self.e.market = '3'
        self.e.market_status = {'999990': {'pass': True}}

    def item(self, sym='999990', sources=1, price=1000, rank=1):
        return {'code': sym, 'name': '합성', 'reference_price': price,
                'source_details': {str(i): {'observed_rank': rank, 'values': {}}
                                   for i in range(sources)}}

    def radar(self):
        r = DynamicRadar(self.cfg, self.auto, self.policy, fallback_source1=True)
        r.available_capital = 20000
        r.capital_context = self.e.capital_context()
        return r

    def eligible(self, row):
        return {'status': 'ELIGIBLE', 'market_status': {'pass': True},
                'overhead_share_estimate': .01, 'resistance': 2000}

    def focus_row(self, sym, sources=1, score=10):
        return {'code': sym, 'status': 'RESERVE', 'source_count': sources,
                'discovery_score': score, 'affordability': {'entry_eligible': True},
                'market_status': {'pass': True}}

    def fresh_radar(self):
        bars = test_v1.bars()
        for bar in bars:
            bar.update(open=900, high=950, low=880, close=900, volume=2000000)
        radar = Mock()
        radar.cache = {(self.clock[0].date(), '999990'): bars}
        return radar

    def source1_setup(self, pattern='A'):
        detector, tick, watch = self.build(pattern)
        watch.update(source_count=1, source_details={'one': {}}, fallback_source1=True)
        detector.ingest(tick, self.clock[0])
        qualify_raw(self.e, [watch], self.clock[0].isoformat())
        return detector, tick, watch

    def daily_approval(self):
        # 상태 경로도 실제 runner와 동일한 automation-production 장부를 사용한다.
        self.e = AutonomousEngine(self.cfg, self.auto, self.policy,
                                  self.root/'runtime/automation-production', self.broker,
                                  lambda: self.clock[0])
        self.e.reconcile(self.snap)
        self.e.connected = True
        self.e.market = '3'
        self.e.market_status = {'999990': {'pass': True}}
        self.e.last_regular_tick_at = self.clock[0]
        self.e.last_radar_at = self.clock[0]
        receipt = {'authorization_kind': 'DAILY_PREOPEN_V13',
                   'trading_day': self.clock[0].date().isoformat(), 'build_id': BUILD_ID,
                   'config_hash': self.cfg.fingerprint(), 'auto_hash': self.auto.fingerprint(),
                   'policy_hash': policy_hash(self.policy), 'security_state_hash': digest([])}
        atomic_json(self.root/'runtime/operator_user_authorization.json', receipt)
        def ready():
            snap = readiness_snapshot(self.e, {'subscription_acks': 1}, True, self.clock[0])
            if snap['state'] != 'OPEN_READY':
                return 'OPEN_READINESS_' + snap['missing'][0]
            # 실제 runner의 승인 경로와 동일한 컨텍스트를 사용한다.
            valid, reasons = authorization_valid(self.root, self.cfg, self.auto,
                self.policy, self.clock[0], **self.e.authorization_context())
            return '' if valid else 'DAILY_AUTHORIZATION_' + reasons[0]
        self.e.readiness_reason = ready

    def test_source1_compression_with_legacy_min2_competes_for_all_eight_slots(self):
        radar = self.radar()
        incumbents = [self.item(f'10000{i}', 2) for i in range(8)]
        evaluated = []
        def evaluate(row):
            evaluated.append(row['code'])
            return self.eligible(row)
        _, old, _ = radar.update(incumbents, evaluate, self.clock[0])
        self.assertEqual(len(old), 8)
        evaluated.clear()
        radar.momentum['999990'] = {'timestamp': self.clock[0].isoformat(),
            'gates': {k: {'result': 'PASS'} for k in ('acceleration','liquidity')}}
        rows, watch, changes = radar.update(incumbents+[self.item()], evaluate, self.clock[0])
        self.assertEqual(len(evaluated), 9)  # 중복 compression 평가 없음
        self.assertEqual(evaluated.count('999990'), 1)
        self.assertEqual(len(watch), 8)
        self.assertIn('999990', changes['added'])
        self.assertEqual(len(changes['removed']), 1)
        row = next(r for r in rows if r['code']=='999990')
        self.assertEqual(row['status'], 'WATCH')
        self.assertTrue(row['fallback_source1'])
        self.assertEqual(self.broker.orders, [])

    def test_source1_compression_reject_and_unknown_are_never_bypassed(self):
        for status in ('REJECT','UNKNOWN'):
            with self.subTest(status=status):
                evaluate = Mock(return_value={'status': status, 'reason': 'STRUCTURE_TEST'})
                rows, watch, _ = self.radar().update([self.item()], evaluate, self.clock[0])
                self.assertEqual(evaluate.call_count, 1)
                self.assertEqual(rows[0]['status'], status)
                self.assertFalse(watch)
                self.assertFalse(rows[0].get('fallback_source1'))

    def test_source1_basic_policy_economic_and_empty_source_gates_still_block(self):
        for item in (self.item('005930'), self.item('017900'), self.item('052260'),
                     self.item(price=20000), self.item(sources=0)):
            with self.subTest(symbol=item['code'], sources=len(item['source_details'])):
                evaluator = Mock(side_effect=self.eligible)
                _, watch, _ = self.radar().update([item], evaluator, self.clock[0])
                evaluator.assert_not_called()
                self.assertEqual(watch, [])

    def test_source_ranking_bonus_retained_without_admission_hard_gate(self):
        rows, watch, _ = self.radar().update(
            [self.item('999980', 2), self.item()], self.eligible, self.clock[0])
        self.assertEqual(len(watch), 2)
        scores = {r['code']: r['discovery_score'] for r in rows}
        self.assertGreater(scores['999980'], scores['999990'])

    def test_source1_focus_replaces_incumbent_by_tier_or_same_tier_quality(self):
        rows = [self.focus_row(f'10000{i}', 2, 20) for i in range(8)]
        old = {r['code']: {**r, 'status':'WATCH'} for r in rows}
        for score, tier in ((10, 0), (23, 3)):
            with self.subTest(score=score, tier=tier):
                new = self.focus_row('999990', 1, score)
                focus = select_focus(old, rows+[new], {'999990': {'tier': tier}})
                self.assertEqual(len(focus.next_watch), 8)
                self.assertIn('999990', focus.promoted)
                self.assertEqual(len(focus.demoted), 1)
        # 같은 tier에서 2점 이하 차이만으로 불필요한 교체를 하지 않는다.
        focus = select_focus(old, rows+[self.focus_row('999990', 1, 21)], {})
        self.assertFalse(focus.applied)

    def test_focus_promotion_synchronizes_watch_and_qualification_without_history_reset(self):
        detector, tick, watch = self.source1_setup()
        reserve = {**watch, 'status':'RESERVE', 'fallback_source1': False}
        qualify_raw(self.e, [reserve], self.clock[0].isoformat())
        previous = copy.deepcopy(list(detector.history['999990']))
        lookup = {'999980': self.focus_row('999980', 2, 1)}
        self.e.source_qualification['999980'] = {'fallback_source1':True}
        selection = select_focus(lookup, [reserve], {'999990':{'tier':0}}, max_watchlist=1)
        apply_focus_selection(self.e, lookup, {'999990':reserve}, selection)
        self.assertEqual(lookup['999990']['status'], 'WATCH')
        self.assertTrue(lookup['999990']['fallback_source1'])
        self.assertTrue(self.e.source_qualification['999990']['fallback_source1'])
        self.assertFalse(self.e.source_qualification['999980']['fallback_source1'])
        self.assertEqual(reserve['status'], 'RESERVE')
        self.assertEqual(list(detector.history['999990']), previous)
        self.assertEqual(self.broker.orders, [])
        order = evaluate_actual_entry(self.e, detector, tick, lookup['999990'],
                                      self.fresh_radar(), {}, Mock())
        self.assertIsNotNone(order)
        self.assertEqual(order['status'], 'ACCEPTED')
        self.assertTrue(order['signal']['fallback_source1'])

    def test_source1_unqualified_denied_even_new_min1_at_all_order_boundaries(self):
        self.auto = replace(self.auto, min_source_count=1)
        self.e.a = self.auto
        sig = {**self.signal(), 'source_count':1}
        self.assertIsNone(self.e.enter(sig, self.tick()))
        qualify_raw(self.e, [{'code':'999990','source_count':1}], self.clock[0].isoformat())
        self.assertEqual(self.e.new_entry_reason('999990'), 'SOURCE_COUNT_BUY_PERMISSION_DENIED')
        self.e.s['orders']['direct'] = {'local':'direct','id':'','symbol':'999990','side':'BUY',
            'qty':1,'limit':1002,'filled':0,'status':'INTENT','signal':sig}
        with self.assertRaisesRegex(Rejected, 'OBSERVATION_NOT_BUY_PERMISSION'):
            self.e.order_guard('kt10000', {'stk_cd':'999990','ord_qty':'1','ord_uv':'1002'})
        self.assertEqual(self.broker.orders, [])

    def test_qualified_source1_real_detector_A_B_C_reaches_unchanged_order_guard(self):
        for pattern in ('A','B','C'):
            with self.subTest(pattern=pattern):
                self.setUp()
                detector, tick, watch = self.source1_setup(pattern)
                signal = detector.signal(tick, watch)
                self.assertIsNotNone(signal)
                self.assertEqual(signal['pattern'], pattern)
                self.assertEqual(signal['evaluation_gates']['warmup']['result'], 'PASS')
                order = evaluate_actual_entry(self.e, detector, tick, watch,
                                              self.fresh_radar(), {}, Mock())
                self.assertIsNotNone(order)
                self.assertEqual(order['status'], 'ACCEPTED')
                self.assertEqual(len(self.broker.orders), 1)
                self.assertTrue(order['signal']['fallback_source1'])

    def test_qualified_source1_failed_strategy_gates_never_order(self):
        for failure in ('warmup','acceleration','liquidity','spread','structure','risk_reward','economic'):
            with self.subTest(failure=failure):
                self.setUp()
                detector, tick, watch = self.source1_setup()
                if failure=='warmup':
                    detector.history['999990'].clear()
                elif failure in ('acceleration','liquidity'):
                    detector.buffers['999990'].pop()
                    tick = replace(tick, volume=1)
                    detector.buffers['999990'].append(tick)
                elif failure=='spread':
                    tick = replace(tick, ask=tick.price+10, bid=tick.price-10)
                elif failure=='structure':
                    watch['status']='UNKNOWN'
                elif failure=='risk_reward':
                    watch['resistance']=1003
                else:
                    # 경제성 FAIL을 주입해 주문층까지 흐르지 않는지 확인한다.
                    with patch('automation.patterns.efficiency', return_value={'decision':'WAIT','inputs':{}}):
                        evaluate_actual_entry(self.e, detector, tick, watch, self.fresh_radar(), {}, Mock())
                    self.assertEqual(self.broker.orders, [])
                    continue
                evaluate_actual_entry(self.e, detector, tick, watch, self.fresh_radar(), {}, Mock())
                self.assertEqual(self.broker.orders, [])

    def test_source1_risk_and_cap_not_relaxed(self):
        self.assertEqual(self.e.risk_equity() * self.cfg.risk_fraction, 400)
        sig = {**self.signal(), 'source_count':1, 'fallback_source1':True,
               'limit':15000, 'stop':14500}
        details = self.e.quantity_details(sig, 20000, 20000, 800)
        self.assertEqual(details['capital_quantity'], 1)
        self.assertEqual(details['risk_quantity'], 0)
        qualify_raw(self.e, [{'code':'999990','source_count':1,'fallback_source1':True}], self.clock[0].isoformat())
        self.e.enter(sig, replace(self.tick(),price=15000,ask=15001,bid=14999))
        self.assertEqual(self.broker.orders, [])
        self.assertEqual(self.cfg.max_positions, 1)
        self.assertEqual(self.cfg.max_entries_per_day, 3)

    def test_daily_approval_allows_own_normal_intent_but_fail_closed_on_foreign_unknown_halt(self):
        for fault in ('NONE','INTENT','UNKNOWN','HALT'):
            with self.subTest(fault=fault):
                self.setUp()
                self.daily_approval()
                if fault=='HALT':
                    self.e.halt('SYNTHETIC_HALT')
                elif fault!='NONE':
                    self.e.s['orders']['foreign'] = {'local':'foreign','id':'','symbol':'999980',
                        'side':'BUY','qty':1,'limit':1002,'filled':0,'amount':0,'status':fault,
                        'signal':self.signal(), 'created':self.clock[0].isoformat()}
                    self.e.save()
                order = self.e.enter(self.signal(), self.tick())
                if fault=='NONE':
                    self.assertIsNotNone(order)
                    self.assertEqual(order['status'], 'ACCEPTED')
                    self.assertEqual(len(self.broker.orders), 1)
                    self.assertEqual(security_state(self.root), [])
                else:
                    self.assertFalse(order and order['status']=='ACCEPTED')
                    self.assertEqual(self.broker.orders, [])

    def test_default_and_example_watch_limit_eight_with_safe_examples(self):
        self.assertEqual(Config().max_watchlist, 8)
        self.assertEqual(AutomationConfig().min_source_count, 1)
        from core.settings import ROOT
        cfg = json.loads((ROOT/'config.example.json').read_text(encoding='utf-8-sig'))
        auto = json.loads((ROOT/'automation.example.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(cfg['max_watchlist'], 8)
        self.assertFalse(cfg['live_mode'])
        self.assertEqual(auto['AUTO_TRADING_CAP_KRW'], 0)
        self.assertFalse(auto['costs_confirmed'])

    def test_final_guard_still_blocks_new_foreign_intent_in_another_ledger(self):
        self.daily_approval()
        send = self.broker.submit
        def inject_foreign(*args):
            atomic_json(self.root/'runtime/automation-other/state.json', {
                'halt':'', 'orders':{'foreign':{'status':'INTENT'}}})
            return send(*args)
        with patch.object(self.broker, 'submit', side_effect=inject_foreign):
            order = self.e.enter(self.signal(), self.tick())
        self.assertEqual(order['status'], 'REJECTED')
        self.assertIn('CRITICAL_STATE_CHANGED', order['failure_reason'])
        self.assertEqual(self.broker.orders, [])
        self.assertEqual(self.e.authorization_context(), {})

    def test_own_intent_visible_to_readonly_checks_and_disk_mismatch_not_exempted(self):
        self.daily_approval()
        send = self.broker.submit
        def change_durable_intent(*args):
            # 일반 상태조회에는 자기 INTENT도 보인다. 예외는 제출 guard 내부뿐이다.
            self.assertTrue(security_state(self.root))
            valid, _ = authorization_valid(self.root, self.cfg, self.auto,
                                           self.policy, self.clock[0])
            self.assertFalse(valid)
            state = copy.deepcopy(self.e.s)
            own = next(o for o in state['orders'].values() if o['status']=='INTENT')
            own['qty'] += 1
            atomic_json(self.e.path, state)
            return send(*args)
        with patch.object(self.broker, 'submit', side_effect=change_durable_intent):
            order = self.e.enter(self.signal(), self.tick())
        self.assertEqual(order['status'], 'REJECTED')
        self.assertIn('CRITICAL_STATE_CHANGED', order['failure_reason'])
        self.assertEqual(self.broker.orders, [])

    def test_source1_cap0_hardlocks_and_reconcile_are_not_bypassed(self):
        sig = {**self.signal(), 'source_count':1, 'fallback_source1':True}
        for symbol in ('005930','017900','052260'):
            self.e.market_status[symbol] = {'pass':True}
            self.assertIsNone(self.e.enter({**sig,'symbol':symbol},replace(self.tick(),symbol=symbol)))
        self.e.account_ok=False
        self.assertIsNone(self.e.enter(sig, self.tick()))
        self.e.account_ok=True
        self.e.a=replace(self.auto,AUTO_TRADING_CAP_KRW=0)
        self.assertIsNone(self.e.enter(sig, self.tick()))
        self.assertEqual(self.broker.orders, [])

    def test_auth_session_identity_server_boundary_has_no_http_calls(self):
        opener = Mock()
        with patch.dict('os.environ', {'KIWOOM_APP_KEY':'synthetic-key','KIWOOM_APP_SECRET':'synthetic-secret'}):
            rest = Rest(self.cfg, opener=opener)
            with self.assertRaisesRegex(ValueError,'계좌/서버 불일치'):
                Rest(replace(self.cfg,broker_environment='mock'),opener=opener,
                     auth_session=rest.auth_session)
        opener.assert_not_called()

    def test_no_permission_created_by_shadow_watch_or_repeat_focus(self):
        detector, tick, watch = self.source1_setup()
        sig = detector.signal(tick, watch)
        for flags in ({'SHADOW_ONLY':True}, {'actual_order_permission':False}, {'diagnostic_only':True}):
            with self.subTest(flags=flags):
                self.assertIsNone(self.e.enter({**sig, **flags, 'fallback_source1':True}, tick))
        self.policy.command('ARMED')
        self.assertIsNone(evaluate_actual_entry(self.e, detector, tick, watch,
                                               self.fresh_radar(), {}, Mock()))
        self.assertEqual(self.broker.orders, [])

    def test_duplicate_entry_and_account_identity_boundary_preserved(self):
        detector, tick, watch = self.source1_setup()
        self.assertIsNotNone(evaluate_actual_entry(self.e, detector, tick, watch,
                                                  self.fresh_radar(), {}, Mock()))
        evaluate_actual_entry(self.e, detector, tick, watch, self.fresh_radar(), {}, Mock())
        self.assertEqual(len(self.broker.orders), 1)
        with patch.object(self.broker, 'identity', 'different-synthetic-account'):
            with self.assertRaisesRegex(RuntimeError, '계좌 불일치'):
                AutonomousEngine(self.cfg, self.auto, self.policy, self.root/'book',
                                 self.broker, lambda:self.clock[0])


if __name__ == '__main__':
    unittest.main()
