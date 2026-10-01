"""안전 예시만 로컬 설정으로 생성. 기존 설정을 덮어쓰지 않는다."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def setup(root=ROOT):
    root=Path(root); created=[]
    for name in ('config','automation'):
        dest=root/(name+'.json')
        if not dest.exists():
            data=json.loads((root/(name+'.example.json')).read_text(encoding='utf-8-sig'))
            if name=='config' and data['live_mode'] is not False: raise ValueError('안전 예시 live_mode=false 필요')
            if name=='automation' and (data['AUTO_TRADING_CAP_KRW']!=0 or data['costs_confirmed'] is not False): raise ValueError('안전 예시 cap=0/costs_confirmed=false 필요')
            with dest.open('x',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2)
            created.append(dest.name)
    return created
if __name__=='__main__': print('안전 설정 생성:',', '.join(setup()) or '없음 — 기존 설정 보존')
