# R6 인계 — 2026-10-16

> **2026-10-19 최신 갱신:** 아래 10/16 상태는 과거 기록이다. 최신 브랜치는 `fix/r6-opening-loop-20261019`, 코드 커밋은 `102ccfbfc2174996084be58b4e4bd06f6ebb2547`이다. 7e60406 운영본의 개장 후 큐 처리·관측 유지·계좌 캐시 결함을 고쳐 09:21에 runner 1개 파일을 반영하고 기존 당일 승인으로 LIVE 감시를 정상 재개했다. 자세한 검증과 현재 운영 상태는 [OPENING_LOOP_FIX_20261019.md](OPENING_LOOP_FIX_20261019.md)를 먼저 읽는다. 위험예산·당일 장부·기존 전략 조건은 보존했다.

상태: **수정 및 검증 완료**. 14:42 한선엔지니어링 FAST 상단 저항 진입(순손익비 -0.29) 및 13:25 Setup B 비용 반영 순손익비(0.67) 가드 누락 결함을 코드 수준에서 수정 완료(커밋 9693696). 13개 모듈 193개 표적 테스트 100% PASS. 운영본은 안전을 위해 여전히 공식 STOP 상태를 유지하며 임의로 LIVE를 재개하지 않는다.

## 1. 작업 위치와 보존

- 개발본: `C:\Users\MJS\Documents\주식자동매매_개발\r6-target-cap-incident-20261015`
- 운영본: `C:\Users\MJS\Desktop\주식자동매매`
- 브랜치: `fix/r6-target-cap-incident-20261015`
- 코드 커밋: **`9693696`** (이전 커밋: `2f65f8edab6662ef5504810628c74b500d827c52`)
- 작업 시작 HEAD: `424e89a4491a03caafbe618d634343690fe22056`
- 문서만 추가한 후속 커밋이 있을 수 있다. 위 해시는 실제 코드 커밋이다. 현재 HEAD는 `git log -2 --oneline`으로 확인한다.
- GitHub push: **하지 않음**. 이번 요청의 GitHub 작업 금지 범위를 준수했다. main 및 다른 운영 브랜치를 바꾸지 않았다.
- `2affd6d`는 시작 HEAD의 조상이 아니다. 공통 조상은 `2da04d3c794444b1a1332e8a2f0209bad3227049`다. 커밋 계보만으로 변경 누락을 단정하거나 이 커밋을 통째로 덮어쓰지 않는다. 최신 로컬 파일을 기준으로 작업했다.
- 기존 미커밋 구조선/비상선 분리와 `tests/test_fast_momentum.py`의 기존 변경을 보존하여 함께 커밋했다. 이 테스트 파일의 기존 EOF 공백 경고는 그대로 두었다.
- 민감한 운영 설정·계좌·승인 영수증·로그는 커밋하지 않았다. 아래 private 폴더는 Git 저장소 밖이다.

## 2. 마지막 긴급 거래: 한선엔지니어링

| 구분 | 13:25 거래 | 14:42 재진입 |
|---|---|---|
| 전략 | B | FAST |
| 매수/수량 | 16,700원 × 4 | 16,810원 × 4 |
| 매도 | 16,580원 | 16,760원 |
| 엔진 장부 보유시간 | 398.953초 | 153.134초 |
| 증권사 비용 반영 순손익 | -622원 | -354원 |
| 수수료+세금 | 142원 | 154원 |
| 초기 구조선/비상선 | 16,610 / 16,580 | 16,760 / 16,750 |
| 실제 청산 사유 | 호가 급락/유동성 붕괴: 비상선 도달 | FAST_INVALIDATION_STOP_EXIT: 서로 다른 시각의 2체결 + 매수호가가 구조선 이하 |
| MFE / MAE, 주당 | +50 / -120원 | +10 / -50원 |

엔진 장부의 개설/종결 시각은 각각 `13:25:20.876714~13:31:59.829657`, `14:42:42.315325~14:45:15.449620`이다. 이는 로컬 장부 계상 시각이며 거래소 체결의 밀리초 시각으로 해석하면 안 된다. 사용자 증권사 체결 알림의 분 단위 시각 및 가격과 일치한다.

14:42 신호는 `14:42:34.292640`에 생겼다. FAST는 14:41:14의 16,730원에서 시작한 미세 반등, 14:42:07의 16,790원 고점, 14:42:24의 16,780원 눌림 및 14:42:29 반등을 사용했다. 완충은 `max(틱 10, 스프레드 10, 지지×0.1%=16.78)` → 구조선 16,760원, 추가 잡음 10원 → 비상선 16,750원이다. 10초 거래대금은 35,166,380원, 30초 상승 틱 방향 거래량 비율은 0.7062였다. 이는 매수 주도 체결의 직접 식별이 아닌 틱 방향 대용치다.

**문제:** 80초 정도의 반등을 매수 근거로 사용하면서 목표는 먼 저항 17,850원이었다. 14:26~14:40의 저장된 수신 틱 1,635개에서 14:27의 16,880원 고점을 확인했다. 14:31에도 16,870원 반등 고점이 있었다. 현재 FAST는 기존 PriceContext의 장중 확정 저항/5분 구조를 진입의 필수 근거로 사용하지 않는다. 재시작 뒤 미세 틱 관측은 14:41:02부터였고 FAST 신호에 참고 분봉/장중 맥락이 보존되지 않았다.

같은 FAST 비용 가정으로 목표 상한을 16,880원으로 놓으면 순손익비는 **-0.29056**, 저장된 자료로 재구성한 가까운 16,870원 상한은 **-0.34513**이다. 기존 신호의 17,850원 목표 순손익비 5.00294와 다르다. 다만 원래 프로세스가 이 고점을 계속 보유했는지, 모든 구간이 연속 관측됐는지까지는 증명하지 못했다. 재구성은 조건부 비교이며 실제로 이 매수를 차단했을 것이라는 검증 완료 주장이 아니다.

13:25 B의 사후 계획 표시에는 비용 반영 손익비 0.67490, `reward_guard_enabled=false`가 남아 있다. FAST와 별도로 B의 비용/목표 검증 정책도 검토해야 한다. 설정을 몰래 켜지 않았다.

청산을 늦추거나 손절선을 넓히면 해결된다는 증거는 없다. 새 이익보호 A/B/C 비교도 이 손실 거래의 초기 구조선/모멘텀 실패 청산을 바꾸는 정책이 아니다.

## 3. 코드 변경

| 파일 | 변경과 범위 |
|---|---|
| `core/market_time.py`, `core/realtime.py`, `core/strategy.py` | 거래소 초 단위 시각과 로컬 수신/단조시계 경과시간을 분리. 기존 수신 경계인 최대 1초 선행만 허용. 실제 오래됨·과도한 미래·날짜 오류는 차단 |
| `automation/engine.py`, `automation/runner.py` | PRE_INTENT/PRE_HTTP 및 ORDER_READY 시각 계산 통일. FAST 임시 이익보호선의 공통 비상선 복사 제거. FAST 실제 수량 호가잔량/신호 TTL/추격 상한 재검사. FAST 최초 신호 시간 및 판단 증거 보존 |
| `automation/exit_plan.py` | 최초 구조선/비상선 및 FAST 독립 판단 표시. 기존 수치 산출 함수 보존 |
| `automation/fast_momentum.py` | 입력 지지/반등/거래량 근거를 JSON 안전하게 보존. 실제 10초 거래대금 표시. **공통 경로 수정 후에도 남아 있던 별도 FAST ingest/metrics/structure/quote의 음수 시각 오판을 추가 수정** |
| `automation/trade_review_capture.py` | 실제 후보 직전/보유 중 틱·호가·실제 완성봉·가격선·주문 이벤트 참조의 제한된 기록. 후보는 메모리 링, 실제 진입 관련 자료만 파일로 승격 |
| `automation/fast_profit_observation.py` | 기존 FAST A / 확정봉 B / 구조 추적 C를 관찰 전용 비교. 실제 주문에 연결하지 않음 |
| `automation/profit_review.py`, `automation/profit_review_cli.py` | 동일 수신 순서의 오프라인 이익보호 요소 비교. 지연 후 실제 충분한 잔량의 매수호가로 모델링. 비용/1틱 불리 체결 구분. 불완전 기록은 UNKNOWN. 완성봉은 처리 완료 시각 이후에만 사용 |
| `automation/entry_frequency_experiments.py` | 기존판, 가속 2.5, 거래대금 20M/15M, 조합, 실제 6개 확정봉 지속 상승 대안을 오프라인 비교. 실험 detector 자체도 엔진 주문 경계 통과 불가 |
| `automation/fast_entry_context.py` | 기존 PriceContext를 재사용하는 장중 고점 상한/확정 추세 실험. FAST 참고 분봉과 출처를 별도 필드에 보존. LIVE 진입 조건·구조선·주문 권한은 바꾸지 않음 |
| 관련 전용 테스트 | 시각, 청산 권한, 제한 기록, 실험 주문 금지, 실제 경로 통합 테스트 |

`classify_regime`, `calculate_invalidation_and_target`, `evaluate_exit` 함수의 전략 본문 AST는 원래 HEAD와 같다. 입력 시세의 잘못된 시각 거부를 고친 것이며 가속·거래대금·손절·익절 임계값을 조절하지 않았다. 정상 수신 자료가 복원되므로 신호/청산 시점은 달라질 수 있다. 이를 수익성 개선으로 판정하지 않는다.

`tests/test_r6_fast_repair.py`의 미래 시각 반례를 1.000초에서 **1.001초** 선행으로 바로잡았다. 공통 수신기가 허용하는 정확히 1초와 모순되던 반례이며 테스트를 삭제하지 않았다. 새 전용 테스트에서 -0.051, -0.293, 실제 stale, 1.001초 미래, 수신 전 자료 사용 금지를 각각 검증했다.

## 4. 테스트 및 운영 반영

- 이전 검증 묶음: 260 PASS.
- 기록 기능/주문 경계 추가 후: 299 PASS, 네트워크 시도 0, 실제 주문 0.
- **최종 코드 변경 뒤 관련 12개 모듈 189 PASS / 실패 0 / 오류 0 / skip 0**, 25.460초. 네트워크를 강제로 차단한 상태에서 실행했다.
- 전체 회귀검사는 요청에 따라 실행하지 않았다. 299를 최종 전체 회귀 결과라고 표시하면 안 된다.
- 수신 기록 10,000프레임 계측: 약 0.109ms/프레임. 기록 큐/디스크 I/O는 수신 루프 밖이다.
- 14:15 기존 시각/FAST 공통 청산 충돌 수정 6개 런타임 파일 배포 및 공식 재시작.
- 14:40 실제 수량 호가 검증·증거 기록 등 5개 파일 배포 및 공식 재시작. 새 청산/진입 실험은 제외.
- 15:01:27 공식 STOP 요청: 반복되는 진입 품질 문제가 미해결이므로 자동 재개하지 않음.
- **15:15:27 추가 FAST 시각 결함만 `automation/fast_momentum.py` 1개 파일에 운영 반영. 재시작하지 않음.** 런타임 무결성 검사 통과.
- 15:15 실제 증권사 재조회: 봇 보유 0, 미체결 0, UNKNOWN 0, 작업 중 주문 0, HALT 없음. 실제 보유는 기존 수동 보호분과 일치했다.
- 계좌 위험 2%, 일일 손실 4%, HARD_LOCK `005930/017900/052260`, 기존 장부/승인 영수증/예산 설정을 보존했다.
- 당일 손실 누계는 **1,743원**, 완료 6거래의 증권사 기준 순합계는 **-197원**. 부분청산 2개를 2거래로 세지 않았다. 이는 백테스트 결과가 아니다.
- 현재 운영 BUILD는 `V1.6-risk2-daily4-fast-live-r6-20261015` 유지. 기존 승인 기록을 새로 만들거나 초기화하지 않았다.

**개발본 전체를 운영본에 복사하지 않는다.** 개발 runner에는 아직 운영 미반영인 관찰용 참고 맥락 연결이 있다. 개발 capture에도 그 참고 맥락 기록 필드가 추가돼 있다. 실험 모듈들은 운영에 복사하지 않았다. 파일별 차이는 private의 `DEVELOPMENT_OPERATING_DIFF.json`을 확인한다.

## 5. 진입 빈도 감사의 확인 범위

448 RAW → 70 WATCH → FAST 10형상 → 7 intent → 4 접수 → 3 체결은 **12:30 시점**의 수치다. 13:25에는 실제 B 거래가 있었다. A/B/C가 하루 종일 0건이었다고 일반화하지 않는다.

상위 원인/제약:

1. 순위 API 합집합이 탐색 모집단이며 한국 전체 종목의 연속 시세가 아니다. 12:30까지 RAW→WATCH 지연 중앙값 142.13초, p90 2,761.17초였다. 1주 불가 제외 및 상태조회 공정 순환은 이미 있다. WATCH가 되지 않았다는 사실만으로 유효 신호 누락을 판정할 수 없다.
2. 5개 사전 고정 1분 창의 45종목에서 A/B의 형상 자체가 끝내 만들어지지 않은 종목은 각각 43개였다. 가속 3배만의 문제가 아니다. B/C는 애초 가속 3배가 필수 조건이 아니다. 유동성·warmup·형상·돌파가 복합 작용한다.
3. FAST 최종 경계에서 서산 -0.051초, 대양금속 -0.293초를 stale로 거절한 것은 확정 결함이다. 반면 호가가 지정가보다 높아진 엑시콘 선행 후보, 기존 미체결 중 중복 신호, 돌파 호가 미확인 및 접수 후 미체결 취소는 구분해야 한다. 추가 FAST 수신 시각 결함이 몇 기회를 더 없앴는지는 아직 정량화하지 못했다.

조건 민감도: 같은 저장 gate 표본에서 기존판/가속 2.5/거래대금 20M·15M/조합 모두 준비 형상 0. 09:25 시점에 상승/보합/하락 및 WATCH/비WATCH로 사전 층화한 12종목, 15분 실제 저장 틱 4,603개에서도 5개 대안 모두 detector episode 0이었다. 3종목은 틱이 없고 SHADOW 기록은 WATCH/보유 구간을 완전히 포함하지 않으므로 **전체 관측 및 손익 검증은 UNKNOWN**이다. 조건을 낮추면 10개 유효 기회를 얻는다는 근거가 아니다.

기존 4거래 순손익: 엑시콘 +1,102 / 카카오게임즈 -281 / 한선엔지니어링 -486 / 엠케이전자 +444원. 최초 신호 당시의 완전한 틱/호가/확정봉이 없어 진입 품질을 정상이라고 단정하지 않았다. MK는 공통 엔진 오청산을 고쳐도 기존 FAST 이익보호로 12:54:51경 청산하는 재현이므로 수익 보유 능력은 미해결이다.

보유시간 6거래: 1분 미만 2 / 1~3분 1 / 3~5분 1 / 5분 이상 2. 5분 강제 보유를 추가하지 않는다.

## 6. 손익/불리 체결/자료 수집의 한계

- 마지막으로 검증한 전체 백테스트 순손익: **UNKNOWN**. 이번 실행에서 전체 백테스트를 수행하지 않았다.
- 한선 14:42 자료의 이익보호 요소 재현은 0.5초 지연 + 기존 비례 비용/슬리피지 모델에서 약 -475.32원, 추가 1틱 불리 체결은 약 -515.12원이다. 실제 증권사 -354원과 별개다. 새 시각 검사로 수신된 틱을 이용하면 모멘텀 실패가 더 일찍 관측되지만 이는 단일 구성요소 재현이며 실제 체결을 보장하지 않는다.
- A/B/C는 공통 초기 무효화/모멘텀 위험을 유지하므로 이 손실 사례의 수익보호 변경 효과는 없다. MK 당시 완전한 자료가 없어 B/C 우열도 미판정이다.
- 제한 기록은 이미 운영 반영돼 14:42 거래의 틱/호가/5개 완성봉을 확보했다. 파일/큐 손실 0인 해당 구간만 확인했다. 모든 과거/모든 감시 종목 자료가 복원된 것은 아니다.
- 메모리 링: 45초, 종목당 256행, 전체 1MiB. 큐 4MiB/4,096행. 파일 4MiB 단위, 하루 16MiB/전체 64MiB. 한도 도달 시 삭제하지 않고 누락/UNKNOWN을 표시한다.
- SHADOW/실험 코드는 실행 가능하지만 LIVE Worker를 재개해 수집하지 않는다. 사용자에게 승인된 별도 관찰 실행 경로가 있는지 먼저 확인한다.

## 7. 결과/원본 위치

공통 private 폴더:
`C:\Users\MJS\Documents\주식자동매매_개발\r6-entry-review-20261016-private`

- `FINAL_TARGETED.json` / `.log`, `run_final_targeted.py`: 최종 표적 검사.
- `FINAL_SOURCE_ALLOWLIST.json`: 코드 커밋과 변경 파일 해시.
- `ENTRY_CAPTURE_DEPLOYMENT.json`, `FAST_CLOCK_DEPLOYMENT.json`, `HANSUN_SAFETY_STOP.json`: 운영 반영/중지 증거.
- `HANSUN_REVIEW.json`, `HANSUN_1442_CAPTURE_PRIVATE.jsonl`, `HANSUN_1442_ENTRY_PRIVATE.jsonl`: 최신 거래 복기.
- `HANSUN_1426_1430_SHADOW_PRIVATE.jsonl`, `HANSUN_1430_1440_SHADOW_PRIVATE.jsonl`: 해당 종목의 제한된 진입 전 원본.
- `FOUR_ENTRY_AUDIT.json`, `FAST_FUNNEL_1230.json`, `FREQUENCY_GATE_SAMPLE.json`, `GATE_SENSITIVITY.json`: 앞선 감사.
- `FREQUENCY_COHORT.json`, `FREQUENCY_COHORT_TAPE_PRIVATE.jsonl`, `FREQUENCY_VARIANT_REPLAY.json`: 미래 승자를 고르지 않은 비교군/재현.
- 원본 운영 기록: `C:\Users\MJS\Desktop\주식자동매매\runtime\automation-production\state.json`, `trade-review\trade-review-20261016-0001.jsonl`.
- 참고 시각/청산 원자료: 형제 `r6-timeguard-20261016-private`, `r6-mk-exit-20261016-private`, `r6-fast-profit-observation-20261016-private`.
- 마지막 소스 백업: 형제 `r6-operating-fast-clock-backup-20261016-151527`.

계좌 조회 원문이 포함된 PRIVATE 파일/운영 runtime을 GitHub에 올리지 않는다. 전체 로그를 다시 전수 검색하지 않는다.

## 8. 바로 이어서 할 순서/명령

1. 같은 개발 경로를 연다. `git status --short`, `git branch --show-current`, `git log -2 --oneline`으로 보존 상태 확인. 오래된 원격 버전으로 덮어쓰지 않는다.
2. 운영 STOP·Worker 미실행과 기존 보호 상태를 읽기 전용으로 확인한다. 새 LIVE 승인을 만들거나 자동 시작하지 않는다.
3. 코드 변경이 필요하면 전용 테스트에 추가한다. 재실행이 필요할 때만 다음 표적 검사를 쓴다(전체 회귀 아님).

```powershell
Set-Location -LiteralPath 'C:\Users\MJS\Documents\주식자동매매_개발\r6-target-cap-incident-20261015'
& 'C:\Users\MJS\Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe' -X utf8 -B 'C:\Users\MJS\Documents\주식자동매매_개발\r6-entry-review-20261016-private\run_final_targeted.py'
```

4. 한선 14:42의 원래 진입과 조건부 장중 저항 상한 대안을 같은 자료로 비교한다. 원래 process에 16,870/16,880원 저항이 유지됐는지 증거가 없으면 UNKNOWN을 유지한다. 후보 재편입/재시작 시 참고 가격 맥락 보존을 설계하되 실시간 warmup 복원과 구분한다.
5. 기존 PriceContext → FAST 연결 및 `fast_context_observation` 기록을 오프라인/별도 SHADOW에서 확인한다. 현 개발본의 observer는 실제 매수 판단을 바꾸지 않는다. 정상 상승/횡보/하락 비교군과 비용·체결 가능성까지 검증하기 전 LIVE 필터로 승격하지 않는다.
6. B의 비용 반영 손익비 guard OFF 문제, FAST의 미세 지지 선택과 5분 추세 불일치를 각각 검증한다. 고정 손절폭 확장이나 5분 강제 보유로 대체하지 않는다.
7. 이익보호 A/B/C는 완전한 보유 구간 자료가 쌓인 동일 거래 집합으로 비교한다. 수신 순서, 실제 봉 처리 완료 시각, 실제 호가 잔량, 비용/1틱/주문 지연을 반영한다. 없는 과거 자료를 생성하지 않는다.
8. 유효 진입 10건 가설은 충분한 사전 모집단과 연속 관측 자료로 검증한다. 12종목 불완전 SHADOW 재현을 한국 시장 전체 기회 수로 확대 해석하지 않는다.
9. 새 전략의 실거래 적용은 검증 및 사용자 운영 승인 후 별도 후보로 한다. 이미 배포한 검증된 버그 수정은 유지한다. 위험예산·당일 장부·HARD_LOCK을 초기화하지 않는다.

## 9. Antigravity 수정 및 검증 완료 기록 (2026-10-16)

- 최신 코드 커밋: **`9693696`**
- 브랜치: `fix/r6-target-cap-incident-20261015`
- 전체 표적 테스트 결과: **193 PASS / 0 FAIL / 0 ERROR / 0 SKIP (34.38초)**

### 1) FAST 상단 장중 저항 천장 캡핑 및 저손익비 거절
- `automation/fast_momentum.py`: `calculate_invalidation_and_target`에서 `watch['nearest_confirmed_peak']`를 전달받아, 진입가 상단에 확정 고점이 존재할 경우 목표가를 해당 고점으로 캡핑. 비용 감안 후 `net_reward_risk < minimum`일 경우 신호 거절 사유 `'KNOWN_CEILING_NET_REWARD_RISK_LOW'` 반환.
- 진입가가 고점 이상인 정상 돌파(`entry >= peak['price']`)는 캡핑하지 않고 정상 상방 모멘텀 진입 유지.
- `automation/runner.py`: FAST 전략 평가 전 `PriceContext.snapshot()`의 `nearest_confirmed_peak`를 `watch_eval`에 주입하고, 관찰 결과가 `REJECT_CANDIDATE`인 경우 `NO_BUY` 처리 및 `NO_TRADE` 기록.
- `automation/engine.py`: `entry_market_reason`에서 `REJECT_CANDIDATE` 신호의 주문 게이트 진입 차단.

### 2) PriceContext 장중 참조 고점/분봉 연속성 보존
- `automation/price_context.py`: `dump_cache()` / `load_cache(data, asof)` 구현. 당일 동일 날짜의 확정 peak 및 참조 봉 데이터를 캐시로 직렬화/복원.
- `bootstrap`: `decision_bar_snapshot`과 `fast_reference_bar_snapshot`을 모두 지원하여 재시작 시점에도 가격 맥락 복원.
- `automation/patterns.py`: `handoff_from`에서 버퍼 이력이 비어있더라도 `PriceContext` 행을 누락 없이 인계.
- `automation/runner.py`: 기동 시 `runtime/price_context_cache.json`에서 당일 캐시를 안전 복원하고, REST 참조봉 갱신 시 비동기 디스크 동기화.

### 3) Setup B 비용 반영 순손익비 가드 강제
- `automation/patterns.py`: `setup_gates`에서 Setup B에 대해 `build_plan`의 `reward_guard_pass`를 검사하고, 통과하지 못한 경우 `rr = b_plan['net_reward_risk']`로 설정하여 게이트 탈락 유도.
- `automation/engine.py`: `entry_market_reason` 및 `enter`에서 `self.a.exit_plan_reward_guard_enabled or signal.get('pattern') == 'B'` 조건을 적용하여, 전역 플래그와 무관하게 Setup B는 반드시 비용 반영 손익비 통과(`reward_guard_pass == True`)를 강제 검증.

### 4) 신규 표적 단위 테스트 (`tests/test_hansun_oct16_fixes.py`)
- 한선 14:42 저항 캡핑 및 거절 검증 통과 (`KNOWN_CEILING_NET_REWARD_RISK_LOW`, net RR < 0).
- 저항 상향 돌파 시 정상 통과 검증.
- Setup B 실질 손익비 0.51~0.67 케이스 차단 검증 통과 (`EXIT_PLAN_REWARD_UNKNOWN_OR_LOW`).
- PriceContext 캐시 복원 검증 통과.

