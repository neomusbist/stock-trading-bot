# R6 research/backtest — Antigravity 인계, 2026-10-20

**개발 전용 / LIVE NO-GO / 플러스 기대값 미입증.** 버전 관리 작업은 종료됐다. 이 문서는 백테스트 교정과 전략 연구만 이어받기 위한 것이다.

## 1. 정확한 인수 상태

- 저장소: `neomusbist/stock-trading-bot` (private).
- 브랜치: `research/backtest`, Draft PR [#2](https://github.com/neomusbist/stock-trading-bot/pull/2), base `dev`.
- 이 인계 전 검증 코드: **`72da577430fe43796040c1861a0c40dbde4fb064`**.
- 최초 AG 인수 기준: **`8174b5d790f36086488e84bb5d0733371fe07aaf`**.
- 연구 이전 운영 소스 기준: **`eac9bc5880d25902d0d9f695ffdaabcac9632a16`**.
- AG의 `7cbd894`, `6a04623`, `8174b5d`와 Work의 `46d7b14`, `72da577` 다섯 커밋이 순서대로 보존돼 있다. 인계 전 diff는 27개 파일이다.
- 이 문서와 마지막 출처 표시 수정은 후속 커밋이다. **최종 인수 SHA는 `git rev-parse HEAD`로 확인하고 채팅 및 비공개 `handoff-final/PUBLICATION.json`에 적힌 전체 SHA와 대조한다.** 문서 자체에 자신의 커밋 SHA를 소급해서 넣지 않는다.
- 로컬 작업 위치: `%USERPROFILE%\Documents\주식자동매매_개발\r6-causal-validation-20261020`.
- AG 원본 작업 트리 `r6-capture-consistency-20261020`와 원본 커밋은 보존했다. 오래된 소스로 이 연구 작업 트리를 덮어쓰지 않는다.

## 2. 최초 요청의 실제 진행 상태

| 항목 | 상태 | 확인된 범위 / 남은 부분 |
|---|---|---|
| 1. AG 89건 백테스트 교정 | 부분 완료 | 잘못된 재생을 교체하고 원형 결과를 재현·무효화했다. 원자료 부족으로 교정된 실측 89건/계좌 5건 손익을 확정할 수 없다. |
| 2. 미래 데이터·가짜 호가 제거 | 완료: 새 재생 경로 | 확정봉 이용 가능 시각과 실제 paired L1을 검사하고, 캐시만으로 체결을 만들지 않는다. 실측 자료가 없으면 UNKNOWN이다. |
| 3. 인과적·계좌 재생기 | 완료: 구현·표적 검증 | 실제 엔진의 진입·주문 심사·누적체결·포지션·청산 경로를 PAPER에서 사용한다. 거래소 대기열/숨은 유동성/정확한 지연은 복원하지 못한다. |
| 4. A/B/C/FAST 및 LB 재평가 | 부분 완료 | 기존 선택 WATCH의 샘플 gate/FAST 사유와 LB의 공통 주문 RR 불일치를 확인했다. A/B/C/FAST 전체를 독립 모집단에서 다시 실행한 성능 비교는 미완료다. |
| 5. 근거 있는 전략 개선 | 부분 완료 | LB의 확인 가능한 입력·가까운 저항·공통 위험검사 불일치를 수정하고 확정 돌파→눌림→재돌파 실험을 구현했다. 수익성 개선을 입증하지 못했다. |
| 6. 실제 엔진 모의 BUY→SELL | 완료: 합성 PAPER | 엔진이 INTENT/PRE_HTTP/누적체결/포지션/매도를 직접 생성한다. 테스트가 broker.submit을 직접 호출하거나 포지션을 수동 삽입하는 우회는 성공으로 인정하지 않는다. |
| 7. 독립 표본 기대값 | 미완료 | 서로 맞는 수신·봉·WATCH·호가·시나리오 자료가 부족하다. 승률/기대값/PF/실측 지연·1틱 영향은 UNKNOWN이다. |

## 3. 변경 코드와 인과성 경계

- `core/market_evidence.py`: START 표시 봉의 종료와 실제 이용 가능 시각을 분리한다. REST/사후 자료로 실시간 워밍업을 꾸미지 않는다.
- `research/causal_data.py`: 체결/호가 신선도, 수신/처리 순서, frozen byte extent와 prefix hash, 봉 이용 가능성 및 누락을 검사한다.
- `research/causal_account.py`: 네트워크 없는 전용 broker와 임시 장부에서 실제 `AutonomousEngine.enter` → 주문검사 → `cumulative_fill` → 기존 매도 함수를 호출한다. 현금/봇 자본/수동 보유/예약 주문/부분체결/취소/위험예산을 분리한다.
- 마지막 소규모 결함 수정: 합성 시험 호가의 fill도 `MEASURED_L1_MARKETABLE_SIMULATION`으로 표시되던 문제를 바로잡았다. `input_evidence_basis`를 별도로 보존하고, 합성·실측·불리 체결 가정을 구분한다. 가격·수량·체결 규칙은 변경하지 않았다.
- `research/causal_replay.py`: LB와 실험 재돌파를 같은 시점 자료로 비교한다. 기록된 ABC/FAST는 샘플 gate 감사이며 완전한 재생이라고 주장하지 않는다.
- `automation/local_breakout.py`: 이용 가능 확정봉/호가, 국소 저항과 비상선 위험, 기존 공통 RR 검사를 적용한다. `automation/shadow_observation.py`는 연구용 이용 가능성 메타데이터만 추가한다.
- `research/retest_strategy.py`: 미검증 PAPER 가설. LIVE 신규 전략으로 연결하지 않는다.
- `research/evidence_collector.py`: 기존 저널을 읽는 sidecar. 구독/REST/토큰/주문/LIVE hook을 만들지 않는다. 두 에피소드, 30분 관측, 메모리 링 1MiB/큐 2MiB/일 16MiB/전체 64MiB 한도와 누락·제외 카운터가 있다. 원본 누락은 복원하지 못한다.
- `tools/{reproduce_89_signals,full_universe_backtest,account_level_replay,run_all_replays,build_1019_bars,eval_1019_theoretical}.py`: 원형의 import 실행/가상 체결을 없애고 명시적 오프라인 CLI로 바꿨다.
- `tests/test_observation_reaction.py`: ABC 세 항목과 LB 연구 항목을 구분하면서 LB 주문 부적격·기각 사유·SHADOW 경계를 추가 검증했다.

## 4. 무효화된 결과와 현재 가능한 결과

원형에서 재현한 **89건/12승/77패/-71,184.2원, 계좌 5건/-2,948.5원 또는 -3,020.1원**은 전략 성능으로 무효다. 현재 봉 최종값을 시작에 이용하고 종가/1틱 아래/1,000주로 가짜 호가를 만들었으며 마지막 종가 강제 청산, 청산 후 MFE, 잘못된 계좌 재생이 섞였다. 원형은 `8174b5d`에 보존했다.

교정 캐시 CLI는 **거래 수와 순손익 UNKNOWN**을 반환한다. 입력은 10/20 **8,545봉/129종목**이며 실제 이용 가능성의 증거가 없는 참고봉이 다수다. 기존 요청의 7,910개라는 숫자를 실제 파일 집계로 간주하지 않는다.

- 10/20 탐색: 798,936 frame/202종목, 신선한 paired quote 302,512개. LB/재돌파 모두 입증된 후보 0, 완료 거래 0, 순손익 `null`. **당시 입력이 증가했고 구형 결과에 frozen manifest가 불완전하여 확정 성능 자료로 쓰지 않는다.**
- 10/19 확인: 41,719 frame/98종목, 14:29~15:00 구간. LB/재돌파 입증 후보 0, 완료 거래 0, 순손익 `null`. frozen prefix가 있지만 오전 샘플 신호와 시간대가 겹치지 않으며 설정도 10/20 시나리오를 사용했다. 독립 기대값 검증으로 인정하지 않는다.
- 합성 엔진 재현: 1,004원 70주 BUY → 정상 1틱 눌림 HOLD → 확정봉 구조 훼손 시 bid 997원 70주 SELL. 완료 왕복 1건, 설정 비용 포함 추정 손익 **-650.5905원**. 실제 시장 거래나 전략 기대값이 아니라 코드 경로 증거다. 마지막 출처 표시 수정 전후 가격/수량/손익은 동일해야 한다.
- 500ms 지연의 도착 전 미체결과 불리 1틱이 BUY limit을 넘으면 체결하지 않는 반례를 시험했다. 실제 자료의 지연·1틱 순손익 민감도는 아직 미검증이다.

## 5. 테스트 결과: 서로 합쳐 쓰지 말 것

1. 기존 전체 적용 범위: **1,887 실행 / 1,886 PASS / 1 FAIL / ERROR 0 / SKIP 0**, 약 1,128초. 원본 `REGRESSION.stdout`.
2. 해당 실패는 AG 인수 원형에서도 `4 != 3`으로 재현했다. 원본 `AG_BASELINE_FAILURE.stderr`. LB trial 추가 후 기존 진단 시험이 전체 3개를 가정했다. 실패 시험을 없애지 않고 ABC 3개/LB 1개와 실제 차단 근거를 별도로 검증하도록 수정했다.
3. 수정 후 기존 보고 및 이번 마무리 재검사: **62 PASS**, 마지막 재검사 7.438초, 실패/오류/SKIP 0, 네트워크 연결 시도 0. `handoff-final/TARGETED.json`.
4. 합성/실측 fill 출처 표시 수정 후 세 개 출처 검사를 포함한 **65개 PASS**, 실패/오류/SKIP 0, 7.703초, 네트워크 시도 0을 확인했다. 최종 결과는 `handoff-final/FINAL_TARGETED.json`이다. 전체 1,887개 재실행 결과로 바꾸어 쓰지 않는다.
5. 미배포 연구 테스트 **49개는 미실행**이다. 제외 이유는 기존 `REGRESSION.stdout`의 `out_of_scope_not_run`에 있다. 삭제하거나 통과로 집계하지 않는다.

## 6. 즉시 재현할 작은 명령

PowerShell에서 기존 연구 폴더를 사용한다. 아래 `$private`는 소스/운영 폴더 밖 비공개 출력 경로다.

```powershell
$research = Join-Path $env:USERPROFILE 'Documents\주식자동매매_개발\r6-causal-validation-20261020'
$private = Join-Path $env:USERPROFILE 'Documents\주식자동매매_개발\r6-causal-validation-20261020-private'
$py = Join-Path $env:USERPROFILE 'Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe'
Set-Location -LiteralPath $research
git status --short --branch
git rev-parse HEAD
git log -7 --oneline
git ls-remote origin refs/heads/research/backtest
& $py -X utf8 -B -m unittest tests.test_causal_replay tests.test_local_breakout_strategy tests.test_local_breakout_e2e_integration tests.test_research_evidence_collector tests.test_shadow_clock_consistency tests.test_observation_reaction
& $py -X utf8 -B tools/run_all_replays.py --output-dir (Join-Path $private 'ag-cache-recheck')
```

캐시 재검사의 UNKNOWN은 실제 호가와 이용 가능 시각이 부족하다는 뜻이다. 부족한 자료를 채우기 위해 가상의 거래를 추가하지 않는다.

실측 재생은 [REPRODUCE.md](REPRODUCE.md)의 CLI를 사용하되 **먼저 불변 입력을 확보**한다. 오래된 `run_measured.py`/`run_independent.py`를 현재 LIVE의 회전 로그에 그대로 다시 실행하면 다른 입력이다. 10/19 결과의 `frozen_bytes`/`sha256_prefix`에 맞는 원본이 현재도 존재하는지 확인하고 일치할 때만 별도 비공개 복사본을 만든다. 불일치·누락은 UNKNOWN으로 남긴다. 10/20 구형 탐색 결과는 정확한 과거 입력 재현이 가능하다고 가정하지 않는다.

다음 장 독립 수집은 선택 사항이며 운영 프로세스 시작과 별개다. 다음 명령은 수동 실행할 때만 동작하고 예약하지 않았다.

```powershell
$live = Join-Path $env:USERPROFILE 'Desktop\주식자동매매'
& $py -X utf8 -B -m research.evidence_collector --source-root $live --output (Join-Path $private 'next-session-evidence') --duration-seconds 21600
```

## 7. 비공개 자료 위치·출처

루트는 `$private`다. GitHub에 원본을 추가하지 않는다.

| 자료 | 파일/경로와 출처 |
|---|---|
| AG 원형 재현 | `AG_ORIGINAL_89.stdout`, `AG_ORIGINAL_ACCOUNT.stdout` — AG 인수 원형 실행 결과 |
| 원본 캐시 집계 | `INPUT_INITIAL_AUDIT.json`, `CORRECTED_CACHE_AUDIT.json` — 인수 캐시를 직접 집계 |
| 설정 시나리오 | `BASELINE_config.json`, `BASELINE_automation.json` — 당시 읽기 전용으로 확보한 설정. 거래일 전체 및 전일과 동일했다는 증거는 아님 |
| 전체 검사/최초 실패 | `REGRESSION.stdout`, `AG_BASELINE_FAILURE.stderr`, `test-capsule/` — 격리 검사 복제본, 배포 후보 아님 |
| 직전 표적 결과 | `FINAL_RECHECK.stderr`, 이번 `handoff-final/TARGETED.json` |
| 마지막 수정 검증 | `handoff-final/FINAL_TARGETED.json`, `.stdout.log`, `.stderr.log`, `ENGINE_SMOKE_FINAL.json`, `CLI/` |
| 제한된 실측 탐색 | `MEASURED_20261020_PROCESSED_L0.json`, `INDEPENDENT_20261019_PROCESSED_L0.json` — 각 입력 경로/해시/범위/가정이 포함됨 |
| 봉/감시 스키마 | `TRADE_REVIEW_SCHEMA_20261019.json`, `TRADE_REVIEW_SCHEMA_20261020.json` |
| 마지막 원격 보존 | `handoff-final/PUBLICATION.json` — 최종 SHA/원격 일치/PR 상태/파일 목록 |

시세 원본은 운영 디렉터리의 `runtime/shadow_ticks.jsonl*` 및 `runtime/automation-production/trade-review/trade-review-YYYYMMDD-*.jsonl`였다. 현재 파일명은 회전으로 다른 바이트를 가리킬 수 있다. 원본은 읽기 전용이며 계좌/토큰/장부를 수집·업로드하지 않는다.

## 8. AG 후속 작업 우선순위와 금지

1. 이 문서와 private 결과를 먼저 읽고 브랜치/SHA/미커밋 상태를 대조한다. 코드·최초 감사·전체 검사를 처음부터 반복하지 않는다.
2. 다음 독립 날짜의 paired tick/book, 이용 가능한 연속 확정봉, 당시 WATCH/소스 자격, 설정·시작 현금 시나리오를 불변 prefix/hash와 함께 확보한다. sidecar는 기존 로그 누락을 해결하지 못하므로 누락률과 원인부터 수치화한다.
3. 기존 A/B/C/FAST를 같은 인과적 시간축으로 재실행하는 작업은 아직 없다. 해당 adapter를 안전한 PAPER 연구에 추가하고 기존 gate와 주문 경계가 유지되는지 검증한다. 샘플 로그 집계와 전략 재실행을 혼동하지 않는다.
4. 기존 LB의 위험/RR 거절, 실측 데이터 부족, 후보 생성 부재를 분리한다. threshold 완화로 신호를 억지 생성하지 않는다. retest 실험은 성공한 개선이 아니다.
5. 설정과 날짜를 동결하고, 개발일과 독립일을 분리해 동일 계좌/비용/유동성/지연/불리 1틱 조건으로 진입부터 청산까지 비교한다. 미청산/미체결/부분체결을 남기고 가능한 표본만 평가한다. 승률·평균 손익·PF·기대값·MFE/MAE·손실/연속손실·coverage를 함께 보고한다.
6. 자료가 부족하면 UNKNOWN을 유지한다. 4%는 갭 손실 상한 보장이 아니다. 소수 합성/실거래로 플러스 기대값을 선언하지 않는다.

**LIVE 변경·재시작·주문·승인 편집·위험 한도 확대·HARD_LOCK 변경·기록 삭제·main/dev 병합·v1.7 출시 금지.** 연구를 실제 배포하려면 별도 검증과 사용자 승인이 필요하다. 현재 LIVE의 2%/4%, HARD_LOCK 005930/017900/052260, 구조선/비상선 분리 및 데이터 장애 보호는 이번 연구에서 변경하지 않았다.
