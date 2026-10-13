"""Frozen point-in-time V1.4/V1.5 price-gate comparison. No runtime or API reads.
Historical outputs belong in a private evidence directory, never source control.
"""
import argparse,json,sys
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.settings import Config
from core.strategy import Tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.patterns import PatternDetector
from automation.trade_quality import fresh_box_basis
from tools.trade_quality_replay import replay,read,contextual_peak


def price_decision(signal,origin=False,direction=False,context=False,peak=None):
    auto=replace(AutomationConfig(),fresh_box_basis_enabled=origin,confirmed_box_direction=direction,entry_context_enabled=context)
    costs=Costs(auto.commission_buy,auto.commission_sell,auto.sell_tax,Config().entry_slippage_fraction,Config().exit_slippage_fraction)
    detector=PatternDetector(Config(),auto,costs)
    at=datetime.fromisoformat(signal['time']);bars=[]
    for raw in signal.get('decision_bar_snapshot',[]):
        b=dict(raw);b['time']=datetime.fromisoformat(b['time']) if isinstance(b['time'],str) else b['time'];bars.append(b)
    geometry={k:signal[k] for k in ('pattern','breakout','support','projection')}
    if 'peak' in signal:geometry['peak']=signal['peak']
    values=signal.get('evaluation_gates',{}).get('breakout',{}).get('values',{})
    price=values.get('price',signal['limit']);ask=values.get('ask',signal['limit'])
    tick=Tick(signal['symbol'],at,price,1,1,ask,ask-1)
    watch={'status':'WATCH','resistance':signal.get('structure',{}).get('resistance'),
        'resistance_room_pass':True,'source_details':signal.get('source_details',{})}
    if context:
        # Historical references were frozen separately; never use a later high.
        detector.price_context.day=at.date()
        detector.price_context.rows[signal['symbol']]={'bars':deque(bars,maxlen=30),'peaks':deque([peak] if peak else [],maxlen=128),
            'first_observed_at':bars[0]['time'].isoformat() if bars else at.isoformat(),'last_complete_at':at.isoformat()}
    gates=detector.setup_gates(tick,watch,geometry,bars)
    return {'price_gates_pass':all(g['result']=='PASS' for g in gates.values()),
        'failed':[k for k,g in gates.items() if g['result']!='PASS'],
        'target':gates['risk_reward']['values']['target'],'rr':gates['risk_reward']['values']['calculated_rr'],
        'origin':gates.get('box_origin'),'direction':gates.get('box_direction'),
        'order_permission':'NOT_REPLAYED: requires all live risk/account/order guards'}


def run(folder,events):
    baseline=replay(folder);audit=read(folder,'after-quality-audit.json');health=read(folder,'latest-health.json')
    trades=sorted(audit['trades']+[health['last_trade']],key=lambda t:t['opened'])
    rows=[]
    for trade,previous in zip(trades,baseline['rows']):
        signal=trade['signal'];peak=previous['resistance']
        rows.append({'symbol':signal['symbol'],'at':signal['time'],'setup':signal['pattern'],'entry':trade['entry'],
            'actual_exit':trade['exit'],'actual_exit_reason':trade['reason'],'observed_net_estimate':trade['pnl_estimate'],
            'v14_default':price_decision(signal),
            'v15_origin_only':price_decision(signal,origin=True),
            'v14_context_direction':price_decision(signal,context=True,direction=True,peak=peak),
            'v15_context_direction_origin':price_decision(signal,context=True,direction=True,origin=True,peak=peak),
            'frozen_known_resistance':peak,'actual_trade_is_not_a_good_trade_label':True})
    independent=[]
    for line in events.read_text(encoding='utf-8-sig').splitlines():
        event=json.loads(line)
        if event.get('event')!='ENTRY_CANDIDATE':continue
        signal=event.get('signal') or event.get('details',{}).get('signal')
        if not isinstance(signal,dict):
            # Some archives put the serialized signal directly at the event root.
            signal=event if all(k in event for k in ('pattern','breakout','support','projection','limit','time')) else None
        if not signal:continue
        if signal.get('pattern')!='C':independent.append({'symbol':signal.get('symbol'),'at':signal.get('time'),'setup':signal.get('pattern'),'origin_pass':True,'reason':'A_B_UNCHANGED'});continue
        passed,proof=fresh_box_basis(signal.get('decision_bar_snapshot'),datetime.fromisoformat(signal['time']),5,.015)
        independent.append({'symbol':signal['symbol'],'at':signal['time'],'setup':'C','origin_pass':passed,'evidence':proof})
    return {'base':'abb29815ca114fc0ce50babc4035f15236a64ba5','scope':'PRICE_GATES_ONLY',
        'network_calls':0,'actual_orders':0,'future_prices_used_for_entry':False,'rows':rows,
        'independent_date':independent,'independent_date_scope':'2026-10-08 retained ENTRY_CANDIDATE snapshots only',
        'origin_blocked_loss':sum(r['observed_net_estimate']<0 and not r['v15_origin_only']['price_gates_pass'] and r['v14_default']['price_gates_pass'] for r in rows),
        'origin_blocked_winner':sum(r['observed_net_estimate']>0 and not r['v15_origin_only']['price_gates_pass'] and r['v14_default']['price_gates_pass'] for r in rows),
        'profitability':'NOT_ESTABLISHED; no counterfactual fills or replacement profits inferred'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--evidence-dir',type=Path,required=True);p.add_argument('--independent-events',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=run(a.evidence_dir,a.independent_events);a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'rows','independent_date'}},ensure_ascii=False))
    for r in result['rows']:print(r['symbol'],r['at'],r['v14_default']['failed'],r['v15_origin_only']['failed'],r['v15_context_direction_origin']['failed'])
    print('independent',[(r['symbol'],r['origin_pass']) for r in result['independent_date']])
