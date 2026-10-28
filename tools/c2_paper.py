"""Offline C2 on frozen real journals; no broker/API/GUI/LIVE write capability."""
import argparse,contextlib,json,os,sys
from pathlib import Path
if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.settings import Config
from automation.policy import AutomationConfig
from tools.benchmark_all_strategies import read_settings
from tools.observe_paper import offline
from research.c2_validation import C2ObservedSession
from research.c2_exits import install_exit_experiment,POLICIES


def run(args):
    output=args.output.resolve()
    if output.exists():raise ValueError('NEW_OUTPUT_DIRECTORY_REQUIRED')
    for path in [*args.events,args.config,args.automation]:
        path=path.resolve()
        if output==path or (path.is_dir() and path in output.parents):raise ValueError('OUTPUT_MUST_BE_OUTSIDE_INPUT')
    cfg=read_settings(args.config,Config);auto=read_settings(args.automation,AutomationConfig)
    output.mkdir(parents=True)
    def session(*a,**kw):
        s=C2ObservedSession(*a,**kw);install_exit_experiment(s.replay,args.exit_policy);return s
    with open(os.devnull,'w') as quiet,contextlib.redirect_stdout(quiet):
        result=offline(args.events,output,cfg,auto,args.initial_cash,args.day,True,session_factory=session)
    result.update(exit_policy=args.exit_policy,input_use='ALREADY_USED_DEVELOPMENT_DATA_NOT_UNSEEN_VALIDATION',
        live_activation_allowed=False,actual_orders=False)
    (output/'RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--events',type=Path,nargs='+',required=True)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--automation',type=Path,required=True)
    p.add_argument('--initial-cash',type=float,required=True);p.add_argument('--day',required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--exit-policy',choices=POLICIES,default='BASELINE')
    r=run(p.parse_args());print(json.dumps(dict(strategy='C2',actual_orders=False,
        stages=r.get('c2_stage_counts'),paper_orders=r.get('orders_submitted',0))))


if __name__=='__main__':main()
