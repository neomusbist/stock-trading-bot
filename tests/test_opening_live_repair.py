"""Oct 14 pipeline and C counterexamples. Market-only fixtures; no live I/O."""
import copy,queue,tempfile,unittest
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from automation.focus_selection import calculate_tier,select_focus,TIER_RANK_ONLY,TIER_FRESH_WEAK
from automation.observation_worker import ObservationWorker
from automation.shadow_observation import ShadowObserver
from automation.patterns import PatternDetector
from automation.radar import DynamicRadar
from automation.runner import prewarm_handoff,focus_readiness
from automation.structural_box import structural_box
from automation.capital import Costs
from automation.policy import AutomationConfig
from core.settings import Config,KST
from core.strategy import Tick
from tests import test_subscription_refresh as wire
from tests import test_automation as engine_fixture

AT=datetime(2026,10,14,9,24,7,tzinfo=KST);SYM='999990'
AUTO=replace(AutomationConfig(),fresh_box_basis_enabled=True,confirmed_box_direction=True,confirmed_invalidation_enabled=True)
COSTS=Costs(.00015,.00015,.002,.002,.003)

def candles(values):
    return [dict(time=AT.replace(second=0)-timedelta(minutes=len(values)-i),open=o,high=h,low=l,close=c,
                 volume=v,first_second=0,gap_reason=None) for i,(o,h,l,c,v) in enumerate(values)]

# The five actual pre-entry completed bars, not the later sell or future candles.
PAN=candles([(6920,6920,6890,6910,58506),(6910,6920,6870,6890,54669),
             (6890,6900,6870,6880,19936),(6880,6890,6870,6890,23851),(6890,6960,6880,6960,273891)])
NORMAL=candles([(10010,10020,10000,10010,100),(10060,10150,10060,10110,100),
                (10110,10120,10100,10110,100),(10110,10120,10090,10100,100),(10100,10120,10100,10110,100)])

def detector(auto=AUTO):return PatternDetector(Config(),auto,COSTS)
def quote(price=10160):return Tick(SYM,AT,price,100,1000,price,price-10)
def watch():return {'status':'WATCH','market_status':{'pass':True},'source_details':{'a':{},'b':{}}}
def row(sym,score=30):
    return {'code':sym,'status':'WATCH','reference_price':10000,'source_count':2,'discovery_score':score,
            'source_details':{'a':{'observed_rank':1,'values':{}},'b':{'observed_rank':2,'values':{}}},
            'affordability':{'entry_eligible':True},'market_status':{'pass':True}}

class OpeningObservationRepairs(unittest.TestCase):
    def test_warming_incumbent_does_not_lose_to_unobserved_same_score(self):
        warming={'warmup':{'result':'FAIL'},'acceleration':{'result':'UNKNOWN'},'liquidity':{'result':'FAIL'}}
        self.assertEqual(calculate_tier(warming,[]),TIER_RANK_ONLY)
        a,b=row(SYM),row('999980',31)
        choice=select_focus({SYM:a},[a,b],{SYM:{'gates':warming}},1)
        self.assertEqual(choice.next_watch,[SYM])
    def test_measured_weakness_and_objectively_stronger_candidate_still_replace(self):
        gates={'warmup':{'result':'PASS'},'acceleration':{'result':'FAIL'},'liquidity':{'result':'PASS'}}
        self.assertEqual(calculate_tier(gates,[]),TIER_FRESH_WEAK)
        a,b=row(SYM),row('999980')
        self.assertEqual(select_focus({SYM:a},[a,b],{SYM:{'gates':gates}},1).next_watch,[SYM])
        strong={k:{'result':'PASS'} for k in ('warmup','spread','structure','market_status','liquidity','acceleration')}
        self.assertEqual(select_focus({SYM:a},[a,b],{SYM:{'gates':gates},'999980':{'gates':strong}},1).next_watch,['999980'])
        warming={**gates,'warmup':{'result':'FAIL'}};b['discovery_score']=33
        self.assertEqual(select_focus({SYM:a},[a,b],{SYM:{'gates':warming}},1).next_watch,['999980'])
    def test_radar_uses_full_evaluation_tier_from_compact_runtime_view(self):
        cfg=replace(Config(),max_watchlist=1);auto=replace(AUTO,setup_aware_watch_priority=True)
        radar=DynamicRadar(cfg,auto,Mock(locked=lambda s:False));radar.available_capital=1000000
        radar.momentum={SYM:{'timestamp':AT.isoformat(),'tier':1,'gates':{'acceleration':{'result':'FAIL'},'liquidity':{'result':'PASS'}}}}
        rows,chosen,_=radar.update([row(SYM),row('999980')],lambda r:{'status':'ELIGIBLE'},AT)
        self.assertEqual(chosen[0]['code'],SYM)
        self.assertEqual(chosen[0]['watch_priority_basis'],'GEOMETRY_BASE_READY')
    def test_old_readiness_does_not_override_rank(self):
        a,b=row(SYM,30),row('999980',40)
        stale={SYM:{'timestamp':(AT-timedelta(seconds=16)).isoformat(),'tier':0}}
        self.assertEqual(select_focus({SYM:a},[a,b],stale,1,clock_iso=AT.isoformat()).next_watch,['999980'])
    def test_actual_watch_evaluation_is_not_ranked_by_an_old_shadow_failure(self):
        d=detector();gates={k:{'result':'PASS'} for k in ('warmup','acceleration','liquidity','spread','structure','market_status')}
        d.last_eval[SYM]={'time':AT,'gates':gates,'trials':[]}
        old={SYM:{'timestamp':(AT-timedelta(seconds=20)).isoformat(),'tier':4}}
        ready=focus_readiness(d,old,AT)
        self.assertEqual(ready[SYM]['tier'],2);self.assertEqual(old[SYM]['tier'],4)
        d.last_eval[SYM]['time']=AT-timedelta(seconds=16)
        self.assertEqual(focus_readiness(d,old,AT)[SYM],old[SYM])
    def test_shadow_keeps_receiver_history_but_never_emits_stale_readiness(self):
        events=[];observer=ShadowObserver(Config(),AUTO,COSTS,lambda kind,**v:events.append((kind,v)))
        observer.detector.continuous_observation=True
        start=AT.replace(second=0)-timedelta(minutes=3)
        r=row(SYM);observer.update([r],start);events.clear()
        for n in range(121):
            t=Tick(SYM,start+timedelta(seconds=n),10000,100,100*(n+1),10010,9990)
            observer.tick(t,AT,transport_received=t.time)
        self.assertEqual(len(observer.detector.usable_bars(SYM)),2)
        self.assertEqual(events,[]);self.assertEqual(observer.latest,{})
        fresh=Tick(SYM,AT,10000,100,18300,10010,9990)
        observer.tick(fresh,fresh.time,transport_received=fresh.time)
        self.assertEqual(observer.detector.base_gates(fresh,watch())['warmup']['result'],'PASS')
        self.assertTrue(any(kind=='SHADOW_EVAL' for kind,_ in events))
    def test_actually_stale_receiver_and_missing_minute_do_not_become_valid_history(self):
        events=[];observer=ShadowObserver(Config(),AUTO,COSTS,lambda kind,**v:events.append(kind))
        observer.detector.continuous_observation=True;observer.update([row(SYM)],AT)
        t=quote();observer.tick(t,AT+timedelta(seconds=10),transport_received=AT+timedelta(seconds=6))
        self.assertNotIn(SYM,observer.detector.last)
        for second in (0,60,180):
            t=Tick(SYM,AT+timedelta(seconds=second),10000,100,1000+second,10010,9990)
            observer.tick(t,t.time,transport_received=t.time)
        self.assertEqual(observer.detector.usable_bars(SYM),[])
    def test_ranked_observer_order_is_preserved_for_subscription_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            w=ObservationWorker(Config(),AUTO,COSTS,Path(folder),limit=3);w.clock=lambda:AT
            values=[row('999970',10),row(SYM,50),row('999980',30)]
            w.publish(values,AT,{})
            self.assertEqual(w.runtime_view()['symbols'],tuple(w.observer.rows))
            self.assertEqual(w.runtime_view()['symbols'],(SYM,'999980','999970'))

class PrewarmReceiverRepair(unittest.TestCase):
    setUp=wire.SubscriptionRefreshTests.setUp
    tick=wire.SubscriptionRefreshTests.tick
    def test_promotion_replays_original_receipt_not_consumer_wall_clock(self):
        for n in range(121):
            t=self.tick(sec=n,cumulative=100*(n+1));self.feed.observation_q.put((t.time,t))
        self.clock=wire.START+timedelta(seconds=200)
        with patch('automation.runner.now',return_value=self.clock):
            prewarm_handoff(self.e,self.d,self.feed,{wire.SYM},{wire.SYM},set(),0,Mock())
        self.assertEqual(len(self.d.usable_bars(wire.SYM)),2)
        self.assertEqual(self.d.observation_start[wire.SYM],wire.START)
        self.assertEqual(self.broker.orders,[])

class BoxStructureRepair(unittest.TestCase):
    def gates(self,bars=NORMAL,price=10160,auto=AUTO):
        d=detector(auto);setup=d.box(bars);self.assertIsNotNone(setup)
        return d.setup_gates(quote(price),watch(),setup,bars),d,setup
    def test_actual_pan_ocean_base_and_target_passed_legacy_but_are_rejected_now(self):
        old,_,_=self.gates(PAN,6970,replace(AUTO,confirmed_invalidation_enabled=False))
        self.assertTrue(all(g['result']=='PASS' for g in old.values()))
        self.assertEqual(old['invalidation']['values']['stop'],6860)
        self.assertEqual(old['risk_reward']['values']['target'],7140)
        new,_,_=self.gates(PAN,6970)
        self.assertEqual(new['box_structure']['result'],'FAIL')
        self.assertEqual(new['box_structure']['values']['reason'],'BOX_CONTAINS_EXPANDING_IMPULSE')
    def test_normal_box_uses_confirmed_floor_and_noise_without_fixed_percent_widening(self):
        gates,_,_=self.gates();proof=gates['box_structure']['values']
        self.assertTrue(all(g['result']=='PASS' for g in gates.values()))
        self.assertEqual(proof['support'],10000);self.assertEqual(proof['structural_stop'],9990)
        self.assertEqual(proof['noise_band'],20);self.assertEqual(proof['emergency_stop'],9970)
        self.assertEqual(gates['risk_reward']['values']['target'],10450)
    def test_unconfirmed_latest_floor_refuses_entry(self):
        raw=copy.deepcopy(NORMAL);raw[-1]['low']=10000
        gates,_,_=self.gates(raw)
        self.assertEqual(gates['box_structure']['values']['reason'],'BOX_SUPPORT_NOT_CONFIRMED')
        self.assertEqual(gates['box_structure']['result'],'FAIL')
    def test_known_prior_ceiling_reduces_reward_and_rejects_instead_of_widening_target(self):
        d=detector();d.prior_high[SYM]=10200;setup=d.box(NORMAL)
        gates=d.setup_gates(quote(),watch(),setup,NORMAL)
        self.assertEqual(gates['risk_reward']['values']['target'],10200)
        self.assertEqual(gates['risk_reward']['result'],'FAIL')
    def test_noise_changes_require_risk_and_reward_recalculation(self):
        raw=copy.deepcopy(NORMAL)
        for b in raw[2:]:b.update(low=10040,high=10140)
        gates,_,_=self.gates(raw)
        self.assertEqual(gates['box_structure']['result'],'PASS')
        self.assertEqual(gates['risk_reward']['result'],'FAIL')
    def test_future_missing_or_partial_support_bar_cannot_supply_proof(self):
        d=detector();setup=d.box(NORMAL)
        for raw in (NORMAL[:-1],NORMAL+[{**NORMAL[-1],'time':AT.replace(second=0)}],
                    [{**b,'gap_reason':'MISSING'} if i==3 else b for i,b in enumerate(NORMAL)]):
            self.assertIsNot(structural_box(raw,AT,setup,Config(),AUTO,10160,10150,10160)[0],True)

class BoxOrderAndExitRepair(unittest.TestCase):
    setUp=engine_fixture.AutomationTests.setUp
    make_engine=engine_fixture.AutomationTests.make_engine
    def enable(self):
        self.clock[0]=AT;self.auto=replace(self.auto,fresh_box_basis_enabled=True,confirmed_box_direction=True,confirmed_invalidation_enabled=True)
        self.engine=self.make_engine();self.engine.reconcile(self.snapshot);self.engine.connected=True;self.engine.market='3'
        d=PatternDetector(self.cfg,self.auto,self.engine.costs);setup=d.box(NORMAL)
        gates=d.setup_gates(quote(),watch(),setup,NORMAL);proof=gates['box_structure']['values']
        self.sig={**setup,'symbol':SYM,'time':AT.isoformat(),'limit':10160,'stop':proof['emergency_stop'],
                  'target':gates['risk_reward']['values']['target'],
                  'evaluation_gates':{**gates,'spread':{'result':'PASS','values':{'bid':10150,'ask':10160}}},
                  'decision_bar_snapshot':[{**b,'time':b['time'].isoformat()} for b in NORMAL],
                  'invalidation_policy':copy.deepcopy(proof),'efficiency':gates['economic']['values']['score']}
    def buy(self):
        self.enable();o=self.engine.enter(self.sig,quote());self.assertIsNotNone(o)
        self.engine.cumulative_fill(o,o['qty'],o['qty']*o['limit']);self.engine.actual={SYM:{'qty':o['qty'],'available':o['qty'],'avg':o['limit']}}
        return self.engine.s['positions'][SYM]
    def test_complete_order_path_sizes_from_hard_line_and_retains_distinct_soft_line(self):
        p=self.buy();self.assertEqual(p['initial_stop'],9970)
        self.assertEqual(p['signal']['invalidation_policy']['structural_stop'],9990)
        self.assertLessEqual(p['qty']*self.sig['efficiency']['risk_per_share'],self.engine.risk_equity()*self.cfg.risk_fraction)
    def test_one_tick_below_structure_above_emergency_does_not_exit(self):
        p=self.buy();self.engine.on_tick(replace(quote(),price=9980,ask=9990,bid=9970))
        self.assertFalse(p['exit_requested']);self.assertEqual(len(self.broker.orders),1)
    def test_confirmed_close_break_requests_protective_exit(self):
        p=self.buy();stamp=AT.replace(second=0)+timedelta(minutes=1);self.clock[0]=stamp+timedelta(minutes=1)
        self.engine.observe_protection_bar(SYM,dict(time=stamp,open=10160,high=10170,low=9980,close=9980))
        self.assertTrue(p['exit_requested']);self.assertEqual(p['exit_reason'],'확정봉 종가의 구조적 무효화')
    def test_wick_recovery_preserves_position(self):
        p=self.buy();stamp=AT.replace(second=0)+timedelta(minutes=1);self.clock[0]=stamp+timedelta(minutes=1)
        self.engine.observe_protection_bar(SYM,dict(time=stamp,open=10160,high=10170,low=9980,close=10020))
        self.assertFalse(p['exit_requested'])
    def test_fresh_hard_line_touch_exits_immediately(self):
        p=self.buy();self.engine.on_tick(replace(quote(),price=9970,ask=9980,bid=9960))
        self.assertTrue(p['exit_requested']);self.assertEqual(len(self.broker.orders),2)
    def test_falsified_proof_stop_ceiling_and_missing_basis_never_order(self):
        for field in ('stop','target','policy','bars'):
            self.enable();sig=copy.deepcopy(self.sig)
            if field=='stop':sig['stop']+=10
            elif field=='target':sig['target']+=10
            elif field=='policy':sig['invalidation_policy']['structural_stop']+=10
            else:sig.pop('decision_bar_snapshot')
            self.assertIsNone(self.engine.enter(sig,quote()));self.assertEqual(self.broker.orders,[])
    def test_latest_receiver_ask_above_limit_still_refuses_even_valid_box(self):
        self.enable();self.engine.entry_quote_provider=lambda sym:(replace(quote(),ask=10170),{})
        self.assertIsNone(self.engine.enter(self.sig,quote()));self.assertEqual(self.broker.orders,[])

class TerminalFeeSettlementRepair(unittest.TestCase):
    def test_terminal_sell_still_requests_broker_charges_until_confirmed(self):
        from automation.runner import pending_broker_settlement
        state={'day':'2026-10-14','trades':[{'completed':True,'closed':AT.isoformat(),'pnl_source':'MODEL'}]}
        self.assertTrue(pending_broker_settlement(state))
        state['trades'][0]['pnl_source']='BROKER_QUERY';self.assertFalse(pending_broker_settlement(state))
        state['trades'][0].update(pnl_source='MODEL',closed=(AT-timedelta(days=1)).isoformat())
        self.assertFalse(pending_broker_settlement(state))

if __name__=='__main__':unittest.main()
