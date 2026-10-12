# 최신 감사: 2026-10-12 연속 운용과 진입 지연

## 20:46 개발 후보: 가격구조와 이익보호 분리

개발 BUILD `V1.3-risk2-watch12-bc20M-tradequality-20261012`, STRATEGY `V1.3-confirmed-box-swing-protection-20261012`. 운영본은 entrycontext 그대로이며 새 LIVE 승인/START/실주문은 수행하지 않았다. 아래 두 전략 변경은 기본 OFF이며 운영 ON은 별도 승인 대상이다. 기존 quiet-book 확인도 기본 OFF, 보유 종목에만 적용하고 신규매수 freshness를 대체하지 않는다.

- C 박스 방향: 기존 폭/최소5봉/돌파 조건에 더해 같은 5개 완전한 연속봉의 마지막 고점·저점이 처음보다 낮고 마지막 종가도 최초 시가보다 낮으면 DECLINING_RANGE로 거부한다. 빈 데이터/미완성/미래/공백봉은 UNKNOWN으로 차단한다. 주문 직전 저장된 당시 봉으로 증거를 재검산한다. 거부된 C 형상이 WATCH에서 높은 준비순위를 유지하지 않도록 동일 gate를 적용한다. A/B의 기존 진입 정의/가속/거래대금 조건은 유지한다.
- 이익보호: 기존 1R 활성화 후 매 틱의 고정 giveback 및 최근1봉 저점 대신, 진입분을 제외한 실제 완성봉 3개에서 확인한 더 높은 눌림 저점 아래로만 보호선을 올리는 별도 정책이다. 비용·매도슬리피지까지 반영한 수익가격보다 높은 보호선만 허용하며 이미 올린 stop은 절대 낮추지 않는다. 확인봉은 기존 포지션 장부에 최대3개 저장한다. 최초 무효화선, 실제 stop breach, emergency bid, STOP_FLAT, 진짜 연결장애, 마감청산은 그대로 처리한다. 이 방식은 실현 전 이익을 더 반납할 수 있으며 수익성 개선은 입증되지 않았다.

저장된 실제 매수 전 스냅샷8개 중 C6개/A2개. 새 C 방향만으로 차단되는 것은 12:42 삼표1건이다. 기존 장중 저항/비용 gate와 합치면 총6건 차단하나 이 중2건은 기존 수익거래이다. 손실4건만 회피한다고 수익개선을 확정하지 않는다. 미래 가격은 BUY 조건 계산에 사용하지 않았으며 대체 체결/손익은 산출하지 않았다. 전체 진입 후 원시봉·호가 경로가 없어서 새 청산정책의 과거 결과는 UNKNOWN이다.

이8건의 신호→주문전송은0.409~2.056초, 신호→최초체결은1.244~6.247초이다. `candidate_detected_at`은 runner가 radar의 매 회 갱신되는 observed_at을 복사한 값으로 최초 시장발견시각이 아니다. 해당23~70초를 최초 발견 지연이라고 해석하지 않는다. 전체 상승 초입 포착/시장국면 기반 선정/수익 기대값 개선은 완료로 주장하지 않는다.

핵심114검사 PASS. 새21개는 실제 detector/가짜 주문경계, 미래봉 제외, 하락C/WATCH 순위, stop/비상/마감, 동일설정 재시작 및5회 연속 매수·매도·계좌대조·정상대기 복귀를 검증한다. 전체 회귀의 최종 결과는 TEST_REPORT 최신 기록으로 확인한다. 실계좌 주문0/프로그램 시작0. GUI TypeError 복구는 앞선 entrycontext 배포의 실제 재열기 확인이며 새 정책 배포 확인과 구분한다.

## 19:50 배포 인수 확인 및 quiet-book 추가 보완

운영 entrycontext BUILD f41f98e는19:30:42 배포/코드9개SHA/compile/import/공식flat recovery를 완료했다. 보존: 계좌·주문·당일손실·예외예산·기존영수증. LIVE 승인/START/실주문0. 추가flag는 entry_context_enabled=true만이다. 사용자 화면 TypeError는 구GUI cached AutomationConfig가 새 설정을 읽은 문제였다. 구창 정상종료 및 창 없는 GUI 프로세스 정리 후 운영GUI 재열기19:50:09 확인: entrycontext BUILD/STOP/OFF/로컬 상태 정상/시작가능, TypeError 없음. 실제 GUI 재열기를 배포 완료 조건에 포함한다.

추가 DEV quoteconfirm BUILD는3개 운영소스+새 기본OFF flag, 보유종목 standing-book REST cross-check를 추가한다. 원래 quote timestamp 보존, 새 HTTP receipt 별도, 정상WS/account/subscription·해당 거래소 route·원래0D timestamp 및 best prices/positive quantities exact match가 모두 필요하다. confirmation expiry/실패와 진짜disconnect emergency는 유지, 신규매수/봉/워밍업/보호선은 확장하지 않는다. 핵심105 및 전체928 PASS/FAIL0/ERROR0/SKIP0/syntax113/network0/orders0. 신규9, 기존919 유지. 실제 당시 REST full depth 미저장으로 과거 손실 방지효과 확정 불가. quote age를 receipt age로 단순 대체하지 않았다. 운영반영/ON 승인 전 개발본에만 존재한다.

## 19:27 최종 추가검증 / 승인된 배포와 데이터청산 복구

사용자가 배포 및 entry_context ON 실험을 승인했다.19:21:24 정상STOP 완료, 봇보유·미체결·UNKNOWN0. 기존 공식 복구에 없는 데이터 비상청산 HALT는 사고 이후의 해당 BOT 청산기록과 전량FILLED SELL을 추가로 요구하는 수동복구 경로로 지원했다. 기존 STOP/idle·신규15초이내실계좌·계좌identity·외부보호잔고·미체결·UNKNOWN 조건은 유지하며 자동해제하지 않는다. 일일위험 또는 다른HALT는 해제하지 않는다. 코드추가 automation/recovery.py 및 신규4개테스트. 최종919 PASS/FAIL0/ERROR0/SKIP0/syntax112, 실제network/orders/program-start0. 대상46 PASS. 앞선915와 이후복구4개를 구분한다.

19:26:59 실제 읽기 API 대조: 계좌identity/외부보호잔고일치·봇보유0·미체결0·UNKNOWN0·현금77,723·장부SHA불변. 실제 주문 API는 transport allowlist에서 거절한다. SOR 198440_AL의ka10004는원본호가19:26:58,응답19:26:59.117, bid1,583/ask1,585/bidqty254/askqty27,633 정상. NXT _NX는ValueError로미검증이며둘다PASS라고하지않는다. 원본호가시간을응답시간으로바꾸지않았다.

강동씨앤엘은C box breakout1,580에대해1,581원8주진입/stop1,565/목표1,606/RR1.5625였다. 약1원상승후비용모델순손실21.1076은고정익절정책때문이아니라보유시세검증실패의비상청산이었다. 당일작은반등을새신호로삼는C의기대값및시세quiet-book을실제장애와구분하는문제는추가검증이필요하다. 확인없는freshness위조·15초guard임의확대·손절선확대는하지않았다. 승인된이번배포는참고저항/종목우선순위/명시적flat복구까지이며전체진입·청산수익성이해결됐다고표시하지않는다.

## 19:16 개발 완료 / 장중 참고 저항 및 WATCH 실행 가능성

개발 BUILD는 V1.3-risk2-watch12-bc20M-entrycontext-20261012, STRATEGY_VERSION은 V1.3-confirmed-intraday-room-20261012이다. 목표가격 상한/진입 자격이 바뀌므로 기존 신호버전과 구분한다. entry_context_enabled 기본값은 false이며 ON은 별도 승인하는 실험이다. 전체 915 PASS / FAIL0 / ERROR0 / SKIP0, syntax111, network_attempts0 / actual_orders0 / actual_program_started false. 대상167 PASS를 포함한다. 기존 검사를 삭제하거나 skip하지 않았다. 최초 전체912의 실패1/오류3은 기존 AFTER depth fixture 생성시 실제 호스트 시간을 사용한 구독 선택 문제였다. 고정 AFTER 시각과 _AL packet으로 fixture를 맞췄으며 기존 assertion은 유지했다.

실제 AFTER 라온시큐어/금호건설은 모두 Setup A였다. 라온은16:06:23 승인, 16:06:27 local체결, 16,410원3주, AFTER 이전고점16,400 돌파/stop16,240/기존목표16,680/RR1.588. 16:09:06 stop청산 모델손익 -622.1325. 금호는16:55:56 승인·local체결, 13,610원1주, AFTER 이전고점13,600 돌파/stop13,470/기존목표13,820/RR1.5. 17:43:54 stop청산 모델손익 -171.002. HTS 3분봉의16:54/17:42는 봉 시작시각이며 정확한 주문시각과 혼동하지 않는다. 두 청산 모두 초기 구조선 이탈이고, 연결장애 청산은 아니다. 이후 반등만으로 손절이 잘못됐다고 확정하지 않는다.

원인: AFTER 전략 데이터는 세션 분리되고 A는 AFTER 고점을 사용한다. 기존 목표는 패턴폭/일봉 저항을 기준으로 하며 앞서 확인한 더 가까운 장중 저항을 충분히 제한하지 않는다. 새 PriceContext는 양쪽 이웃이 모두 완성된 연속1분봉 고점만 참고로 저장한다. 역할 변경/정규장→AFTER/transport reset에도 당일 참고만 유지하고 전략봉·틱·warmup은 별개로 초기화한다. 256종목/종목당30봉·128고점으로 제한하며 추가 API/구독은 없다. 재시작에서는 당일 장부의 기존 매수 전 snapshot만 참고 복원하고 warmup 복원은0이다. 미래/익일/미완성 snapshot은 쓰지 않는다. 과거 수신하지 않은 구간은 UNKNOWN이다.

ON일 때 목표는 min(기존 목표, 확인된 근접 장중 고점)이다. 기존 RR1.5/Economic를 재계산하고 주문 직전 proof/목표/유한 RR를 확인한다. 손절선, A/B/C geometry, 가속, 비용, risk2/daily4/cap81300/WATCH12/보유1/주문 안전조건은 유지한다. 이는 상승 초입을 새로 발견하는 전략이 아니다. 별도 큰 시장지수 체계는 아직 실응답 검증이 부족하며 UNKNOWN으로 남는다.

같은 시점 자료의 제한 replay: 저장된 진입 전7개 snapshot 기준 새 조건은6개 차단했다. AFTER 라온의 확인 저항16,450 기준 RR0.235, AFTER 금호13,700 기준 RR0.643으로 둘 다 FAIL이다. 그러나 차단6개에는 손실4개와 기존 수익2개가 함께 있다. 금호 오전 손실은 보존된 참고 부족으로 통과했다. 전체 틱/호가/가상체결 replay나 수익성 개선 입증이 아니다. 사후 가격으로 BUY 판단을 바꾸지 않았다.

반복 선정의 코드 원인 하나: B/C candidate_generated + liquidity만으로 높은 geometry tier가 가능해 RR/Economic/invalidation 등 가격 gate 실패인 후보도 우대됐다. 이제 그 가격 gate가 통과한 형상만 우대하며 breakout 대기는 허용한다. 유효 A, source1, 동티어 +2 hysteresis는 유지한다. 16시 이후 저장178 cycle에서 RAW 고유373/WATCH 고유32/실제 승인2였다. 두 종목만 검색한 것은 아니지만 STATUS_SCAN_LIMIT7636, ONE_SHARE_UNAFFORDABLE7889, PATTERN_OR_GATE_NOT_READY6319 등의 반복 단계 탈락이 있었다. 수치는 고유종목 수와 실패 이벤트 수가 서로 다르며 표본 종료 이후의 전일 최종치가 아니다. 전시장 초기 발견 지연과 실전 다양성 개선은 아직 입증하지 않았다.

19:12:49 읽기 확인: 설치 BUILD는 기존 daybudget1000, worker7552 LIVE/연결·account 정상, 보유0/활성주문0, HALT로 주문권한OFF였다. 198440의19:02:47 청산은 원본 quote 시각19:02:16/last tick약40초, fresh REST 응답도 원본 quote는 오래된 상태, 15초 재확인 종료 때문이다. WS/account/subscription은 정상이고 1.46초 뒤 fresh0D가 도착했으나 SELL intent는 이미 제출됐다. 단순 전역 단절이라고 설명하면 틀리며, 조용한 동일호가 vs 실제 관측불능 API 의미는 미검증이다. 청산 guard/HALT를 우회하거나 임의 타이머를 늘리지 않았다.

운영본 코드/config/approval/장부 변경0, STOP0, 실제 테스트 주문0이다. 현재 오늘 총 예산3,880.266 중 gross daily_loss3,694.5081, flat 잔여185.7579이며 신규1,000원을 다시 부여하지 않는다. 첨부 통합지시문의 LIVE 별도 승인에 따라 코드8개 및 entry_context_enabled=true의 최소 배포안을 준비한다. 배포는 사용자 승인→정상STOP/idle→공식 실계좌 보유·미체결·UNKNOWN 대조/정당한 HALT 원인 해결→backup→hash검증 순서이며 새 LIVE는 사용자가 직접 승인한다. 본 개발검사 통과를 실전 전체 정상화나 수익 회복 완료라고 표시하지 않는다.

## 15:12 최종 운영 반영 / 위험예산 예외

사용자요청에따라 code325f940 기준6개를15:11:50 STOP/idle/실계좌읽기대조후배포했다. 현재BUILD V1.3-risk2-watch12-bc20M-daybudget1000-20261012. 최종893 PASS/FAIL0/ERROR0/SKIP0, syntax109/network0/orders0/실제프로그램시작0.6개code SHA256개발동일/syntax·importPASS. 백업Documents/주식자동매매_개발/backups/daily_budget1000_20261012_151146. 원래main/미커밋작업보존, origin/fix/continuous-trading-20261012에push했다.

공식set_daily_risk_remaining_today(1000)으로 automation.json의날짜2026-10-12/총일일예산3880.266 두key만설정했다. daily_loss2880.266/당일장부/config/observation/policy/승인receipt SHA보존,flat잔여1,000원. 기본daily4/risk2/cap81300/보유1/신호·청산은유지하고예외만익일만료한다. 계좌현금78536/보호외부잔고대조/미체결0을실제읽기확인했으며실주문0이다.

15:12:33 현재STOP/HALT없음/보유미체결UNKNOWN0이며새BUILD/설정의사용자승인은아직없어정상차단된다. 기존receipt를복사하지않았다. 정규장14:55cutoff가이미지났으므로사용자는GUI를다시열고15:40이후새설정직접승인·시작,16:00이후AFTER readiness충족을확인해야한다. 신규매수19:45미만/청산19:55/지원20:00정책은변경없음. 거래당2%와잔여예산중작은값으로수량계산하며급락/슬리피지손실의절대상한을보장하는것은아니다. 진입품질·수익성이개선됐다는완료판정은여전히미검증이다.

## 15:05 추가 요청 / 일일 위험예산 예외는 전략 개선과 구분

15:10 최종 전체893 PASS/FAIL0/ERROR0/SKIP0, syntax109/network0/orders0/actualprogram-start0. 기존881개를 유지하고 위험예외10개 및현재승인cap/횟수제한없는AFTER2개를 추가했다. 저장된설정을정규화해서대량변경하지않고 날짜/총한도2개필드만 추가하는 보존검증도통과했다. 현재 code6개 운영배포 준비이며 아직 실제예외설정은 적용전이다.

사용자 직접 승인 후 observation BUILD 실제 worker10932의14:48:37~14:53:24 LIVE/OPEN_READY/connected/account_ok를 확인했다.14:54:13 별도 STOP revision206 요청으로14:54:17 정상종료됐으며 작업자는 이 STOP 또는 대리LIVE시작을 실행하지 않았다. 실제 요청 주체는 UNKNOWN이다. HALT/보유/미체결은0, 기존 daily_loss2,880.266 유지.

사용자 요청의 오늘 잔여1,000원은 총 일일 위험예산3,880.266원으로 구현한다. 기본risk2/daily4/cap81300/포지션1과 신호·청산은 그대로이고, 기본daily4보다 높아진 예외임을 명시한다. 날짜2026-10-12와 절대한도가 automation fingerprint에 결합되며 다음 날짜에는 예외를 사용하지 않는다. 기존 손실/주문/체결/승인receipt를 초기화·복사하지 않는다. 사용자의 새BUILD 직접승인이 필요하다. 현재 추가코드는 전체검증 중이며 운영에는 아직 반영되지 않았다.

핵심10개검사에서 새예산이 preview뿐 아니라 super.enter 및 실제 모의 broker guard에 도달하고, 소진시 신규BUY0/보호SELL가능/재시작장부보존/다음날만료/UNKNOWN·HARD_LOCK유지를 확인했다. AFTER에 남은 과거cap20000/일일3회 하드코드를 현재승인cap·정상검증설정으로 정정하며 freshness·hash·사용자승인·cutoff는 그대로 유지한다. 이는 초기상승/진입기대값 개선을 입증한 변경이 아니다.

## 14:47 최종 적용 상태

최종881 PASS/FAIL0/ERROR0/SKIP0, syntax108 및network/orders/program-start0. source61dd5c73a207462feaee62bc6043824b3865f317까지 origin/fix/continuous-trading-20261012 push 완료.14:34 code10개 배포에 이어14:43:26 최종operator_service 잠금소유 보정1개를 정상STOP/idle/fresh account 아래 반영했다. 마지막 계좌현금78,536, 계좌identity/외부보호잔고/미체결0 대조, day/orders/trades/daily_loss 및config/automation/observation/approval/policy hash 보존. 설치45개 application source는 syntax/normalized내용이 개발본과 일치하며 새10개는 최종code SHA256 일치한다. 기존6개LF/CRLF 차이는 덮어쓰지 않았다.

BUILD_ID=V1.3-risk2-watch12-bc20M-observation-20261012, STRATEGY_VERSION/STRATEGY_EXPERIMENT 유지. 새BUILD의 사용자 직접LIVE승인은 이 문서 작성 시 아직 실제heartbeat로 확인되지 않았다. 사용자가 직접 승인하겠다고 답했고, GUI가 없음을 확인한 뒤14:45 GUI만 열었다. 작업자가 START/LIVE 승인/실제주문을 실행한 것은 아니다. 현재STOP/HALT없음/보유0/미체결0이며 새실행 여부는 매번 actualheartbeat로 재확인해야 한다.

당일 손실은초기화되지않았다. flat 장부equity78,123.018, gross daily_loss2,880.266, 기존daily4%3,124.92072 중잔여244.65472. 이익이loss사용액을복원하지않는기존정책이다. day/symbol/consecutive count limits null이지만 기존시간cooldown900초 및잔여risk 수량0 차단은보존했다. 실제broker비용확정손익과model손익을혼동하지않는다. 전체전략정상화/수익성/실전HALT율0를입증한완료보고가아니다.

## 14:41 배포 체크포인트 / 전략 개선 완료와 구분

사용자는 기존 포지션 정상 정리 뒤 배포하고 새BUILD를 직접 승인하겠다고 명시했다.14:33:51 flat/active0/HALT없음을 확인하고 정상STOP·idle 이후 인증/실계좌 잔고/미체결을 대조했다.14:34:15 source323875d의 code10개를 observation BUILD로 배포했다. config/automation/observation/승인receipt hash 불변, day/orders/trades/daily_loss 보존, 실제 주문/START/대리LIVE승인0. code-onlybackup observation_auth_code_20261012_143208. 교체10개 SHA256 일치, application45개 syntax 및 내용일치; 기존6개는 LF/CRLF 차이만이라 그대로 보존했다.

복구 뒤 삼표 C가14:29:21 다시7,590원4주 체결되어14:33:24 7,480원에 구조청산, 비용모델 손익-508.882. 이 정상 청산은 HALT없이 대기로 복귀했다. 오늘5개 실제 매수는 모두C이며, 오늘 모델 순손익-1,648.235, gross daily_loss2,880.266. 실제broker현금78,536 및 모델cash가 같다는 주장은 하지 않는다. 손실예산은 이익으로 복원하지 않는 기존정책이다. 이 거래는 관측/장부 안정화만으로 C의 진입 품질이 개선됐다고 말할 수 없음을 보여준다.

추가 최종경계: Gateway recovery는 공식 recover_halt가 잡은 idle OS locks 자체를 running으로 오인하면 안 된다. 자기thread가 idle context를 보유한 때만 recovery snapshot을 허용하고, 다른thread/독립호출은 차단한다. 실제 OS lock을 가진 공식경로+다른thread거절 targeted 검증을 추가했다. 최종881 PASS/FAIL0/ERROR0/SKIP0, syntax108 정상, network/orders/program-start0. 마지막 operator_service 보정의 최소 배포를 준비한다. 일반시작/현재 HALT없는 매매경로를 대신 승인하거나 자동START하지 않는다.

## 14:28 추가 인증 사고 / 운영과 독립 점검의 격리

14:10:28 배포 준비용 독립 조회가 OAuth 토큰을 발급한 뒤, 운영 계좌 조회는14:10:34부터 return_code3 / `8005:Token이 유효하지 않습니다`를 반복했다. 토큰 발급과 실패의 시간관계, 이전 runner가 계속 사용한 메모리 토큰을 대조하면 독립 인증이 운영 토큰을 무효화했을 가능성이 높다. 이 점검은 작업자가 실행한 것으로, 운영 중 독립 인증은 올바른 읽기 전용 검사 방식이 아니다. 서버가 이전 토큰을 폐기한 원본 이벤트는 없으므로 인과를 완전 입증했다고 표현하지 않는다.

14:17:30 STOP revision198을 받아14:17:33 exit0으로 종료했다. 이 작업에서 STOP/새 배포/LIVE 시작을 실행하지 않았고, 저장된 MODE 기록에는 요청 주체가 없어 주체는 UNKNOWN이다. HALT가 빈 상태라 이 STOP을 거래 후 HALT로 합산하지 않는다. 사용자 GUI 재시작 기록14:23:20 이후 같은 continuity BUILD로 LIVE가 재개됐다. 작업자의 이후 독립 계좌검사는 running guard에서 인증 전에 거절됐다.

확인된 재발 경로를 개발본에서 수정했다. Gateway의 독립 snapshot은 stopped runner 및 idle OS locks 동안만 수행하며, HALT recovery의 독립 snapshot도 running을 거절한다. 정상 조회에서 코드3과 명시적8005 token-invalid 응답이 함께 있으면 공유 인증 lock 아래 토큰을 한 번만 갱신하고 조회만 재시도한다. 다른 업무 오류/진단 client/BUY·SELL·cancel은 인증 재발급·주문 재전송 경로에 들어가지 않는다. 장부·HARD_LOCK·승인·위험 설정은 변경하지 않는다. private 배포 준비/실행 helper의 독립 인증도 반드시 STOP·idle 뒤로 이동했다. 신규 targeted10 + 기존 pipeline15 =25 PASS. 최종 전체880 PASS/FAIL0/ERROR0/SKIP0, syntax108 정상, network/orders/program-start0. 추가 코드는 아직 운영 미반영이다.

시간축 정정: `trend_5m`/`trend_15m`는 실제5분/15분 봉 여러 개의 추세가 아니라, 최근5개/15개 연속 완성1분봉의 관측 window다. UP/DOWN은 구간 수익률과 매1분 higher-low/lower-high가 모두 맞는 경우이며, MIXED는 하락 확정이 아니다. 이 값을 곧바로 시장 추세 hard gate로 사용하지 않는다.

상태: 운영 최소 복구 완료, 추가 관측 안정화 전체870 PASS. 추가 운영 반영은 사용자 승인 대기. 아래 과거 보고는 역사 기록이며 현재 완료 판정이 아니다.

## 확인된 문제와 수정 범위

1. 정상 SELL 체결이 조회에 먼저 나타나고 로컬 체결 장부가 늦게 반영되는 경합을 합성 입력으로 재현했다. 체결 전 시작한 잔고 조회는 fresh 증거가 아니며, 자기 미체결 SELL로 설명되는 수량 감소만 제한된 시간 동안 재대조한다. UNKNOWN/설명 불가/초과 수량/시간초과 HALT는 유지한다. 실제 과거 원본 잔고 응답은 저장되지 않아 삼표 사고의 정확한 패킷 내용은 UNKNOWN이다.
2. WebSocket REG 전송의 closed-transport 예외가 worker를 종료할 수 있었다. transport 예외만 정상 disconnect/reconnect 경로로 넘기고 같은 cycle에서 주문을 즉시 차단한다. 프로그래밍 오류/PermissionError를 복구 성공으로 숨기지 않는다.
3. WATCH 역할 변경이 실제 구독 제외로 이어져 관측을 잃었다. 라온의 12:06:06에는 이미 124초/완성봉2개였지만, 12:06:28 release 후 12:06:34 rejoin에서 실제 구독 공백5.418723초로 reset됐다. 이후 새 epoch12:06:44, 12:08:38에는 16,550원/112초/완성1봉이었다. 이 가격차가 곧 실행 가능한 수익기회였다는 뜻은 아니다.
4. 기존 WATCH 상태정보는 180초까지 유효하나 갱신 지연으로 신호를 차단할 수 있다. 실제 WATCH는 기존 radar 한 cycle 먼저 갱신하고, main entry가 radar가 이미 갱신한 immutable metadata를 사용한다. 주문측 TTL/20회 status budget은 완화하지 않는다. main thread의 동기 REST JIT 호출은 추가하지 않는다.
5. 현재 A/C target은 일봉 저항과 패턴 projection의 작은 값을 사용한다. 가까운 장중 고점이 반드시 반영되는 구조가 아니다. 새 독립 ENTRY_CONTEXT_AUDIT는 양쪽의 연속·완성 1분봉으로 확인된 국소 고점, 원래/재검토 target·RR·Economic·5/15분 문맥을 기록한다. 이 진단은 주문 조건을 바꾸지 않는다. snapshot 밖의 장중 고점은 UNKNOWN으로 남긴다.

## 세 거래의 당시 판단

| 사례 | 실제 signal | entry / initial invalidation | 기존 target / 가격 RR | 완성봉에서 확인한 가까운 고점 | 진단 RR | 판정 |
|---|---|---|---|---|---|---|
| 금호건설 | C, 11:09:58 | 13,800 / 13,640 | 14,050 / 1.5625 | 저장된 마지막12봉에는 10:38의13,950원 없음 | 해당 고점 확인 시0.9375 | 전일 일봉과 짧은 snapshot만으로 온전한 장중 저항을 판단할 수 없음 |
| 라온시큐어 오후 | C, 12:28:04 | 16,530 / 16,330 | 16,860 / 1.65 | 12:21 국소고점16,560 | 0.15 | 관측 reset 때문에 급등 초기에 데이터 준비 부족. 고점 cap은 이후 수익 거래도 거절함 |
| 삼표시멘트 | C, 12:42:29 | 7,590 / 7,460 | 7,800 / 1.6154 | 12:33 고점7,630, 좌우 완성봉으로 당시 확인 가능 | 0.3077 | 작은 box breakout만으로 큰 하락 흐름의 무효화를 확인하는 구조가 아님 |

오늘 네 실제 매수 체결은 모두 C였다. A acceleration을 낮추는 것이 이 사례들의 입증된 해결책은 아니다. 삼표의 실제 exit7,450은 초기 invalidation7,460 아래이며, 가격 -2% 고정손절을 임의 적용한 결과가 아니다. 5분/15분 trend는 현재 진단이며 hard entry gate가 아니다. 삼표 MIXED/MIXED, 금호 UP/MIXED, 오후 라온 MIXED/UNKNOWN이었다.

최근 국소고점 cap만 기존 RR1.5와 비교하면 삼표 손실 진입과 두 라온 이익 진입을 함께 거절한다. 따라서 이 단일 변경의 기대값 개선을 주장할 수 없다. 개발 V1.4의 '+15%와 15분 MIXED/DOWN' 차단, snapshot 양 끝을 swing peak로 인정하는 로직, 09:00~09:05 Setup O는 검증되지 않아 운영에 병합하지 않았다. 장중 새 상승은 Setup O로 해결되지 않는다.

## 탐색 범위와 편향 (09:00~13:30 저장된288 radar cycles)

- 필터 후 고유 RAW330, 한 번 이상 affordability 통과272, radar row 기준 WATCH53, RESERVE57. 단계별 고유집합은 중복되며 합산할 수 없다. fast-focus WATCH까지의 완전한 합집합으로 해석하지 않는다.
- cycle RAW 최소137/평균143.24/최대152. 전체 상장시장 전수검사나 0168과 동일한 후보집합이 아니다.
- API별 필터 후 고유 기여: ka10029 92 / ka10032 161 / ka10027 151 / ka00198 78 / ka10031 28. 반환 원본에서 보통주 필터 전에 제외된 개수는 로그만으로 UNKNOWN이다. configured rank multiplier2, 기존 첫 응답 page만 사용, RAW 상한300, radar60초, WATCH12.
- 한 번 이상 매수 가능: 3만~5만원22종목, 5만~81,300원18종목. 수천/1만원대 전용 상한은 없다. 가격대 이동에 따라 집계집합은 중복될 수 있다.
- 예상체결(ka10029) 단독으로 관측된 종목79, 그 중 radar WATCH12. 단독 관측 가격이 저장 구간에서 하나뿐인 종목46, 그 중 WATCH7. 장중 실제 상승/거래량 증가를 보장하는 source가 아니라는 진단 필요. 이를 현재가라고 무조건 믿거나 순위 API5개를 독립적인 실시간 상승증거5개로 해석하면 안 된다.
- 기존 거래 이력 자체를 점수에 더하는 코드는 없다. 동일tier 기존 WATCH +2 hysteresis, higher tier 교체, 이미 봉이 준비된 후보의 tier 우선, active WATCH 상태조회 우선 및20회 조회 budget이 간접적인 재선택 편향을 만들 수 있다. 이 때문에 새 종목이 더 좋았다고 단정할 수는 없다.

키움 REST 공식 이용안내: 국내 조회TR/주문TR 각각 초당5회, 토큰당 세션1개, 실시간 시세200종목. OpenAPI+ 화면수 한도와 혼동하지 않는다. 공식출처: https://openapi2.kiwoom.com/intro?dummyVal=0 . 공유 API rate 간격/조회budget/구독상한을 이번 패치에서 임의 확대하지 않았다.

## 지연과 시간축 해석

13:30까지 실제300 latency samples: cycle 평균13.287초/p95 23.856초/최대43.139초, rank평균2.839초, compression10.427초, status8.575초, history1.426초, API wait4.945초. 하위구간은 겹칠 수 있어 합산 금지. Python 신호 계산 시간과 HTTP 대기를 혼동하지 않는다.

70초 baseline+acceleration, A2/B4/C5는 데이터 준비/형상 정의의 일부이며 고정 pilot sleep과 다르다. 현재 C는 상승 재개 여부를 5/15분 문맥에서 강제 검증하지 않으므로 하락 중 작은 박스도 geometry에 들어올 수 있다. 필요 조건을 단순 완화하기 전에 실제 관측 손실/worker restart/status latency를 제거한다. 정확한 과거 raw tick·호가 전체가 저장되지 않아 조기 주문 가능 시각/가상 체결·MFE/MAE·전략 기대값의 완전한 counterfactual replay는 미검증이다.

## 연속 거래와 안전 상태

- 첫849 회귀 최소 복구 패치는 운영에 반영됐다. 사용자 직접 새 BUILD LIVE 승인 후 연결/계좌/OPEN_READY 확인. HALT를 직접 JSON 편집해 해제하지 않았다.
- 두 라온 정상 이익청산 후 다음 거래가 이어졌다. 모든 거래 뒤 HALT가 걸렸다는 주장과는 다르다. 금호 PermissionError HALT는 11:11:43으로 실제 exit11:13:49보다 먼저다. 삼표 quantity mismatch HALT는12:58:00, local fill 반영은12:58:04다.
- cutoff까지 WORKER_FATAL19 중 WS closed 예외18/PermissionError1. worker 시작 횟수와 실제 재시작 원인은 로그 환경을 분리해 집계한다. 과거 fatal에는 stack/filename이 없어 PermissionError의 정확한 경로를 소급 단정하지 않는다.
- 합성 브로커 실제 Engine 경로로5회 연속 BUY/fill/SELL/fill/reconcile 후 정상대기 복귀를 검증했다. 정상 이익/손실, 다른 종목 및 기존 시간 cooldown 경과 후 같은 종목 재진입을 포함한다. 실제 주문 테스트0. 이를 실전 HALT율0이나 수익성 입증으로 간주하지 않는다.
- cap은 실제 사용자 승인값, risk2/daily4/WATCH12/maxpositions1 유지. 일일/동일종목/연속손실 count limit은 운영 설정null, 거래횟수는 기록. 시간 cooldown900초는 별도 기존 정책으로 보존했다. 수익이 gross daily_loss를 되돌리지 않는 현재 예산 정책도 바꾸지 않았다.
- Windows live log reader는 READ/WRITE/DELETE 공유로 rotation을 막지 않는다. 열린 reader 동안 lossless rotate가 성공하는 신규 검증을 추가했다. 이것이 과거 PermissionError의 유일 원인이었다고 주장하지 않는다.

## 남은 인수 조건

추가 BUILD observation 패치는 전체검사/commit/push 및 구체적인 사용자 배포승인 필요. 운영 LIVE를 유지하며 개발한다. 새로운 LIVE 승인은 사용자 직접 수행하며 receipt복사/대신 승인 금지. 운영 raw account/credential/runtime/거래 JSON은 Git 제외. 기대값 검증, 전시장 발견지연 및 미수집 후보의 신호 가능성, 장시간 queue/열 부하는 아직 UNKNOWN이다.

# 역사 기준: 2026-10-07 Discovery/WATCH 복원

기존 Work preopen 계획을 재개하지 않고 de5fc0c 뒤의 미커밋 후보 관찰 작업만 복원했다. 현재 위험 실험 2%/일일4%·승인자본20,000·포지션1·진입3과 기존 전략/청산/승인 정책을 보존했다. 최종 전체 551 PASS이며 실제 주문/네트워크/운영본 배포는 없다. 아래 V1.1 감사는 역사 기록이다. 이번 변경의 상세 판정은 마지막 2026-10-07 절에 있다.

# V1.1 code audit and observation design
Build: V1.1-audit-20261002. This public report contains code findings and synthetic verification only, not private trading logs or account results.

Observation and permission are separate. Affordable, officially normal, unprotected source-one candidates can accumulate SHADOW data. Actual BUY still requires at least two current sources or the existing explicitly qualified source-one WATCH path, a valid A/B/C Setup, risk quantity, confirmed costs, account/market/session checks and explicit LIVE authorization. Virtual SHADOW signals cannot enter the real order path.

Sole resistance-room failures are deferred to breakout A/C evaluation; B and other structure failures remain unchanged. Continuous valid observations survive source qualification changes. Missing minutes, disconnections and actual unsubscribe reset invalid data. Metadata and tick work use separate queues; shared CPU/API/WebSocket load still requires real-session verification.

The original order/fill/recovery engine remains. Initial structural invalidation is stored at fill. Protection only rises. Exit intentions persist across failures; partial fills, cancellation confirmation and reconciliation prevent duplicate selling. Local stops are not broker-native stop orders and do not guarantee maximum realized loss.

Risk remains 0.3%; authorization caps never increase automatically. Entry cutoff 15:05, flatten 15:10, support end 15:20, overnight disallowed. HARD_LOCK and external holding protections remain. Synthetic regression is not evidence of profitability or actual fill success.


## 2026-10-07 interrupted Discovery patch restoration

Verified starting HEAD: de5fc0c, main. No later commits or staged changes existed. Twelve unstaged files were preserved byte-for-byte before editing. Their purpose matches the interrupted Discovery work, but Git evidence alone cannot prove author identity.

Already implemented: unified focus competition, same-tier +2 incumbent hysteresis, better-quality replacement, max_watchlist default/example 8. Incomplete: radar still rejected source-one with installed min_source_count=2 and reused an empty-slot-only fallback path; fast promotion updated engine qualification without updating WATCH row flags; the fresh-structure signal recomputation discarded that flag. The interrupted diff also removed the actual source floor from order guards. That order-floor change was not needed for observation and has been restored to the prior boundary.

Completion: source-one is compressed once and competes in the same pool, with basic safety/economic/status/structure checks intact. Explicit qualified WATCH markers propagate through the real signal recheck. SHADOW-only flags remain blocked. Strategy thresholds, sizing formula, risk experiment, exit/STOP/session policies were not changed.

Connected synthetic approval/order regression found a separate required safety invariant failure: durable own INTENT was rejected by the daily authorization critical-state check. A narrowly scoped current-order context now excludes only that exact current BUY record during guard evaluation. Foreign INTENT, UNKNOWN, HALT, mismatched disk records, account identity and duplicate guards remain fail-closed. Read-only authorization still sees all INTENT records.

Replay scope: 163 saved candidate cycles for six requested symbols. All six contain source-one historical rejections. No source-only rejection remains at the patched observation entrance. Other recorded economic/weakening gates remain; missing point-in-time official status remains UNKNOWN. Closed daily history ends 2026-10-06 and is recomputed only at recorded prices. This is a partial-universe entrance replay, not proof of actual WATCH allocation, Setup, BUY, execution or profitability. Full raw logs remain private and unmodified. No broker was instantiated by replay.

Deployment/push are not part of this restoration. Desktop installation, personal configuration, approvals and runtime are untouched. Applying source code requires a controlled restart; an explicit saved WATCH limit of 5 still overrides the new default 8.
