# R6 독립 실행 경로 수정 인계

## 보존된 코드

- 원격 개발 브랜치: `fix/r6-midday-focus-20261019`
- 검증·게시된 코드 커밋: `7d22772770c69ee9add5b626763b8a8f7fa67539`
- 코드 링크: https://github.com/neomusbist/stock-trading-bot/commit/7d22772770c69ee9add5b626763b8a8f7fa67539
- 대응하는 로컬 코드 커밋: `b98860e9ceb2b8ecbb32e8c2ff825db1da888376`
- 두 코드 커밋의 소스 tree: `8688eabb9d6a48fe3e7cc67d31439f446f85b864`
- 로컬 작업 브랜치: `fix/r6-independent-execution-20261019`
- 로컬 작업공간: `C:\Users\MJS\Documents\주식자동매매_개발\r6-independent-execution-20261019`
- 출발 기준: `557e83c6bb13d39c7fc1f8d79f15798b2a576fc0`

연결된 GitHub API로 동일 source tree를 게시했다. 원격 코드 커밋과 로컬 코드
커밋은 메타데이터 때문에 SHA가 다르다. 이 인계 문서 갱신은 코드 이후 별도
문서 커밋이며, 최종 원격 HEAD는 브랜치에서 확인한다. main과 LIVE는 변경하지 않았다.
기존 `r6-target-cap-incident-20261015` worktree의 로컬 브랜치는 557e83c로 보존했다.
그 오래된 로컬 HEAD를 원격으로 강제 push하거나 최신 파일을 덮어쓰지 않는다.

## 이어서 읽을 자료와 실제 변경

먼저 [독립 검증 보고서](audit/r6_opportunity_and_orderpath_20261019/INDEPENDENT_REVIEW_20261019.md)를 읽는다.
생산 코드 변경은 runner, focus_selection, engine, position_health 네 파일이다.
실제 non-WATCH 수신 큐→동적 WATCH→기존 LIVE 재평가 연결, FAST 수신/판정 시각
일치, 관찰 전략의 우선순위 격리, A/B/C 원래 트리거 만료, 정확한 주문 limit,
FAST 구조선·비상선 권한 일치를 수정했다. 새로운 전략이나 임계값을 활성화하지 않았다.

- `audit/r6_opportunity_and_orderpath_20261019/TARGETED.json`: 최종 331/331 PASS,
  실패·오류·SKIP·네트워크 시도·실주문 0, 전체 회귀검사는 아님.
- `BASELINE.json`: 같은 반례 9개를 기존 기준판에서 실행한 결과 7 FAIL/2 ERROR.
- `INDEPENDENT_VERIFICATION.json`: 실제 소액 설정의 합성 주문 수치,
  개발/운영 source 15개 차이와 각 SHA256, 저장 운영 상태.
- `tests/test_r6_execution_integrity.py`: 새 회귀 34개. 신호 mock 없는 실제 FAST
  경로와 모의 접수·체결, 중복 차단, 두 세션 승인, 자금·위험·시간·호가 반례.

기존 테스트 세 군데의 가정도 정정했다. 기존 1틱 위 ask를 허용하던 양성 주문
fixture, 추가 관찰 trial을 금지하던 기록 검사, 순RR 0.69를 PASS라고 기대하던
구조선 fixture다. RR 조건을 낮추지 않았고 원래 RR 부족 예는 거절로 남겼다.

## 재개 명령

기존 worktree에서 읽고 재현한다. 새 clone이나 GitHub용 중복 폴더를 만들 필요가 없다.

```powershell
Set-Location -LiteralPath 'C:\Users\MJS\Documents\주식자동매매_개발\r6-independent-execution-20261019'
git status --short
git log -3 --oneline
& 'C:\Users\MJS\Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe' -X utf8 -B audit/r6_opportunity_and_orderpath_20261019/run_independent_checks.py --output 'C:\Users\MJS\Documents\주식자동매매_개발\r6-independent-review-20261019-private'
```

원격을 다시 받을 때에는 현재 작업을 보존한 뒤 `fix/r6-midday-focus-20261019`를
fetch하여 위 코드 커밋/소스 tree를 확인한다. reset --hard, clean, 강제 push,
운영본 복사는 이 인계 명령에 포함하지 않는다.

## 남은 검증과 배포 경계

1. 기존 LIVE와 개발본 사이의 15개 source 차이를 먼저 대조한다. 기존 Antigravity
   변경 14개를 이번 네 파일 버그 패치로 오인해서 전부 복사하지 않는다.
   LIVE release 표시는 6c2711d이나 engine/observation_worker manifest가 실제와 다르다.
2. 현재 LIVE 소스를 기준으로 승인 범위의 버그 수정만 결합한 운영 후보를 만들고,
   그 정확한 후보에서 관련 회귀·해시·무결성을 확인한다.
3. 그 후 사용자 배포 승인을 받은 범위에서만 공식 STOP/새 실계좌 대조/백업/
   파일 교체/무결성/BUILD 승인 절차를 수행한다. 최신 지시는 무승인 LIVE 수정과
   재시작을 금지한다. 저장 보유 0을 새 실계좌 확인으로 대체하지 않는다.
4. 실제 수신 부하에서 우선순위 큐 지연·관측 누락·신호→주문 지연·실패 사유를
   측정한다. 매수 수를 채우려 임계값을 낮추지 않는다.
5. 원본과 연결되지 않은 FSN 47건/3파동/1.98ms 실증을 재사용하지 않는다.
   기존 재현 CLI는 의도적으로 INVALID_FOR_CAUSAL_REPLAY/종료 코드 2다.

마지막 저장 상태(19:55:20 KST): STOP/종료, 보유·활성 주문·UNKNOWN 0, HALT 없음,
당일 손실 128원. 실제 브로커를 새로 조회하지 않았다. 이번 작업은 LIVE 미배포다.
계좌 위험 2%, 일일 4%, HARD_LOCK 005930/017900/052260, 기존 시각·구조선 분리,
실제 비상선, 데이터 장애 보호, 누적 장부를 유지한다.

## 수익성 및 자료의 한계

마지막 확인된 실제 완료 거래는 루닛 12,890원×5주를 같은 가격에 청산한 -128원이다.
새 패치에 대한 유효한 비용 차감 백테스트 순손익은 UNKNOWN이다.
합성 1,001원×67주 모의 주문은 수신/위험/주문 연결의 증거이며 수익 증거가 아니다.
1틱 위 ask는 같은 limit으로 승인하지 않고 거절한다. 지연 중 원래 트리거가
만료되면 최신 호가가 있어도 거절한다. 실제 1틱 불리 체결 손익과 지연별 기대값은
원본 부족으로 미입증이다. 독립 SHADOW 수집 기능은 기존대로 유지하지만 이 작업에서
실제 SHADOW 프로세스를 새로 실행하지 않았다. 관찰 정책의 가상 수익도 주장하지 않는다.

정밀 원본은 운영 `runtime/automation-production/state.json`,
`runtime/radar_latest.json`, `runtime/candidate_summary.json` 및 기존 trade-review에 있다.
비공개 테스트 원본/최종 로그/게시 결과는 별도
`C:\Users\MJS\Documents\주식자동매매_개발\r6-independent-review-20261019-private`에 있다.
Lunit/MK의 정제된 기존 사건 fixture는 tests 아래에 보존했다.
