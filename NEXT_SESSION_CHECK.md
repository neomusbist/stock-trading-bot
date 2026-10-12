# 다음 운용 전 현재 기준

## 2026-10-12 20:46 최신 우선 사항

운영본은 `V1.3-risk2-watch12-bc20M-entrycontext-20261012`, STOP/OFF, 로컬 장부의 봇보유·미체결·UNKNOWN0, HALT 없음. 이것은 현재 실계좌 재조회 결과와 구분한다. 새 개발 후보 tradequality는 자동 LIVE에 올리지 않는다. quoteconfirm 단독3파일 배포안은 더 넓은 정책 변경안과 합치기 전에 사용자 승인을 다시 확인해야 한다.

개발 후보의 confirmed_box_direction / confirmed_structure_protection / stable_position_book_confirmation은 기본 false. 승인된 선택만 ON으로 반영하고 다른 설정, 주문 장부와 영수증은 보존한다. 수익보호 변경은 이익반납 확대 가능성이 있으며 수익성 미검증이다. 적용 전 최종 전체 PASS·fresh 실계좌 대조·idle·백업·파일SHA 일치가 필요하다. 새 GUI를 닫았다가 재열어 실제 BUILD와 설정 스키마를 확인한다. 새 BUILD 당일 LIVE는 사용자가 직접 승인한다.

10/12 한정 위험예산 예외는10/13 KST에 자동 만료되어 기본 일일4%로 돌아가야 한다. 오늘 손실을 오늘 중 지우거나 내일 예외로 연장하지 않는다. 승인자본81,300원·거래당2%·WATCH12·동시1·일/종목/연속손실 횟수 null을 확인한다. 기존 same-symbol cooldown900초는 이번 품질 실험에서 변경하지 않는다. 기존 일봉/돌파/비용/주문 안전조건을 추가 승인 없이 완화하지 않는다.

진입 시간 감사에서 candidate_detected_at은 최신 radar cycle일 수 있다. 최초 발견을 증명하는 RAW/WATCH/실제수신 기록과 구분한다. 5/15분 시장상태가 UNKNOWN이면 사후 차트를 붙여 당시 알았던 것으로 만들지 않는다. 새 C방향 gate는 시장 전체 국면 판정이나 초기 급등 전략의 완성을 뜻하지 않는다.

## 아래는 이전 인계 기록

현재 후보 코드는 별도 fix/weekend-stabilization-20261008 분기의 안정화 BUILD다. 운영본에 아직 배포하지 않았으며 아래 예전 max_entries3/cap20000 절차를 최신 운영값으로 적용하지 않는다. 현재 승인자본은 실제 사용자 승인값과 실제 현금으로 동적으로 산정한다. 장중 제거한 일/종목/연속손실 횟수 제한 패치를 보존한다. risk2/daily4/maxposition1은 유지.

1. 최신 V1.3_HANDOFF_20261007.md 마지막 체크포인트와 최종 regression6 결과(833 PASS/0FAIL/0ERROR/0SKIP, syntax98, network0)를 읽는다. 새 BUILD에는 새 당일 hash 결합 명시 승인이 필요하며 과거 receipt를 복사하지 않는다.
2. 실제 주문이 차단된 상태에서 ka10004 SOR/NXT 원본시각/호가 필드 및 0J 별도 ACK를 확인한다. 불능을 PASS로 위조하지 않는다. KRX/NXT 보유 합계가 실제 계좌와 중복 없이 일치하는지도 확인한다.
3. 운영 종료/봇 보유/미체결/UNKNOWN/기존 HALT 원인을 대조한 뒤 변경 코드만 백업·배포하고 운영 config/runtime/account/approval/log hash를 비교한다. 기존 HALT를 파일 편집으로 임의 삭제하지 않는다.
4. GUI는 08시 이후 사용자가 준비/시작 승인. 09시 readiness와 전략 warmup70초/A2B4C5는 별도. 전송 직전 fresh quote/돌파/chase/실제 수량 depth를 검증한다.
5. 정규14:55 신규진입 cutoff/15:10 청산, AFTER19:45 cutoff/19:55 청산/20:00 종료 유지. 시장 음수 자체는 새 BUY 차단 조건이 아니다. 시장/종목5·15분 및 최초 발견은 진단으로 기록.
6. 초기 구조 무효화/현재 이익 보호선/비상 호가선 및 stale 재확인 실패를 따로 본다. 기존 1R/60% 이익 보호 정책을 검증 없이 넓히지 않는다.

## 아래는 과거 운용 전 체크의 역사 기록

# V1.3 다음 정규장 확인 절차

빌드 `V1.3-risk2-watch12-bc20M-opening-20261008`. 프로그램 재가동과 실제 LIVE는 사용자 결정입니다. 다음 검사는 오프라인 결과를 실제 계좌/시장 성공으로 확대 해석하지 않기 위한 절차입니다.

1. **08:00 이후** 바탕화면 「주식자동매매」 안의 실행파일을 엽니다. 빌드·현재 모드/감시 실행 여부·실제 주문 권한을 구분하고 보호정책·봇 잔량·미체결·불명확·HALT를 확인합니다.
2. **자동매매 시작**으로 준비를 시작합니다. 고정 시간 검사를 반복하지 않습니다. 로컬 정상과 실제 account/auth/WS 확인을 구분합니다. 08시 REST/account/auth 가능 여부가 실제로 확인되지 않으면 미확인 상태와 원문 오류를 보존합니다. 개장 전 regular tick 부재는 그 자체로 실패가 아닙니다.
3. 실제 오늘 LIVE 운용을 선택하면 **현재 승인 cap**을 확인하고 시작 버튼을 누릅니다. 버튼 클릭이 당일 명시 승인입니다. 별도 승인 문자열·금액 재입력·추가 확인창은 없습니다. 확인된 실제 비용과 현재 위험실험 값을 검토합니다. cap 20,000원·risk2%·daily4%·max_positions1·max_entries3이며 프로그램이 임의로 cap이나 승인을 늘리지 않습니다. 이 작업자가 실제 승인을 자동 생성하지 않습니다.
4. **09:00** 첫 regular tick부터 관측되는지 확인합니다. market 정규장, WS/REG ACK, 신선한 regular tick, 신선한 account, radar pipeline 증거가 충족되면 즉시 OPEN_READY여야 합니다. 필요한 증거가 없으면 BUY=0과 개별 차단 사유가 보여야 합니다. preopen 승인+runner 준비가 유효하면 개장 때 별도 버튼을 누르지 않습니다.
5. OPEN_READY와 전략 준비를 구분합니다. 70초 buffer·A2/B4/C5 유효 완성봉은 그대로이며 premarket 틱/봉은 제외됩니다. SHADOW/WATCH와 fast promotion, source2 주경로/source1 빈칸 보조, warmup 보존, 두 heartbeat를 확인합니다.
6. 시작 snapshot/ENTRY_EVAL/RiskGate의 build/strategy/experiment와 risk_fraction·max_daily_loss_fraction·allowed_risk_krw·remaining_daily_risk_krw를 확인합니다. 초기 20,000원 자본이라면 400원/800원이고 실제 남은 위험은 실현손실/예약위험을 차감합니다. 조건이 없으면 NO_TRADE가 정상입니다.
7. 실제 체결이 발생하면 초기 무효화·수량·보호선·MFE/MAE·청산의도와 체결/잔량을 따로 확인합니다. 일일 4% 한도는 신규매수만 차단하고 기존 보호는 계속합니다. API 단절·부분체결·거절·미체결은 실제 확인 전 성공으로 표시하지 않습니다.
8. **14:55 BUY 차단 / 15:10 봇 청산 / 15:20 지원 종료**를 확인합니다. STOP은 보호 유지, STOP_FLAT은 봇만 청산, KILL은 기존 긴급 절차입니다. HARD_LOCK/외부보유 보호 유지와 미청산 잔량을 HTS에서 확인합니다.

설정/cap/build 변경 시 당일 승인도 무효이며 다음 거래일은 별도 사용자 승인이 필요합니다. build가 다른 과거 readiness 증빙을 재사용하지 않습니다. 저장 자료를 지우거나 cap/비용을 억지로 바꿔 검사 조건을 맞추지 않습니다.

실시장 08시 API 가용성, 첫 틱 수신 지연, 장시간 성능, 실제 주문/청산 체결 품질, 슬리피지, 수익성은 오프라인 테스트로 확정되지 않습니다. 실제 승인/계좌/시장 로그는 Git 밖에 보관합니다.


### 2026-10-08 첫 실행 추가 확인
- 전일 승인은 날짜/build가 달라 재사용할 수 없습니다. 사용자의 오늘 시작 클릭이 새로운 승인을 만듭니다. 프로그램이 자동으로 LIVE를 승인하지 않습니다.
- 기존 10/7 0체결 내부 거절 3건은 실제 진입 0건입니다. 원본 주문/legacy 시도 횟수는 보존하고 FIRST_FILL_V2 기준으로 구분합니다. 다음 거래일 정상 시작 시 일일 진입·실패 latch·진단을 초기화하며, 전일 미해결 주문/HALT는 자동 해제하지 않습니다.
- 원본 로그 64MiB 회전과 archive 보존을 확인합니다. 어제 로그는 삭제하지 않았습니다. 콘솔은 감시 시작 전만 회전합니다.
- 첫 실제 접수는 포지션이 아닙니다. 최초 체결 때만 entry_count=1이며, 추가 부분체결·중복 통지는 그대로 1이어야 합니다. 내부 거절과 broker 응답은 구분합니다.
- 실시장 CPU, API 대기, 장시간 부하와 실제 체결 품질은 아직 미검증입니다. 합성 속도/테스트 PASS를 실제 거래 성공으로 표현하지 않습니다.


### 10/08 개장 관측 준비 확인
- 08:00 이후 실행파일을 열고 현재cap·모드·보호종목·미해결 주문을 확인한 뒤 `사전 준비 / 자동매매 시작`을 한 번 클릭합니다. 이것이 오늘 설정에 대한 사용자 승인입니다. 이 작업자가 대신 실행하지 않았습니다.
- 08:55 이전 후보/구독 준비 및 account/WS 상태를 확인합니다. API 거절이나 후보 부재를 PASS로 표시하지 않습니다. 원문 오류/미확인 사유를 남깁니다.
- 09시 첫 틱에서 REGULAR epoch, fresh tick, 신선한 reconcile, radar/REGACK 증거를 확인합니다. readiness와 전략 warmup/완성봉은 별도입니다. A2/B4/C5까지 기다리고 실제 breakout·RR·비용·수량 조건을 유지합니다.
- radar 실제 latency의 rank/status/history/structure/APIwait/HTTP, 두 heartbeat, WATCH/SHADOW 데이터 지연, subscription churn을 관측합니다. 새 fast cadence나 관찰용량 확대는 적용하지 않았습니다.
- 0체결 내부 거절은 entry_count를 증가시키지 않습니다. 첫 실제 BUY fill에만1회. max_positions1/max_entries3, 위험400원/일일800원 시작기준 및 예약/실현손실 차감은 그대로입니다.
