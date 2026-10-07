"""Offline runner-path CPU/I/O benchmark; never imports credentials or calls a real broker."""
import argparse,contextlib,cProfile,hashlib,io,json,math,os,pstats,socket,statistics,sys,tempfile,time
from dataclasses import asdict,replace
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.settings import Config,KST
from core.strategy import Tick
from core.realtime import parse_tick,Feed
from core.util import atomic_json
from automation.policy import AutomationConfig,Policy
from automation.engine import AutonomousEngine
from automation.patterns import PatternDetector
from automation.radar import DynamicRadar,RadarWorker,observation_structure
from automation.runner import process_message,SubscriptionState
from automation.shadow_observation import ShadowObserver
from automation.capital import Costs
from operator_ui_service import UserOperations
from tests.test_v1 import bars
from tests import test_audit_followup as fixture

class FakeBroker:
    identity='NIGHT-PROFILE-SYNTHETIC'
    def __init__(self):self.orders=[];self.cfg=None
    def submit(self,*a,**k):raise AssertionError('PROFILE OBSERVE: NO ORDERS')
    def cancel(self,*a,**k):raise AssertionError('PROFILE OBSERVE: NO CANCELS')

def percentile(values,p):
    a=sorted(values);return a[min(len(a)-1,int((len(a)-1)*p))] if a else None

def memory_sample():
    """Windows process measurements only; not an estimate of production session memory."""
    if os.name!='nt':return {}
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(k,ctypes.c_size_t) for k in
            ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]
    kernel=ctypes.WinDLL('kernel32');psapi=ctypes.WinDLL('psapi');kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    value=Counters();value.cb=ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(value),value.cb):return {}
    return {'working_set_MB':value.WorkingSetSize/1e6,'peak_working_set_MB':value.PeakWorkingSetSize/1e6,'private_MB':value.PrivateUsage/1e6}

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--no-profile',action='store_true');p.add_argument('--pace-seconds',type=float,default=0);args=p.parse_args()
    def denied(*a,**k):raise AssertionError('OFFLINE NETWORK DENIED')
    socket.socket.connect=denied;socket.create_connection=denied
    cfg=replace(Config(),live_mode=True,risk_fraction=.02,max_daily_loss_fraction=.04,max_watchlist=12,max_positions=1,max_position_fraction=1)
    auto=replace(AutomationConfig(),AUTO_TRADING_CAP_KRW=20000,costs_confirmed=True)
    clock=[datetime(2026,10,8,9,0,tzinfo=KST)];latency={};counters={'atomic_json_writes':0,'atomic_json_bytes':0,'subscription_ops':0,'gui_polls':0,'watch_messages':0,'radar_cycles':0}
    def timed(name,fn):
        def wrapped(*a,**k):
            start=time.perf_counter()
            try:return fn(*a,**k)
            finally:latency.setdefault(name,[]).append((time.perf_counter()-start)*1000)
        return wrapped
    import core.util as util
    raw_write=util._atomic_json
    def written(path,value):
        result=raw_write(path,value);counters['atomic_json_writes']+=1;counters['atomic_json_bytes']+=Path(path).stat().st_size;return result
    atomic_patch=patch.object(util,'_atomic_json',side_effect=written);atomic_patch.start()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='night-profile-') as d:
        root=Path(d);folder=root/'runtime/automation-production'
        atomic_json(root/'config.json',asdict(cfg));atomic_json(root/'automation.json',asdict(auto))
        policy=Policy(root/'runtime/automation_policy.json');policy.command('ARMED')
        broker=FakeBroker();broker.cfg=cfg
        console=root/'runtime/operator_console.txt'
        trace=[];shadow_trace=[];cpu_samples=[];start=time.perf_counter();cpu_start=time.process_time();sample_start=start;sample_cpu=cpu_start
        profile=cProfile.Profile(timer=time.thread_time)
        if args.no_profile:
            class NoProfile:
                def enable(self):pass
                def disable(self):pass
                def dump_stats(self,*a):pass
            profile=NoProfile()
        with console.open('w',encoding='utf-8') as stdout,contextlib.redirect_stdout(stdout):
            engine=AutonomousEngine(cfg,auto,policy,folder,broker,lambda:clock[0]);engine.reconcile({'cash':20480,'equity':20480,'holdings':{},'orders':[]});engine.connected=True;engine.market='3'
            detector=PatternDetector(cfg,auto,engine.costs);detector.continuous_observation=True
            for name in ('ingest','signal','evaluation_gates'):setattr(detector,name,timed('observation' if name=='ingest' else 'setup_'+name,getattr(detector,name)))
            engine.entry_gates=timed('risk_gate',engine.entry_gates)
            symbols=[f'{900000+i:06}' for i in range(12)]
            history=bars()
            for b in history:b.update(open=900,high=950,low=880,close=900,volume=2000000)
            lookup={s:{'code':s,'name':'합성','status':'WATCH','source_count':2,'market_status':{'pass':True},'resistance':2000,'previous_close':1000,'overhead_share_estimate':.01} for s in symbols}
            radar=SimpleNamespace(cache={(clock[0].date(),s):history for s in symbols})
            account=SimpleNamespace(need_fills=False,request=SimpleNamespace(set=lambda:None))
            def shadow_event(kind,**kw):
                if kind in {'SHADOW_EVAL','OBSERVATION_READINESS_CHANGED'}:shadow_trace.append({'event':kind,**kw})
                engine.event(kind,**kw)
            prices={};observer=ShadowObserver(cfg,auto,engine.costs,shadow_event);observer.detector.continuous_observation=True
            shadowrows=[{'code':f'{910000+i:06}','name':'합성관찰','source_count':1,'status':'REJECT','reference_price':1000,'affordability':{'entry_eligible':True},'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True},'resistance':2000}} for i in range(32)]
            observer.update(shadowrows,clock[0])
            feed=Feed(broker,symbols);subscriptions=SubscriptionState()
            gui=UserOperations(root,clock=lambda:clock[0]);gui_poll=timed('gui_snapshot',gui.snapshot)
            from automation import runner
            original_parse=runner.parse_tick
            with patch.object(runner,'now',side_effect=lambda:clock[0]),patch.object(runner,'parse_tick',side_effect=timed('parse',original_parse)):
                profile.enable()
                for second in range(181):
                    clock[0]=datetime(2026,10,8,9,0,tzinfo=KST)+timedelta(seconds=second)
                    engine.last_account=time.monotonic();engine.last_radar_at=clock[0]
                    # Unchanged subscriptions are intentionally exercised every loop.
                    before_ops=subscriptions.refresh(engine,detector,feed,set(symbols),clock[0]);counters['subscription_ops']+=int(before_ops)
                    for i,s in enumerate(symbols):
                        price=1000+int(second//30);qty=1000+(second%11)*25
                        item={'type':'0B','item':s,'values':{'20':clock[0].strftime('%H%M%S'),'10':str(price),'15':str(qty),'13':str((second+1)*1500),'27':str(price+1),'28':str(price-1),'290':'2'}}
                        frame={'trnm':'REAL','data':[item]}
                        before=time.perf_counter();process_message(engine,detector,frame,clock[0],set(symbols),lookup,radar,prices,account,True,subscriptions)
                        latency.setdefault('watch_tick_to_evaluation',[]).append((time.perf_counter()-before)*1000);counters['watch_messages']+=1
                        if second%5==0:
                            trial=detector.last_eval.get(s,{})
                            trace.append({'symbol':s,'second':second,'trials':trial,'gates':detector.evaluation_gates(detector.last[s],lookup[s]),'entries':dict(engine.s['entries'])})
                    for row in shadowrows:
                        t=Tick(row['code'],clock[0],1000,1000,(second+1)*1000,1001,999)
                        timed('shadow_tick_to_evaluation',observer.tick)(t,t.time)
                    if second%15==0:
                        cycle=time.perf_counter();r=DynamicRadar(cfg,auto,policy,fallback_source1=True);r.available_capital=20000
                        items=[{'code':s,'reference_price':1000,'source_details':{'1':{'observed_rank':i+1,'values':{}},'2':{'observed_rank':i+1,'values':{}}}} for i,s in enumerate(symbols)]
                        r.update(items,lambda row:observation_structure(history,row['reference_price'],cfg,clock[0].date()),clock[0])
                        latency.setdefault('candidate_cycle',[]).append((time.perf_counter()-cycle)*1000);counters['radar_cycles']+=1
                        gui_poll();counters['gui_polls']+=1
                    if second%10==0:
                        engine.save();atomic_json(folder/'heartbeat.json',{'time':clock[0].isoformat(),'connected':True,'market':'3','session':'SUPPORTED_SESSION','account_ok':True})
                    engine.timer(prices)
                    if args.pace_seconds:
                        time.sleep(max(0,start+args.pace_seconds*(second+1)/181-time.perf_counter()))
                    current=time.perf_counter()
                    if current-sample_start>=.25:
                        cpu_samples.append(100*(time.process_time()-sample_cpu)/(current-sample_start));sample_cpu=time.process_time();sample_start=current
                # Real pattern to INTENT boundary: fake broker, independent temporary books.
                for pattern in ('A','B','C'):
                    f=fixture.AuditFixture();f.setUp()
                    try:
                        f.cfg=replace(f.cfg,risk_fraction=.02,max_daily_loss_fraction=.04)
                        f.e.c=f.cfg
                        detector2,t,watch=fixture.SetupReachabilityTests.build(f,pattern)
                        detector2.ingest(t,t.time);sig=detector2.signal(t,watch)
                        assert sig is not None
                        f.e.entry_gates=timed('risk_gate',f.e.entry_gates)
                        before=time.perf_counter();original_submit=f.e.submit
                        def boundary(*a,**kw):
                            latency.setdefault('signal_to_pre_intent',[]).append((time.perf_counter()-before)*1000)
                            return original_submit(*a,**kw)
                        f.e.submit=boundary
                        order=f.e.enter(sig,t)
                        latency.setdefault('signal_to_fake_accept',[]).append((time.perf_counter()-before)*1000)
                        trace.append({'setup':pattern,'signal':sig,'order_status':order['status'] if order else None,'qty':order['qty'] if order else None})
                    finally:f.doCleanups()
                profile.disable()
        wall=time.perf_counter()-start;cpu=time.process_time()-cpu_start
        files=list(root.rglob('*'));logs=sum(f.stat().st_size for f in files if f.is_file() and (f.suffix=='.jsonl' or f==console));total=logs+counters['atomic_json_bytes']
        stats=None if args.no_profile else pstats.Stats(profile);top=[]
        for (file,line,func),(cc,nc,selftime,cum,callers) in sorted((stats.stats if stats else {}).items(),key=lambda x:x[1][3],reverse=True)[:20]:top.append({'function':func,'file':str(file).replace(str(ROOT),'REPO'),'line':line,'calls':nc,'self_cpu_seconds':selftime,'cumulative_cpu_seconds':cum})
        metrics={'pace_seconds':args.pace_seconds,'virtual_market_seconds':181,'wall_seconds':wall,'cpu_seconds':cpu,'average_cpu_one_core_percent':100*cpu/wall,'average_cpu_machine_percent':100*cpu/wall/(os.cpu_count() or 1),'peak_cpu_one_core_percent':max(cpu_samples,default=0),'peak_cpu_machine_percent':max(cpu_samples,default=0)/(os.cpu_count() or 1),'logical_processors':os.cpu_count(),'disk_logical_write_MB_min':total/1e6*60/wall,'workload_disk_write_MB_min':total/1e6*60/181,'log_growth_MB_min':logs/1e6*60/wall,'workload_log_growth_MB_min':logs/1e6*60/181,'json_writes_min':counters['atomic_json_writes']*60/wall,'subscription_ops_min':counters['subscription_ops']*60/wall,'console_bytes':console.stat().st_size,'log_bytes':logs,'counters':counters,'latency_ms':{k:{'n':len(v),'p50':percentile(v,.5),'p95':percentile(v,.95),'max':max(v)} for k,v in latency.items()},'cpu_top20':top,'orders':0,'network_calls':0,'limitation':'Compressed synthetic 181-second market stream with real code/fake broker/temp disk. CPU sampled process-time; machine normalization divides logical CPU count. Logical write bytes are not physical disk throughput. Candidate cycles exclude real HTTP waits. Production task-manager 30 percent not reproduced.'}
        metrics['memory']=memory_sample()
        args.output.write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8');args.output.with_suffix('.trace.json').write_text(json.dumps(trace,ensure_ascii=False,sort_keys=True,default=str),encoding='utf-8');args.output.with_suffix('.shadow_trace.json').write_text(json.dumps(shadow_trace,ensure_ascii=False,sort_keys=True,default=str),encoding='utf-8');profile.dump_stats(str(args.output.with_suffix('.pstats')))
    atomic_patch.stop();print(json.dumps({k:v for k,v in metrics.items() if k not in {'cpu_top20','latency_ms'}},ensure_ascii=False));print(json.dumps(metrics['latency_ms'],ensure_ascii=False))
if __name__=='__main__':main()

