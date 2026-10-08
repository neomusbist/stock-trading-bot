# 2026-10-08 안정화: 최종 오프라인 검증 완료 / 운영 미배포

최종 코드 기준 전체 regression6: **833 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. 시작 보존 기준 797개 + 신규 36개. syntax 98개 정상. 실제 네트워크 시도 0 / 실제 주문 0 / 운영 프로그램 시작 0. 관련 대상 63 PASS는 전체에 포함되므로 합산하지 않는다. 마지막 전체 실행 약 148.30초. 초기 실패·오류 및 수정 근거는 아래 역사 기록과 개발 검증 산출물에 보존했다. 삭제/skip한 기존 검사는 없다.

주요 안전 검증: persisted INTENT와 승인 exact equality 유지, PRE_HTTP evidence는 별도 이벤트로 기록; A/C 및 위험·기존 청산 수식 유지; B의 첫 완성봉 시가→impulse 완성 종가 상승 확인으로 wick-only 오인 차단과 정상 첫 봉 상승 보존; 실제 risk-sized quantity depth / freshness / source 및 SHADOW 격리 / UNKNOWN·HALT·HARD_LOCK / lifecycle·다음날 시작 회귀 통과. 테스트 PASS는 실제 API 응답·실제 체결·전략 기대값의 입증이 아니다.

BUILD_ID=V1.3-risk2-watch12-bc20M-stabilization-20261008
STRATEGY_VERSION=V1.3-b-confirmed-impulse-bc20M-20261008
STRATEGY_EXPERIMENT=20261007-risk2-daily4-dynamiccap-watch12-accelA-bc20M-bclose20261008
risk_fraction=.02 / max_daily_loss_fraction=.04 / WATCH12 / max_positions1 / 동적 승인 cap. B 허용 형상이 달라졌으므로 전략 버전을 구분한다. 일/종목/연속손실 횟수 제한 제거의 기존 장중 패치는 보존.

별도 fix/weekend-stabilization-20261008 분기에서만 검증했다. 기존 main 및 운영본 소스·config·runtime·계좌·승인·거래로그를 쓰지 않았고, merge/push/배포/프로세스 재시작/대신 LIVE 승인도 하지 않았다. 실제 응답 검증, exit 정책 비교, 장시간 부하 및 종료 후 배포 인수검사는 미완료다. 아래 진행중 표현은 과거 체크포인트 기록이며 최신 판정은 이 절이다.

# 마지막 형상 감사 후 검증 상태

full regression5: 831 PASS/0FAIL/0ERROR/0SKIP. 첫 완성봉 몸통 내 정상 상승을 B impulse에서 버리지 않도록 기준을 첫 완성봉 시가→impulse 완성 종가로 정리했다. 기존 .008 유지. 신규 positive/negative2개 포함 관련63 PASS. 마지막 full regression6 결과 전 배포 완료로 표시하지 않는다.

# 최종 코드 검증 진행상태 (10/08 안정화)

830 PASS였던 기준에 주문근거 기록을 추가한 후 regression4에서 자기 INTENT 저장값 일치 회귀3건을 발견했다. PRE_HTTP 진단을 별도 이벤트로 분리하여 기존 authorization exact match를 유지했다. 관련 대상52 PASS/0FAIL/0ERROR/0SKIP, 실제 network0. 최종 full regression5 결과를 확인하기 전 배포 완료로 취급하지 않는다. 초기 실패는 보존한다.

# 10/08 거래 후 안정화: 개발 검증 상태

별도 fix/weekend-stabilization-20261008 분기, 운영본 미배포. 시작 기준797 PASS, 기능 수정 후830 PASS/0FAIL/0ERROR/0SKIP, syntax98 정상, 실제 network0/orders0/program-start0. 기존 검사 삭제/skip 없음. 신규33개 포함.

마지막 주문근거 기록(완성봉 snapshot 및 전송 직전 receiver evidence) 추가는 대상25 PASS. 마지막 코드 기준 전체 regression4는 아직 실행 전이며 이 문서는 최종 배포 완료 보고가 아님. 25개 대상 검사는 전체에 포함되므로 합산하지 않는다. B 종가 impulse 확인은 전략 정의 변경이므로 STRATEGY_VERSION을 구분한다. A/C 및 위험/청산 수식 변경 없음.

BUILD V1.3-risk2-watch12-bc20M-stabilization-20261008 / STRATEGY V1.3-b-confirmed-impulse-bc20M-20261008. 실제 API 응답/원시 과거 틱 완전 replay/장시간 CPU/새 전략 기대값 미검증. 인계문서의 최신 10/08 안정화 절차부터 이어간다.

## 아래는 이전 검증의 역사 기록

# 10/08 심야 2차 최종 검증

BUILD `V1.3-risk2-watch12-bc20M-opening-20261008` / STRATEGY_VERSION 및 risk 실험 식별자 동일. 기존715 + 신규15 = **730 PASS / 0 FAIL / 0 ERROR / 0 SKIP**, 71.47초. targeted38개는 전체에 중복 포함되므로 합산하지 않음. 새 build 기대값만 최신화하고 기존 검사 assertion은 유지.

새 검사는 한 signal 내 계산1회, 같은 시각 quote/depth/status 변경 재검사, SHADOW diagnostic sizing dedupe/tier 유지, daily cache 변경/날짜/삭제/용량상한, metadata refresh의 chart 보존, +2 hysteresis 및 stronger tier 승격, frozen Tick 재사용/외부값 거부, 실제 runner 08:55 구독12→09첫틱, 실제 ingest A2/B4/C5 도달, 다종목28→affordable24→WATCH12/관찰24→각Setup→fake first fill1/max_positions1을 포함합니다.

3쌍 동일 입력 비교 각각 WATCH447/SHADOW640 기록, metadata32종목×6회 snapshot 동일. 실제 단말 CPU30% 재현이나 실제 거래 검증이 아님. 로그/승인/계좌/runtime/설정은 변경하지 않음. full suite 네트워크 차단, 가짜 broker 및 임시 장부 사용. 실제 주문·네트워크·LIVE 승인·START 0. 배포는 full PASS 뒤 최소 allowlist와 코드백업/보호hash로 검증.

## 이전 야간 안정화 기록 (715 기준, 역사 증빙)

# 2026-10-07 야간 안정화 검증

BUILD_ID: `V1.3-risk2-watch12-bc20M-night-20261007`. STRATEGY_VERSION/STRATEGY_EXPERIMENT·A/B/C·WATCH12·risk2/daily4/cap20000는 유지. 실제 운영 설정 변경 없음.

전체 1차(PHASE1 이후): **702 PASS / 0 FAIL / 0 ERROR / 0 SKIP**, 79.62초. 전체 2차(최종 코드): **715 PASS / 0 FAIL / 0 ERROR / 0 SKIP**, 71.75초. 기존 695개 + 신규 20개. 다음날/entry/readiness 재확인 36 PASS. targeted 수를 전체 검사 수에 합산하지 않음.

첫 전체 실행의 701개 중 1 ERROR는 폐장 검사 fixture가 AFTER 활성 설정에서도 16:00을 폐장으로 가정했기 때문. 20:01로 수정하고 credentials/Rest 미호출 assertion을 유지했다. 신규 통합검사 작성 중 존재하지 않는 detector helper 호출을 실제 base_gates warmup 값으로 수정했다. 검사 삭제/skip/안전 assertion 완화 없음. 최초 실패 출력은 private 검증 폴더에 보존.

## Entry lifecycle

enter 직전 시도 횟수 증가를 제거했다. 최초 유효 BUY 체결→position 생성에서 local lifecycle별 1회 증가. 추가/중복 fill은 0회 증가. INTENT/접수/0체결 거절·취소는 0회. active unfilled BUY는 daily slot/현금/위험 예약을 유지한다. 실패/0체결 취소 종목은 당일 자동 재전송 금지 latch로 분리한다. UNKNOWN/HALT/foreign INTENT/disk mismatch/duplicate는 계속 차단한다.

10/7 세 authorization 내부 거절은 filled=0이므로 실제 진입 0건으로 해석한다. 원본 REJECTED 레코드와 이전 시도 count migration 증빙은 보존한다. broker 전달 증거가 없으면 broker reject로 표시하지 않는다. signal/internal reject/HTTP submit 또는 명시 response/broker reject/first-fill count를 구분한다. 실제 운영 장부를 이번 작업에서 migration하거나 초기화하지 않는다. 다음 사용자 실행 때 정상 날짜 전환/기존 장부 migration 경로를 사용한다.

## 성능 측정 범위

실제 코드·fake broker·격리 runtime으로 동일 181초 합성 스트림(WATCH12 2,172틱, SHADOW32 5,792틱, GUI13회, radar13회)을 12초 pacing하여 비교했다. CPU는 4 logical CPU로 정규화한 process-time 표본이다. 실제 장중 작업관리자 30%를 재현하지 않았으며 30%→14% 개선이라고 해석하지 않는다. disk 수치는 쓰는 논리 바이트이며 물리 디스크 처리량이 아니다. 쓰기/log/JSON/구독 rate는 합성 시장 181초당 양을 1분으로 정규화했다. cProfile 계측 시간은 아래 비계측 latency와 섞지 않는다.

| 항목 | 전 | 후 | 해석 |
|---|---:|---:|---|
| 평균 CPU (%) | 15.228 | 14.416 | 동일 합성 부하 |
| peak CPU (%) | 24.513 | 23.818 | 250ms 이상 표본 |
| 논리 disk write (MB/min) | 3.151 | 1.678 | physical throughput 아님 |
| log 증가 (MB/min) | 3.077 | 1.604 | 원본 JSONL 유지 |
| JSON writes/min | 24.199 | 24.199 | 절감하지 않음; 안전 저장 유지 |
| subscription ops/min | 0.331 | 0.331 | 변화 없는 refresh181회 중 실제1회 |
| candidate cycle (ms) p50 / p95 / max | 1.167 / 1.268 / 1.699 | 1.178 / 1.706 / 1.773 | API 대기 없는 합성 입력 |
| WATCH tick→evaluation (ms) p50 / p95 / max | 0.607 / 1.667 / 5.921 | 0.615 / 1.661 / 4.869 | API 대기 없는 합성 입력 |
| GUI snapshot (ms) p50 / p95 / max | 154.978 / 207.431 / 270.276 | 78.739 / 90.839 / 94.698 | API 대기 없는 합성 입력 |

WATCH 경로 속도는 거의 동일하고 짧은 radar 표본 p95는 후가 더 느리다. radar 평균 8.375초/최대10.065초·status5.597초·APIwait5.351초는 이전 저장 운영 감사 수치이며 이번 HTTP 없는 측정으로 개선을 주장하지 않는다. slow collect를 별도 스레드에서 Event로 대기시킨 검사 중 WATCH 평가가 완료됨을 확인했다. 실제 OS/GIL/API 부하를 포함한 장시간 검증은 남아 있다.

### 최종 hot path 비계측 latency

| 단계 | n | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|
| parse | 2172 | 0.060 | 0.112 | 1.588 |
| observation | 2172 | 0.067 | 0.125 | 0.488 |
| setup_signal | 2172 | 0.360 | 0.791 | 2.916 |
| setup_evaluation_gates | 2616 | 0.002 | 0.003 | 0.425 |
| risk_gate | 3 | 9.025 | 9.025 | 10.516 |
| signal_to_pre_intent | 3 | 13.044 | 13.044 | 13.573 |
| signal_to_fake_accept | 3 | 46.145 | 46.145 | 46.297 |

RiskGate/INTENT는 A/B/C 각 1개 정상 합성 신호(n=3)여서 p95의 통계적 신뢰는 낮다. fake acceptance 시간은 네트워크 시간이나 실제 체결 지연이 아니다.

### CPU 상위 20개 함수 (최적화 전, cProfile)

| 함수/위치 | 호출 | self CPU s | cumulative CPU s |
|---|---:|---:|---:|
| REPO\tools\night_profile.py:41 wrapped | 9145 | 0.1719 | 25.5625 |
| REPO\automation\shadow_observation.py:90 tick | 5792 | 0.3906 | 22.2500 |
| REPO\automation\patterns.py:182 signal | 7391 | 1.5781 | 21.4219 |
| REPO\operator_ui_service.py:349 snapshot | 13 | 0.0625 | 17.4219 |
| REPO\local_diagnostics.py:28 redact | 337379 | 5.2969 | 15.8281 |
| REPO\automation\patterns.py:252 base_gates | 7391 | 3.4375 | 12.5781 |
| REPO\automation\runner.py:325 process_message | 2172 | 0.2031 | 10.9688 |
| REPO\automation\decisions.py:41 safe_values | 563135 | 6.1250 | 9.5625 |
| REPO\automation\runner.py:266 evaluate_actual_entry | 2172 | 0.0625 | 7.9063 |
| REPO\automation\decisions.py:47 gate | 96421 | 0.4844 | 6.4062 |
| <frozen os>:811 getenv | 198192 | 0.9531 | 6.1094 |
| REPO\automation\patterns.py:140 setup_trials | 7391 | 1.0781 | 5.5156 |
| ~:0 <built-in method builtins.isinstance> | 2754523 | 5.3594 | 5.3594 |
| <frozen _collections_abc>:808 get | 198192 | 1.2656 | 5.1562 |
| REPO\automation\engine.py:119 event | 761 | 0.0469 | 5.0000 |
| REPO\automation\shadow_observation.py:36 event | 640 | 0.0156 | 3.9375 |
| <frozen os>:712 __getitem__ | 198192 | 1.0625 | 3.8906 |
| REPO\tools\night_profile.py:71 <lambda> | 640 | 0.0000 | 3.8906 |
| C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_3.13.3824.0_x64__qbz5n2kfra8p0\Lib\unittest\mock.py:1164 __call__ | 4409 | 0.0938 | 3.2969 |
| ~:0 <method 'total_seconds' of 'datetime.timedelta' objects> | 1299335 | 3.2656 | 3.2656 |

재귀 redaction의 매 노드 env/regex 준비와 동일 로그 tail 중복, safe_values 재귀 변환, SHADOW/PatternDetector 반복 gate 평가가 큰 비용이었다. cumulative는 부모/자식이 겹쳐 합계하면 안 된다. profiler wrapped와 타이머 자체 비용도 포함된다. 전략 판단 캐시나 SHADOW/WATCH 통합은 적용하지 않았다.

### 최적화 후 CPU 상위 20개

| 함수/위치 | 호출 | self CPU s | cumulative CPU s |
|---|---:|---:|---:|
| REPO\automation\shadow_observation.py:90 tick | 5792 | 0.5312 | 18.7188 |
| REPO\automation\patterns.py:182 signal | 7391 | 1.2969 | 17.9531 |
| REPO\tools\night_profile.py:41 wrapped | 9148 | 0.1719 | 14.4531 |
| REPO\automation\patterns.py:252 base_gates | 7391 | 2.7031 | 10.3594 |
| REPO\automation\runner.py:325 process_message | 2172 | 0.0625 | 9.2031 |
| REPO\operator_ui_service.py:349 snapshot | 13 | 0.0000 | 7.1406 |
| REPO\automation\runner.py:266 evaluate_actual_entry | 2172 | 0.1250 | 6.5781 |
| REPO\automation\decisions.py:51 safe_values | 97182 | 0.4062 | 6.2031 |
| REPO\local_diagnostics.py:28 redact | 78 | 0.0156 | 6.1562 |
| REPO\local_diagnostics.py:34 walk | 337379 | 3.8750 | 6.1406 |
| REPO\automation\decisions.py:41 _safe_walk | 485031 | 4.0938 | 5.7969 |
| REPO\automation\decisions.py:56 gate | 96421 | 0.4062 | 4.9531 |
| REPO\automation\patterns.py:140 setup_trials | 7391 | 0.9844 | 4.8438 |
| REPO\automation\engine.py:119 event | 761 | 0.0156 | 4.0781 |
| REPO\automation\shadow_observation.py:36 event | 640 | 0.0312 | 3.5000 |
| REPO\tools\night_profile.py:77 <lambda> | 640 | 0.0000 | 3.4688 |
| ~:0 <method 'total_seconds' of 'datetime.timedelta' objects> | 1299335 | 3.0781 | 3.0781 |
| C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_3.13.3824.0_x64__qbz5n2kfra8p0\Lib\unittest\mock.py:1164 __call__ | 4409 | 0.0781 | 2.7500 |
| ~:0 <built-in method builtins.isinstance> | 1447195 | 2.7031 | 2.7031 |
| REPO\core\engine.py:115 event | 761 | 0.0312 | 2.3750 |

## 로그·안전·동등성

콘솔 4,641,579→198,651 bytes (95.72% 감소). 전체 log 9,283,158→4,840,230 bytes (47.86% 감소). machine JSONL payload는 동일하고 human full 중복만 줄었다.
원본 JSONL3종은 64MiB 보존 회전, console은 runner 시작 전만 회전. 실행 중 대형 압축/삭제 없음. 짧은 bounded retry 후 PermissionError는 전파하며 원본 로그를 잃지 않는다. GUI는 512KB bounded active/archive tail을 한 번 읽는다. Windows API signatures만 캐시하고 KILL/HALT/UNKNOWN/승인/정책/장부 내용은 매번 읽는다. 동시 writer + 반복 GUI snapshot 검사가 통과했다.

성능 변경 전후 **447개 판단 기록 완전 동일**: WATCH 평가 gates/setup_trials/signal/entry/invalidation/quantity/order status/filled-entry count. 정상 first-fill 및 daily count 수정 자체는 의도된 의미 변경이며 performance equivalence 비교의 baseline에는 이미 적용한 상태다. SHADOW 주문 격리·구독/continuity·패턴/위험/보호/종료 테스트 유지.

## 다음날 E2E / 미검증

2026-10-08 08시 GUI 시작 클릭 한 번으로 기존 표시 cap 승인을 수행하고, 09시 receipt를 변경하지 않고 실제 첫 정규장 틱→OPEN_READY, 전략 warmup 후 유효 B→fake accepted(진입count0)→first fill(count1)→duplicate fill(count1)까지 같은 임시 장부에서 검증했다. 09시 재승인·승인문자열·금액 재입력 없음. standalone runner 준비검사와 A/B/C/first-fill/safety 회귀를 함께 실행했다.

전일 승인/다른 build는 거절되고 해당 날짜 클릭으로 다시 승인한다. preopen tick은 정규장 봉에 섞지 않는다. 이전 HALT/전일 미해결 주문을 날짜만 바뀌었다고 풀지 않는다. 실제 장중 API 가용성, CPU peak, first tick 지연, 주문/취소/청산 체결·슬리피지·수익성은 미검증. 실제 주문 0건·실계좌 네트워크 0건.

## 배포 상태

전체 PASS 후 종료를 확인하고 배포 완료: 13개 교체 + 2개 추가, SHA256 15/15 일치, 계좌/설정/승인/runtime/기존 로그 보호 데이터525/525 해시 일치. Python 소스40개 구문검사와 모듈9개 import 통과. 백업은 운영폴더 _backup_night_stabilization_20261007에 코드·문서만 보존했다. 최종 파일별 SHA256은 private deployment.json에 기록했다. 테스트/프로파일 도구는 운영본에 배포하지 않는다. Git 기존 46개 변경을 보존; 이번 작업 commit/push/reset/restore/revert 없음.

---

## 이전 검증 기록

# 2026-10-07 Discovery/WATCH 복원 최종 검증

기준 `de5fc0c`의 정리된 기존 533개 검사 + 신규 18개 = **551 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. 최종 전체 실행 60.30초. 아래 620개 표기는 테스트 정리 전의 역사 결과이며 현재 검사 수가 아니다.

최종 targeted: **386 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. 후보 압축, SHADOW, WATCH 경쟁/승격, 구독, A/B/C, readiness, 위험 identity, 보호·복구·GUI 운영 경로를 포함한다. 전체/targeted 모두 네트워크 접근 시도 0건. 실제 broker 주문 0건.

추가 18개 검사는 source1의 min_source_count=2 운영 설정 호환, 전체 8슬롯 경쟁, 더 좋은 tier/점수 교체와 작은 점수차 유지, 후보 행/현재 자격/재생성 신호의 fallback 일치, 정상 history 유지, A/B/C 실제 detector부터 가짜 broker guard까지 도달, warmup/가속/유동성/spread/구조/RR/Economic/risk 실패 차단을 확인한다. 별도로 자기 정상 INTENT와 외부 INTENT/UNKNOWN/HALT, 타 장부 INTENT, 저장 레코드 불일치, HARD_LOCK/외부보유/자본0/계좌 freshness, 중복 및 계좌 identity를 검증한다.

개발 과정에서 숨기지 않은 실패:
- 최초 연결 targeted 268개 중 정상 자기 INTENT의 당일 승인 오차단 1건을 재현했다. 정확한 현재 레코드 한 개만 구분하는 수정 후 통과했다.
- 전체 첫 실행은 474개, 470 PASS / 1 FAIL / 3 ERROR였다. 개발 QtTest overlay가 빠져 GUI 3모듈이 로드되지 않았고, continuous mock 후보에 실제 입력이 항상 갖는 source_count가 빠져 있었다. 기존 QtTest를 개발 실행기에 연결하고 mock 후보에 source_count/source_details를 명시했다. 기존 assertion을 삭제하거나 완화하지 않았다. 이 원인 해결 후 최종 전체 결과는 위 551 PASS이다. 최초 실패 출력은 private 검증 자료에 보존했다.

구문검사: 공개 Python 소스 73파일 정상. diff whitespace 검사 정상. repo_guard/staged 검사는 최종 커밋 전 별도로 수행한다. 원본 운영 로그와 계좌/승인/설정은 Git에 포함하지 않는다.

오늘 6종목 163주기 재검사는 source-only 거절 0건을 확인했다. 비엘팜텍 36회 관찰 압축 자격 통과 외 표본은 기존 경제성/거래대금/구조/감속 또는 당시 공식 상태 미확인으로 탈락/대기한다. 전일까지 일봉 재계산은 당시 캐시 보유를 증명하지 않으며, 부분 유니버스 replay로 실제 WATCH 편성·Setup·BUY·수익성을 주장하지 않는다. 실운영본 배포/재시작 및 Git push는 수행하지 않는다.

---

## 테스트 정리 이전의 역사 결과

# Offline verification
The preserved V1.1 suite has 620 tests, including original regressions and SHADOW/entry/exit/GUI checks. Previous validation: 620 PASS, 0 FAIL, 0 ERROR, 0 SKIP. Re-run the entire suite after installing dependencies; do not infer current results from this historical count.

Tests use fake brokers, synthetic accounts, temporary files and Qt test services. No real broker orders or account network tests form part of this result. A/B/C paths, source/SHADOW isolation, cap-zero permission, protected holdings, monotone stops, rejection/partial fill/recovery and session boundaries are covered.

Local verification outputs, account data, actual logs, screenshots, performance samples and backup archives are excluded from this repository. QtTest is a development dependency; production GUI modules are QtCore/QtGui/QtWidgets.
