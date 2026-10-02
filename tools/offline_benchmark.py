"""실제 API를 사용하지 않는 관찰/큐 CPU 계측. 장중 API latency로 해석하지 않는다."""
import argparse
import json
import socket
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.settings import Config,KST
from core.strategy import Tick
from core.realtime import Feed
from core.util import floor_tick
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.shadow_observation import ShadowObserver

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    def denied(*args,**kw):raise AssertionError('오프라인 계측: 네트워크 금지')
    socket.socket.connect=denied;socket.create_connection=denied
    cfg=Config();auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000)
    start=datetime(2026,10,2,11,0,tzinfo=KST);events=[]
    observer=ShadowObserver(cfg,auto,Costs(.00015,.00015,.002,.002,.003),lambda *a,**kw:events.append(kw))
    observer.detector.continuous_observation=True
    rows=[{'code':f'{i:05}0','name':'합성','source_count':1,'status':'REJECT','reference_price':1000,
        'affordability':{'entry_eligible':True},'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True},'resistance':1500}} for i in range(1,33)]
    observer.update(rows,start);durations=[]
    for second in range(181):
        for row in rows:
            tick=Tick(row['code'],start+timedelta(seconds=second),1000,100,100*(second+1),1001,999)
            before=time.perf_counter();observer.tick(tick,tick.time);durations.append((time.perf_counter()-before)*1000)
    class NoRest:pass
    feed=Feed(NoRest(),[]);feed.configure_observation({'999990'},{r['code'] for r in rows},None)
    message={'trnm':'REAL','data':[{'type':'0B','item':r['code'],'values':{'20':'110000','10':'1000','15':'100','13':'100','27':'1001','28':'999','290':'2'}} for r in rows]}
    before=time.perf_counter()
    for _ in range(100):feed.emit(message)
    routing_ms=(time.perf_counter()-before)*1000
    feed.emit({'trnm':'REAL','data':[{'type':'00','values':{}}]})
    costs=Costs(.00015,.00015,.002,.002,.003);risk_table=[]
    for entry in (400,1000,3000,6000,8000,10000,15000,20000):
        stop=floor_tick(entry*(1-cfg.min_stop_fraction));unit=entry-stop+costs.roundtrip(entry,stop)+stop*cfg.exit_slippage_fraction
        cash_quantity=int(20000//(entry*(1+auto.commission_buy)));risk_quantity=int(60//unit)
        risk_table.append({'entry':entry,'minimum_distance_stop':stop,'one_share_cost':entry*(1+auto.commission_buy),
            'per_share_risk':unit,'capital_quantity':cash_quantity,'risk_quantity':risk_quantity,'quantity_upper_bound':min(cash_quantity,risk_quantity)})
    result={'symbols':32,'ticks':len(durations),'tick_cpu_ms_mean':statistics.mean(durations),'tick_cpu_ms_max':max(durations),
        'tick_cpu_ms_p95':sorted(durations)[int(len(durations)*.95)],'virtual_records':len(events),
        'routing_3200_ticks_ms':routing_ms,'priority_queue_messages':feed.q.qsize(),'diagnostic_queue_ticks':feed.observation_q.qsize(),
        'real_queue_overflow':feed.overflow.is_set(),'actual_network_calls':0,'actual_orders':0,
        'risk_example_basis':'승인/현금 20,000원, 위험 0.3%=60원, 최소 무효화 거리 0.2% 및 예시 비용. Setup 통과 증거가 아닌 가장 짧은 손절거리의 수량 상한.',
        'affordability_vs_risk':risk_table,
        'limitation':'합성 틱과 메모리 로그의 CPU 계측. 실제 시장 HTTP/파일 I/O/WebSocket/전체 후보 주기의 실측이 아니다.'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
