"""GUI 의존성/초기화 실패 시 기존 콘솔로 연결. 주문이나 설정을 변경하지 않는다."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
for folder in ('vendor','gui_vendor'): sys.path.insert(0,str(ROOT/folder))

def launch(gui=None,console=None):
    try:
        if gui is None:
            from operator_gui import run
            gui=run
        return gui()
    except Exception as error:
        print('GUI를 열지 못했습니다:',type(error).__name__,str(error))
        print('기존 콘솔로 전환합니다. 감시가 실행 중이라면 계속될 수 있습니다. 콘솔 중지/비상정지로 확인하세요.')
        if console is None:
            from operator_console import main
            console=main
        return console()

if __name__=='__main__': raise SystemExit(launch())
