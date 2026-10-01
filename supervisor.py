"""크래시 후 동일 장부로 재시작. 콘솔 중단은 STOP 요청이며 강제 프로세스 종료가 아니다."""
import os
import subprocess
import sys
import time
from pathlib import Path
from core.settings import ROOT,now
from core.util import ProcessLock,atomic_json
from automation.policy import Policy
from automation.runner import user_command

def main():
    with ProcessLock(ROOT/'runtime/supervisor.lock'):
        policy=Policy(ROOT/'runtime/automation_policy.json'); failures=0
        while True:
            child=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'main.py'),*sys.argv[1:]],cwd=ROOT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
            launched=time.monotonic()
            while child.poll() is None:
                try: time.sleep(.5)
                except KeyboardInterrupt:
                    user_command(policy,'STOP')
                    print('STOP 요청: 관리 포지션과 미체결이 정리될 때까지 실행을 유지합니다.')
            if child.returncode==0: return 0
            failures=failures+1 if time.monotonic()-launched<300 else 1
            delay=min(30,2**min(failures,5))
            atomic_json(ROOT/'runtime/supervisor_alert.json',{'time':now().isoformat(),'exit_code':child.returncode,
                'failures':failures,'retry_seconds':delay,'action':'동일 장부/계좌 대조 후 재시작. 반복 오류는 HTS에서 확인'})
            print(f'실행 오류. {delay}초 후 같은 장부로 보호/계좌 복구를 재시도합니다.')
            try: time.sleep(delay)
            except KeyboardInterrupt: user_command(policy,'STOP')

if __name__=='__main__': sys.exit(main())
