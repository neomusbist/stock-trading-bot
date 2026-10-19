"""최신 정책의 금지 경계/실제 누계/재시작을 가짜 브로커로 검증한다. 네트워크 없음."""
import json
import time
import unittest
from datetime import timedelta
from dataclasses import replace
from unittest.mock import Mock,patch
from tests import test_automation as fixture
SYMBOL=fixture.SYMBOL
from automation.policy import BREAKEVEN_EXITS,TEST_RELEASE_SYMBOL,Policy,AutomationConfig
from automation.market_status import assess_status
from automation.radar import DynamicRadar,RadarWorker
from automation.runner import AccountWorker
from core.broker import Rest,Rejected
from contextlib import contextmanager
from core.util import atomic_json

class PolicyUpdateTests(unittest.TestCase):
    setUp=fixture.AutomationTests.setUp
    make_engine=fixture.AutomationTests.make_engine
    tick=fixture.AutomationTests.tick
    signal=fixture.AutomationTests.signal
    buy=fixture.AutomationTests.buy

    @contextmanager
    def mock_breakeven(self,symbol='052260',price=5834,max_qty=13):
        with patch.dict('automation.policy.BREAKEVEN_EXITS',{symbol:price}), \
             patch.dict('automation.existing.BREAKEVEN_EXITS',{symbol:price}), \
             patch.dict('automation.policy.BREAKEVEN_MAX_INVENTORY',{symbol:max_qty}), \
             patch.dict('automation.existing.BREAKEVEN_MAX_INVENTORY',{symbol:max_qty}), \
             patch('automation.policy.HARD_LOCKS',{'005930','017900'}), \
             patch('automation.engine.HARD_LOCKS',{'005930','017900'}):
            yield

    def holding(self,sym='052260',qty=3,bid=5840,depth=None,available=None):
        h={'qty':qty,'available':qty if available is None else available,'avg':5800}
        book={'time':self.clock[0].isoformat(),'bid':bid,'bid_qty':qty if depth is None else depth,
              'regular':True,'status':{'pass':True,'reason_code':'STATUS_NO_KNOWN_SPECIAL_FLAG'}}
        snap={'cash':1000000,'equity':9000000,'orders':[],'holdings':{sym:h},'books':{sym:book}}
        self.engine.reconcile(snap); return snap

    def events(self):
        path=self.engine.folder/'events.jsonl'
        return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_samsung_manual_hold_still_blocks_all_orders(self):
        self.holding('005930',bid=100000)
        self.engine.timer(); self.engine.enter(self.signal('005930'),self.tick(sym='005930'))
        self.assertFalse(self.broker.orders); self.assertTrue(self.policy.locked('005930'))
        self.assertIsNone(self.engine.submit('SELL','005930',3,100000,{'ownership':'EXISTING_POLICY'}))

    def test_gwang_hard_lock_no_new_buy_and_no_exit(self):
        self.assertTrue(self.policy.locked('017900'))
        self.engine.enter(self.signal('017900'),self.tick(sym='017900'))
        self.assertFalse(self.broker.orders)
        self.holding('017900',bid=13000); cash=self.engine.s['cash']; self.engine.timer()
        self.assertEqual(self.broker.orders,[])
        self.assertFalse(self.engine.s['orders']); self.assertEqual(self.engine.s['cash'],cash)
        self.assertFalse(self.engine.s['positions']); self.assertFalse(self.engine.s['trades'])

    def test_hyundai_hard_lock_no_new_buy_and_no_exit(self):
        self.assertTrue(self.policy.locked('052260'))
        self.engine.enter(self.signal('052260'),self.tick(sym='052260'))
        self.assertFalse(self.broker.orders)
        self.holding('052260',bid=5840); cash=self.engine.s['cash']; self.engine.timer()
        self.assertEqual(self.broker.orders,[])
        self.assertFalse(self.engine.s['orders']); self.assertEqual(self.engine.s['cash'],cash)
        self.assertFalse(self.engine.s['positions']); self.assertFalse(self.engine.s['trades'])

    def test_hyundai_breakeven_rounds_up_to_valid_tick(self):
        with self.mock_breakeven():
            self.holding('052260',bid=5840); cash=self.engine.s['cash']; self.engine.timer()
            self.assertEqual(self.broker.orders,[('SELL','052260',3,5840)])
            o=list(self.engine.s['orders'].values())[-1]
            self.assertNotEqual(o['status'],'FILLED')
            self.engine.cumulative_fill(o,3,3*5840)
            self.assertEqual(o['status'],'FILLED'); self.assertEqual(self.engine.s['cash'],cash)
            self.assertFalse(self.engine.s['positions']); self.assertFalse(self.engine.s['trades'])

    def test_hyundai_below_threshold_no_sell_and_no_average_down(self):
        with self.mock_breakeven():
            self.holding(bid=5830); self.engine.on_tick(self.tick(price=13000,sym='052260')); self.engine.timer()
            self.assertFalse(self.broker.orders)
            self.engine.enter(self.signal('052260'),self.tick(sym='052260'))
            self.assertFalse(self.broker.orders)

    def test_hyundai_opportunity_cost_disconnected_even_live_positive_cap(self):
        self.holding('052260',bid=5840)
        evidence={'decision':'REPLACE','hold_efficiency':{'score':0},'new_after_existing_sell_cost':999,
                  'candidate_efficiency':{'decision':'TRADE'},'held':{'support':5000}}
        self.assertIsNone(self.engine.replace_legacy(evidence)); self.assertFalse(self.broker.orders)

    def test_insufficient_depth_or_available_no_sell(self):
        self.holding(depth=2); self.engine.timer(); self.assertFalse(self.broker.orders)
        self.holding(available=2); self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_missing_or_stale_book_hold(self):
        snap=self.holding(); self.engine.books={}; self.engine.timer(); self.assertFalse(self.broker.orders)
        self.engine.books=snap['books']; self.clock[0]+=timedelta(seconds=6); self.engine.timer()
        self.assertFalse(self.broker.orders)

    def test_external_market_unknown_auction_and_outside_no_sale(self):
        self.holding()
        for market in ('UNKNOWN','2','4'):
            self.engine.market=market; self.engine.timer()
        self.engine.market='3'; self.clock[0]=self.clock[0].replace(hour=15,minute=20)
        self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_armed_zero_cap_blocks_all_external_sales(self):
        self.holding(); self.policy.command('ARMED'); self.engine.timer()
        self.assertFalse(self.broker.orders)

    def test_generic_bot_exit_cannot_bypass_existing_policy(self):
        self.holding()
        self.engine.s['positions']['052260']={'ownership':'BOT','qty':3,'qty_blocked':False}
        self.assertIsNone(self.engine.submit('SELL','052260',3))

    def test_partial_external_fill_reconcile_retry_only_remaining(self):
        with self.mock_breakeven():
            snap=self.holding(); self.engine.timer(); o=list(self.engine.s['orders'].values())[-1]
            self.engine.cumulative_fill(o,1,5840)
            self.engine.cumulative_fill(o,1,5840)
            self.assertEqual(self.engine.s['automation']['existing']['052260']['qty'],2)
            self.assertEqual(len(self.engine.s['automation']['external_exit_fills']),1)
            self.assertEqual(o['status'],'ACCEPTED')
            o['status']='CANCEL_SENT'
            snap['holdings']['052260'].update(qty=2,available=2)
            self.engine.reconcile(snap); self.engine.timer()
            self.assertEqual(self.broker.orders[-1],('SELL','052260',2,5840))

    def test_external_duplicate_and_old_account_snapshot_do_not_resell(self):
        with self.mock_breakeven():
            snap=self.holding(); self.engine.timer(); o=list(self.engine.s['orders'].values())[-1]
            self.engine.submit('SELL','052260',3,5840,o['signal'])
            self.assertEqual(len(self.broker.orders),1)
            self.engine.cumulative_fill(o,3,3*5840)
            self.engine.reconcile(snap); self.engine.timer()
            self.assertEqual(len(self.broker.orders),1)

    def test_ninetech_default_external_holding_not_sold(self):
        self.holding(TEST_RELEASE_SYMBOL,qty=1,bid=2000); self.engine.timer()
        self.assertFalse(self.broker.orders)

    def test_test_release_exact_explicit_confirmation_only(self):
        for sym,confirm in (('052260','TEST_CAPITAL_RELEASE'),(TEST_RELEASE_SYMBOL,None)):
            with self.assertRaises(ValueError): self.policy.command('TEST_CAPITAL_RELEASE',sym,confirm)
        self.assertNotIn('test_capital_release',self.policy.state)

    def test_test_release_one_order_fill_closes_policy_cap_stays_unchanged(self):
        snap=self.holding(TEST_RELEASE_SYMBOL,qty=1,bid=2000); cap=self.engine.a.capital_limit; cash=self.engine.s['cash']
        self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE')
        self.engine.timer(); self.engine.timer(); self.assertEqual(len(self.broker.orders),1)
        o=list(self.engine.s['orders'].values())[-1]
        self.engine.cumulative_fill(o,1,2000); self.policy.refresh()
        self.assertEqual(self.policy.state['test_capital_release']['status'],'COMPLETED')
        self.assertEqual(self.engine.a.capital_limit,cap); self.assertEqual(self.engine.s['cash'],cash)
        with self.assertRaises(ValueError): self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE')
        self.engine.reconcile(snap); self.engine.timer(); self.assertEqual(len(self.broker.orders),1)

    def test_test_release_disallowed_conditions_table(self):
        # 1. more than 1 share not assumed test inventory
        self.holding(TEST_RELEASE_SYMBOL,qty=2,bid=2000)
        self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE')
        self.engine.timer(); self.assertFalse(self.broker.orders)
        # 2. approved but OFF mode no sale
        self.setUp()
        self.holding(TEST_RELEASE_SYMBOL,qty=1,bid=2000)
        self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE'); self.policy.command('OFF')
        self.engine.timer(); self.assertFalse(self.broker.orders)
        # 3. crash reserved submitting status not reused
        self.setUp()
        self.holding(TEST_RELEASE_SYMBOL,qty=1,bid=2000)
        self.policy.command('TEST_CAPITAL_RELEASE',TEST_RELEASE_SYMBOL,'TEST_CAPITAL_RELEASE')
        self.policy.state['test_capital_release']['status']='SUBMITTING'; atomic_json(self.policy.path,self.policy.state)
        self.engine.timer(); self.assertFalse(self.broker.orders)

    def test_entry_cutoff_exact_boundary_and_default_flatten(self):
        self.clock[0]=self.clock[0].replace(hour=14,minute=54,second=59)
        self.assertTrue(self.engine.entry_window())
        self.clock[0]+=timedelta(seconds=1); self.assertFalse(self.engine.entry_window())
        self.assertFalse(self.engine.session_ending())
        self.clock[0]=self.clock[0].replace(hour=15,minute=10); self.assertTrue(self.engine.session_ending())

    def test_session_cutoff_and_forced_flatten_behavior(self):
        # 1. forced flatten
        self.buy(); self.clock[0]=self.clock[0].replace(hour=15,minute=10)
        p=self.engine.s['positions'][SYMBOL]; p['last_tick']=self.clock[0].isoformat(); self.engine.last_account=time.monotonic()
        self.engine.timer(); self.assertTrue(p['exit_requested']); self.assertEqual(self.broker.orders[-1][0],'SELL')
        self.assertEqual(p['exit_reason'],'당일 청산 시각')
        # 2. cutoff cancels pending buy
        self.setUp()
        self.engine.enter(self.signal(),self.tick()); self.clock[0]=self.clock[0].replace(hour=15,minute=5)
        self.engine.timer(); self.assertEqual(len(self.broker.cancels),1)

    def test_session_order_config_and_overnight_opt_in(self):
        self.engine.a=replace(self.auto,ALLOW_OVERNIGHT=True)
        self.clock[0]=self.clock[0].replace(hour=15,minute=10)
        self.assertFalse(self.engine.session_ending()); self.assertFalse(self.engine.entry_window())
        for config in (replace(self.auto,new_entry_cutoff='15:10:00'),replace(self.auto,force_flatten_start='15:20:00'),replace(self.auto,ALLOW_OVERNIGHT=1)):
            with self.assertRaises(ValueError): config.validate()

    def test_no_cancel_or_exit_network_at_1520_even_pending(self):
        self.engine.enter(self.signal(),self.tick()); self.clock[0]=self.clock[0].replace(hour=15,minute=20)
        self.engine.timer(); self.assertFalse(self.broker.cancels); self.assertEqual(len(self.broker.orders),1)

    def test_unknown_status_blocks_even_live_with_positive_cap(self):
        self.engine.market_status={}; self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)

    def test_special_status_blocks_at_final_order_boundary(self):
        o=self.engine.enter(self.signal(),self.tick()); o['status']='INTENT'
        self.engine.market_status[SYMBOL]=assess_status({'state':'거래정지','auditInfo':'정상'})
        with self.assertRaises(Rejected): self.engine.order_guard('kt10000',{'stk_cd':SYMBOL,'ord_qty':str(o['qty']),'ord_uv':str(o['limit'])})

    def test_entry_logs_no_buy_with_values_and_throttles(self):
        self.policy.command('ARMED')
        for _ in range(100): self.engine.enter(self.signal(),self.tick())
        rows=[r for r in self.events() if r['event']=='ENTRY_EVAL']; self.assertEqual(len(rows),1)
        row=rows[0]; self.assertEqual(row['decision'],'NO_BUY'); self.assertEqual(row['gates']['policy']['result'],'FAIL')
        for k in ('timestamp','strategy_version','name','state','primary_reason_code','additional_reason_codes','entry_price','current_price','invalidation','protection','quantity','MFE','MAE','costs','net_pnl_completed'): self.assertIn(k,row)
        self.assertEqual(row['gates']['policy']['values']['approved_cap'],1000000)
        self.clock[0]+=timedelta(seconds=30); self.engine.enter(self.signal(),self.tick())
        self.assertEqual(len([r for r in self.events() if r['event']=='ENTRY_EVAL']),2)

    def test_hold_stop_change_and_completed_exit_logs(self):
        self.buy(); self.engine.on_tick(self.tick(10200)); self.engine.on_tick(self.tick(10300))
        rows=[r for r in self.events() if r['event']=='POSITION_EVAL']; self.assertGreaterEqual(len(rows),1)
        self.assertEqual(rows[-1]['decision'],'HOLD'); self.assertIsNotNone(rows[-1]['MFE'])
        p=self.engine.s['positions'][SYMBOL]; self.engine.on_tick(self.tick(p['stop']-20))
        o=list(self.engine.s['orders'].values())[-1]; self.engine.cumulative_fill(o,o['qty'],o['qty']*(p['stop']-20))
        last=[r for r in self.events() if r['event']=='EXIT_EVAL'][-1]
        self.assertEqual(last['primary_reason_code'],'BOT_FILL_COMPLETE'); self.assertIsNotNone(last['net_pnl_completed'])

    def test_manual_and_external_inventory_protection_table(self):
        # 1. external inventory over depth limit (qty=14)
        self.holding(qty=14,depth=100); self.engine.timer(); self.assertFalse(self.broker.orders)
        # 2. cap0 cannot sell existing
        self.holding(); self.engine.a=replace(self.auto,AUTO_TRADING_CAP_KRW=0)
        self.engine.timer(); self.assertFalse(self.broker.orders)
        # 3. stop/stop_flat/kill never force sell manual inventory
        self.holding(bid=13000)
        for mode in ('STOP','STOP_FLAT','KILL'):
            self.policy.command(mode); self.engine.controls(); self.engine.timer()
        self.assertFalse(self.broker.orders); self.assertFalse(self.engine.s['positions'])

    def test_next_day_residual_restores_exit_intent_waits_reconcile(self):
        self.buy(); qty=self.engine.s['positions'][SYMBOL]['qty']; self.clock[0]+=timedelta(days=1)
        restored=self.make_engine(); self.assertTrue(restored.s['positions'][SYMBOL]['exit_requested'])
        restored.timer(); self.assertEqual(len(self.broker.orders),1)
        restored.reconcile({'cash':999000,'equity':1000000,'holdings':{SYMBOL:{'qty':qty,'available':qty,'avg':10020}},'orders':[]})
        restored.market='3'; restored.connected=True; restored.timer({SYMBOL:10000})
        self.assertEqual(self.broker.orders[-1][0],'SELL')

    def test_session_end_preserves_real_quantity_and_order_intent(self):
        self.buy(); self.clock[0]=self.clock[0].replace(hour=15,minute=20)
        qty=self.engine.s['positions'][SYMBOL]['qty']; self.engine.timer(); self.engine.save()
        state=json.loads(self.engine.path.read_text(encoding='utf-8')); self.assertEqual(state['positions'][SYMBOL]['qty'],qty)
        self.assertEqual(len(self.broker.orders),1)

    def test_invalid_inputs_and_bad_symbol_rejected_before_intent(self):
        for value in (None,0,-1,float('nan')):
            sig=self.signal(); sig['stop']=value
            self.assertIsNone(self.engine.enter(sig,self.tick()))
        self.assertFalse(self.broker.orders)
        self.assertTrue(any(r.get('primary_reason_code')=='INVALIDATION_UNKNOWN_OR_INVALID' for r in self.events()))
        self.engine.enter(self.signal('bad'),self.tick(sym='bad')); self.assertFalse(self.engine.s['orders'])

    def test_corrupt_state_is_not_silently_reconstructed(self):
        self.engine.path.write_text('{incomplete',encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError): self.make_engine()
        self.assertFalse(self.broker.orders)

    def test_stale_or_future_market_status_no_new_order(self):
        for age in (181,-10):
            self.engine.market_status[SYMBOL]={'pass':True,'checked_at':(self.clock[0]-timedelta(seconds=age)).isoformat()}
            self.engine.enter(self.signal(),self.tick())
        self.assertFalse(self.broker.orders)

    def test_pending_external_restart_no_second_order(self):
        with self.mock_breakeven():
            snap=self.holding(); self.engine.timer()
            restored=self.make_engine(); restored.market='3'; restored.connected=True
            o=list(restored.s['orders'].values())[-1]
            snap['orders']=[{'ord_no':o['id'],'stk_cd':'052260','oso_qty':str(o['qty']),'cntr_tot_amt':'0'}]
            restored.reconcile(snap); restored.timer(); self.assertEqual(len(self.broker.orders),1)

    def test_complete_trade_costs_and_price_band_are_analysis_only(self):
        from automation.performance import report
        self.buy(); p=self.engine.s['positions'][SYMBOL]; q=p['qty']
        self.engine.request_exit(SYMBOL,'사용자 STOP_FLAT'); self.engine.drive_exits(); o=list(self.engine.s['orders'].values())[-1]
        self.engine.cumulative_fill(o,1,10200)
        self.engine.cumulative_fill(o,q,q*10200)
        row=report(self.engine.s['trades'])['completed_trades'][0]
        self.assertAlmostEqual(row['gross_pnl']-row['commission']-row['tax'],row['net_pnl'])
        self.assertEqual(row['qty'],q); self.assertEqual(row['strategy_version'],'V1.6-fast-live-20261015')
        self.assertEqual(row['price_band'],'10000~30000'); self.assertIsNone(row['actual_slippage'])

    def test_protection_update_has_full_immediate_decision_context(self):
        self.buy(); self.engine.on_tick(self.tick(10400))
        rows=[r for r in self.events() if r['event']=='PROTECTION_UPDATE']; self.assertTrue(rows)
        self.assertGreaterEqual(rows[-1]['protection_price'],rows[-1]['invalidation_price'])
        self.assertIn('allowed_risk_krw',rows[-1]); self.assertIn('actual_orderable_cash',rows[-1])

    def test_api_rejection_quote_retains_original_message_offline(self):
        rest=Mock(); rest.snapshot.return_value={'cash':0,'equity':0,'orders':[],'holdings':{'052260':{'qty':13,'available':13,'avg':12100}}}
        rest.existing_book.side_effect=Rejected('rejected',api='ka10004',response={'return_code':20,'return_msg':' 원문 유지 '})
        worker=AccountWorker(rest,1); worker.external_symbols={'052260'}
        worker.request.wait=Mock(side_effect=lambda *a:worker.stop.set()); worker.run()
        result=worker.q.get_nowait(); error=result['snapshot']['book_errors']['052260']
        self.assertEqual(error['return_msg'],' 원문 유지 '); self.assertEqual(error['return_code'],20)
        self.assertEqual(result['snapshot']['books'],{})

    def test_official_status_blocks_before_chart_work(self):
        rest=Mock(); rest.stock_status.return_value=assess_status({'state':'정리매매'})
        worker=RadarWorker(self.cfg,self.auto,self.policy,rest,self.folder)
        result=worker.evaluate({'code':SYMBOL,'reference_price':100})
        self.assertEqual(result['reason'],'SPECIAL_LIQUIDATION'); rest.pages.assert_not_called()

    def test_original_collection_cli_after_hours_blocks_before_credentials(self):
        import main
        from dataclasses import asdict
        from core.settings import Config
        atomic_json(self.folder/'config.json',asdict(Config()))
        for flag in ('--collect-only','--check-account','--check-connection','--legacy-monitor'):
            with patch.object(main,'ROOT',self.folder),patch.object(main,'now',return_value=self.clock[0].replace(hour=20)),\
                 patch.object(main,'prerequisites'),patch.object(main,'Rest') as rest,\
                 patch.object(main,'load_credentials') as credentials,patch('sys.argv',['main.py',flag]):
                self.assertEqual(main.main(),0); rest.assert_not_called(); credentials.assert_not_called()

    def test_utc_clock_obeys_korean_cutoff_and_naive_clock_no_buy(self):
        from datetime import timezone
        self.clock[0]=self.clock[0].replace(hour=15,minute=6).astimezone(timezone.utc)
        restored=self.make_engine(); restored.market='3'; self.assertFalse(restored.entry_window())
        self.engine.clock=lambda:self.clock[0].replace(tzinfo=None)
        self.engine.enter(self.signal(),self.tick()); self.assertFalse(self.broker.orders)

class MarketAndCapitalTests(unittest.TestCase):
    def test_stock_status_assessments_table(self):
        for state,reason in (('정리매매','SPECIAL_LIQUIDATION'),('상장폐지절차','SPECIAL_DELISTING'),('거래정지','TRADING_SUSPENDED'),('단일가매매','SPECIAL_NON_CONTINUOUS')):
            result=assess_status({'state':state,'auditInfo':'정상'}); self.assertFalse(result['pass']); self.assertEqual(result['reason_code'],reason)
        for state in (None,'','미확인특수값','999'):
            self.assertIsNone(assess_status({'state':state})['pass'])
        self.assertTrue(assess_status({'state':'증거금100%','auditInfo':'정상'})['pass'])
        self.assertTrue(assess_status({'state':'증거금20%|담보대출|신용가능','auditInfo':'정상'})['pass'])
        self.assertTrue(assess_status({'state':'관리종목','auditInfo':'투자주의환기종목'})['pass'])

    def test_affordable_candidate_not_displaced_by_five_expensive_symbols(self):
        policy=Mock(); policy.locked.return_value=False
        radar=DynamicRadar(__import__('core.settings',fromlist=['Config']).Config(),replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=10000),policy)
        radar.available_capital=2000
        def item(sym,price): return {'code':sym,'name':'합성','reference_price':price,'source_details':{s:{'observed_rank':1,'values':{}} for s in ('a','b')}}
        rows,watch,_=radar.update([item(str(999900+i*10),100000) for i in range(5)]+[item('999990',1000)],
            lambda x:{'status':'ELIGIBLE','overhead_share_estimate':.01},__import__('tests.test_automation',fromlist=['START']).START)
        self.assertEqual([x['code'] for x in watch],['999990'])
        self.assertTrue(all(r['reason']=='ONE_SHARE_UNAFFORDABLE' for r in rows[:5]))

    def test_zero_cap_watch_is_observation_only_without_fictitious_cash(self):
        policy=Mock(); policy.locked.return_value=False; a=AutomationConfig()
        radar=DynamicRadar(__import__('core.settings',fromlist=['Config']).Config(),a,policy); radar.available_capital=0
        x={'code':'999990','name':'합성','reference_price':100,'source_details':{s:{'observed_rank':1,'values':{}} for s in ('a','b')}}
        _,watch,_=radar.update([x],lambda x:{'status':'ELIGIBLE','overhead_share_estimate':0},__import__('tests.test_automation',fromlist=['START']).START)
        self.assertTrue(watch); self.assertFalse(watch[0]['affordability']['entry_eligible'])

    def test_status_and_quote_official_api_fields_parsed_without_network(self):
        rest=Rest.__new__(Rest)
        rest.query=Mock(side_effect=[{'code':'017900','state':'증거금100%','auditInfo':'정상'},
            {'bid_req_base_tm':'144000','buy_fpr_bid':'+12140','buy_fpr_req':'3'}])
        from tests.test_automation import START
        with patch('core.broker.now',return_value=START): book=rest.existing_book('017900')
        self.assertEqual(book['bid'],12140); self.assertEqual(book['bid_qty'],3)
        self.assertTrue(book['status']['pass']); self.assertEqual(book['time'],START.isoformat())
        self.assertEqual(rest.query.call_args_list[0].args[0],'ka10100'); self.assertEqual(rest.query.call_args_list[1].args[0],'ka10004')

    def test_exact_supported_time_boundaries(self):
        from automation.session import supported
        from tests.test_automation import START
        for hour,minute,second,expected in ((8,59,59,False),(9,0,0,True),(15,19,59,True),(15,20,0,False)):
            self.assertEqual(supported(START.replace(hour=hour,minute=minute,second=second)),expected)

    def test_invalid_quote_depth_and_prices_rejected(self):
        rest=Rest.__new__(Rest); rest.stock_status=Mock(return_value={'pass':True})
        for bid,depth in (('','1'),('12140','-100'),('12140','1.5')):
            rest.query=Mock(return_value={'bid_req_base_tm':'144000','buy_fpr_bid':bid,'buy_fpr_req':depth})
            with self.assertRaises(ValueError): rest.existing_book('017900')

if __name__=='__main__': unittest.main()
