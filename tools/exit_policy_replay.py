"""Compare frozen exits and observation-only hypotheses, without runtime/API use.

Input trades must be copied out of operating state by the operator. Optional
timelines explicitly declare actual-received or later-reference provenance.
Future/incomplete bars are never passed to a past decision. No imaginary fills.
"""
import argparse
import copy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.settings import Config
from automation.policy import AutomationConfig
from automation.exit_plan import build_plan
from automation.exit_candidates import observe_candidates
from automation.after_momentum import after_momentum_failure
from automation.trade_quality import complete_sequence
from automation.performance import completed_trades,report


def compare(trades,timelines=None):
    cfg=Config();auto=replace(AutomationConfig(),confirmed_invalidation_enabled=True,after_momentum_failure_enabled=True)
    rows=completed_trades(trades);out=[]
    for trade in rows:
        signal=copy.deepcopy(trade.get('signal') or {})
        signal.setdefault('pattern',trade.get('pattern','UNKNOWN'));signal.setdefault('stop',trade['initial_stop'])
        plan=build_plan(cfg,auto,signal,trade['entry'],trade['qty'],True)
        timeline=(timelines or {}).get(trade['trade_id'])
        item={'symbol':trade['symbol'],'opened':trade['opened'],'closed':trade['closed'],'setup':signal['pattern'],
              'entry':trade['entry'],'exit':trade['exit_price'],'quantity':trade['qty'],
              'actual_pnl':trade['pnl_estimate'],'cost_source':trade['pnl_source'],
              'MFE_recorded':trade['MFE'],'MAE_recorded':trade['MAE'],'capture_fraction':trade['mfe_capture_fraction'],
              'post_entry_price_coverage':'UNKNOWN_INCOMPLETE_TICKS_AND_QUOTES',
              'target':plan['target'],'target_reached':trade['target_reached'],'recorded_target_reached':trade['recorded_target_reached'],
              'beyond_target_observed_peak':trade['beyond_initial_target'],
              'first_target_touch_at':None,'post_target_drawdown':None,
              'policies':{},'quotes_or_alternative_fills_known':False,
              'early_normal_pullback_exit_count':'UNKNOWN','late_structure_exit_count':'UNKNOWN'}
        for name in ('TARGET_FULL','TARGET_PARTIAL','TARGET_REASSESS','STRUCTURAL_TRAIL','AE3'):
            item['policies'][name]={'first_signal_at':None,'alternative_fill_price':None,'alternative_net_pnl':None,
                'status':'UNKNOWN_NO_POST_ENTRY_PRICE_HISTORY', 'quantity': None}
        # Recorded exits span older builds and user interventions. They cannot
        # establish the expectancy of a policy deployed only after those exits.
        item['recorded_exit']={'build':trade.get('exit_build_id'),'entry_build':signal.get('entry_build_id'),
                              'at':trade['closed'],'reason':trade.get('reason'),'actual_pnl':trade['pnl_estimate']}
        trigger=trade.get('exit_trigger_evidence') or {}
        ae3=(trigger.get('exit_state') or {}).get('plan',{}).get('profit_policy')=='AE3'
        manual=any(word in str(trade.get('reason') or '') for word in ('사용자','수동'))
        item['ae3_execution_proven']=bool(ae3 and not manual)
        item['policies']['AE3'].update(status='RECORDED_AE3_EXIT_EVIDENCE' if ae3 and not manual else 'HISTORICAL_EXIT_NOT_VALIDATED_AE3',
                                      actual_exit_at=trade['closed'] if ae3 and not manual else None,reason=trade.get('reason'))
        if trade['qty']<2:item['policies']['TARGET_PARTIAL']['status']='NOT_APPLICABLE_ONE_SHARE'
        if timeline:
            if timeline.get('basis') not in ('ACTUALLY_RECEIVED_COMPLETED_BARS','LATER_REFERENCE_COUNTERFACTUAL'):
                raise ValueError('Explicit timeline provenance required')
            for name in ('TARGET_FULL','TARGET_PARTIAL','TARGET_REASSESS','STRUCTURAL_TRAIL'):
                if item['policies'][name]['status']!='NOT_APPLICABLE_ONE_SHARE':
                    item['policies'][name]['status']='REFERENCE_NO_SIGNAL_OBSERVED_EXECUTION_UNKNOWN'
            opened=datetime.fromisoformat(trade['opened']);p=dict(signal=signal,opened=trade['opened'],avg=trade['entry'],
                qty=trade['qty'],initial_stop=trade['initial_stop'],stop=trade['initial_stop'],initial_r=trade['initial_risk'],
                high=trade['entry'],low=trade['entry'],protection_bars=[],exit_plan=plan,exit_requested=False)
            candidate={};first_failure=None
            bars=sorted(timeline.get('bars',[]),key=lambda b:b['time'])
            for bar in bars:
                stamp=datetime.fromisoformat(bar['time']);at=stamp+timedelta(minutes=1)
                if stamp<opened or at>datetime.fromisoformat(trade['closed']):continue
                if not complete_sequence([bar],at,1,opened):continue
                p['protection_bars']=(p['protection_bars']+[copy.deepcopy(bar)])[-30:]
                p['high']=max(p['high'],bar['high']);p['low']=min(p['low'],bar['low'])
                # Knowledge time of a completed reference, never an intrabar fill.
                if plan['target'] is not None and bar['high']>=plan['target'] and not p.get('target_touch'):
                    p['target_touch']={'time':at.isoformat(),'price':bar['high'],'basis':timeline['basis']}
                    item['first_target_touch_at']=at.isoformat()
                candidate=observe_candidates(cfg,auto,p,at,candidate)
                for name,row in candidate['policies'].items():
                    if row['first_decision_at'] and item['policies'][name]['first_signal_at'] is None:
                        item['policies'][name].update(first_signal_at=row['first_decision_at'],status='REFERENCE_SIGNAL_ONLY',quantity=row['quantity'])
                failed,evidence=after_momentum_failure(p,at)
                if failed and first_failure is None:first_failure={'asof':at.isoformat(),'evidence':evidence}
            item['timeline_basis']=timeline['basis'];item['reference_received_at']=timeline.get('received_at')
            item['ae3_after_reference_failure']=first_failure
            if first_failure:
                item['policies']['AE3'].update(first_signal_at=first_failure['asof'],status='AFTER_REFERENCE_SIGNAL_ONLY',quantity=trade['qty'])
        out.append(item)
    actual=report(trades)
    return {'scope':'FROZEN_POINT_IN_TIME_DIAGNOSTIC_NO_ORDERS','trade_count':len(rows),
            'dates':sorted({r['opened'][:10] for r in rows}),'recorded_historical_policy':actual['all'],
            'historical_policy_note':'Mixed historical builds/manual exits; not measured AE3 expectancy',
            'ae3_execution_evidence_count':sum(r['ae3_execution_proven'] for r in out),
            'ae3_expectancy':'UNKNOWN_WITHOUT_SEPARATE_ACTUAL_POLICY_SAMPLE',
            'trade_results':out,'alternative_realized_pnl_known_count':0,
            'new_policy_expectancy_proven':False,'winning_trades_early_exit_effect':'UNKNOWN_WITHOUT_EXECUTABLE_TIMELINES',
            'quantity_note':'1주 부분익절 불가; 2주는 1+1, 3주는 1+2. 고정·최소 수수료 영향은 미확인',
            'new_policy_orders_enabled':False}


def main():
    p=argparse.ArgumentParser();p.add_argument('--trades',type=Path,required=True)
    p.add_argument('--timelines',type=Path);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();trades=json.loads(args.trades.read_text(encoding='utf-8-sig'))
    timelines=json.loads(args.timelines.read_text(encoding='utf-8-sig')) if args.timelines else None
    result=compare(trades,timelines);args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('trade_count','dates','alternative_realized_pnl_known_count','new_policy_expectancy_proven','new_policy_orders_enabled')},ensure_ascii=False))


if __name__=='__main__':main()
