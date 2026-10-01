"""번호로 사용하는 한글 운영 메뉴. 기존 명령만 호출하며 주문 코드를 포함하지 않는다."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
from operator_service import OperatorService,MENUS,read_json
from core.util import ProcessLock

def money(value): return '미확인' if value is None else f'{value:,.0f}원'
def yes(text,reader=input): return reader(text+'\n진행하려면 yes, 취소하려면 Enter: ').strip().lower()=='yes'
def show_status(s,writer=print,details=True):
    writer('\n================ 주식자동매매 =================')
    writer(f"현재 시각: {s['time'][:19].replace('T',' ')} / {'V1.1 운영시간 안 (시장 상태도 확인 필요)' if s['supported'] else 'V1.1 운영시간 밖 — 자동매매 연결/주문 차단, 메뉴·로컬 진단 가능'}")
    writer(f"운영모드: {s['mode']} / 실제 자동매매 설정(live_mode): {'켜짐' if s['live_mode'] else '꺼짐'}")
    writer(f"LIVE 승인: {'ON' if s.get('live_approved') else 'OFF'} / 실제 주문 루프: {s.get('order_loop','NOT RUNNING')}")
    writer('HALT: '+(s.get('halt') or '없음'))
    if s.get('halt'):
        info=s.get('halt_info',{}); writer('!! 주문 안전중단 유지 / 발생: '+str(info.get('occurred_at') or '과거 기록에 시각 없음')+' / 분류: '+str(info.get('classification') or 'MANUAL_REVIEW'))
    process=s['process']; writer(f"감시 프로세스: {process['state']} / 실제매매: {'ON (조건부 허용)' if s['trading_on'] else 'OFF'}")
    if process['running']:
        writer(f"PID: {process.get('pid','확인 필요')} / 시작: {process.get('started','확인 필요')} / 모드: {s['mode']}")
        writer(f"프로세스 heartbeat: {process.get('heartbeat','미확인')} / 시세 heartbeat: {s['observation_time'] or '미확인'}")
        if process.get('managed') and not process.get('process_heartbeat_fresh'): writer('프로세스 heartbeat가 오래됐습니다. 중복 시작하지 않고 상세 상태를 확인하세요.')
    writer(f"현재 후보: {s['candidate_count']}개 / 봇 포지션: {s['position_count']}종목 ({s['positions']}주) / 봇 미체결: {s['pending']}건")
    writer(f"실제 주문 가능 여부: {s['order_permission']}")
    writer(f"승인한도: {money(s['cap'])} / 실제 주문가능현금: {money(s['cash'])} / 가용자본: {money(s['available'])}")
    writer(f"현금 확인 시각: {s['cash_time'] or '미확인'} — 기록값은 현재 잔고를 보장하지 않음")
    writer(f"비용확인(costs_confirmed): {'확인됨' if s['costs_confirmed'] else '미확인 — 일반 LIVE 시작 차단'}")
    writer(f"WebSocket: {s['websocket']} / 계좌조회: {s['account']}")
    writer(f"봇 보유: {s['positions']}주 / 봇 미체결: {s['pending']}건 / 불명확 주문: {s['uncertain']}건")
    writer('삼성전자 005930: HARD_LOCK / 자동매매 금지')
    writer('광전자 017900: HARD_LOCK / 자동매매 금지')
    if not s['hard_ok']: writer('!! 잠금 정의 오류 — 시운전/LIVE 차단 !!')
    writer(f"마지막 판단: {s['last_decision']}\n현재 오류: {s['current_error']}")
    writer('감시는 별도 프로세스입니다. 메뉴 창을 닫아도 감시가 남을 수 있습니다. 중단은 4번/바탕화면 정상중단을 사용하세요.')
    if not details: return
    writer('현대바이오랜드 052260: BREAKEVEN_EXIT / 5,834원 기준, 기존 조건에 따른 매도만')
    writer(f"나인테크 과거 시험매도 기록: {s['test_release']} / 시험 전용 기능 종료 (일반 종목 영구제외 아님)")
    summary=s.get('candidate_summary',{})
    if summary:
        writer('후보 집계 기준시각: '+str(summary.get('timestamp'))+' / 감시 주기: '+str(summary.get('cycle_started')))
        writer('원시 후보 {raw_candidates} / 1주 가능 {affordable} / 1주 불가 {unaffordable} / 미평가 {not_yet_evaluated}'.format(**summary))
        writer('유동성 탈락 {liquidity_fail} / spread 탈락 {spread_fail} / 구조 탈락 {structure_fail} / setup 미완성 {setup_not_ready} / 위험수량 0 {risk_zero_qty} / 최종 진입가능 {final_entry_eligible}'.format(**summary))
        writer('최종 사유별 종목수: '+str(summary.get('reason_counts',{}))+' (최신 판단 기준, 탈락 gate는 중복 가능)')
    writer(f"현재 관찰: {s['watch']} / 마지막 관측: {s['observation_time'] or '없음'}")
    writer(f"마지막 주문: {s['last_order']}\n과거/최근 오류 기록: {s['error']}\n전략버전: {s['strategy_version']}")
    writer('최근 판단 (원본 로그는 그대로 보존):')
    for line in s['decisions']: writer('  '+line)
    if not s['decisions']: writer('  아직 판단 기록 없음')

def show_result(r,writer=print):
    if not r: writer('아직 사용자 메뉴에서 수행한 시운전 결과가 없습니다.'); return
    verdict={'ALREADY_RUNNING':'이미 감시 중입니다.','PASS':'PASS — 확인한 항목 통과','FAIL':'FAIL — 중단/확인 필요','INCONCLUSIVE':'INCONCLUSIVE — 관측 부족, 통과로 인정하지 않음',
             'REQUESTED':'요청 저장 (완료 여부는 상태화면 확인)','SAVED':'설정 저장','STARTED':'시작 요청 완료'}.get(r.get('verdict'),r.get('verdict'))
    writer('\n조회/운영 결과: '+str(verdict))
    for key,label in (('timestamp','검사 시각'),('account_ok','계좌조회'),('websocket_ok','WebSocket'),
                      ('regular_ticks','정규장 틱 관측 수'),('radar_ok','후보검색'),('candidate_count','후보 수'),
                      ('entry_evaluations','기록된 진입 판단 수'),('signals','기록된 진입신호 수'),('orders','실제 주문'),('samsung_orders','삼성전자 주문'),('gwang_orders','광전자 주문'),
                      ('filled_qty','나인테크 실제 체결 수량'),('remaining_qty','나인테크 잔여 수량')):
        if key in r:
            v=r[key]; writer(f"- {label}: {'정상' if v is True else '미확인/부족' if v is False else '미확인' if v is None else v}")
    for key,label in (('cash','실제 주문가능현금'),('cap','승인한도'),('available','실제 자동매매 가용자본'),('proposed_3000_max','향후 3,000원 승인 시 상한')):
        if key in r: writer(f'- {label}: {money(r[key])}')
    for reason in r.get('reasons',[]): writer('- '+reason)
    if r.get('note'): writer('- '+r['note'])

def handle(service,choice,reader=input,writer=print):
    service.select(choice)
    if choice=='1':
        show_status(service.public_status(),writer)
        if service.public_status()['mode'] in {'OFF','STOP'} and yes('실주문이 꺼진 0원 상태이며 미체결/봇 보유가 없으면 조회 대기 ARMED로 전환할 수 있습니다.',reader):
            show_result(service.arm_safe(True),writer)
    elif choice=='2': show_result(service.pilot(writer),writer)
    elif choice=='3':
        status=service.public_status()
        if status['process']['running']: show_result(service.start_continuous(),writer)
        elif status['mode']=='STOP' and status['live_configured'] is False and status['live_mode']:
            if yes('기존 승인한 LIVE 감시를 재개하면 실제 주문이 가능해집니다. 기존 설정/승인이 동일할 때만 재개합니다.',reader):
                show_result(service.resume_live_continuous(True),writer)
        elif status['mode']=='LIVE':
            if yes('LIVE 상태입니다. 계속 감시를 시작하면 기존 승인한도 내 실제 주문이 가능해집니다.',reader):
                show_result(service.start_continuous(confirm_live=True),writer)
        else: show_result(service.start_continuous(),writer)
    elif choice=='4': show_result(service.stop('STOP'),writer)
    elif choice=='5':
        # 생산 메뉴에서 제거. 고립된 과거 계약 재현용 fixture만 select를 통과한다.
        if service.running():
            writer('나인테크 시험매도는 별도 경로입니다. 먼저 4번으로 감시를 안전하게 중단하고 중지를 확인한 뒤 5번을 선택하세요.'); return True
        writer('\n*** 실제 매도 확인 ***\n267320 나인테크 기존 보유 1주만 실제 매도합니다. 다른 종목은 매매하지 않습니다.')
        if yes('전체 LIVE는 켜지지 않습니다. 이 1회 실제 매도를 승인하시겠습니까?',reader): show_result(service.release(True,writer),writer)
        else: writer('취소했습니다. 승인하지 않았습니다.')
    elif choice=='6':
        if service.running():
            writer('실행 중에는 LIVE 설정을 바꾸지 않습니다. 먼저 4번으로 안전하게 중단하고 감시 중지를 확인한 뒤 6번을 선택하세요.'); return True
        ready=service.prepare_live(writer); show_result(ready,writer)
        if not service.public_status()['costs_confirmed']:
            writer('비용 설정 미확인: 실제 확인한 수수료/세금이 없으면 여기서 중단하세요. 예시값을 자동 승인하지 않습니다.')
            if yes('증권사에서 실제 확인한 비용을 지금 입력하시겠습니까? (LIVE는 아직 켜지지 않음)',reader):
                buy=reader('매수 수수료율 (% 숫자): '); sell=reader('매도 수수료율 (% 숫자): '); tax=reader('매도 세금률 (% 숫자): ')
                if yes('입력한 세 값이 실제 확인한 비용입니까?',reader):
                    show_result(service.confirm_costs(buy,sell,tax,True),writer)
                    ready=service.prepare_live(writer); show_result(ready,writer)
        if ready.get('verdict')=='PASS' and yes('준비 점검만 통과했습니다. 별도 금액 승인과 최종 확인을 거쳐 실제 LIVE를 시작하시겠습니까?',reader):
            remaining=service.live_ready_seconds(); writer(f'준비 검사 유효시간: 60초 / 남은 시간: {remaining:.0f}초')
            if remaining<=0:
                writer('준비 검사 유효시간이 지났거나 설정이 바뀌었습니다. 6번에서 다시 검사한 뒤 별도로 승인하세요.'); return True
            amount=reader('승인할 금액을 원 단위로 입력하세요. 이번 시험은 3000만 허용: ').strip()
            if service.live_ready_seconds()<=0:
                writer('입력 중 준비 검사가 만료됐습니다. LIVE는 켜지지 않았습니다. 6번에서 재검사하세요.'); return True
            confirmation=reader('실제 주문이 능해집니다. 진행하려면 실제매매시작 을 정확히 입력 (Enter는 취소): ').strip()
            show_result(service.activate_live(amount,confirmation,start=False),writer)
    elif choice in {'7','8'}:
        if yes('봇 포지션 청산을 요청합니다. 삼성전자·광전자는 자동매도하지 않습니다. 진행할까요?',reader):
            show_result(service.stop({'7':'STOP_FLAT','8':'KILL'}[choice]),writer)
    elif choice=='10':
        # 생산 메뉴에서 제거. 과거 취소 계약을 재현하는 비활성 경로.
        if yes('아직 제출되지 않은 나인테크 1주 시험매도 승인을 취소하시겠습니까?',reader):
            show_result(service.cancel_test_release(True),writer)
        else:
            writer('취소하지 않았습니다.')
    elif choice=='9':
        show_result(read_json(service.root/'runtime/operator_last_result.json'),writer)
        show_status(service.public_status(),writer)
    elif choice=='0':
        if service.running():
            if yes('감시가 실행 중입니다. 메뉴만 닫으면 별도 감시는 계속됩니다. 메뉴만 닫으시겠습니까? (Enter는 안전 STOP 요청)',reader):
                writer('메뉴만 종료합니다. 감시는 계속 실행 중입니다. 바탕화면 정상중단/비상정지 파일로 중단할 수 있습니다.'); return False
            show_result(service.stop('STOP'),writer)
            writer('감시가 실행 중이라 정상 중단을 요청했습니다. 보유/미체결 처리가 끝나기 전에는 화면을 닫지 않습니다. [1]에서 확인하세요.')
            return True
        writer('화면을 종료합니다.'); return False
    return True

def main(reader=input,writer=print,service=None,argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--self-check',action='store_true')
    p.add_argument('--emergency',choices=['STOP','STOP_FLAT','KILL']); args=p.parse_args(argv)
    s=service or OperatorService()
    if args.self_check: show_status(s.public_status(),writer); return 0
    if args.emergency:
        show_result(s.stop(args.emergency),writer)
        writer('잠시 뒤 메인 화면 [1]에서 실제 보유/미체결 상태를 확인하세요. 나인테크 미체결은 HTS 확인이 필요할 수 있습니다.')
        if sys.stdin.isatty(): reader('창을 닫으려면 Enter: ')
        return 0
    with ProcessLock(s.root/'runtime/operator_ui.lock'):
        while True:
            try:
                try: show_status(s.public_status(),writer,details=False)
                except Exception as error:
                    writer('현재 상태를 읽을 수 없습니다. 시운전/LIVE를 시작하지 않습니다. 중단 요청 또는 종료만 선택하세요. '+str(error))
                for number,label in MENUS.items(): writer(f'[{number}] {label}')
                choice=reader('\n번호를 입력하고 Enter를 누르세요: ').strip()
                if choice=='': continue
                if not handle(s,choice,reader,writer): return 0
            except EOFError:
                if s.running(): show_result(s.stop('STOP'),writer)
                return 0
            except KeyboardInterrupt:
                show_result(s.stop('STOP'),writer); writer('정상 중단을 요청했습니다. 보호/미체결 확인을 위해 메뉴를 유지합니다.')
            except Exception as e:
                try: show_result(s.save_error(e),writer)
                except Exception: writer('설정/기록을 읽을 수 없습니다. 실제 동작을 시작하지 않았습니다. '+str(e))

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as e:
        print('운영 메뉴 시작 오류:',e)
        if sys.stdin.isatty(): input('창을 닫으려면 Enter: ')
        raise SystemExit(1)
