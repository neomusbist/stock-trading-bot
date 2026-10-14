# R6 개발 인계 — 2026-10-14

## 보존 상태

- 개발 브랜치: `fix/r6-work-repair-20261014`.
- 기준 로컬 커밋: `2affd6dea43dc2c0c03cb22e6e5f397bc5cbde8e`. 그 위의 독립 수정이다. main과 LIVE R5는 병합·교체하지 않았다.
- 원본 R6 GitHub 브랜치 `feat/v1.6-fast-momentum-r6-20261014` 게시 완료. 원격 HEAD `3ad45e15f76f29edb6bd8dfa1948230a7967daef`의 tree는 원본 `2affd6d`의 `24575c4d9a6e938a48d629bba202594eb66a7f34`와 일치한다.
- 로컬 CLI 인증을 사용할 수 없어 승인받은 GitHub 연결로 6개 원본 변경을 순서대로 게시했다. 원본 커밋은 로컬에 보존하며 게시 커밋 SHA가 달라졌음을 숨기지 않는다. 이번 수정 브랜치 게시도 성공했고 로컬·원격의 정확한 코드 커밋은 `bfe5dd9ecd61640a07a4753e62c76c36f1c82c0e`로 일치한다. 코드 tree `599e10ec8e1cf1db1a071342cc43b9e4523af4f8`. 기존 로컬 체크포인트 9791b9b/81b27fc는 별도 보존됐다. 이후 인계 문서만 추가하는 커밋의 HEAD는 `git rev-parse HEAD`로 확인한다.
- 재개 시 가장 먼저 `git branch --show-current`, `git rev-parse HEAD`, `git status --short`를 확인한다. 미커밋 변경을 지우거나 원본 R6로 다시 개발하지 않는다.

## 실제 수정

- `automation/fast_momentum.py`: 임의의 고정 손절폭·손익비를 맞춘 목표가 제거. 당시 확인된 미시 지지와 알려진 저항, 신선한 실제 호가·잔량, 비용·비상선 위험으로 정수 수량 계산. 단일 정상 무효화선 접촉과 두 독립 체결의 구조 붕괴를 구분. 고정 +1.8% 보호·시간 만료 대신 확정된 진입 후 고저점 구조를 이용한다. FAST는 여전히 SHADOW 전용이다.
- `automation/shadow_observation.py`, `automation/observation_worker.py`: 모든 유효 틱의 누적과 평가 주기 분리, `shadow_view` 저항/asof 전달 누락 수정, 수신·처리 시각 구분, 구독 공백·큐 유실 초기화, 동시 생산자 sequence 보호, callback 완료 뒤 processed sequence, 비동기 원시 TRADE/BOOK 기록·로테이션·종료 flush.
- `automation/shadow_fast_runtime.py`: 독립 데이터 전용 진입점. source-root와 LIVE 충돌 검사가 없으면 인증 불허. 주문·계좌 HTTP endpoint 차단. 실제 FAST→후속 신선한 호가 가상 계좌→파일 기록 연결. 데이터 끝 강제청산 금지.
- `automation/runner.py`, `operator_service.py`, `operator_ui_service.py`: readiness 없는 상태의 ON 기본값 제거, GUI가 읽는 시점의 시세·계좌 나이 재계산, 수신기 실패·적체·지연 반영, 정확한 OFF 사유 로그. 개별 종목 5초 최종 주문 검사 유지.
- 전용 신규 테스트 4개와 기존 FAST/logger 테스트의 잘못된 가정을 수정했다. 기존 정상 A/B/C·위험·주문 안전장치를 완화하지 않았다.
- `audit/r6_work_repair_20261014/replay.py`: 기존 snapshot proxy 진입을 고정 비교군으로 남기고 없는 호가를 만들어 체결하지 않음, 누적 손실·승인자본·지연·틱 이동·비용 분리·미실현 구분을 수정. 이 코드를 실제 FAST 백테스트라고 부르지 않는다.

## 현재 확인 결과와 한계

- FAST/SHADOW/기존 fixture/실행 회계 대상 검사: 마지막 64개 PASS, 실패 0, 오류 0, 종료코드 0.
- 실제 두 운영 서비스와 주문 직전 가짜 브로커 경로: 87 PASS, 네트워크 시도 0. 5/7초 quiet와 12/20초 OFF, 단절·계좌 만료·지연·큐 적체를 구분했다.
- 최종 개발본 전체 1,430 PASS 및 GitHub와 동일한 코드 커밋의 깨끗한 detached checkout 전체 1,430 PASS, 실패0/오류0/skip0/네트워크시도0/AST오류0/종료0. identity/replay 결과 6개 canonical hash도 일치했다. TEST_RESULTS.json 참조.
- 원본 오후 파일 SHA-256 `0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c`, 67,132,914바이트. 실제 구간 14:21:21~14:52:49, 6,072 ENTRY_EVAL. 원시 체결·잔량 tape가 아니다.
- 원본에서 서산 7,060~7,320원, 엘앤씨바이오 61,700~66,900원 확인. 문제의 7,650~7,720/13,400원대는 이 동일 파일에 없다. 기존 일부 보고의 종목명은 잘못 연결돼 있다. 잘못된 요약의 최초 출처는 미확보이므로 인코딩 탓이라고 단정하지 않는다.
- 이전 proxy 재현: 기준 +1,469.92원, 1틱 -278.17원, 2틱 -2,116.29원, 3틱 -2,894.39원, 한 snapshot 지연 -61.41원. 실제 FAST 또는 실제 체결의 수익이 아니다.
- 엄격한 실행·회계 proxy: 기준 2건 청산 -282.139원, 1틱 -471.89원, 2틱 -874.956원, 3틱 -1,238.062원. 1 snapshot 지연 기준 -132.4615원. 종료 시 에스엠벡셀 30주 가상 보유가 남고 마지막 bid가 없어 미실현은 UNKNOWN. 최종 재실행 CSV를 우선한다.
- 단일 거래일 31분의 snapshot, 호가 누락, 독립 원시 데이터 없음 때문에 새 FAST 수익성·초입 우위·1틱 내성은 UNKNOWN이다. 5~10분당 왕복 1회도 입증되지 않았다.
- 실제 SHADOW 접속은 마지막 확인 R5 LIVE PID 19568 때문에 `LIVE_WORKER_LOCK_ACTIVE`로 인증 전에 차단됐다. 추가 토큰 발급·실주문·R6 LIVE 배포는 하지 않았다.
- 사용자가 요청한 HALT는 공식 STOP→신선한 계좌 대조→recover_halt 경로로 해제했다. 당시 일일 손실 1,256원 유지. 이후 R5가 다시 LIVE로 확인됐다. 이 문서의 상태는 확인 시점 기록이므로 재조회한다.

## Antigravity 재개 순서

1. 이 작업 폴더의 현재 HEAD와 미커밋 변경, GitHub 수정 브랜치 존재를 확인한다. 다른 작업공간·main·운영본을 덮어쓰지 않는다.
2. `python -B -m unittest tests.test_r6_fast_repair tests.test_r6_shadow_runtime tests.test_r6_readiness_repair tests.test_r6_execution_repair tests.test_fast_momentum tests.test_shadow_tick_logger -q`를 실행한다.
3. 전체 회귀는 Qt offscreen 및 socket 접속 차단 harness로 수행한다. 완료 로그·종료코드·실패가 없으면 PASS로 적지 않는다. `REPRODUCE.md`의 최종 명령을 따른다.
4. `$env:USERPROFILE\Desktop\주식자동매매\runtime\automation-production\archive\events.jsonl.20261014-145249-570438.7232.32e27bce`를 해시 확인 후 `python -B -m audit.r6_work_repair_20261014.replay "<원본>" --out "<개별 재현 폴더>"`로 재현한다.
5. SHADOW 실행은 `python -B -m automation.shadow_fast_runtime --check --source-root "<R5 운영 폴더>" --output "<운영 외 독립 SHADOW 폴더>"`부터. R5 실행 중이면 우회·강제 종료·토큰 추가 발급하지 않는다. 사용자가 승인한 공식 STOP과 보유·미체결·UNKNOWN 대조 뒤에만 다음 실데이터 실행을 한다.
6. 2026-10-15 08:50~09:10 `--seconds 1200`으로 실제 원시 TRADE/BOOK을 수집한다. 파일·구독 ACK·exchange/receiver/processed time·큐 유실·flush·자원 사용을 확인한다. 누락을 감지할 수 있다는 것과 무손실 보장은 다르다.
7. 실제 FAST를 원시 tape로 재생하여 A/B/C와 발견·관측·신호·후속 호가 적격 시각을 비교한다. 0~3틱 및 1프레임 이상 지연, 비용, 단일 계좌·2%/4%·한 종목을 유지한다. 별도 거래일도 평가하고 손실/수익 양쪽의 과잉 차단을 측정한다.
8. 수정→전체 회귀→새 clean checkout 재현→민감정보 staged 검사→별도 브랜치 게시. 수익성 미입증 상태에서 LIVE를 테스트 수단으로 사용하지 않는다.

## 로컬 증거 위치

- 개발: `$env:USERPROFILE\Documents\주식자동매매_개발\r6-work-repair-20261014`.
- 개인 증거: 같은 상위 폴더의 `r6-work-repair-private-20261014` 및 `r6-work-repair-audit-20261014`. HALT 복구 결과·백업, 원본 이전 replay, KRX 명칭 확인, 전체 검사 harness와 로그를 보존했다. 인증 파일과 계좌 원본은 Git에 넣지 않는다.
- 저장소 공개 결과: `audit/r6_work_repair_20261014/`.

이 인계서는 성공 선언이 아니다. 전체 검사·게시·실접속 미완료는 최종 기록에서 구분한다.

## 최종 인계 상태

**부분 완료**: 실제 코드 수정·GitHub 게시·개발본 및 clean checkout 전수 검사·자료 재현·인계 문서 보존은 완료. 실제 FAST의 독립 시장 수익성, 1틱 체결 내성, 개장 RAW→신호 지연 비교와 증권사 실구독은 미완료다. 10월14일 22:23 인증 없는 --check는 source STOP 상태에서 충돌 검사 PASS/주문 surface false/네트워크0/종료0. 이를 실시간 수신 PASS로 읽지 않는다. 운영 R5는 배포하지 않았고 main은 3c6fb27557fc0c556ce8312a4bd1000dd07f7d7c로 유지했다.

재개 명령: 기존 작업 폴더에서 `git log -5 --oneline`, `git status --short`, `git rev-parse HEAD`, `git rev-parse origin/fix/r6-work-repair-20261014`부터 확인한다. 코드 기준은 bfe5dd9ecd61640a07a4753e62c76c36f1c82c0e이고 이후 문서 커밋은 해당 코드와 동일하다. 다른 LLM의 미커밋 변경을 reset하지 않는다. 위 순서와 SHADOW_RUNBOOK의 조건을 유지해 10월15일 실제 자료 수집·검증을 이어간다.
