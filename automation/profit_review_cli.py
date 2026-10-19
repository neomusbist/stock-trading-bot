"""python -m automation.profit_review_cli: offline only, never opens an API."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.settings import load_config
from automation.policy import load_automation
from automation.profit_review import compare_trade,summarize

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--state',type=Path,required=True);p.add_argument('--capture-dir',type=Path,required=True)
    p.add_argument('--day',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--config-root',type=Path,default=ROOT);p.add_argument('--delay-seconds',type=float,default=.5)
    args=p.parse_args();files=sorted(args.capture_dir.glob('trade-review-'+args.day.replace('-','')+'-????.jsonl'))
    if sum(f.stat().st_size for f in files)>64*1024*1024:raise ValueError('BOUNDED_INPUT_LIMIT_64MB')
    records=[]
    for f in files:
        for line in f.open(encoding='utf-8'):
            try:records.append(json.loads(line))
            except ValueError:records.append({'event':'CAPTURE_STATUS','at':args.day+'T00:00:00+09:00',
                'write_errors':1,'corruption_time':'UNKNOWN','corruption_detected':True})
    status=args.capture_dir/'capture-status.json'
    if status.exists():records.append(json.loads(status.read_text(encoding='utf-8')))
    state=json.loads(args.state.read_text(encoding='utf-8'))
    grouped={}
    for t in state['trades']:
        if t.get('pattern')!='FAST' or not t['opened'].startswith(args.day):continue
        key=t.get('trade_id') or (t['symbol'],t['opened'])
        if key not in grouped:grouped[key]=dict(t)
        else:
            grouped[key]['qty']+=t['qty'];grouped[key]['pnl_estimate']+=t['pnl_estimate']
            if grouped[key].get('pnl_source')=='BROKER_QUERY' and t.get('pnl_source')=='BROKER_QUERY':
                grouped[key]['net_pnl']=grouped[key].get('net_pnl',0)+t.get('net_pnl',0)
            else:grouped[key]['pnl_source']='MIXED_OR_UNKNOWN'
    cfg=load_config(args.config_root/'config.json');auto=load_automation(args.config_root/'automation.json')
    reviews=[compare_trade(cfg,auto,t,records,delay_seconds=args.delay_seconds) for t in grouped.values()]
    report={'reviews':reviews,'summary':summarize(reviews)}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report['summary'],ensure_ascii=False))

if __name__=='__main__':main()
