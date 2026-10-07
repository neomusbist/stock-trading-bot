"""사용자가 직접 실행하는 운영명령. RELEASE/LIVE는 명시적 확인 문자열을 요구한다."""
import argparse
import json
from core.settings import ROOT
from core.util import ProcessLock
from automation.policy import Policy

def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['status','OFF','ARMED','LIVE','STOP','STOP_FLAT','KILL','RESET_KILL','RELEASE'])
    p.add_argument('symbol',nargs='?'); p.add_argument('--confirm')
    a=p.parse_args()
    with ProcessLock(ROOT/'runtime/control.lock'):
        policy=Policy(ROOT/'runtime/automation_policy.json')
        if a.command=='KILL' and a.confirm!='KILL':
            raise ValueError("비상정지(KILL)를 실행하려면 명시적 확인 옵션(--confirm KILL)이 필요합니다.")
        if a.command=='RESET_KILL' and a.confirm!='RESET KILL':
            raise ValueError("비상정지 해제를 실행하려면 명시적 확인 옵션(--confirm \"RESET KILL\")이 필요합니다.")
        if a.command in {'OFF','ARMED'}:
            for path in (ROOT/'runtime').glob('automation-*/state.json'):
                state=json.loads(path.read_text(encoding='utf-8'))
                if state['positions'] or any(o['status'] not in {'FILLED','CANCELED','REJECTED'} for o in state['orders'].values()):
                    raise ValueError('관리 포지션/미체결이 있습니다. 보호를 유지하는 STOP 또는 STOP_FLAT을 사용하세요.')
        if a.command!='status': policy.command(a.command,a.symbol,a.confirm)
        print('운영상태:',policy.mode)
        print('MANUAL_HOLD (HARD_LOCK):','005930,017900 (모든 자동매수/매도 최종 차단)')
        print('BREAKEVEN_EXIT: 052260 >=5834원 (전량 지정가/실제 체결 확인)')
        print('과거 시험매도 기록 (기능 종료):',policy.state.get('test_capital_release',{'status':'DISABLED'}))
        if a.command=='LIVE': print('live_mode=true, 허용 현금, 비용 확인과 계좌 대조가 모두 통과해야 실주문이 가능합니다.')

if __name__=='__main__': main()
