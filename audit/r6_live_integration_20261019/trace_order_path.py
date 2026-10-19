"""Actual receiver/strategy/order code, synthetic ticks and FakeBroker only."""
import contextlib,json,sys,time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tests import test_r6_execution_integrity as fixture
from automation import runner

def main():
    case=fixture.ReceiverPromotionTests();case.setUp()
    try:
        case.seed_receiver();start=case.at
        # One neutral print remains a valid signal under unchanged FAST rules.
        # Six prints that invalidate it are a separate mandatory NO_BUY test.
        for i in range(1):
            case.at=start+timedelta(seconds=(i+1)*.05)
            fields={'20':case.at.strftime('%H%M%S'),'10':'1001','15':'1000','13':str(12000+1000*i),
                '27':'1001','28':'1000','290':'2','61':'10000','71':'10000'}
            with patch('core.realtime.now',return_value=case.at):
                case.feed.emit({'trnm':'REAL','data':[{'type':'0B','item':fixture.SYM,'values':fields}]})
        lookup={};rows={fixture.SYM:case.row};boundaries=[]
        actual_guard=case.e.entry_market_reason
        def guard(signal,quantity,fallback=None,phase='PRE_INTENT'):
            result=actual_guard(signal,quantity,fallback,phase)
            boundaries.append({'phase':phase,'result':result,'quantity':quantity,'limit':signal['limit'],
                'signal_time':signal.get('time'),'signal_detected_at':signal.get('signal_detected_at'),
                'trigger_tick_at':fallback.time.isoformat() if fallback is not None else None,
                'trigger_received_at':getattr(fallback,'received_at',None),
                'checked_at':case.at.isoformat()})
            return result
        case.e.entry_market_reason=guard
        callback=lambda t,r:runner.evaluate_observed_entry(case.e,case.detector,t,r,lookup,
            case.radar,case.prices,case.account,rows)
        probe=lambda t,r:runner.evaluate_shadow_opportunity(case.e,case.detector,t,case.row,
            case.radar,case.prices,case.account,probe_only=True)
        for _ in range(24):
            runner.prewarm_handoff(case.e,case.detector,case.feed,{fixture.SYM},set(),set(),0,
                on_opportunity=callback,opportunity_probe=probe)
            if case.broker.orders:break
        assert len(case.broker.orders)==1,case.broker.orders
        assert any(r['phase']=='PRE_HTTP' and r['result'] is None for r in boundaries)
        assert fixture.SYM in lookup
        events=[json.loads(line) for line in (case.e.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        selected=[]
        for event in events:
            if event.get('event') in {'WATCH_DYNAMIC_PROMOTION','ENTRY_CANDIDATE','ENTRY_EXECUTION_RECHECK','ORDER_ACCEPTED','ORDER_REJECTED','BUY_ORDER'}:
                selected.append({k:event[k] for k in ('time','event','symbol','pattern','reason','phase','decision','primary_reason_code','quantity','limit') if k in event})
        result={'fixture':'SYNTHETIC_999990_CAUSAL_PREFIX_NO_STRATEGY_SIGNAL_MOCK',
            'stages':['Feed.emit','observation_q','PatternDetector/FastMomentumStrategy','WATCH_DYNAMIC_PROMOTION',
                'LIVE_REEVALUATION','PRE_INTENT','PRE_HTTP','FakeBroker.submit'],
            'boundaries':boundaries,'orders':case.broker.orders,'actual_available_cash':fixture.CASH,
            'events':selected,'real_broker_connected':False,'real_orders':0,'profitability_proven':False}
    finally:case.doCleanups()
    output=Path(sys.argv[1]);output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='events'},ensure_ascii=False))

if __name__=='__main__':main()
