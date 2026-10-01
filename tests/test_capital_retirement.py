"""현재 설치 정책 검사. 실제 계좌/API 대신 기존 가짜 브로커와 합성 입력을 사용한다."""
import copy,json,time,unittest
from dataclasses import replace,asdict
from unittest.mock import patch,Mock
from tests import test_automation as f
from tests import test_operator_interface as ui
from automation.policy import legacy_test_release_enabled,AutomationConfig
from automation.radar import DynamicRadar,collect
from automation.candidate_audit import CandidateAudit
from automation.decisions import gate
from automation.allocation import efficiency
from core.util import atomic_json
from core.settings import Config
from operator_service import MENUS
from operator_console import handle,show_status

class CapitalRetirementTests(unittest.TestCase):
    make_engine=f.AutomationTests.make_engine
    tick=f.AutomationTests.tick
    def setUp(self):
        f.AutomationTests.setUp(self)
        p=patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',False); p.start(); self.addCleanup(p.stop)
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=3000,replacement_enabled=False)
        self.cfg=replace(self.cfg,max_position_fraction=1.0,max_positions=1)
        self.engine=self.make_engine(); self.snapshot={'cash':3420,'equity':3420,'holdings':{},'orders':[]}
        self.engine.reconcile(self.snapshot); self.engine.connected=True; self.engine.market='3'
    def small_signal(self,sym=f.SYMBOL,entry=2500,stop=2490):
        return {'symbol':sym,'limit':entry,'stop':stop,'pattern':'B','time':self.clock[0].isoformat(),
                'efficiency':efficiency(entry,stop,entry+300,.8,30,self.engine.costs)}
    def risk_permits(self):
        # 합성 "위험도 허용" 시나리오 전용. 설치 risk_fraction=.003은 변경하지 않는다.
        self.cfg=replace(self.cfg,risk_fraction=.01,max_daily_loss_fraction=.02)
        self.engine=self.make_engine(); self.engine.reconcile(self.snapshot); self.engine.connected=True; self.engine.market='3'
    def quantities(self,sig=None):
        return self.engine.quantity_details(sig or self.small_signal(),self.engine.risk_equity(),self.engine.s['cash'],60)
    def test_actual3420_cap3000_effective3000(self):
        self.assertEqual(self.engine.auto_available_capital(),3000); self.assertEqual(self.engine.watch_budget(),3000)
    def test_actual2000_cap3000_effective2000(self):
        self.engine.reconcile({**self.snapshot,'cash':2000})
        self.assertEqual(self.engine.auto_available_capital(),2000); self.assertEqual(self.engine.watch_budget(),2000)
        self.assertEqual(self.quantities()['capital_quantity'],0)
    def test_actual10000_never_increases_approved_cap(self):
        self.engine.reconcile({**self.snapshot,'cash':10000})
        self.assertEqual(self.engine.auto_available_capital(),3000); self.assertEqual(self.engine.a.capital_limit,3000)
        self.assertEqual(self.engine.s['automation']['approved_cap'],3000)
    def test_stale_account_blocks_buy_and_budget(self):
        self.risk_permits(); self.engine.last_account=time.monotonic()-16
        self.assertEqual(self.quantities()['reason_code'],'ACCOUNT_CAPITAL_UNAVAILABLE')
        self.assertEqual(self.engine.watch_budget(),0)
        self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(self.broker.orders,[])
    def test_failed_account_blocks_buy_even_recent_cache(self):
        self.risk_permits(); self.engine.account_ok=False
        self.assertEqual(self.quantities()['quantity'],0)
        self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(self.broker.orders,[])
    def test_2500_risk_permits_not_blocked_by_20percent_or_self_intent(self):
        self.risk_permits(); sizing=self.quantities(); self.assertEqual(sizing['quantity'],1)
        o=self.engine.enter(self.small_signal(),self.tick(2500))
        self.assertEqual(o['status'],'ACCEPTED'); self.assertEqual(self.broker.orders,[('BUY',f.SYMBOL,1,2500)])
    def test_2500_current_risk_rejects_with_precise_reason(self):
        s=self.quantities(); self.assertEqual(s['capital_quantity'],1); self.assertEqual(s['risk_quantity'],0)
        self.assertEqual(s['reason_code'],'RISK_BUDGET_ZERO_QTY'); self.assertEqual(s['risk_budget'],9)
        self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(self.broker.orders,[])
        events=[json.loads(x) for x in (self.engine.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([x for x in events if x['event']=='ENTRY_EVAL'][-1]['primary_reason_code'],'RISK_BUDGET_ZERO_QTY')
    def test_far_invalidation_still_zero_with_affordable_price(self):
        self.risk_permits(); s=self.quantities(self.small_signal(stop=2400))
        self.assertEqual(s['capital_quantity'],1); self.assertEqual(s['quantity'],0); self.assertEqual(s['reason_code'],'RISK_BUDGET_ZERO_QTY')
    def test_3100_one_share_unaffordable(self):
        self.risk_permits(); s=self.quantities(self.small_signal(entry=3100,stop=3090))
        self.assertEqual(s['quantity'],0); self.assertEqual(s['reason_code'],'ONE_SHARE_UNAFFORDABLE')
    def test_buy_cost_prevents_exact3000_share(self):
        self.assertEqual(self.quantities(self.small_signal(entry=3000,stop=2990))['capital_quantity'],0)
    def test_pending_buy_reserves_single_slot_and_total_cap(self):
        self.risk_permits(); self.engine.enter(self.small_signal(),self.tick(2500))
        self.engine.enter(self.small_signal('999980'),self.tick(2500,'999980'))
        self.assertEqual(len(self.broker.orders),1); self.assertLessEqual(sum(q*p*(1+self.auto.commission_buy) for side,s,q,p in self.broker.orders),3000)
    def test_unknown_order_not_resent_on_restart(self):
        self.risk_permits()
        with patch.object(self.broker,'submit',side_effect=TimeoutError('synthetic lost response')) as send:
            o=self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(o['status'],'UNKNOWN')
            self.engine=self.make_engine(); self.engine.reconcile(self.snapshot); self.engine.connected=True; self.engine.market='3'
            self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(send.call_count,1)
    def test_locked_buy_and_forced_sell_zero(self):
        self.risk_permits()
        for sym in ('005930','017900'):
            self.engine.enter(self.small_signal(sym),self.tick(2500,sym))
            self.engine.submit('BUY',sym,1,2500,self.small_signal(sym))
            self.engine.s['positions'][sym]={'ownership':'BOT','qty':1,'qty_blocked':False,'avg':2500,'stop':2490}
            self.assertIsNone(self.engine.submit('SELL',sym,1,2500))
        self.assertEqual(self.broker.orders,[])
    def test_zero_cap_no_buy_but_keeps_signal_log(self):
        self.auto=replace(self.auto,AUTO_TRADING_CAP_KRW=0); self.engine=self.make_engine(); self.engine.reconcile(self.snapshot)
        self.engine.connected=True; self.engine.market='3'; self.engine.enter(self.small_signal(),self.tick(2500))
        self.assertEqual(self.broker.orders,[]); self.assertFalse(self.engine.s['positions'])
        text=(self.engine.folder/'events.jsonl').read_text(encoding='utf-8'); self.assertIn('AUTO_CAP_ZERO',text); self.assertIn('signal_detected": true',text)
    def item(self,sym,price):
        return {'code':sym,'name':'합성','reference_price':price,'source_details':{s:{'observed_rank':1,'values':{}} for s in ('ka10027','ka10030')}}
    def test_affordable_candidates_keep_structure_evaluation(self):
        radar=DynamicRadar(self.cfg,self.auto,self.policy); radar.available_capital=self.engine.watch_budget(); radar.capital_context=self.engine.capital_context()
        evaluated=[]
        def evaluate(x): evaluated.append(x['code']); return {'status':'ELIGIBLE','reason':'구조 필터 통과','overhead_share_estimate':.1}
        rows,watch,_=radar.update([self.item('999990',3100),self.item('999980',2500),self.item('267320',2700)],evaluate,self.clock[0])
        self.assertEqual(evaluated,['999980','267320']); self.assertEqual(rows[0]['reason'],'ONE_SHARE_UNAFFORDABLE')
        self.assertEqual({r['code'] for r in watch},{'999980','267320'})
    def test_unfresh_discovery_not_mislabeled_unaffordable(self):
        radar=DynamicRadar(self.cfg,self.auto,self.policy); radar.available_capital=0; radar.capital_context={'account_fresh':False}
        evaluate=Mock(); rows,watch,_=radar.update([self.item('999980',2500)],evaluate,self.clock[0])
        self.assertEqual(rows[0]['reason'],'ACCOUNT_CAPITAL_UNAVAILABLE'); evaluate.assert_not_called(); self.assertFalse(watch)
    def test_existing_five_sources_unchanged_no_price_preference(self):
        from candidate_builder import RANKS
        rest=Mock(); rest.pages.return_value=([{'stk_cd':'999990','stk_nm':'합성','cur_prc':'2500'}],{})
        rows=collect(rest); self.assertEqual(rest.pages.call_count,5); self.assertEqual(len(rows[0]['source_details']),5)
        self.assertEqual(len(RANKS),5)
    def test_retired_direct_policy_approve_and_cancel_fail(self):
        self.assertFalse(legacy_test_release_enabled())
        with self.assertRaisesRegex(ValueError,'RETIRED'): self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        with self.assertRaisesRegex(ValueError,'RETIRED'): self.policy.cancel_test_release('CANCEL_TEST_CAPITAL_RELEASE')
        self.assertEqual(self.broker.orders,[])
    def test_retired_forged_approval_no_external_sell(self):
        self.policy.state['test_capital_release']={'symbol':'267320','quantity':1,'side':'SELL','max_qty':1,'one_shot':True,'status':'APPROVED'}
        atomic_json(self.policy.path,self.policy.state)
        self.engine.reconcile({**self.snapshot,'holdings':{'267320':{'qty':1,'available':1,'avg':2855}},
          'books':{'267320':{'bid':2700,'bid_qty':10,'regular':True,'status':{'pass':True},'time':self.clock[0].isoformat()}}})
        self.engine.evaluate_existing(); self.engine.submit('SELL','267320',1,2700,{'ownership':'EXISTING_POLICY','policy':'TEST_CAPITAL_RELEASE'})
        self.assertEqual(self.broker.orders,[]); self.assertFalse(self.engine.test_release_authorized('kt10001',{'stk_cd':'267320'}))
    def test_ninetech_ordinary_bot_buy_sell_use_general_core(self):
        self.risk_permits(); self.engine.market_status['267320']={'pass':True}
        o=self.engine.enter(self.small_signal('267320'),self.tick(2500,'267320')); self.assertEqual(o['status'],'ACCEPTED')
        self.engine.cumulative_fill(o,1,2500); self.engine.actual={'267320':{'qty':1,'available':1,'avg':2500}}
        self.engine.request_exit('267320','合성 정상청산'); self.engine.drive_exits({'267320':2490})
        self.assertEqual([o[0] for o in self.broker.orders],['BUY','SELL'])
    def test_completed_history_recovery_does_not_rewrite_approval_or_orders(self):
        with patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',True):
            self.policy.command('TEST_CAPITAL_RELEASE','267320','TEST_CAPITAL_RELEASE')
        approval=self.policy.state['test_capital_release']; approval.update(status='COMPLETED',filled_qty=1,order_local='historic',completed_at='2026-10-01T13:45:00+09:00')
        atomic_json(self.policy.path,self.policy.state); before=self.policy.path.read_bytes()
        order={'local':'historic','symbol':'267320','side':'SELL','qty':1,'filled':1,'amount':2700,'status':'FILLED','id':'REAL-HISTORY','signal':{'ownership':'EXISTING_POLICY','policy':'TEST_CAPITAL_RELEASE','starting_qty':1}}
        self.engine.s['orders']['historic']=copy.deepcopy(order); self.engine.s['automation']['capital_refresh_pending']=False
        self.engine.recover_test_release(); self.assertEqual(self.policy.path.read_bytes(),before); self.assertEqual(self.engine.s['orders']['historic'],order)
    def test_audit_unique_counts_and_reason_code_preservation(self):
        audit=CandidateAudit(); rows=[{**self.item('999990',2500),'status':'WATCH','affordability':{'entry_eligible':True}},
            {**self.item('999980',3100),'status':'REJECT','reason':'ONE_SHARE_UNAFFORDABLE','affordability':{'reason_code':'ONE_SHARE_UNAFFORDABLE','entry_eligible':False}}]
        audit.begin(rows,self.clock[0].isoformat(),self.engine.capital_context())
        gates={'liquidity':gate(False,turnover=10),'spread':gate(False,spread=.1),'risk_quantity':gate(False,risk_quantity=0,risk_per_share=30)}
        for _ in range(50): audit.observe('999990','RISK_BUDGET_ZERO_QTY',gates,'NO_BUY')
        s=audit.summary(self.clock[0].isoformat()); self.assertEqual((s['raw_candidates'],s['affordable'],s['unaffordable']),(2,1,1))
        self.assertEqual((s['liquidity_fail'],s['spread_fail'],s['risk_zero_qty']),(1,1,1)); self.assertEqual(s['reason_counts']['RISK_BUDGET_ZERO_QTY'],1)
        self.assertEqual(s['final_entry_eligible'],0); self.assertEqual(s['not_yet_evaluated'],1)
    def test_audit_empty_and_new_cycle_do_not_claim_gate_pass(self):
        a=CandidateAudit(); self.assertEqual(a.summary('t')['raw_candidates'],0)
        a.begin([self.item('999990',2500)],'t',{}); self.assertEqual(a.summary('t')['final_entry_eligible'],0)
        self.assertEqual(a.summary('t')['not_yet_evaluated'],1)
    def test_audit_tracks_suppressed_same_decisions_without_tick_log_growth(self):
        a=CandidateAudit(); a.begin([self.item(f.SYMBOL,2500)],'t',self.engine.capital_context()); self.engine.candidate_audit=a
        gates={'structure':gate(True,price=2500),'pattern':gate(False,setup='B')}
        before=len((self.engine.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()) if (self.engine.folder/'events.jsonl').exists() else 0
        for _ in range(20): self.engine.decisions.emit('ENTRY_EVAL',f.SYMBOL,'NO_BUY','SETUP_NOT_READY',gates)
        after=len((self.engine.folder/'events.jsonl').read_text(encoding='utf-8').splitlines())
        self.assertEqual(after-before,1); self.assertEqual(a.summary('t')['setup_not_ready'],1)
        self.assertEqual(a.summary('t')['reason_counts']['SETUP_NOT_READY'],1)
    def test_final_guard_rechecks_cash_fall_during_submit(self):
        self.risk_permits(); original=self.broker.submit
        def falling(side,sym,qty,price):
            self.engine.available_cash=2000
            return original(side,sym,qty,price)
        with patch.object(self.broker,'submit',side_effect=falling): o=self.engine.enter(self.small_signal(),self.tick(2500))
        self.assertEqual(o['status'],'REJECTED'); self.assertEqual(self.broker.orders,[]); self.assertEqual(self.engine.a.capital_limit,3000)
    def test_no_new_buy_after_cutoff_with_available_full_cap(self):
        self.risk_permits(); self.clock[0]=self.clock[0].replace(hour=15,minute=5,second=0)
        self.engine.enter(self.small_signal(),self.tick(2500)); self.assertEqual(self.broker.orders,[])
    def test_query_started_age_not_reset_to_fresh_on_response(self):
        from datetime import timedelta
        self.engine.reconcile({**self.snapshot,'query_started_at':(self.clock[0]-timedelta(seconds=16)).isoformat()})
        self.assertFalse(self.engine.capital_account_valid()); self.assertEqual(self.quantities()['quantity'],0)
    def test_invalid_cash_fails_closed_without_replacing_capital(self):
        for value in (float('nan'),float('inf'),-1,'3420',None):
            with self.assertRaises(ValueError): self.engine.reconcile({**self.snapshot,'cash':value})
            self.assertFalse(self.engine.account_ok); self.assertEqual(self.engine.available_cash,3420)
        self.assertEqual(self.engine.a.capital_limit,3000); self.assertEqual(self.broker.orders,[])
    def test_full_position_allocation_config_valid_risk_limit_unchanged(self):
        self.cfg.validate()
        self.assertEqual(self.cfg.risk_fraction,.003)
        with self.assertRaises(ValueError): replace(self.cfg,max_position_fraction=1.01).validate()
        with self.assertRaises(ValueError): replace(self.cfg,risk_fraction=1).validate()

class RetiredMenuTests(unittest.TestCase):
    save_config=ui.OperatorInterfaceTests.save_config
    save_auto=ui.OperatorInterfaceTests.save_auto
    def setUp(self):
        ui.OperatorInterfaceTests.setUp(self)
        p=patch('automation.policy.LEGACY_TEST_RELEASE_ENABLED',False); p.start(); self.addCleanup(p.stop)
    def test_menu_has_no_5_or_10_invalid_input_does_not_mutate(self):
        self.assertNotIn('5',MENUS); self.assertNotIn('10',MENUS); before=self.policy.path.read_bytes()
        for choice in ('5','10'):
            with self.assertRaises(ValueError): handle(self.s,choice,reader=lambda _: 'yes',writer=lambda _:None)
        self.assertEqual(self.policy.path.read_bytes(),before); self.assertEqual(self.gateway.orders,[])
    def test_service_release_cancel_inaccessible(self):
        with self.assertRaisesRegex(ValueError,'RETIRED'): self.s.release(True)
        with self.assertRaisesRegex(ValueError,'RETIRED'): self.s.cancel_test_release(True)
        self.assertEqual(self.gateway.orders,[])
    def test_config_migration_only_exact_two_user_approved_fields(self):
        self.save_auto(AUTO_TRADING_CAP_KRW=3000,costs_confirmed=True); self.save_config(live_mode=True)
        old,auto,_=self.s.settings(); previous={'config_hash':old.fingerprint()}
        self.save_config(max_position_fraction=1.0,max_positions=1); cfg,auto,_=self.s.settings()
        self.assertFalse(self.s.resume_config_matches(cfg,auto,previous))
        atomic_json(self.root/'runtime/operator_config_change.json',{'scope':'USER_APPROVED_SMALL_CAP_SINGLE_POSITION',
            'from_hash':old.fingerprint(),'to_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint()})
        self.assertTrue(self.s.resume_config_matches(cfg,auto,previous))
        self.assertFalse(self.s.resume_config_matches(replace(cfg,risk_fraction=.01),auto,previous))
        self.assertFalse(self.s.resume_config_matches(cfg,replace(auto,AUTO_TRADING_CAP_KRW=5000),previous))
    def test_status_statistics_read_only(self):
        sample=CandidateAudit().summary(self.clock[0].isoformat(),{'effective_auto_cap':0})
        atomic_json(self.root/'runtime/candidate_summary.json',sample); before=(self.root/'runtime/candidate_summary.json').read_bytes()
        out=[]; show_status(self.s.public_status(),out.append)
        self.assertIn('원시 후보','\n'.join(out)); self.assertEqual((self.root/'runtime/candidate_summary.json').read_bytes(),before)
    def test_retired_runner_purpose_blocks_before_supervisor(self):
        import operator_runner
        supervise=Mock()
        with self.assertRaisesRegex(ValueError,'RETIRED'):
            operator_runner.run(self.root,clock=lambda:self.clock[0],supervise=supervise,purpose='TEST_CAPITAL_RELEASE')
        supervise.assert_not_called(); self.assertFalse((self.root/'runtime/operator_runner.json').exists())

if __name__=='__main__': unittest.main()
