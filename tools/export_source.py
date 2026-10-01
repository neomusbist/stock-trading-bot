"""리뷰한 소스만 새 빈 폴더로 복사한다. 운영자료/백업은 변경하지 않는다."""
import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import date,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from repo_guard import allowed,scan

def export(root,dest):
    root=Path(root).resolve(); dest=Path(dest).resolve()
    if dest==root or root in dest.parents and dest.name!='github_source': raise ValueError('별도 새 소스 폴더를 지정하세요.')
    if dest.exists(): raise FileExistsError('기존 폴더를 덮어쓰지 않습니다. 새 이름으로 지정하세요.')
    dest.mkdir(parents=True)
    selected=[]
    for folder in (root,root/'core',root/'automation',root/'tests',root/'tools'):
        for path in folder.iterdir():
            if not path.is_file(): continue
            name=path.relative_to(root).as_posix()
            if allowed(name) and name!='SOURCE_MANIFEST.json':
                target=dest/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,target); selected.append(name)
    # 테스트에서 사용한 요청/응답 필드 식별자만 남긴다. 공식 설명/문서 원본은 제외.
    contract=json.loads((root/'reference/contract.json').read_text(encoding='utf-8'))
    keys={'kt10000','ka00198','ka10027','ka10029','ka10032','ka10031'}
    slim={key:{side:{part:[{k:v for k,v in field.items() if k in {'element','required'}} for field in fields]
        for part,fields in contract[key][side].items() if part=='body'} for side in ('request','response')} for key in sorted(keys)}
    # 설치본 전용 추가 제외규칙은 업로드 폴더의 최소 합성 fixture에는 적용하지 않는다.
    ignore=(dest/'.gitignore').read_text(encoding='utf-8').split('# LOCAL_INSTALL_ONLY')[0]
    (dest/'.gitignore').write_text(ignore,encoding='utf-8')
    (dest/'reference').mkdir(); (dest/'reference/contract.json').write_text(json.dumps(slim,ensure_ascii=False,indent=2),encoding='utf-8'); selected.append('reference/contract.json')
    fixture=dest/'tests/fixtures'; fixture.mkdir()
    with (fixture/'original_052260_daily.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['datetime','open','high','low','close','volume']); w.writeheader()
        for i in range(250): w.writerow({'datetime':(date(2026,9,30)-timedelta(days=250-i)).isoformat(),'open':9900,'high':9950,'low':9850,'close':9900,'volume':2000000})
    (fixture/'README.md').write_text('이 CSV는 공개 소스용 합성 250행입니다. 과거 테스트 경로 이름만 호환합니다. 실제 종목의 시세/계좌 데이터가 아닙니다. 기존 테스트의 UNKNOWN 및 500행 미만 조건을 그대로 검사합니다.\n',encoding='utf-8')
    selected+=['tests/fixtures/original_052260_daily.csv','tests/fixtures/README.md']
    manifest={'files':[{'path':n,'sha256':hashlib.sha256((dest/n).read_bytes()).hexdigest()} for n in sorted(selected)],
        'local_data_included':False,'credentials_included':False,'fixture':'synthetic','api_fixture':'field identifiers only'}
    (dest/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    result=scan(dest)
    if not result['passed']: raise ValueError('소스 검사 실패 — 값은 표시하지 않음: '+json.dumps(result,ensure_ascii=False))
    return result
if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]); p.add_argument('--dest',type=Path,required=True)
    args=p.parse_args(); print(json.dumps(export(args.root,args.dest),ensure_ascii=False,indent=2))
