import ast,copy,json,tempfile,unittest
from datetime import datetime,timedelta
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock,patch
from core.settings import Config,KST,ROOT
from automation.policy import AutomationConfig,Policy
from automation.radar import RadarWorker
from operator_ui_service import human_blocker_reason,extract_important_events
STAMP=datetime(2026,10,7,18,58,40,tzinfo=KST)
OLD=['158430','203650','008970','018880']
class RuntimeAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.cfg=replace(Config(),max_watchlist=12,risk_fraction=.02,max_daily_loss_fraction=.04)
        self.auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True,max_scan_charts=20)
        self.rest=Mock();self.rest.profile={}
        self.rest.stock_status.side_effect=lambda sym:{'pass':True,'reason_code':'NORMAL','checked_at':STAMP.isoformat()}
        self.worker=RadarWorker(self.cfg,self.auto,Policy(self.root/'policy'),self.rest,self.root,fallback_source1=True)
        self.worker.available_capital=20000
    def item(self,sym,sources=1,price=1000):
        return {'code':sym,'name':'synthetic','reference_price':price,'source_details':{str(i):{'observed_rank':i+1,'values':{}} for i in range(sources)}}
    def cycle(self,items,old=OLD):
        self.worker.radar.watch={s:{**self.item(s),'status':'WATCH'} for s in old}
        for x in items:self.worker.cache[(STAMP.date(),x['code'])]=[]
        def collect(*args):self.worker.stop.set();self.worker.request.set();return copy.deepcopy(items)
        def structure(bars,price,cfg,day):return {'status':'ELIGIBLE','reason':'구조 필터 통과','overhead_share_estimate':0}
        self.worker.observation_sink=Mock()
        with patch('automation.radar.collect',side_effect=collect),patch('automation.radar.now',return_value=STAMP),patch('automation.radar.observation_structure',side_effect=structure):self.worker.run()
        raw=self.worker.q.get_nowait();final=self.worker.q.get_nowait()
        return raw,final
    def test_existing_source1_status_refreshed_before_twenty_new_source2_candidates(self):
        items=[self.item(f'6000{i:02}',2) for i in range(20)]+[self.item(s) for s in OLD]
        for s in OLD:self.worker.status_cache[s]={'pass':True,'checked_at':(STAMP-timedelta(seconds=181)).isoformat()}
        raw,final=self.cycle(items)
        calls=[x.args[0] for x in self.rest.stock_status.call_args_list]
        self.assertEqual(calls[:4],OLD);self.assertEqual(len(calls),20)
        self.assertTrue(all(r.get('market_status',{}).get('pass') for r in final['rows'] if r['code'] in OLD))
        self.assertEqual(sum(r['reason']=='STATUS_SCAN_LIMIT' for r in final['rows']),4)
        self.assertEqual(raw['phase'],'RAW');self.assertEqual(raw['rows'][0]['code'],'600000')
        self.assertTrue(all(r['status']=='RAW' for r in raw['rows']))
    def test_twelve_incumbents_fit_existing_twenty_status_budget(self):
        old=[f'7000{i:02}' for i in range(12)]
        _,final=self.cycle([self.item(f'6000{i:02}',2) for i in range(25)]+[self.item(s) for s in old],old)
        queried=[x.args[0] for x in self.rest.stock_status.call_args_list]
        self.assertEqual(queried[:12],old);self.assertEqual(len(queried),20)
        self.assertEqual(self.worker.a.max_scan_charts,20)
    def test_actual_fast_promoted_watch_is_prioritized_before_background_rank_watch(self):
        active=['900001','900002','900003','900004']
        rank=[f'7000{i:02}' for i in range(12)]
        self.worker.status_priority_symbols=frozenset(active)
        _,final=self.cycle([self.item(f'6000{i:02}',2) for i in range(20)]+[self.item(s) for s in rank+active],old=rank)
        queried=[x.args[0] for x in self.rest.stock_status.call_args_list]
        self.assertEqual(queried[:4],active)
        self.assertEqual(len(queried),20)
        self.assertTrue(all(r.get('market_status',{}).get('pass') for r in final['rows'] if r['code'] in active))
    def test_main_thread_publishes_immutable_watch_metadata_priority(self):
        tree=ast.parse((ROOT/'automation/runner.py').read_text(encoding='utf-8'))
        assignments=[n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Attribute) and t.attr=='status_priority_symbols' for t in n.targets)]
        self.assertEqual(len(assignments),1)
        self.assertEqual(ast.unparse(assignments[0].value),'frozenset(lookup)')
    def test_refresh_does_not_reuse_expired_normal_status_when_api_reports_special_state(self):
        sym=OLD[0];self.worker.status_cache[sym]={'pass':True,'checked_at':(STAMP-timedelta(seconds=181)).isoformat()}
        self.rest.stock_status.return_value=None;self.rest.stock_status.side_effect=lambda s:{'pass':False,'reason_code':'TRADING_HALTED','checked_at':STAMP.isoformat()}
        _,final=self.cycle([self.item(sym)],old=[sym])
        row=final['rows'][0];self.assertEqual(row['status'],'REJECT');self.assertEqual(row['reason'],'TRADING_HALTED');self.assertEqual(final['watch'],[])
    def test_api_error_leaves_unknown_and_never_uses_expired_status(self):
        sym=OLD[0];self.worker.status_cache[sym]={'pass':True,'checked_at':(STAMP-timedelta(seconds=181)).isoformat()}
        self.rest.stock_status.side_effect=RuntimeError('synthetic read failure')
        _,final=self.cycle([self.item(sym)],old=[sym]);self.assertEqual(final['rows'][0]['status'],'UNKNOWN');self.assertEqual(final['watch'],[])
    def test_fresh_cache_age_rule_is_unchanged(self):
        sym=OLD[0];self.worker.status_cache[sym]={'pass':True,'checked_at':(STAMP-timedelta(seconds=180)).isoformat()}
        _,final=self.cycle([self.item(sym)],old=[sym]);self.rest.stock_status.assert_not_called();self.assertEqual(final['rows'][0]['market_status']['pass'],True)
    def test_unaffordable_and_hard_lock_skip_status_requests(self):
        _,final=self.cycle([self.item('005930'),self.item('017900'),self.item('600001',price=30000)],old=[])
        self.rest.stock_status.assert_not_called();self.assertEqual(final['watch'],[])
    def trial(self,kind,reasons,**extra):
        return {'setup':kind,'evaluated':True,'failed_reasons':reasons,'entry_ready':False,'candidate_generated':kind=='C',**extra}
    def row(self):
        return {'primary_reason_code':'PATTERN_OR_GATE_NOT_READY','decision':'NO_BUY','symbol':'010170','name':'대한광통신','timestamp':STAMP.isoformat(),
                'event':'ENTRY_EVAL','gates':{'acceleration':{'result':'FAIL'},'pattern':{'result':'FAIL'}},
                'setup_trials':[self.trial('A',['ACCELERATION_FAIL']),self.trial('B',['NO_CONTROLLED_PULLBACK']),self.trial('C',['BREAKOUT_NOT_REACHED','ECONOMIC_FAIL'],setup_geometry={'breakout':17710})]}
    def test_setup_specific_display_does_not_claim_acceleration_blocks_b_or_c(self):
        text=human_blocker_reason(self.row());self.assertIn('A: 가속 미충족',text);self.assertIn('B: 눌림 형상 없음',text);self.assertIn('C: 돌파 미도달 (17,710원)',text)
        self.assertNotIn('가속',text.split('B:')[1]);self.assertIn('비용 대비 여유 부족',text)
    def test_no_trials_preserves_generic_reason_and_no_signal_is_invented(self):
        row=self.row();row.pop('setup_trials');self.assertIn('가속',human_blocker_reason(row));self.assertNotIn('진입 조건 통과',human_blocker_reason(row))
    def test_warmup_elapsed_sufficient_but_one_bar_is_explained(self):
        row=self.row();row['setup_trials']=[self.trial('B',['WARMUP_FAIL','INSUFFICIENT_USABLE_BARS'],gates={'warmup':{'result':'FAIL','values':{'elapsed_seconds':336,'required_seconds':70,'completed_bars':1}}})]
        text=human_blocker_reason(row);self.assertIn('유효 완성봉 1개',text);self.assertNotIn('336/70',text)
    def test_warmup_elapsed_insufficient_reports_actual_seconds(self):
        row=self.row();row['setup_trials']=[self.trial('B',['WARMUP_FAIL'],gates={'warmup':{'values':{'elapsed_seconds':11,'required_seconds':70,'completed_bars':0}}})]
        self.assertIn('관측 11/70초',human_blocker_reason(row))
    def test_unknown_depth_is_unconfirmed_not_zero_liquidity(self):
        row=self.row();row['setup_trials']=[self.trial('C',['LIQUIDITY_FAIL'],gates={'liquidity':{'result':'UNKNOWN','values':{'mode':'depth'}}})]
        self.assertIn('호가 잔량 미확인',human_blocker_reason(row))
    def test_ui_important_event_uses_stored_trials_without_modifying_record(self):
        row=self.row();before=copy.deepcopy(row);events=extract_important_events([row]);self.assertIn('돌파 미도달',events[0][3]);self.assertEqual(row,before)
    def test_reserve_compression_is_watch_wait_not_structure_rejection(self):
        row={'event':'ENTRY_EVAL','decision':'NO_BUY','primary_reason_code':'구조 필터 통과','gates':{'candidate_compression':{'result':'FAIL','values':{'status':'RESERVE'}}}}
        self.assertIn('WATCH 순위/자리 대기',human_blocker_reason(row));self.assertIn('WATCH 순위/자리 대기',extract_important_events([row])[0][3])
    def test_status_budget_and_ui_label_are_not_strategy_or_order_changes(self):
        repo=ROOT
        text=(repo/'operator_gui.py').read_text(encoding='utf-8');self.assertIn("'최근 평가'",text)
        tree=ast.parse((repo/'operator_ui_service.py').read_text(encoding='utf-8'))
        for n in ast.walk(tree):
            if isinstance(n,ast.FunctionDef) and n.name in {'human_blocker_reason','setup_wait_summary','extract_important_events'}:
                self.assertFalse(any(isinstance(c,ast.Call) and isinstance(c.func,ast.Attribute) and c.func.attr in {'order','buy','sell','submit','enter'} for c in ast.walk(n)))

if __name__=='__main__':unittest.main()

