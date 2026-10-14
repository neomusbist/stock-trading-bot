"""GUI/간소화 콘솔의 운영 서비스. 전략·주문 API에는 접근하지 않는다."""
import json
from dataclasses import asdict, replace
from datetime import datetime
from core.util import ProcessLock, atomic_json
from operator_service import OperatorService, read_json, tail_events, age, in_window, decision_line, STRATEGY_VERSION
from automation.policy import Policy, HARD_LOCKS, EXISTING_POLICIES, BREAKEVEN_EXITS
from automation.session import supported, control_supported, after_control_supported, SessionPlan
from automation.decisions import BUILD_ID
from local_diagnostics import redact

PUBLIC_MENUS={'1':'상태 / 진단','2':'자동매매 시작','3':'자동매매 중지',
              '4':'설정 / 승인한도','5':'거래기록 / 로그','6':'봇 포지션 청산 후 중지',
              '9':'비상정지','0':'프로그램 종료'}
GATE_NAMES={'warmup':'관측 준비','acceleration':'가속','liquidity':'거래대금','spread':'호가 간격',
    'structure':'일봉 구조','market_status':'종목상태','pattern':'Setup','breakout':'돌파','chase':'추격 거리',
    'invalidation':'무효화 거리','risk_reward':'보상/위험','resistance':'상단 여유','economic':'비용/효율',
    'policy':'운영 승인','session':'거래시간','connection_account':'계좌/통신','tick':'시세 신선도',
    'repeat_slots':'재진입/보유한도','risk_quantity':'위험수량','pullback_structure':'눌림 지지 확인',
    'breakout_structure':'돌파 범위 지지·기존 고점 확인'}
CANDIDATE_FIELDS={'raw_candidates':'원시 후보','affordable':'1주 매수 가능',
    'unaffordable':'1주 매수 불가','affordability_unknown_or_observation':'자본확인 불가 / 관찰',
    'liquidity_fail':'유동성 탈락','spread_fail':'호가 간격 탈락','structure_fail':'구조 탈락',
    'setup_not_ready':'Setup 미완성','risk_zero_qty':'위험수량 0','final_entry_eligible':'최종 진입가능'}
UI_REASONS={'RISK_BUDGET_ZERO_QTY':'거래당 위험예산으로 1주 매수 불가',
    'NO_BUY_MIN_QTY_ZERO':'비용·자본·위험 조건에 따른 허용수량 0',
    'CAP_EXHAUSTED':'승인자본 없음 또는 사용 가능한 자본 소진',
    'NO_BUY_POLICY_BLOCK':'운영 승인 또는 기존 보유 정책으로 매수 차단',
    'NO_BUY_REPEAT_OR_SLOT_LIMIT':'재진입 또는 동시 보유 한도',
    'NO_BUY_ACCOUNT_OR_CONNECTION':'계좌 또는 연결 확인 필요',
    'NO_BUY_OUTSIDE_ENTRY_WINDOW':'신규진입 가능시간 밖',
    'NO_BUY_STALE_OR_NON_REGULAR_TICK':'시세 지연 또는 정규장 틱 미확인',
    'LIVE_NOT_AUTHORIZED':'실제매매 명시 승인 없음','LIVE_MODE_DISABLED':'실제매매 설정 꺼짐',
    'COSTS_UNCONFIRMED':'실제 비용 미확인','EXTERNAL_HOLD_NO_BUY':'기존 외부 보유: 추가매수 금지',
    'CONSECUTIVE_LOSS_LIMIT':'연속손실 제한','MODE_STOP_NO_NEW_ENTRY':'정상 중지 요청: 신규진입 차단',
    'MODE_KILL_NO_NEW_ENTRY':'비상정지: 신규진입 차단','NOT_YET_EVALUATED':'아직 조건 평가 전'}
UI_REASONS.update({'EXTERNAL_BOOK_STALE':'기존 보유 매도 호가가 오래되어 전량 매도 가능 여부 미확인',
    'EXTERNAL_BOOK_UNKNOWN':'기존 보유 매도 호가 미확인','EXTERNAL_BOOK_NOT_FILLABLE':'기존 보유 전량 매도 가능한 호가 부족',
    'ACCOUNT_CAPITAL_UNAVAILABLE':'신선한 계좌 자본 조회 미확인','INVALIDATION_UNKNOWN_OR_INVALID':'구조적 무효화가격 미확인 또는 유효하지 않음'})
UI_REASONS.update({
    'AUTO_CAP_ZERO': '승인한도 0원 (관찰 전용)',
    'ONE_SHARE_UNAFFORDABLE': '1주 매수 자본 부족',
    'SHADOW_ONLY': '단일 소스 (SHADOW 관찰 데이터 축적 중)',
    'WATCH_FALLBACK_SOURCE1': 'WATCH 승격 (단일소스 보조 슬롯)',
    'RESISTANCE_CLEARANCE_INSUFFICIENT': '상단 저항 여유 부족',
    'ACCELERATION_FAIL': '체결 가속 미달',
    'SPREAD_FAIL': '호가 스프레드 과대',
    'LIQUIDITY_FAIL': '거래대금 기준 미달',
    'STRUCTURE_FAIL': '일봉 구조 기준 미달',
    'STOP_DISTANCE_EXCEEDED': '무효화(손절) 거리 과대',
    'RR_RATIO_INSUFFICIENT': '손익비(RR) 부족',
    'WARMUP_INCOMPLETE': '완성봉 데이터 축적 중',
    'OUTSIDE_WINDOW': '신규진입 가능시간 밖',
    'EFFICIENCY_UNKNOWN_OR_LOW': '비용 대비 기대수익 낮음',
    'NO_BUY_EFFICIENCY': '비용 대비 기대수익 낮음',
    'SETUP_NOT_READY': 'Setup 미완성 (A/B/C 형상 대기)',
    'MANUAL_HOLD': '수동 보유 관리 (HARD_LOCK)',
    'SELL_SESSION_FLATTEN': '정규장 마감 청산 (15:10)',
    'SELL_STOP_FLAT': '사용자 전량 청산 요청',
    'SELL_EMERGENCY': '비상정지 청산',
    'SELL_STALE_QUOTE': '시세 지연 / 호가 미확인 청산',
    'SELL_PROFIT_PROTECTION_HIT': '보호선(트레일링) 이탈 청산',
    'SELL_INVALIDATION_HIT': '초기 무효화선 이탈 청산',
    'SELL_BREAKEVEN_EXIT': '본전 청산 조건 도달',
    'SELL_EXPLICIT_TEST_RELEASE': '시험매도 승인 실행',
    'EVALUATION_PENDING': '평가 대기'
})

def format_observation_age(timestamp_str, clock=None, is_stale=False):
    """중립적인 경과 시간 표시. 데이터가 런타임에서 stale로 판정된 경우에만 지연 경고 표시."""
    if not timestamp_str:
        return '미확인' + (' (데이터 지연)' if is_stale else '')
    try:
        t = datetime.fromisoformat(str(timestamp_str))
        now_dt = clock() if callable(clock) else clock if isinstance(clock, datetime) else datetime.now(t.tzinfo)
        delta = max(0, int((now_dt - t).total_seconds()))
        if delta < 60: text = f"{delta}초 전"
        elif delta < 3600: text = f"{delta // 60}분 {delta % 60}초 전"
        else: text = f"{delta // 3600}시간 전"
    except Exception:
        text = str(timestamp_str)[11:19] or '최근'
    if is_stale:
        text += ' (데이터 지연)'
    return text

SETUP_WAIT_REASONS={
    'INSUFFICIENT_USABLE_BARS':'유효 완성봉 부족','WARMUP_FAIL':'관측 준비 미완료',
    'ACCELERATION_FAIL':'가속 미충족','PRIOR_HIGH_NOT_BROKEN':'직전 고점 미돌파',
    'NO_CONTROLLED_PULLBACK':'눌림 형상 없음','NO_REBREAKOUT':'재돌파 미도달',
    'BOX_NOT_FORMED':'박스 형상 없음','NO_INTRADAY_BOX':'박스 형상 없음',
    'BREAKOUT_NOT_REACHED':'돌파 미도달','RISK_REWARD_FAIL':'RR 미충족','ECONOMIC_FAIL':'비용 대비 여유 부족',
    'ENTRY_CONTEXT_FAIL':'장중 저항까지 상승 여유 부족',
    'ENTRY_CONTEXT_PROOF_MISSING_OR_INVALID':'장중 저항 검사 근거 미확인',
    'ENTRY_CONTEXT_RR_CHANGED':'주문 직전 상승 여유 변경',
    'PULLBACK_STRUCTURE_FAIL':'눌림 지지 확인 또는 가격 여유 부족',
    'BREAKOUT_STRUCTURE_FAIL':'이전 가격구조 부족 또는 돌파 지지 미확인',
    'BREAKOUT_STRUCTURE_PROOF_MISSING_OR_INVALID':'주문 직전 돌파 지지 근거 변경',
    'BREAKOUT_REFERENCE_NOT_KNOWN_AT_SIGNAL':'매수 판단 당시 확인되지 않은 과거 시세',
    'PULLBACK_STRUCTURE_PROOF_MISSING_OR_INVALID':'주문 직전 눌림 지지 근거 변경',
    'PULLBACK_STRUCTURE_SPREAD_CHANGED':'주문 직전 호가 간격 확대',
    'PULLBACK_ENTRY_BOOK_STALE':'주문 직전 호가 시각 지연',
    'PULLBACK_ENTRY_BOOK_UNAVAILABLE':'주문 직전 실제 호가 미확인',
    'BOX_DIRECTION_FAIL':'내려가는 가격 구간 또는 박스 방향 근거 부족',
    'BOX_DIRECTION_PROOF_MISSING_OR_INVALID':'주문 직전 박스 방향 검사 근거 미확인',
    'LIQUIDITY_FAIL':'유동성 미충족','SPREAD_FAIL':'호가 간격 초과','STRUCTURE_FAIL':'구조 미충족',
    'RESISTANCE_FAIL':'상단 여유 부족','INVALIDATION_FAIL':'무효화 조건 미충족','CHASE_FAIL':'추격 범위 초과'}


def setup_wait_summary(row):
    """Explain stored per-setup trials. Never invent a signal or order permission."""
    trials=row.get('setup_trials') or (row.get('details') or {}).get('setup_trials') or []
    parts=[]
    for trial in trials:
        if not isinstance(trial,dict) or trial.get('setup') not in {'A','B','C'}:continue
        gates=trial.get('gates') or {};labels=[]
        for reason in trial.get('failed_reasons') or []:
            label=SETUP_WAIT_REASONS.get(reason,reason)
            if reason=='WARMUP_FAIL':
                values=(gates.get('warmup') or {}).get('values') or {}
                elapsed=values.get('elapsed_seconds');required=values.get('required_seconds');bars=values.get('completed_bars')
                if elapsed is not None and required is not None:
                    label=(f'관측 {elapsed:g}/{required:g}초' if elapsed<required else
                           f'유효 완성봉 {bars}개' if bars is not None else label)
            elif reason=='LIQUIDITY_FAIL':
                gate=gates.get('liquidity') or {}
                if gate.get('result')=='UNKNOWN':label='호가 잔량 미확인' if gate.get('values',{}).get('mode')=='depth' else '유동성 미확인'
            elif reason in {'BREAKOUT_NOT_REACHED','NO_REBREAKOUT'}:
                geometry=trial.get('setup_geometry') or {};price=geometry.get('breakout')
                if price is not None:label+=f' ({price:,.0f}원)'
            if label not in labels:labels.append(label)
        if not labels:
            labels=['진입 조건 통과' if trial.get('entry_ready') is True else '평가 대기' if not trial.get('evaluated') else '형상/조건 미완성']
        parts.append(trial['setup']+': '+', '.join(labels))
    return ' | '.join(parts)


def context_description(row):
    context=row.get('market_context') or next((t.get('context') for t in row.get('setup_trials') or [] if t.get('context')),{}) or {}
    discovery=row.get('discovery_context') or {}
    if not context and not discovery:return ''
    labels={'UP':'상승','DOWN':'하락','MIXED':'혼조','UNKNOWN':'미확인'}
    values=['진단: 종목 5분 '+labels.get(context.get('trend_5m',{}).get('status'),'미확인'),
            '15분 '+labels.get(context.get('trend_15m',{}).get('status'),'미확인'),
            '시장 '+labels.get(context.get('market_regime'),'미확인')]
    if discovery:values.append('장전 준비 후보' if discovery.get('preopen_prepared') else '장중 발견 후보')
    if discovery.get('first_raw_at'):values.append('최초 발견 '+discovery['first_raw_at'][11:19])
    return ' / '.join(values)

SETUP_WAIT_REASONS.update({'IMPULSE_CLOSE_PROGRESS_INSUFFICIENT':'완성봉 종가 상승 근거 부족 (아래꼬리만으로 상승 인정 안 함)',
                         'PULLBACK_VOLUME_FAIL':'눌림 거래량 조건 미충족','PULLBACK_INVALIDATION_FAIL':'눌림이 최초 상승 기준 이탈'})
UI_REASONS.update({'ENTRY_MARKET_DATA_UNAVAILABLE':'주문 직전 최신 체결 시세 미확인',
                  'ENTRY_MARKET_DATA_STALE':'주문 직전 체결 시세 지연','ENTRY_SPREAD_CHANGED':'주문 직전 호가 간격 초과',
                  'ENTRY_QUOTE_ABOVE_LIMIT':'현재 매도호가가 승인된 주문가 초과','ENTRY_CHASE_CHANGED':'주문 직전 추격 범위 초과',
                  'ENTRY_DEPTH_UNAVAILABLE':'주문 직전 호가 잔량 미확인','ENTRY_DEPTH_STALE':'주문 직전 호가 잔량 지연',
                  'ENTRY_DEPTH_INSUFFICIENT':'실제 위험수량 대비 최우선 호가 잔량 부족'})

def human_blocker_reason(row):
    """후보 종목의 주요 차단/대기 사유를 사람이 읽기 쉬운 한국어로 반환."""
    gates = row.get('gates') or row.get('gate_results') or {}
    reason = row.get('primary_reason_code') or row.get('reason_code')
    compression=gates.get('candidate_compression') or {}
    status=row.get('discovery_status') or row.get('state') or (compression.get('values',{}).get('status') if isinstance(compression,dict) else None)
    if status=='RESERVE' and not row.get('evaluation_observed'):
        return 'WATCH 순위/자리 대기 — 구조 통과만으로 진입하지 않음'
    summary=setup_wait_summary(row)
    if summary and reason in {'PATTERN_OR_GATE_NOT_READY','SHADOW_ONLY',None}:
        return ('관찰 전용 | ' if reason=='SHADOW_ONLY' else '')+summary
    failed = [GATE_NAMES.get(k, k) for k, v in gates.items() if (not isinstance(v,dict) or v.get('required',True)) and (v.get('result') if isinstance(v, dict) else v) != 'PASS']
    reason_text = UI_REASONS.get(reason, reason)
    if failed:
        gate_summary = ', '.join(failed)
        return gate_summary + (f" ({reason_text})" if reason_text and reason_text != gate_summary else '')
    if reason_text:
        return reason_text
    return '평가 대기'


UI_REASONS['STATUS_SCAN_LIMIT']='종목상태 조회 예산 소진 — 다음 조회 대기'

def extract_important_events(log_rows, max_events=40):
    """저장소에 실제 존재하는 이벤트 타입만 추출/포맷팅."""
    events = []
    for r in log_rows or []:
        kind = r.get('event')
        stamp = (r.get('timestamp') or r.get('time') or '')[11:19]
        sym = r.get('symbol') or ''
        name = r.get('name') or sym or '시스템'
        item = None
        if kind == 'ENTRY_EVAL':
            setup = r.get('setup') or ''
            decision = r.get('decision')
            code = r.get('primary_reason_code') or r.get('reason') or ''
            desc = UI_REASONS.get(code, code)
            if code=='PATTERN_OR_GATE_NOT_READY' and setup_wait_summary(r):desc=human_blocker_reason(r)
            compression=(r.get('gates') or {}).get('candidate_compression') or {}
            if isinstance(compression,dict) and compression.get('values',{}).get('status')=='RESERVE':desc=human_blocker_reason(r)
            target = f"{name} ({sym})" if sym else name
            if r.get('signal_detected') and decision == 'BUY':
                item = (stamp, '진입 후보', target, f"Setup {setup} 조건 충족 → 매수 후보 승격")
            elif r.get('signal_detected') and decision == 'NO_BUY':
                item = (stamp, 'RiskGate 차단', target, f"Setup {setup} 포착 | {desc}으로 주문 제외")
            elif decision == 'NO_BUY' and setup:
                item = (stamp, 'Setup 미충족', target, f"Setup {setup} 시도 | {desc}")
            elif decision == 'NO_BUY' or code:
                item = (stamp, '진입 제외', target, f"{desc or '조건 미충족'}")
        elif kind == 'EXIT_EVAL':
            decision = r.get('decision')
            code = r.get('primary_reason_code') or r.get('reason') or 'EXIT'
            desc = UI_REASONS.get(code, code)
            target = f"{name} ({sym})" if sym else name
            if decision == 'SELL':
                item = (stamp, '청산 판단', target, f"매도 실행 판단: {desc}")
            elif decision == 'HOLD':
                item = (stamp, '보유 유지', target, f"포지션 유지: {desc}")
        elif kind == 'WATCH_TRANSITION':
            to_st = r.get('to_status') or r.get('to_state') or r.get('state') or 'WATCH'
            reason = r.get('reason','')
            if reason == 'WATCH_FALLBACK_SOURCE1':
                item = (stamp, '보조 승격', f"{name} ({sym})" if sym else name, "단일 소스 fallback → WATCH 슬롯 승격")
            else:
                item = (stamp, '후보 승격', f"{name} ({sym})" if sym else name, f"다중 소스 확인 → {to_st} 진입검토 대상으로 전환")
        elif kind == 'SHADOW_TRANSITION':
            to_st = r.get('to_state') or r.get('state') or 'SHADOW'
            item = (stamp, '관찰 등록', f"{name} ({sym})" if sym else name, f"후보 발견 → {to_st} 관찰 데이터 축적 시작")
        elif kind in ('BUY_ORDER', 'SELL_ORDER'):
            side = '매수' if kind == 'BUY_ORDER' else '매도'
            qty = r.get('qty', '?')
            lim = money(r.get('limit') or r.get('price'))
            item = (stamp, f'{side} 주문', f"{name} ({sym})" if sym else name, f"{qty}주 @ {lim} 전송")
        elif kind == 'ORDER_ACCEPTED':
            side = r.get('side', '')
            qty = r.get('qty', '?')
            item = (stamp, '주문 접수', f"{name} ({sym})" if sym else name, f"{side} {qty}주 증권사 접수 완료")
        elif kind == 'ORDER_REJECTED':
            reason = r.get('reason') or '증권사 거부'
            item = (stamp, '주문 거절', f"{name} ({sym})" if sym else name, f"거절 사유: {reason}")
        elif kind in ('FILL', 'EXISTING_POLICY_FILL'):
            side = r.get('side', '체결')
            qty = r.get('qty', '?')
            prc = money(r.get('price'))
            item = (stamp, '체결 완료', f"{name} ({sym})" if sym else name, f"{side} {qty}주 @ {prc}")
        elif kind == 'STOP_RAISED':
            old_s = money(r.get('old_stop'))
            new_s = money(r.get('new_stop'))
            item = (stamp, '보호선 상향', f"{name} ({sym})" if sym else name, f"트레일링 스탑 조정: {old_s} → {new_s}")
        elif kind == 'ENTRY_HALTED':
            reason = r.get('reason') or '안전 기준 초과'
            item = (stamp, '안전 중단', '시스템', f"HALT 발동: {reason}")
        elif kind == 'HALT_RECOVERED':
            item = (stamp, 'HALT 복구', '시스템', '정상 감시 복구 완료')
        elif kind == 'STOP_REQUESTED':
            cmd = r.get('command') or 'STOP'
            item = (stamp, '중지 요청', '시스템', f"{cmd} 명령 접수: 신규진입 차단")
        elif kind == 'CONNECTED':
            item = (stamp, '연결 수립', '시스템', '실시간 시세 및 브로커 연결 수립')
        elif kind == 'DISCONNECTED':
            item = (stamp, '연결 단절', '시스템', str(r.get('message', '연결 해제됨')))
        elif kind == 'OBSERVATION_READINESS_CHANGED':
            tier_name = r.get('readiness_kind') or f"Tier {r.get('tier')}"
            target = f"{name} ({sym})" if sym else name
            item = (stamp, '준비도 변화', target, f"관찰 준비도 전환: {tier_name} (보유봉 {r.get('usable_completed_bars',0)}/{r.get('stored_completed_bars',0)})")
        elif kind == 'FOCUS_PROMOTION_REQUESTED':
            target = f"{name} ({sym})" if sym else name
            item = (stamp, '승격 요청', target, f"Focus 승격 요청: {r.get('primary_reason') or r.get('tier')}")
        elif kind == 'FOCUS_SELECTION_APPLIED':
            item = (stamp, 'Focus 적용', '시스템', f"WATCH 교체: +{r.get('promoted')} -{r.get('demoted')}")
        elif kind == 'FOCUS_SELECTION_BLOCKED':
            target = f"{name} ({sym})" if sym else name
            item = (stamp, 'Focus 보류', target, f"승격 보류: {r.get('reason')}")
        elif kind in ('SESSION_PLAN', 'ENTRY_WINDOW_CHANGED'):
            item = (stamp, '세션 계획', '시스템', f"신규매수 마감 {r.get('effective_new_entry_end','14:55')}, 청산 {r.get('force_flatten_start','15:10')}")

        if item:
            events.append(item)
    return events[-max_events:]

def human_decision(row):
    reason=row.get('primary_reason_code') or row.get('reason_code')
    sizing=row.get('gates',{}).get('risk_quantity',{}).get('values',{})
    specific=sizing.get('reason_code') if reason=='NO_BUY_MIN_QTY_ZERO' else reason
    text=UI_REASONS.get(specific) or UI_REASONS.get(reason)
    if text:
        stamp=(row.get('timestamp') or row.get('time') or '')[11:19]
        decision={'BUY':'매수 후보','NO_BUY':'매수 안 함','HOLD':'보유 / 관찰 유지','SELL':'매도 판단'}.get(row.get('decision'),'상태 기록')
        return stamp+' '+str(row.get('name') or row.get('symbol') or '운영')+' → '+text+' → '+decision
    return decision_line(row)

def money(value): return '미확인' if value is None else f'{value:,.0f}원'

def result_text(result):
    if not result: return '아직 요청 / 검사 결과가 없습니다.'
    verdict={'STARTED':'감시 시작 요청 완료 — 새 계좌·연결 상태를 확인하세요.',
        'ALREADY_RUNNING':'이미 감시 중입니다.','REQUESTED':'요청을 저장했습니다. 실제 종료 / 체결 완료는 상태에서 확인하세요.',
        'SAVED':'명시 확인한 설정 / 승인을 저장했습니다.','PASS':'PASS — 확인한 검사 항목 통과',
        'FAIL':'FAIL — 진행하지 않았습니다. 확인이 필요합니다.',
        'INCONCLUSIVE':'INCONCLUSIVE — 관측 부족, 통과로 인정하지 않습니다.'}.get(result.get('verdict'),str(result.get('verdict') or result.get('status') or '진단 결과'))
    lines=[verdict]
    if result.get('timestamp'): lines.append('기록시각: '+result['timestamp'])
    for key,label in (('cap','승인한도'),('cash','주문가능현금'),('available','가용자본')):
        if key in result: lines.append(label+': '+money(result[key]))
    for key,label in (('orders','실제 주문 수'),('regular_ticks','정규장 틱 수'),('candidate_count','후보 수'),('signals','신호 수'),('network_calls','연결 호출 수')):
        if key in result: lines.append(label+': '+str(result[key]))
    lines.extend(result.get('reasons',[]))
    if result.get('note'): lines.append(result['note'])
    return '\n'.join(lines)

def presentation(s):
    running=s['process']['running']; mode=s['mode']
    if mode=='KILL': operation='비상정지 / 보호처리 확인 필요' if running else '비상정지'
    elif mode=='STOP_FLAT': operation='청산 중' if running else '청산 요청 / 감시 중지'
    elif running and mode=='STOP': operation='중지 요청 / 기존 포지션 보호 중'
    elif running: operation='자동매매 중' if s['trading_on'] else '시장 관찰 / 주문 대기'
    else: operation='정지'
    clock=datetime.fromisoformat(s['time'])
    eff_cutoff=s.get('effective_new_entry_end','14:55:00')
    if s.get('current_session')=='AFTER':
        market='AFTER 거래 시간 (16:00~20:00)' if s.get('after_supported') else 'AFTER 준비 (15:40~16:00)'
    else:
        market='08시 사전 준비 / 09시 개장 증거 대기' if s.get('control_supported') and not s['supported'] else '운영시간 밖' if not s['supported'] else '신규진입 마감' if clock.strftime('%H:%M:%S')>=s.get('entry_cutoff','15:05:00') else '관찰 중 · 신규매수 종료' if clock.strftime('%H:%M:%S')>=eff_cutoff else '신규거래 시간 / 정규장 확인' if s.get('market_confirmed') else '신규거래 시간 / 시장 현재성 미확인'
    safety='HALT — 안전중단' if s.get('halt') else '주의 — 보호정책 확인 필요' if not s['hard_ok'] else '주의 — 불명확 주문' if s['uncertain'] else '주의 — 현재 오류' if s.get('active_error') else '주의 — 프로세스 응답 지연' if running and s['process'].get('managed') and not s['process'].get('process_heartbeat_fresh') else '주의 — 계좌 / 실시간 확인 필요' if running and (not s.get('account_fresh') or s['websocket']!='현재 연결 정상') else '정상 / 계좌·실시간 연결 확인' if (s['supported'] or s.get('after_supported')) and s.get('account_fresh') and s['websocket']=='현재 연결 정상' else '로컬 상태 정상 / 실계좌 미확인'
    return operation,market,safety

def candidate_display(s):
    """화면에만 적용하는 현재성 판정. 원본 집계·전략 gate는 변경하지 않는다."""
    empty={key:'-' for key in CANDIDATE_FIELDS}
    if not s['supported'] and not s.get('after_supported'):
        return empty,'현재 운영시간 밖입니다. 최신 후보 집계가 없습니다.'
    summary=s.get('candidate_summary') or {}
    try:
        now=datetime.fromisoformat(s['time'])
        elapsed=(now-datetime.fromisoformat(summary['timestamp'])).total_seconds()
        fresh=0<=elapsed<=s.get('candidate_refresh_seconds',60)*3
        if s['process']['running']:
            fresh=fresh and datetime.fromisoformat(summary['cycle_started'])>=datetime.fromisoformat(s['process']['started'])
    except (ValueError,TypeError,KeyError,OverflowError):
        fresh=False
    if not fresh:
        return empty,'최신 후보 집계가 없습니다. 현재 후보 미확인.'
    return {key:summary.get(key,'-') if summary.get(key) is not None else '-' for key in CANDIDATE_FIELDS},'집계시각: '+summary['timestamp']+' / 탈락 gate는 중복 가능'

def candidate_wait_reason(s):
    """저장 집계를 설명할 뿐 Setup이나 BUY 판단을 만들지 않는다."""
    counts,_=candidate_display(s)
    if counts['raw_candidates']=='-':return '최신 집계 대기 중'
    summary=s.get('candidate_summary') or {}
    if s.get('cap')==0:return '승인한도 0원: 관찰만 수행하며 실제 신규매수는 금지됩니다.'
    if summary.get('affordable')==0:return money(s.get('available'))+'으로 비용 포함 1주 가능한 후보가 없습니다.'
    if summary.get('watch')==0:return '매수 가능한 종목을 관찰 중입니다. 실제 WATCH 자격을 기다립니다.'
    if summary.get('setup_candidates')==0:return 'WATCH '+str(summary['watch'])+'종목: 설명 가능한 Setup 형상을 기다립니다.'
    if summary.get('risk_zero_qty',0)>0:return '위험예산으로 수량 0인 후보 '+str(summary['risk_zero_qty'])+'종목. 다른 gate 탈락과 중복될 수 있습니다.'
    return '현재 집계의 주문가능 '+str(summary.get('final_entry_eligible',0))+'종목. 주문 직전에 계좌·시장·승인·위험을 다시 확인합니다.'

def board_freshness(row,clock):
    row=dict(row)
    # Evaluation emission time and ranking refresh time are not trade timestamps.
    row['evaluation_fresh']=bool(row.get('gates') and 0<=age(row.get('timestamp'),clock)<=15)
    row['data_fresh']=bool(0<=age(row['last_normal_tick_at'],clock)<=15) if row.get('last_normal_tick_at') else row['evaluation_fresh']
    row['data_note']=('현재 틱 판단' if row['evaluation_fresh'] else '정상 틱 수신 / 조건 평가 갱신 대기') if row['data_fresh'] else '틱 판단 대기 / 오래된 기록'
    if row.get('price_basis')=='RANK_REFERENCE':row['data_note']+='; 가격은 랭킹 조회값'
    return row

def start_block(s):
    if s['process']['running']: return '이미 실행 중입니다.'
    if not s.get('control_supported',s['supported']): return '운영시간 밖(준비시간 밖)입니다. 08:00 이후 사전 준비/승인, 09:00 이후 증거 확인 뒤 주문 가능합니다.'
    if s.get('halt'): return 'HALT가 활성화되어 있습니다: '+str(s['halt'])
    if not s['hard_ok']: return '삼성전자·광전자 보호정책을 확인해야 합니다.'
    if s['uncertain']: return '결과가 불명확한 주문을 먼저 확인하세요. 자동 재전송하지 않습니다.'
    recovery=s['mode'] in {'STOP','STOP_FLAT','KILL'} and bool(s['positions'] or s['pending'])
    if recovery: return ''  # 기존 STOP 보호/복구 전용 재개는 신규진입 마감 후에도 보존.
    if s['mode'] in {'KILL','STOP_FLAT','OFF'}: return '중단 원인을 상세진단에서 확인하고 기존 안전 전환 절차를 사용하세요.'
    t_str = datetime.fromisoformat(s['time']).strftime('%H:%M:%S')
    is_after = bool(s.get('after_supported') or s.get('current_session') == 'AFTER')
    if is_after:
        if t_str >= '20:00:00': return 'AFTER 운영시간 마감입니다.'
    else:
        if t_str >= s.get('entry_cutoff','15:05:00'): return '신규진입 마감입니다. 진단·기록은 이용 가능합니다.'
    if s['mode'] in {'STOP','LIVE'} and s['live_mode']:
        if not s['live_approved'] and not s.get('guided_start'): return '실제 자동매매 승인이 없습니다. 설정에서 준비 검사와 명시 승인이 필요합니다.'
        if not s['costs_confirmed'] and not s.get('guided_start'): return '비용 설정 미확인'
        if s['cap']<=0: return '승인한도가 0원입니다.'
    elif s['mode']=='STOP' and not s.get('guided_start'): return '관찰 재개는 0원·실주문 비활성 상태의 조회 대기 전환이 필요합니다.'
    return ''

def position_invalidation_lines(position,enabled=False):
    policy=position.get('signal',{}).get('invalidation_policy')
    if not enabled or not policy:return position.get('initial_stop'),position.get('stop'),False
    from core.util import floor_tick
    soft=max(policy['structural_stop'],position.get('stop',position['initial_stop']))
    hard=max(position['initial_stop'],floor_tick(soft-policy['noise_band']))
    return policy['structural_stop'],hard,True


def exit_plan_presentation(row,order_permission=False):
    """Arrange engine-authored facts; no new targets, thresholds or decisions."""
    v=row.get('exit_state') or {};plan=v.get('plan') or {};lines=v.get('lines') or {}
    data=v.get('market_data') or {};info=v.get('presentation') or {}
    target=info.get('target') or {};profit=info.get('profit') or {}
    orders=v.get('active_orders') or []
    sell=[o for o in orders if o.get('side')=='SELL']
    order_text='실제 활성 매도 주문 없음' if not sell else ' / '.join(
        f"매도 {o.get('qty','?')}주 · 체결 {o.get('filled','?')}주 · {o.get('status','UNKNOWN')} · {o.get('reason') or '주문 사유 UNKNOWN'}" for o in sell)
    if not orders and v.get('pending_orders'):order_text=f"활성 주문 {v['pending_orders']}건 / 세부 주문 기록 대기"
    requested=(v.get('exit_reason') or row.get('hold_reason')) if v.get('exit_requested') else '청산 요청 없음'
    conditions=list(info.get('conditions') or [])
    conditions.append({'key':'orders','label':'실제 매도 주문','status':'활성' if sell else '데이터 부족' if v.get('pending_orders') and not orders else '미발동',
                       'detail':order_text})
    active=profit.get('protect_price')
    protection=(money(active)+' · 이익보호') if active is not None else money(lines.get('active_structure'))+(' · 종가 구조선' if lines.get('close_based') else ' · 체결가 보호선')
    initial=target.get('initial',plan.get('initial_target',plan.get('target')))
    current=target.get('current',plan.get('target'))
    change_reason=target.get('change_reason') or ('최초 목표 유지' if current==initial and initial is not None else 'UNKNOWN')
    expected=profit.get('expected_condition') or '데이터 부족 / 엔진의 구체적 익절 조건 기록 대기'
    if data.get('bid') is None and data.get('current') is None:expected+=' · 현재 시세 판단 대기'
    next_exit=v.get('next_exit') or '엔진 청산 조건 기록 대기'
    if v.get('exit_requested'):next_exit='발동한 청산 요청 처리 중: '+str(requested)
    permission='허용' if order_permission and v.get('order_allowed') else '차단'
    return {'summary': [
                ('entry','실제 평균 체결가',money(plan.get('entry'))),
                ('current','현재가 / 실제 신선한 체결',money(data.get('current'))),
                ('initial_target','최초 예상 목표가',money(initial) if initial is not None else 'UNKNOWN'),
                ('current_target','현재 예상 목표가',money(current)+' · '+str(change_reason)),
                ('protection','현재 보호선',protection),
                ('activation','1R 가격 / 보호 발동가격',money(profit.get('one_r_price',plan.get('one_r_price')))+' / '+money(profit.get('activation_price',plan.get('profit_activation_price')))),
                ('net','호가 기준 예상 순손익',money(v.get('estimated_net_pnl'))),
                ('profit_status','수익보호 상태',profit.get('status') or '엔진 기록 대기')],
            'policy':profit.get('policy_text') or '목표가 도달만으로 매도하지 않음 / 구조적 청산 또는 보호선 이탈 시 매도',
            'expected':expected,'next_exit':next_exit,'orders':order_text+' · 주문 '+permission,
            'conditions':conditions}


def exit_plan_text(row,order_permission=False):
    """Format persisted engine values. No GUI target/protection/PnL calculation."""
    v=row.get('exit_state') or {};plan=v.get('plan') or {};lines=v.get('lines') or {}
    data=v.get('market_data') or {}
    if not plan:return '청산 계획: 엔진의 새 청산 상태 기록 대기. 기존 보호 정책은 유지합니다.'
    target=money(plan.get('target')) if plan.get('target_status')!='UNKNOWN' else 'UNKNOWN / 가격구조 근거 부족'
    rr=plan.get('net_reward_risk');rr_text=f'{rr:.2f}' if isinstance(rr,(int,float)) else 'UNKNOWN'
    orders='허용' if order_permission and v.get('order_allowed') else '차단 / 계좌·운영 상태 확인 필요'
    quote='신선한 매수호가' if data.get('quote_fresh') else '별도 재확인된 동일 호가' if data.get('standing_fresh') else '판단 대기 / 호가 오래됨'
    deadline=plan.get('flatten_at') or '당일 강제청산 없음 (설정 확인)'
    text=[
        f"진입 이유: Setup {plan.get('setup','?')} · {plan.get('entry_logic','미확인')} | 청산 정책: AE3",
        f"진입 평균가 {money(plan.get('entry'))} | 최초 구조 무효화 {money(plan.get('initial_invalidation'))} | 최초 비상선 {money(plan.get('initial_emergency'))}",
        f"현재 {'완성봉 종가 구조선' if lines.get('close_based') else '체결가 보호선'} {money(lines.get('active_structure'))} | 즉시 비상선 {money(lines.get('emergency'))}",
        f"예상 목표가 {target} · {plan.get('target_action')} | 비용 포함 예상 손익비 {rr_text}",
        f"목표 근거: {plan.get('target_basis','판단 대기')} | 확인된 저항 {money(plan.get('confirmed_resistance'))}",
        f"1R 가격 {money(plan.get('one_r_price'))} | 보호 발동가격 {money(plan.get('profit_activation_price'))} | 이익보호 {v.get('protection_status','판단 대기')} | {plan.get('profit_activation')}",
        f"최고 기록가 {money(v.get('highest'))} | 최대 유리 움직임(MFE) {money(v.get('mfe_per_share'))}/주 | 추세 {v.get('trend_status','판단 불가')}",
        f"지금 보유하는 이유: {v.get('hold_reason','엔진 판단 대기')}",
        f"다음 매도 조건: {v.get('next_exit','엔진 판단 대기')}",
        f"예상 순손익 {money(v.get('estimated_net_pnl'))} · {quote} | 잔여 위험 추정 {money(v.get('remaining_risk_estimate'))} (급락 체결손실 상한 아님)",
        f"최종 청산 {deadline} · {plan.get('session')} / {plan.get('venue')} · 거래 종료 {plan.get('market_close')} | 주문 {orders} | 미체결 {v.get('pending_orders',0)}건",
        '목표가 매도 예약 주문 없음. 예상 손익은 체결·비용 확정 전 추정이며, 새 익절 후보는 관찰 전용입니다.'
    ]
    info=v.get('presentation') or {};target=info.get('target') or {};profit=info.get('profit') or {}
    if info:
        panel=exit_plan_presentation(row,order_permission)
        text += ['',f"최초 예상 목표가 {money(target.get('initial'))} | 현재 예상 목표가 {money(target.get('current'))} · {target.get('change_reason')}",
            '진입 당시 기록된 가격 손익비: '+(f"{target['initial_price_rr']:.2f}" if isinstance(target.get('initial_price_rr'),(int,float)) else 'UNKNOWN')+' / 현재 설정 비용 포함 예상 손익비: '+rr_text,
            f"실제 익절 정책: {profit.get('active_policy')} / {profit.get('policy_text')}",
            '예상 익절 조건: '+panel['expected'], '이익보호 규칙: '+str(profit.get('rule')),
            '목표 도달 처리: '+str(target.get('reassessment')),
            '실제 목표 도달 기록: '+(str(target['touch'].get('time'))+' / '+money(target['touch'].get('price')) if target.get('touch') else '수신 체결로 확인된 도달 기록 없음'),
            panel['orders'],'보호선 변경 이력: '+str(info.get('history_note'))]
        for h in info.get('history') or []:
            ev=h.get('evidence') or {}
            text.append(f"  {h.get('time','UNKNOWN')} · {money(h.get('stop'))} · {ev.get('reason') or ev.get('policy') or '최초 보호선'}")
        text.append('조건별 상태:')
        for c in panel['conditions']:text.append(f"  [{c['status']}] {c['label']}: {c['detail']}")
        evidence=profit.get('evidence') or {}
        text.append('확정 지지 근거: '+(f"저점 {money(evidence.get('pivot_low'))} · 확인 {evidence.get('confirmation_time','UNKNOWN')} · {evidence.get('reason','UNKNOWN')}" if evidence else 'UNKNOWN / 확정 지지 근거 기록 없음'))
        after=info.get('after') or {};ev=after.get('evidence') or {}
        text.append(f"AFTER 실제 판단 근거: {'ON' if after.get('enabled') else '미적용'} · {ev.get('reason','UNKNOWN')} · 반등 저점 {money(ev.get('rebound_floor'))} · 확정봉 {ev.get('decline_times') or 'UNKNOWN'} · 종가 {ev.get('closes') or 'UNKNOWN'}")
    return '\n'.join(text)


class UserOperations(OperatorService):
    """기존 서비스 상속: 시작/STOP/진단과 별도 명시 자본 승인을 연결한다."""
    def snapshot(self):
        folder,state,hb=self.latest(); recent_events=tail_events(folder/'events.jsonl')
        cfg,auto,policy=self.settings()
        s=self.public_status(recent_events=recent_events,source=(folder,state,hb),settings=(cfg,auto,policy))
        rows=recent_events[-250:]
        cash_fresh=age(s['cash_time'],self.clock())<=cfg.reconcile_seconds*3
        market_fresh=(s['process']['running'] and 0<=age(hb.get('time'),self.clock())<=10
            and (not s['process'].get('managed') or hb.get('time','')>=s['process'].get('started','')))
        s.update(market_confirmed=bool(s['supported'] and market_fresh and hb.get('connected')
                and hb.get('market')=='3' and hb.get('session')=='SUPPORTED_SESSION'),
            market_code=hb.get('market'),session_classification=hb.get('session'),
            subscription_acks=hb.get('counts',{}).get('subscription_acks'),
            candidate_refresh_seconds=auto.scan_seconds,entry_cutoff=auto.new_entry_cutoff,flatten_start=auto.force_flatten_start,
            effective_new_entry_end=SessionPlan(hard_new_entry_end=auto.new_entry_cutoff,force_flatten_start=auto.force_flatten_start,allow_overnight=auto.ALLOW_OVERNIGHT).effective_new_entry_end,
            cash_fresh=cash_fresh,account_fresh=cash_fresh and s['account']=='현재 조회 정상',
            decision_rows=redact([r for r in rows if r.get('decision')][-10:]),
            log_rows=redact(rows[-80:]),orders=redact(list(state.get('orders',{}).values())[-100:]),
            trades=redact(state.get('trades',[])[-100:]),market_heartbeat=hb.get('time'),
            recent_result=redact(read_json(self.root/'runtime/operator_last_result.json',{})),
            advanced={'risk_fraction':cfg.risk_fraction,'max_position_fraction':cfg.max_position_fraction,
                'max_positions':cfg.max_positions,'entry_slippage_fraction':cfg.entry_slippage_fraction,
                'exit_slippage_fraction':cfg.exit_slippage_fraction,
                'new_entry_cutoff':auto.new_entry_cutoff,'force_flatten_start':auto.force_flatten_start,
                'ALLOW_OVERNIGHT':auto.ALLOW_OVERNIGHT,'commission_buy':auto.commission_buy,
                'commission_sell':auto.commission_sell,'sell_tax':auto.sell_tax,'costs_confirmed':auto.costs_confirmed})
        s['hard_ok']=s['hard_ok'] and not any(x in policy.get('releases',{}) for x in HARD_LOCKS)
        from automation.readiness import authorization_valid
        valid,missing=authorization_valid(self.root,cfg,auto,policy,self.clock())
        s['daily_authorization_valid']=valid;s['authorization_missing']=missing
        s['readiness']=hb.get('readiness',{})
        ready_ok=s['readiness'].get('order_ready')
        if ready_ok is None:
            ready_state=s['readiness'].get('state')
            ready_missing=s['readiness'].get('missing',[])
            ready_source=s['readiness'].get('tick_lag_source')
            ready_age=s['readiness'].get('regular_tick_age')
            ready_ok=(ready_state=='OPEN_READY') or (
                ready_missing==['FRESH_REGULAR_TICK_NOT_READY']
                and ready_source=='WS_SOURCE_SILENT'
                and isinstance(ready_age,(int,float))
                and ready_age<=cfg.stale_seconds*2
                and hb.get('connected')
            ) if 'readiness' in hb else True
        if not valid or not ready_ok or hb.get('fatal') or not s.get('trading_on'):s['trading_on']=False
        s['guided_start']=True;s['build_id']=BUILD_ID
        s['decision_rows']=[r for r in s['decision_rows'] if r.get('primary_reason_code')!='ONE_SHARE_UNAFFORDABLE' and r.get('state')!='EXTERNAL_POLICY_HOLD']
        shadow=read_json(self.root/'runtime/shadow_snapshot.json',{})
        s['shadow_diagnostics']={'snapshot_at':shadow.get('timestamp'),'performance':shadow.get('performance',{}),'last_error_record':redact(shadow.get('last_error'))}
        board={}
        if s['process']['running'] and (s['supported'] or s.get('after_supported')):
            for row in shadow.get('rows',[]):
                symbol=row['code'];evaluation=shadow.get('evaluations',{}).get(symbol,{})
                board[symbol]={'symbol':symbol,'name':row.get('name'),'state':'SHADOW','current_price':evaluation.get('current_price',row.get('reference_price')),
                    'source_count':row.get('source_count'),'gates':evaluation.get('gates',{}),'timestamp':evaluation.get('timestamp',shadow.get('timestamp')),
                    'primary_reason_code':'SHADOW_ONLY','setup_trials':evaluation.get('setup_trials')}
            for row in (s.get('candidate_summary') or {}).get('candidates',[]):
                if row.get('discovery_status') not in {'WATCH','RESERVE'}:continue
                symbol=row['symbol'];board[symbol]={**board.get(symbol,{}),**row,'state':row.get('discovery_status'),'gates':row.get('gate_results',{})}
        for symbol,row in board.items():
            observed=hb.get('watch_observation',{}).get(symbol,{})
            row.update(observed)
            if observed.get('last_trade_price') is not None:
                row['current_price']=observed['last_trade_price'];row['price_basis']='RECEIVED_TRADE'
            else:row['price_basis']='EVALUATED_TRADE' if row.get('gates') and row.get('evaluation_observed',True) else 'RANK_REFERENCE'
        s['candidate_board']=[board_freshness(row,self.clock()) for row in list(board.values())[:40]]
        radar=read_json(self.root/'runtime/radar_latest.json',{})
        s['candidate_performance']=radar.get('performance',{}) if s['process']['running'] and 0<=age(radar.get('time'),self.clock())<=auto.scan_seconds*3 else {}
        s['bot_positions']=[]
        for sym,p in state.get('positions',{}).items():
            if p.get('ownership')!='BOT' or sym in HARD_LOCKS or sym in BREAKEVEN_EXITS:continue
            from automation.exit_plan import display_state
            view=display_state(p.get('exit_state'),self.clock(),cfg.stale_seconds)
            # Orders may be saved after the last tick-authored projection.
            # Read their real journal states even when prices have expired.
            from core.engine import TERMINAL
            active=[o for o in state.get('orders',{}).values() if o.get('symbol')==sym and o.get('status') not in TERMINAL]
            view['pending_orders']=len(active)
            view['active_orders']=[{k:o.get(k) for k in ('side','status','qty','filled','limit','created')} |
                                  {'reason':(o.get('signal') or {}).get('reason')} for o in active]
            view['exit_requested']=bool(p.get('exit_requested'))
            view['exit_reason']=p.get('exit_reason')
            if view['exit_requested']:
                view['hold_reason']='청산 요청 처리 중: '+str(p.get('exit_reason') or '사유 UNKNOWN')
                if view.get('market_data',{}).get('current') is None and view.get('market_data',{}).get('bid') is None:
                    view['trend_status']='청산 요청 유지 / 현재 추세 판단 불가'
            current=view.get('market_data',{}).get('current')
            structural,protective,close_based=position_invalidation_lines(p,auto.confirmed_invalidation_enabled)
            if view.get('lines'):
                structural=view['lines']['initial_structure'];close_based=view['lines']['close_based']
                protective=view['lines']['emergency'] if close_based else view['lines']['active_structure']
            s['bot_positions'].append({'symbol':sym,'name':p.get('signal',{}).get('name') or sym,'entry':p.get('avg'),'current':current,'quantity':p.get('qty'),
                'setup':p.get('pattern'),'invalidation':structural,'protection':protective,'high':p.get('high'),
                'MFE':p.get('high',p['avg'])-p['avg'],'MAE':p['avg']-p.get('low',p['avg']),
                'risk':view.get('remaining_risk_estimate'),'exit_state':view,
                'exit_mode':'수량 확인 필요 / 자동청산 보류' if p.get('qty_blocked') else '청산 대기' if p.get('exit_requested') else '구조 / 수익 보호',
                'protection_evidence':p.get('protection_evidence'),'data_health':p.get('data_health'),
                'hold_reason':'장부·실계좌 수량 불일치. HTS에서 보유·미체결 확인 필요' if p.get('qty_blocked') else p.get('exit_reason') if p.get('exit_requested') else view.get('hold_reason','엔진 청산 상태 대기'),
                'next_exit':view.get('next_exit','엔진 청산 조건 기록 대기 / 기존 보호 유지')})
        s['active_error']=s['current_error'] not in {'현재 오류 미확인 / 과거 기록은 상세보기','기록 없음 (정상 연결을 뜻하지 않음)'}
        s['protection_rows']=[{'symbol':sym,'policy':'HARD_LOCK / 자동매매 금지' if sym in HARD_LOCKS else p,
             'breakeven':BREAKEVEN_EXITS.get(sym)} for sym,p in EXISTING_POLICIES.items()]
        s['operation'],s['market_text'],s['safety_text']=presentation(s)
        s['start_block']=start_block(s)
        s['decisions']=[human_decision(r) for r in s['decision_rows']]
        s['important_events']=extract_important_events(rows)
        return s

    def start_user(self,confirmed=False):
        s=self.snapshot(); reason=start_block(s)
        if s['process']['running']: return self.start_continuous()
        if reason: raise ValueError(reason)
        recovery=s['mode'] in {'STOP','STOP_FLAT','KILL'} and bool(s['positions'] or s['pending'])
        if recovery: return self.start_continuous()  # 기존 보호 전용 경로. 신규매수 권한을 늘리지 않는다.
        if s['mode']=='STOP' and not s['live_mode'] and s['cap']==0 and not s['live_approved']:
            self.arm_safe(True);return self.start_continuous()
        if s['mode'] in {'STOP','LIVE'} and s['live_mode']:
            from automation.readiness import authorization_valid
            c,a,p=self.settings();valid,why=authorization_valid(self.root,c,a,p,self.clock())
            if not valid:raise ValueError('오늘 LIVE 명시 승인을 다시 확인하세요: '+', '.join(why))
            if confirmed is not True: raise ValueError('실제 자동매매 시작 확인이 필요합니다.')
            receipt=read_json(self.root/'runtime/operator_user_authorization.json',{})
            cfg,auto,policy=self.settings()
            if receipt.get('config_hash')==cfg.fingerprint() and receipt.get('auto_hash')==auto.fingerprint():
                with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
                    c,a,p=self.settings()
                    counts=self.state_counts()
                    is_ctrl=control_supported(self.clock()) or (getattr(cfg,'after_enabled',False) and after_control_supported(self.clock()))
                    cutoff_hit=self.clock().strftime('%H:%M:%S')>='20:00:00' if getattr(cfg,'after_enabled',False) else self.clock().strftime('%H:%M:%S')>=a.new_entry_cutoff
                    if (c!=cfg or a!=auto or p!=policy or counts['positions'] or counts['pending'] or counts['uncertain']
                        or not is_ctrl or cutoff_hit
                        or any(st.get('halt') for _,st in self.states()) or not self.hard_locks_ok()
                        or any(x in p.get('releases',{}) for x in HARD_LOCKS)):
                        raise ValueError('시작 직전 설정/승인/장부가 변경되었습니다.')
                    Policy(self.root/'runtime/automation_policy.json').command('LIVE',confirmation='LIVE')
                try:
                    result=self.start_continuous(confirm_live=True)
                    if result.get('verdict')=='FAIL': self.stop('STOP')
                    return result
                except Exception:
                    self.stop('STOP'); raise
            return self.resume_live_continuous(True)
        return self.start_continuous(confirm_live=confirmed)

    def prepare_start(self,progress=lambda _:None):
        """시작 버튼의 조회 검사. LIVE 승인은 별도 사용자 승인 버튼 선택에서만 수행한다."""
        s=self.snapshot();reason=start_block(s)
        if reason:raise ValueError(reason)
        if s['positions'] or s['pending']:
            return {'verdict':'READY_EXISTING','note':'기존 봇 보호 전용 재개. 신규 승인 권한은 늘리지 않는다.'}
        cfg,auto,policy=self.settings()
        if policy['mode']=='LIVE':
            # 비정상 종료 뒤 mode만 남았어도 검사 중에는 신규 실행 권한을 잠근다.
            self.stop('STOP');cfg,auto,policy=self.settings()
        if auto.capital_limit>0 and not auto.costs_confirmed:
            return {'verdict':'COST_CONFIRMATION_REQUIRED','cap':auto.capital_limit,'note':'비용 설정 미확인. 실제 확인한 비용을 명시 입력하기 전까지 LIVE 준비와 주문은 차단됩니다.'}
        if not supported(self.clock()):
            result=self.preopen_local_ready()
            if result.get('verdict')!='PASS':return result
        proof=self.pilot_proof()
        if proof.get('verdict')!='PASS' or proof.get('strategy_version')!=STRATEGY_VERSION or proof.get('build_id')!=BUILD_ID or not self._approval_basis(cfg,auto,policy):
            progress('안전검사: readiness 증거를 확인합니다. 고정 대기 없이 충족 즉시 완료합니다.')
            result=self.pilot(progress,_recovery=True,_operational=True)
            if result.get('verdict')!='PASS':return result
        if auto.capital_limit==0:
            return {'verdict':'READY_OBSERVE','note':'승인한도 0원. 시장 관찰만 수행합니다.'}
        if not auto.costs_confirmed:raise ValueError('비용 설정 미확인. 증권사에서 확인한 비용을 설정해야 합니다.')
        progress('계좌·연결·보호정책과 승인한도를 검사합니다.')
        receipt=read_json(self.root/'runtime/operator_user_authorization.json',{})
        from automation.readiness import authorization_valid
        valid,_=authorization_valid(self.root,cfg,auto,policy,self.clock())
        if cfg.live_mode and policy.get('live_authorized') and valid:
            # 재개 시에도 계좌와 연결을 새로 확인한다. 기존 승인한도와 mode를 바꾸지 않는다.
            if not supported(self.clock()):
                return {'verdict':'READY_LIVE','cap':auto.capital_limit,'note':'오늘 명시 승인 유효. 09시 개장 증거까지 주문 OFF; 감시를 시작하면 자동 준비합니다.'}
            snap=self.gateway.snapshot();self.remember_account(snap)
            if snap['orders']:raise ValueError('실계좌 미체결 주문 확인 필요')
            before=self.clock();rc=self.gateway.command(['--check-connection'],timeout=65)
            report=read_json(self.root/'connection_check.json',{})
            if rc or not report.get('passed') or not in_window(report.get('started'),before,self.clock()) or not report.get('counts',{}).get('valid_regular_ticks'):raise ValueError('정규장 연결·신선한 틱 확인 필요')
            if self.settings()!=(cfg,auto,policy):raise ValueError('검사 중 설정/승인 변경')
            return {'verdict':'READY_LIVE','cap':auto.capital_limit,'note':'기존 명시 승인 범위. 실제 시작 확인이 필요합니다.'}
        result=self.prepare_capital(progress)
        return {**result,'verdict':'NEEDS_LIVE_APPROVAL','note':'준비 검사 완료. 현재 승인한도 확인과 사용자 승인 버튼 선택 전까지 실주문 OFF.'}

    def _flat_settings(self):
        cfg,auto,policy=self.settings(); counts=self.state_counts()
        if self.running(): raise ValueError('먼저 감시를 정상 중지하고 종료를 확인하세요.')
        if policy['mode'] not in {'STOP','ARMED','OFF'}: raise ValueError('정지 또는 조회 대기 상태에서만 설정할 수 있습니다.')
        if counts['positions'] or counts['pending'] or counts['uncertain']: raise ValueError('봇 보유·미체결·불명확 주문을 먼저 확인하세요.')
        if not self.hard_locks_ok() or any(x in policy.get('releases',{}) for x in HARD_LOCKS): raise ValueError('HARD_LOCK 보호정책 확인 필요')
        if any(st.get('halt') for _,st in self.states()): raise ValueError('HALT가 활성화되어 있습니다.')
        if policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}: raise ValueError('종료된 시험매도 장부를 확인하세요.')
        return cfg,auto,policy

    def _approval_basis(self,cfg,auto,policy):
        pilot=self.pilot_proof()
        if pilot.get('verdict')!='PASS' or pilot.get('strategy_version')!=STRATEGY_VERSION: return False
        if (pilot.get('operational_requalification') and pilot.get('timestamp','')[:10]==self.clock().date().isoformat()
            and pilot.get('config_hash')==cfg.fingerprint() and pilot.get('auto_hash')==auto.fingerprint()
            and pilot.get('policy_revision')==policy.get('revision') and policy['mode'] in {'ARMED','STOP','LIVE'}):return True
        if (pilot.get('requalification') and pilot.get('timestamp','')[:10]==self.clock().date().isoformat()
            and pilot.get('config_hash')==cfg.fingerprint() and pilot.get('auto_hash')==auto.fingerprint()
            and pilot.get('policy_revision')==policy.get('revision') and policy['mode']=='ARMED'
            and not cfg.live_mode and not policy.get('live_authorized')): return True
        if pilot.get('timestamp','')[:10]==self.clock().date().isoformat() and auto.capital_limit==0 and not cfg.live_mode and not policy.get('live_authorized'): return True
        previous=read_json(self.root/'runtime/operator_runner.json',{})
        if (policy.get('live_authorized') and cfg.live_mode and previous.get('initial_mode')=='LIVE'
            and self.resume_config_matches(cfg,auto,previous) and previous.get('auto_hash')==auto.fingerprint()): return True
        receipt=read_json(self.root/'runtime/operator_user_authorization.json',{})
        if (policy.get('live_authorized') and cfg.live_mode and receipt.get('config_hash')==cfg.fingerprint()
            and receipt.get('auto_hash')==auto.fingerprint()): return True
        change=read_json(self.root/'runtime/operator_cap_approval_basis.json',{})
        basis_revision=change.get('policy_revision')
        current_revision=policy.get('revision')
        same_revision=basis_revision==current_revision
        harmless=False
        if isinstance(basis_revision,int) and isinstance(current_revision,int) and current_revision>basis_revision:
            later=[c for c in policy.get('commands',[]) if c.get('revision',-1)>basis_revision]
            harmless=(policy['mode'] in {'STOP','ARMED'} and not policy.get('releases')
                and [c.get('revision') for c in later]==list(range(basis_revision+1,current_revision+1))
                and all(c.get('command') in {'STOP','ARMED'} for c in later))
        return bool(change.get('approval_basis_valid') and change.get('config_hash')==cfg.fingerprint()
            and change.get('auto_hash')==auto.fingerprint() and (same_revision or harmless)
            and (not change.get('initial_pilot_day') or change['initial_pilot_day']==self.clock().date().isoformat()))

    def change_cap(self,amount,confirmed=False,expected_cap=None):
        value=str(amount).strip()
        if not value.isascii() or not value.isdigit() or len(value)>12: raise ValueError('승인한도는 0 이상의 원 단위 정수로 입력하세요.')
        if confirmed is not True: raise ValueError('승인한도 변경 확인이 필요합니다.')
        expected=self._flat_settings()
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            # 이 메서드가 잡은 idle lock은 running()의 별도 실행 판정에 포함되므로
            # 잠금 안에서는 PID가 아닌 장부/정책을 다시 확인한다.
            cfg,auto,policy=self.settings(); counts=self.state_counts()
            if (cfg,auto,policy)!=expected: raise ValueError('확인 후 설정/승인이 변경되었습니다. 다시 확인하세요.')
            if expected_cap is not None and auto.capital_limit!=expected_cap: raise ValueError('확인창 표시 후 승인한도가 변경되었습니다. 다시 확인하세요.')
            if policy['mode'] not in {'STOP','ARMED','OFF'} or counts['positions'] or counts['pending'] or counts['uncertain']:
                raise ValueError('감시 중지·봇 보유/미체결 없음 조건이 필요합니다.')
            if any(st.get('halt') for _,st in self.states()) or not self.hard_locks_ok() or any(x in policy.get('releases',{}) for x in HARD_LOCKS): raise ValueError('HALT/보호정책을 확인하세요.')
            if policy.get('test_capital_release',{}).get('status') in {'APPROVED','SUBMITTING'}: raise ValueError('종료된 시험매도 장부를 확인하세요.')
            basis=self._approval_basis(cfg,auto,policy)
            prior_basis=read_json(self.root/'runtime/operator_cap_approval_basis.json',{})
            pilot=self.pilot_proof()
            initial_day='' if policy.get('live_authorized') and cfg.live_mode else pilot.get('timestamp','')[:10] if auto.capital_limit==0 and not cfg.live_mode else prior_basis.get('initial_pilot_day','')
            paths=[self.root/'config.json',self.root/'automation.json',self.root/'runtime/automation_policy.json',self.root/'runtime/operator_cap_approval_basis.json']
            old=[read_json(p) for p in paths]
            try:
                new_cfg=replace(cfg,live_mode=False).validate(); new_auto=replace(auto,AUTO_TRADING_CAP_KRW=int(value)).validate()
                atomic_json(paths[0],asdict(new_cfg)); atomic_json(paths[1],asdict(new_auto))
                p=Policy(paths[2]); p.command('ARMED'); p.state['live_authorized']=False; atomic_json(paths[2],p.state)
                atomic_json(paths[3],{'timestamp':self.clock().isoformat(),'from_cap':auto.capital_limit,'to_cap':int(value),
                    'approval_basis_valid':basis,'initial_pilot_day':initial_day,'config_hash':new_cfg.fingerprint(),'auto_hash':new_auto.fingerprint(),'policy_revision':p.state['revision']})
            except Exception:
                for path,data in zip(paths,old):
                    if data is not None: atomic_json(path,data)
                    elif path.exists(): path.unlink()
                raise
        return self.save_result('cap_change',{'verdict':'SAVED','cap':int(value),'note':'명시 승인한 한도만 변경했습니다. 실제매매는 꺼졌으며 별도 준비 검사·승인·시작이 필요합니다.'})

    def prepare_capital(self,progress=lambda _:None):
        cfg,auto,policy=self._flat_settings()
        is_after = getattr(cfg, 'after_enabled', False) and after_control_supported(self.clock())
        is_regular = control_supported(self.clock()) and self.clock().strftime('%H:%M:%S') < auto.new_entry_cutoff
        if not is_after and not is_regular: raise ValueError('신규진입 가능시간에 준비 검사를 수행하세요.')
        if auto.capital_limit<=0: raise ValueError('승인한도가 0원입니다. 준비 검사로 한도를 자동 증액하지 않습니다.')
        if not auto.costs_confirmed: raise ValueError('비용 설정 미확인')
        if not self._approval_basis(cfg,auto,policy): raise ValueError('최초 안전 시운전 또는 유효한 기존 LIVE 승인 근거가 필요합니다.')
        with self.idle(): pass
        if not supported(self.clock()):
            return self.save_result('capital_ready',{'verdict':'PASS','cap':auto.capital_limit,'cash':None,'available':None,
                'readiness_phase':'PREOPEN_LOCAL_READY','config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),
                'policy_revision':policy['revision'],'account_ok':False,'websocket_ok':False,
                'note':'당일 로컬 사전검사 완료. 계좌/실시간은 감시 시작 후 실제 응답으로 확인하며 09시 OPEN_READY 전 주문은 불가합니다.'})
        started=self.clock(); progress('계좌와 주문가능현금을 조회합니다. 실제 주문은 하지 않습니다.')
        snap=self.gateway.snapshot(); self.remember_account(snap)
        if snap['orders']: raise ValueError('실제 계좌에 미체결 주문이 있습니다.')
        rc=self.gateway.command(['--check-connection'],timeout=65)
        connection=read_json(self.root/'connection_check.json',{})
        if rc or not connection.get('passed') or not in_window(connection.get('started'),started,self.clock()) or not connection.get('counts',{}).get('valid_regular_ticks'):
            raise ValueError('이번 정규장 연결·신선한 틱 검사가 미완료입니다. 실제매매를 승인하지 않습니다.')
        c,a,p=self._flat_settings()
        is_after_now = getattr(c, 'after_enabled', False) and after_control_supported(self.clock())
        is_regular_now = control_supported(self.clock()) and self.clock().strftime('%H:%M:%S') < a.new_entry_cutoff
        if (c,a,p)!=(cfg,auto,policy) or (not is_after_now and not is_regular_now): raise ValueError('검사 중 설정/승인 또는 운영시간이 변경되었습니다.')
        return self.save_result('capital_ready',{'verdict':'PASS','cash':snap['cash'],'cap':auto.capital_limit,'available':min(snap['cash'],auto.capital_limit),
            'config_hash':cfg.fingerprint(),'auto_hash':auto.fingerprint(),'policy_revision':policy['revision'],
            'note':'조회 검사만 통과했습니다. 실제매매 명시 승인과 시작은 별도입니다.'})

    def authorize_capital(self,amount,confirmation):
        cfg,auto,policy=self._flat_settings()
        if str(amount)!=str(auto.capital_limit) or (confirmation is not True and confirmation!='실제매매승인'): raise ValueError('현재 승인한도 금액과 실제매매승인 문구를 정확히 입력하세요.')
        proof=read_json(self.root/'runtime/operator_capital_ready.json',{})
        with self.idle(),ProcessLock(self.root/'runtime/control.lock'):
            current=self.settings(); counts=self.state_counts()
            is_after = getattr(cfg, 'after_enabled', False) and after_control_supported(self.clock())
            is_regular = control_supported(self.clock()) and self.clock().strftime('%H:%M:%S') < auto.new_entry_cutoff
            if current!=(cfg,auto,policy) or counts['positions'] or counts['pending'] or counts['uncertain']: raise ValueError('승인 직전 상태가 변경되었습니다.')
            if (proof.get('verdict')!='PASS' or age(proof.get('timestamp'),self.clock())>60
                or proof.get('config_hash')!=cfg.fingerprint() or proof.get('auto_hash')!=auto.fingerprint()
                or proof.get('policy_revision')!=policy['revision'] or not auto.costs_confirmed or auto.capital_limit<=0
                or (not is_after and not is_regular)
                or not self._approval_basis(cfg,auto,policy) or any(st.get('halt') for _,st in self.states())
                or not self.hard_locks_ok() or any(x in policy.get('releases',{}) for x in HARD_LOCKS)):
                raise ValueError('최신 준비 검사·비용·승인 근거·시간·장부를 다시 확인하세요.')
            paths=[self.root/'config.json',self.root/'runtime/automation_policy.json',self.root/'runtime/operator_user_authorization.json']
            old=[read_json(p) for p in paths]
            try:
                new_cfg=replace(cfg,live_mode=True).validate(); atomic_json(paths[0],asdict(new_cfg))
                p=Policy(paths[1]); p.command('LIVE',confirmation='LIVE'); p.command('STOP')
                if is_after:
                    p.state['after_live_authorized']=True
                    atomic_json(paths[1],p.state)
                from automation.readiness import policy_hash,security_state,digest
                atomic_json(paths[2],{'timestamp':self.clock().isoformat(),'cap':auto.capital_limit,
                    'authorization_kind':'DAILY_PREOPEN_V13','trading_day':self.clock().date().isoformat(),
                    'policy_hash':policy_hash(p.state),'security_state_hash':digest(security_state(self.root)),
                    'build_id':BUILD_ID,
                    'config_hash':new_cfg.fingerprint(),'auto_hash':auto.fingerprint(),'policy_revision':p.state['revision']})
            except Exception:
                for path,data in zip(paths,old):
                    if data is not None: atomic_json(path,data)
                    elif path.exists(): path.unlink()
                raise
        return self.save_result('capital_authorization',{'verdict':'SAVED','cap':auto.capital_limit,
            'note':'오늘 이 build/설정/승인한도의 LIVE 동의를 저장했습니다. 시작 시 개장 증거 전 주문은 차단됩니다.'})

    def activate_after_live(self, amount, confirmation=False):
        from operator_service import OperatorService
        svc = OperatorService(self.root, clock=self.clock)
        return svc.activate_after_live(amount, confirmation)

    def local_diagnostic(self):
        from local_diagnostics import diagnose
        return diagnose(self.root,self.clock())
