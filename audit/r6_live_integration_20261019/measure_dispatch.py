"""Causal synthetic dispatcher benchmark; not a market opportunity/profit study."""
import contextlib
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tests import test_r6_execution_integrity as fixture
from automation import runner
from core.strategy import Tick

def main():
    case=fixture.AccountFixture();case.setUp()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            symbols=[str(900000+i) for i in range(64)]
            from core.realtime import Feed
            from types import SimpleNamespace
            feed=Feed(SimpleNamespace(cfg=case.cfg),symbols)
            feed.configure_observation(set(),set(symbols),None)
            origin=case.at;case.at=origin+timedelta(seconds=10,microseconds=400000)
            rows={s:{'code':s,'name':'Synthetic '+s,'status':'SHADOW','source_count':2,
                'reference_price':984,'resistance':1045,'resistance_asof':origin.isoformat(),
                'discovery_score':1.,'market_status':{'pass':True,'reason_code':'SYNTHETIC_NORMAL_STATUS'}}
                for s in symbols}
            for index,price in enumerate(fixture.CAUSAL):
                for sym in symbols:
                    at=origin+timedelta(seconds=index)
                    tick=Tick(sym,at,price,1000,1000*(index+1),price,price-1,
                        ask_qty=10000,bid_qty=10000,depth_available=True,book_time=at)
                    feed.observation_q.put((at,tick))
            for index in range(6):
                for sym in symbols:
                    at=origin+timedelta(seconds=10,microseconds=(index+1)*50000)
                    tick=Tick(sym,at,1001,1000,12000+1000*index,1001,1000,
                        ask_qty=10000,bid_qty=10000,depth_available=True,book_time=at)
                    feed.observation_q.put((at,tick))
            prefix_diagnostics='--prefix-diagnostics' in sys.argv[2:]
            calls=[];passes=[];started=time.perf_counter()
            def probe(t,r):
                return runner.evaluate_shadow_opportunity(case.e,case.detector,t,rows[t.symbol],
                    case.radar,case.prices,case.account,probe_only=True)
            def callback(t,r):
                calls.append({'symbol':t.symbol,'source_at':t.time.isoformat(),
                    'processing_at':case.at.isoformat(),'current_strategy_valid':bool(probe(t,r)),
                    'dispatch_elapsed_ms':1000*(time.perf_counter()-started)})
            while (not feed.observation_q.empty() or getattr(feed,'_opportunity_pending',{})) and len(passes)<2000:
                before=len(calls);turn=time.perf_counter()
                runner.prewarm_handoff(case.e,case.detector,feed,set(symbols),set(),set(),0,
                    on_opportunity=callback,opportunity_probe=probe if prefix_diagnostics else None)
                passes.append({'ms':1000*(time.perf_counter()-turn),'evaluations':len(calls)-before})
            at_breakout={r['symbol'] for r in calls if r['source_at'].endswith('30:10+09:00')}
            result={'input':'CAUSAL_SYNTHETIC_64_SYMBOLS_1088_FRAMES_WITH_NEUTRAL_TAIL',
                'frames':64*(len(fixture.CAUSAL)+6),'symbols':64,'passes':len(passes),
                'seconds':round(time.perf_counter()-started,4),'max_callbacks_per_pass':max(p['evaluations'] for p in passes),
                'max_pass_ms':round(max(p['ms'] for p in passes),3),
                'signal_prefix_symbols':len(at_breakout),'all_symbols_dispatched':len({r['symbol'] for r in calls}),
                'current_valid_signal_symbols':len({r['symbol'] for r in calls if r['current_strategy_valid']}),
                'per_frame_probe_enabled':prefix_diagnostics,
                'remaining_frames':feed.observation_q.qsize(),'real_orders':0,'mock_orders':len(case.broker.orders),
                'full_order_guards_measured':False,'dispatches':calls,'passes_detail':passes}
    finally:case.doCleanups()
    output=Path(sys.argv[1]);output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('dispatches','passes_detail')},ensure_ascii=False))
    return 0 if result['all_symbols_dispatched']==64 and result['max_callbacks_per_pass']<=2 and result['remaining_frames']==0 else 1

if __name__=='__main__':raise SystemExit(main())
