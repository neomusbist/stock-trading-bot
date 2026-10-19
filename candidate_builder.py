"""기존 다섯 후보 소스를 유지하고 장기/최근 가격구조로 감시목록을 압축한다."""
import argparse
import csv
import json
import statistics
from datetime import date
from pathlib import Path
from core.settings import ROOT, load_config, now
from core.util import number, code, read_csv, write_csv, atomic_json

RANKS=[
 ('ka10029','exp_cntr_flu_rt_upper',30,{'mrkt_tp':'000','sort_tp':'1','trde_qty_cnd':'0','stk_cnd':'4','crd_cnd':'0','pric_cnd':'0','stex_tp':'1'}),
 ('ka10032','trde_prica_upper',50,{'mrkt_tp':'000','mang_stk_incls':'0','stex_tp':'1'}),
 ('ka10027','pred_pre_flu_rt_upper',30,{'mrkt_tp':'000','sort_tp':'1','trde_qty_cnd':'0000','stk_cnd':'4','crd_cnd':'0','updown_incls':'0','pric_cnd':'0','trde_prica_cnd':'0','stex_tp':'1'}),
 ('ka00198','item_inq_rank',20,{'qry_tp':'1'}),
 ('ka10031','pred_trde_qty_upper',30,{'mrkt_tp':'000','qry_tp':'1','rank_strt':'0','rank_end':'100','stex_tp':'1'})]

def collect_universe(rest):
    combined={}
    for api,key,limit,payload in RANKS:
        rows,_=rest.pages(api,payload,key,path='/api/dostk/stkinfo' if api=='ka00198' else '/api/dostk/rkinfo',limit=limit)
        for r in rows:
            symbol=code(r['stk_cd']); name=r['stk_nm']
            x=combined.setdefault(symbol,{'code':symbol,'name':name,'sources':[],'reference_price':None})
            x['sources'].append(api)
            if api=='ka10029' and number(r['exp_cntr_pric'])>0:
                x['reference_price']=number(r['exp_cntr_pric'])
    for x in combined.values():
        x['sources']='|'.join(x['sources'])
    return list(combined.values())

def bars_from_csv(path,asof):
    result=[]; seen=set()
    for r in read_csv(path):
        d=date.fromisoformat(r['datetime'][:10])
        if d>=asof or str(r.get('is_complete','True')).lower()!='true':
            continue
        if d in seen:
            raise ValueError('일봉 날짜 중복')
        seen.add(d)
        b={k:number(r[k]) for k in ('open','high','low','close','volume')}
        if not (0<b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']):
            raise ValueError('OHLC 오류')
        b['datetime']=d.isoformat(); result.append(b)
    return sorted(result,key=lambda x:x['datetime'])

def fetch_bars(rest,symbol,cfg,folder,asof):
    rows,_=rest.pages('ka10081',{'stk_cd':symbol,'base_dt':asof.strftime('%Y%m%d'),'upd_stkpc_tp':'1'},
        'stk_dt_pole_chart_qry',path='/api/dostk/chart',limit=cfg.history_bars+1)
    out=[]
    for r in rows:
        dt=r['dt']; d=f'{dt[:4]}-{dt[4:6]}-{dt[6:8]}'
        out.append({'datetime':d,'open':number(r['open_pric']),'high':number(r['high_pric']),
          'low':number(r['low_pric']),'close':number(r['cur_prc']),'volume':number(r['trde_qty']),'is_complete':d<asof.isoformat()})
    p=folder/f'{symbol}_daily.csv'; write_csv(p,out)
    return bars_from_csv(p,asof)

def structure(bars,reference,cfg,asof):
    min_bars = getattr(cfg, 'min_young_history_bars', 60) if len(bars) < cfg.min_history_bars else cfg.min_history_bars
    if len(bars)<min_bars:
        return {'status':'UNKNOWN','reason':f'일봉 부족 {len(bars)}/{min_bars}','history_count':len(bars)}
    bars=bars[-cfg.history_bars:]
    if (asof-date.fromisoformat(bars[-1]['datetime'])).days>cfg.max_history_age_days:
        return {'status':'UNKNOWN','reason':'과거 일봉 갱신 필요','history_count':len(bars)}
    p=reference or bars[-1]['close']
    hi=max(b['high'] for b in bars); lo=min(b['low'] for b in bars)
    total=sum(b['volume'] for b in bars)
    if not total or hi==lo or p<=0:
        return {'status':'UNKNOWN','reason':'유효 가격/거래량 부족'}
    # 각 일봉 거래량이 low~high에 균등 분포했다고 가정한 명시적인 근사치.
    overhead=0
    for b in bars:
        span=b['high']-b['low']
        portion=max(0,min(b['high'],p*(1+cfg.overhead_band))-max(b['low'],p))/span if span else float(p<b['close']<=p*(1+cfg.overhead_band))
        overhead+=portion*b['volume']
    recent=bars[-20:]; support=min(b['low'] for b in bars[-5:])
    recent_high=max(b['high'] for b in recent)
    resistance_candidates=[bars[i]['high'] for i in range(2,len(bars)-2)
        if bars[i]['high']>p and bars[i]['high']>=max(x['high'] for x in bars[i-2:i+3])]
    resistance=min(resistance_candidates) if resistance_candidates else None
    room=resistance/p-1 if resistance else None
    range20=(recent_high-min(b['low'] for b in recent))/p
    turnover=statistics.median(b['close']*b['volume'] for b in recent)
    reasons=[]
    if overhead/total>cfg.max_overhead_share: reasons.append('상단 거래 밀집 추정치 초과')
    if room is not None and room<cfg.min_resistance_room: reasons.append('상단 저항 여유 부족')
    if range20>cfg.max_recent_range: reasons.append('최근 20일 가격폭 과대')
    if p<=support: reasons.append('최근 5일 저점 이탈')
    if turnover<cfg.min_daily_turnover: reasons.append('거래대금 부족')
    return {'status':'REJECT' if reasons else 'ELIGIBLE','reason':' | '.join(reasons) or '구조 필터 통과',
        'history_count':len(bars),'history_start':bars[0]['datetime'],'history_end':bars[-1]['datetime'],
        'reference_price':p,'reference_basis':'장전 예상체결가' if reference else '전일 종가',
        'previous_close':bars[-1]['close'],'long_low':lo,'long_high':hi,'long_position':(p-lo)/(hi-lo),
        'overhead_share_estimate':overhead/total,'resistance':resistance,'resistance_room':room,
        'recent_high':recent_high,'recent_support':support,'range20':range20,'median_turnover20':turnover}

def build(cfg,rest=None,offline=False,asof=None,root=ROOT):
    asof=asof or now().date(); root=Path(root)
    if offline:
        universe=[{'code':code(r['종목코드']),'name':r['종목명'],'sources':r.get('선정근거',''),'reference_price':None}
            for r in read_csv(root/'reference/original/symbols_today.csv')]
    else:
        universe=collect_universe(rest)
    rows=[]
    for item in universe:
        x={**item,'asof':asof.isoformat()}
        try:
            name=x['name'].upper()
            if not x['code'].endswith('0') or '스팩' in name or name.endswith('우') or any(name.startswith(b) for b in ('KODEX','TIGER','ACE','RISE','SOL','PLUS','KOSEF','HANARO','ARIRANG','WOORI','TIMEFOLIO','KBSTAR')) or 'ETN' in name:
                x.update(status='REJECT',reason='V1 보통주 대상 외(보수적 명칭/코드 필터)')
            else:
                bars=bars_from_csv(root/'data/raw'/f'{x["code"]}_daily.csv',asof) if offline else fetch_bars(rest,x['code'],cfg,root/'data/raw',asof)
                x.update(structure(bars,item['reference_price'],cfg,asof))
        except (ValueError,KeyError,OSError,RuntimeError) as e:
            x.update(status='UNKNOWN',reason=str(e))
        rows.append(x)
    eligible=sorted([r for r in rows if r['status']=='ELIGIBLE'],key=lambda r:(r['overhead_share_estimate'],-r['median_turnover20'],r['code']))
    selected=eligible[:cfg.max_watchlist]
    for r in eligible:
        r['status']='WATCH' if r in selected else 'RESERVE'
    write_csv(root/'candidate_audit.csv',rows)
    write_csv(root/'symbols_today.csv',selected,fields=list(dict.fromkeys(k for r in rows for k in r)))
    atomic_json(root/'watchlist.json',{'asof':asof.isoformat(),'offline':offline,'config_hash':cfg.fingerprint(),'items':selected})
    return rows,selected

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--offline',action='store_true'); p.add_argument('--config',default=str(ROOT/'config.json'))
    args=p.parse_args(); cfg=load_config(args.config)
    from core.broker import Rest
    from automation.session import supported
    if not args.offline and not supported(now()):
        print('OUT_OF_SUPPORTED_SESSION: 정규장 지원 시간 밖 수집을 시작하지 않습니다.')
        raise SystemExit(0)
    rest=None if args.offline else Rest(cfg)
    if rest is not None: rest.session_guard=lambda:supported(now())
    rows,watch=build(cfg,rest=rest,offline=args.offline)
    print(f'후보 {len(rows)} / 감시 {len(watch)} / UNKNOWN은 매수 신호에 사용하지 않음')
