from pathlib import Path
import json
from core.util import write_csv

def export(root,rows,state=None):
    """xlsx는 요약 산출물이며 주문 상태의 원본은 runtime/state.json이다."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    root=Path(root); w=Workbook(); w.remove(w.active)
    tables={'후보구조':rows,'감시종목':[r for r in rows if r.get('status')=='WATCH']}
    if state:
        tables.update({'포지션':list(state['positions'].values()),'주문':list(state['orders'].values()),'거래복기':state['trades'],
          '운영': [{'거래일':state['day'],'모드':state.get('automation',{}).get('mode','dry-run' if state['identity']=='paper' else 'live'),
          '신규진입중단':state['halt'],'실현손익_비용추정':state['realized'],'손실누계_비용추정':state['daily_loss']} ]})
        if 'automation' in state:
            from automation.performance import report
            stats=report(state['trades'])
            tables.update({'전체성과':[stats['all']],'완결거래':stats['completed_trades'],'전략성과':[{'pattern':k,**v} for k,v in stats['patterns'].items()],
                '시간대성과':[{'hour':k,**v} for k,v in stats['hours'].items()],
                '자본정책':[{k:v for k,v in state['automation'].items() if k not in {'existing','config_hash'}}]})
            if state.get('verification'):
                tables['검증상태']=[state['verification']]
    for name,data in tables.items():
        s=w.create_sheet(name); fields=list(dict.fromkeys(k for r in data for k in r))
        if not fields: s.append(['해당 자료 없음']); continue
        s.append(fields)
        for row in data:
            values=[]
            for k in fields:
                v=row.get(k)
                if isinstance(v,(dict,list)): v=json.dumps(v,ensure_ascii=False)
                # 외부 종목명/문구가 수식으로 해석되지 않도록 한다.
                if isinstance(v,str) and v[:1] in ('=','+','-','@'): v="'"+v
                values.append(v)
            s.append(values)
        s.freeze_panes='A2'; s.auto_filter.ref=s.dimensions
        for cell in s[1]: cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='25465F')
        for col in s.columns:
            s.column_dimensions[col[0].column_letter].width=min(44,max(12,max(len(str(x.value or '')) for x in col)+2))
    destination=root/'swing_summary.xlsx'
    temp=root/'swing_summary.tmp.xlsx'; w.save(temp)
    try: temp.replace(destination)
    except PermissionError:
        destination=root/'swing_summary_latest.xlsx'; temp.replace(destination)
    if state:
        fields=list(dict.fromkeys(['symbol','opened','closed','qty','entry','exit','pnl_estimate','MFE','MAE','initial_stop','final_stop','reason']+[k for t in state['trades'] for k in t]))
        write_csv(root/'trade_review.csv',state['trades'],fields=fields)
    return destination

