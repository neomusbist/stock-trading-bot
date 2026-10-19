# R6 주문 준비 상태 감사·수정

기준 로컬 커밋: `2affd6dea43dc2c0c03cb22e6e5f397bc5cbde8e`.
작업본: `<independent R6 development worktree>`.
현재 LIVE R5의 코드, 승인, 예산, 장부, 주문, 프로세스를 이 작업자가 변경하지 않았다.
운영 HALT/STOP 복구는 상위 작업자가 별도로 담당했다. R6 실제 주문은 수행하지 않았다.

## 기존 감사의 한계

`tests/test_order_ready_synchronization.py`의 종전 GUI 동기화 검사는 `bool(heartbeat['readiness']['order_ready'])`를 시험하는 방식이었다. 실제 `OperatorService.public_status()`나 `UserOperations.snapshot()`을 실행하지 않았으므로, 이전 감사의 “GUI와 엔진 상태 불일치가 완벽히 차단됐다”는 주장은 그 시험만으로 입증되지 않는다.

새 전용 시험은 두 실제 서비스 호출 경로를 사용한다. UI 위젯의 실화면을 켜거나 실제 LIVE를 시작하지는 않았다. 모의 승인 판정, 가짜 프로세스 정보, 임시 파일만 사용했다.

## 확인한 결함과 실제 변경

1. **준비 증거가 없는 상태가 ON으로 해석됐다.** 두 서비스 모두 heartbeat에 `readiness`가 없으면 `True`를 기본값으로 사용했다. 이제 `READINESS_UNKNOWN`으로 OFF를 표시한다. 실제로 증거가 있는 구형 R5의 `order_ready` 누락 프로토콜은 기존 상태·시각 자료로 판정한다.
2. **저장된 시세 나이가 GUI에서 멈춰 있었다.** 발행 당시 7초인 무틱 상태의 `order_ready=True`를 heartbeat가 10초 이내라는 이유로 그대로 사용했다. 같은 heartbeat를 5초 뒤 읽으면 시세는 12초지만 종전 GUI는 ON이었다. 이제 원래 체결시각 또는 기록된 나이와 heartbeat 경과시간으로 현재 나이를 계산하여 OFF로 바뀐다. 원본 heartbeat는 수정하지 않는다. 엔진의 명시적 OFF를 GUI가 ON으로 복원할 수도 없다.
3. **같은 WAITING 안에서 일어난 주문 준비 ON/OFF 전환 로그가 빠졌다.** 7초 무틱은 WAITING이지만 quiet grace로 준비 ON, 12초 무틱은 WAITING/준비 OFF다. 종전 `READINESS_CHANGED`는 상태 문자열만 비교하여 이 전환을 기록하지 못했다. 새 `ORDER_READINESS_CHANGED`는 준비 boolean, 정확한 사유, 처리지연 출처의 변화를 기록한다. `actual_order_permission=False` 및 범위 설명으로 이 이벤트가 승인이나 개별 종목 주문 허가가 아님을 명시한다.
4. **수신기가 이미 연결 실패를 알고 있어도 메인 큐 처리 전에는 연결로 남을 수 있었다.** 준비도 평가에 실제 Feed의 `transport_failed=True`를 반영한다. 큐의 ERROR가 아직 상태 writer에 도달하지 않아 `engine.connected=True`여도 신규 BUY는 즉시 준비 OFF가 된다. heartbeat 연결 표시도 이 증거를 사용한다.

수정 파일:

- `automation/runner.py`: 공통 준비 판정, heartbeat 현재성 재평가, 전환 로그, 수신기 연결 실패 반영.
- `operator_service.py`: 공통 준비 판정 결과와 구조화된 차단 사유 사용.
- `operator_ui_service.py`: 중복 quiet-grace 계산 제거, 실제 서비스의 판정 사용, 한국어 시세/계좌/처리 지연 사유 표시.
- `tests/test_r6_readiness_repair.py`: 전용 통합 재현 12개.

R5 개발 중간본에서 있었던 `shadow` 생성 전 직접 참조 오류는 R6 기준 커밋에서 이미 지연 lambda 및 생성 후 재바인딩으로 수정돼 있었다. 이 수정은 중복 구현하지 않았다. 새 준비 상태 검사는 기존 runner 시험 두 개도 통과했다.

## 유지한 안전 경계

- 시장 전체의 조용한 상태와 개별 종목 가격 현재성은 서로 다르다.
- 기존 5초 시세 기준과, 지연/적체 증거가 없는 경우에만 적용하는 최대 10초 global quiet grace를 유지했다.
- 10초를 넘는 무틱, 실제 연결 실패, 계좌 조회 만료, 처리지연/수신큐 적체는 grace를 주지 않는다.
- 개별 종목 주문 API 직전 `entry_market_reason()` / `order_guard()`의 5초 검사는 변경하지 않았다.
- 승인, LIVE 모드, 계좌, 현금, 위험, 보호 청산을 준비 boolean으로 우회하지 않았다.
- 화면 ON은 개별 종목이 매수 적격이라는 뜻이 아니다. 구조·호가·수량·승인 등 최종 경계가 별도로 적용된다.
- 정상적으로 조용한 구간이 10초를 넘으면 OFF가 될 수 있다. 이를 영구 ON으로 만드는 것은 이번 수정의 목표가 아니며 안전상 정당한 OFF를 제거하지 않았다.

## 재현 결과

| 조건 | 데이터 준비 | 실제 서비스/GUI 모델 | 최종 개별 주문 |
|---|---|---|---|
| 5초 무틱, 연결/계좌 정상 | ON | ON | 해당 종목의 별도 경계 필요 |
| 7초 무틱, 정상 처리/빈 큐 | ON, 기존 quiet grace | ON | 7초 된 해당 종목 틱은 거부 |
| 12초 무틱 | OFF | OFF, 시세 수신 대기 | 불허 |
| 20초 무틱 | OFF | OFF, 시세 수신 대기 | 불허 |
| 원래 7초 heartbeat를 5초 후 조회 | 저장된 ON을 현재 12초로 재평가 | OFF | 허가 증가 없음 |
| readiness 자체 누락 | UNKNOWN/OFF | OFF | 허가 증가 없음 |
| WS 단절 | OFF | OFF | 불허 |
| 수신기 실패, ERROR 메인 큐 미처리 | OFF | OFF | 기존 connected 값으로 허가하지 않음 |
| 계좌 조회 만료 | OFF | OFF | 불허 |
| 계좌 나이 14초 기록 + 2초 경과 | 현재 16초 → OFF | OFF | 허가 증가 없음 |
| 7초 무틱 + 처리지연 1.5초 | OFF, EVENT_LOOP_LAG | OFF | grace 불허 |
| 7초 무틱 + 수신큐 8건 | OFF, QUEUE_BUFFER | OFF | grace 불허 |
| 미래/11초 만료 heartbeat | OFF | OFF | 허가 증가 없음 |
| global grace ON 후, mock API 직전 해당 틱이 7초로 변경 | global과 종목 경계 분리 | 해당 종목 거부 | PRE_HTTP에서 REJECTED, mock broker 주문 0 |

마지막 사례는 새 판정을 수식으로 모방한 검사가 아니다. 실제 `AutonomousEngine.enter()` → `submit()` → 가짜 브로커가 호출하는 실제 `order_guard()` 경로를 실행했다. 신호 이후 stale로 바뀐 틱이 `PRE_HTTP` 재검증에서 거부됨을 확인했다. 증권사 API는 호출하지 않았다.

대상 검사 실행:

`python -` 기반 고립 harness에서 `unittest.defaultTestLoader.loadTestsFromNames(['tests.test_r6_readiness_repair', 'tests.test_order_ready_synchronization', 'tests.test_runner_loop', 'tests.test_operator_interface', 'tests.test_continuous'])` 실행.

개발본 경로를 import 최우선으로 사용했고 `socket.socket.connect`와 `connect_ex`를 예외로 차단했다.

- 결과: **87 PASS, 실패 0, 오류 0, 네트워크 시도 0**.
- 실행시간: 44.248341초.
- 종료코드: 0.
- 상세 로그: `readiness-targeted.log`, 수치 결과: `readiness-targeted.json`.
- 전체 저장소 회귀검사와 깨끗한 최종 커밋 재현은 상위 통합 작업에서 수행한다. 이 문서는 전체 회귀 결과를 대신하지 않는다.

## CPU 실측과 한계

`cpu-development-active.json`에 2026-10-14 17:04:25 시점 2.055초 표본을 보존했다.

- 논리 프로세서: 4개.
- 시스템 CPU 표본: 34%.
- 표본 중 프로세스 CPU는 4개 프로세서 기준으로 정규화했다.
- 측정 PowerShell 18.434%, 열린 GUI pythonw 6.461%, Python 6.461%, ChatGPT 프로세스 일부 1.900%/1.710%.
- 이때 개발 도구가 작동 중이었고, 실제 LIVE Worker는 상위 작업자가 STOP한 상태다.
- 따라서 이 표본은 오늘 LIVE ON/OFF의 CPU 원인을 입증하지 않는다. 개발 도구가 완전히 멈춘 상태의 대조 표본과, LIVE 가동 중 같은 시각의 프로세스 CPU·큐·수신지연·메인 지연 결합 자료는 확보하지 못했다.

하드웨어 성능 부족 판단은 **UNKNOWN**이다. CPU 100%를 원인으로 단정하지 않았다. 새 구조화 이벤트에는 `event_loop_lag_seconds`, `feed_queue_depth`, 시세 나이, 계좌 나이와 연결 실패가 함께 남으므로 이후 같은 시각을 대조할 수 있다.

## 판정

**주문 준비와 GUI 판정의 오프라인 통합 경계: PASS.** 동일한 입력으로 종전의 부당한 ON 유지/누락 로그를 재현하고 실제 코드에서 수정했다.

**2026-10-15 실제 SHADOW에서의 처리율·자원 사용·연결 안정성: 아직 실측 전.** 원시 수신 자료 기록 및 실행 준비 여부는 다른 담당의 검증 결과를 합쳐 판정해야 한다.

**LIVE 실거래 준비 또는 수익성: 이 문서로 PASS 판정하지 않음.** R6를 LIVE에 배포하거나 시작하지 않았다. 전체 회귀, 원본 시장 자료, 체결 내성, 독립 자료 검증 및 사용자 직접 승인 절차는 별도로 필요하다.
