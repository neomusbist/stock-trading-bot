"""Bounded journal-reference reconstruction, never full real-time signal bars.

SHADOW journals are selected and can lose intervening prints. Their OHLCV may
be useful as references but cannot assert an entire minute of observation.
The original AG cache is retained; an explicit separate --output is required.
"""
import argparse
from collections import defaultdict,Counter
from datetime import timedelta
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_data import read_frames,file_sha,json_values


def build(paths,day):
    rows=defaultdict(dict);current={};stats=Counter();prior={}
    def finish(symbol,bar,known):
        bar=dict(bar);bar['available_at']=known;bar['observation_source']='SAMPLED_JOURNAL_REFERENCE'
        bar['gap_reason']='SELECTED_JOURNAL_NOT_FULL_RECEIVER_COVERAGE'
        rows[symbol][bar['time']]=bar
    for f in read_frames(paths,day,stats=stats):
        t=f.tick;minute=t.time.replace(second=0,microsecond=0);b=current.get(t.symbol)
        previous=prior.get(t.symbol)
        if previous and t.cumulative<=previous.cumulative:stats['DUPLICATE_OR_REVERSED_CUMULATIVE']+=1;continue
        missing=bool(previous and t.cumulative-previous.cumulative!=t.volume)
        stats['CUMULATIVE_DELTA_MISMATCH']+=missing;prior[t.symbol]=t
        if b and b['time']!=minute:
            finish(t.symbol,b,max(b['time']+timedelta(minutes=1),f.available_at));b=None
        if b is None:
            b={'time':minute,'open':t.price,'high':t.price,'low':t.price,'close':t.price,
               'volume':0,'first_second':t.time.second,'tick_count':0,'missing_cumulative_deltas':0}
            current[t.symbol]=b
        b['high']=max(b['high'],t.price);b['low']=min(b['low'],t.price);b['close']=t.price
        b['volume']+=t.volume;b['tick_count']+=1;b['missing_cumulative_deltas']+=missing
    # EOF is not proof that the current minute was complete. Retain separately
    # with UNKNOWN availability instead of creating a fake close-time fill.
    result={'day':day,'rows':{s:{'bars':[bs[k] for k in sorted(bs)]} for s,bs in rows.items()},
        'unconfirmed_eof_bars':list(current.values()),'source':'SAMPLED_JOURNAL_REFERENCE_ONLY',
        'counts':dict(stats),'tradeability':'UNKNOWN','actual_order_permission':False,
        'inputs':[{'name':Path(p).name,'sha256':file_sha(p)} for p in paths]}
    return json_values(result)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ticks',type=Path,nargs='+',required=True);p.add_argument('--day',default='2026-10-19')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    result=build(a.ticks,a.day);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(a.output),'source':result['source'],'tradeability':'UNKNOWN'},ensure_ascii=False))

if __name__=='__main__':main()
