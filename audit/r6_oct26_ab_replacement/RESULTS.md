# R6 A/B 전략 교체 연구 및 2026-10-26 개장 성능 병목 분석 보고서

- **작성 시각:** 2026-10-26 (장중 LIVE 무중단·무변경 상태에서 수행)
- **대상 브랜치:** `research/backtest`
- **신규 비활성 연구 모듈:** `research/ab_replacement_strategies.py`, `tests/test_ab_replacement_strategies.py`
- **운영본 보호 원칙:** 실행 중인 Windows LIVE(`PID 47360`, `OPEN_READY`)는 일절 수정·재시작하지 않았으며, 모든 연구 코드는 `LIVE_ENABLED = False`, `actual_order_permission = False`로 격리됨.

---

## P0. 기존 A/B의 실전 실패 기준선 확정 (Incremental Patching 중단)

| 구분 | 2026-10-23 정규장 (보존 코호트) | 2026-10-26 09:00~09:30 (실시간 표본) | 구조적 실패 원인 (Root Cause) |
| :--- | :---: | :---: | :--- |
| **기존 A (돌파)** | 구조검증 PASS `94` / `max_entry < min_trigger` `94` / RR PASS `0` / 실제 주문 `0` | 독립 구조검증 PASS `22` / 가격구간 존재 `21` / 트리거 교집합 `0` / RR PASS `0` / 실제 주문 `0` | 돌파 전략임에도 가장 가까운 직전 분봉 고점(`confirmed_overhead`)을 최종 목표가 상한(`target`)으로 고정하고 손절폭은 120분 참조/국소 범위로 산정하여, 비용 포함 최대 허용 진입가(`maximum_entry`)가 항상 돌파 트리거(`minimum_trigger_entry`)보다 낮게 형성됨. |
| **기존 B (눌림)** | 구조검증 PASS `26` / `target <= min_trigger` `18` / `max_entry < min_trigger` `26` / RR PASS `0` / 실제 주문 `0` | 독립 구조검증 PASS `2` / 가격구간 존재 `0` / 트리거 교집합 `0` / RR PASS `0` / 실제 주문 `0` | 지지 반등 후 완성봉 고점 재돌파(`minimum_trigger_entry`)를 요구하여 진입 트리거가 직전 파동 고점(`target`)과 겹치거나 이를 초과함(`025980`: `target=4,315`, `min_trigger=4,320`, `net_RR=-0.1045`; `010170`: `target=13,490`, `min_trigger=13,500`, `net_RR=-0.1146`). |
| **기존 `InitialBaseResearch`** | 계획 `96`건 / 중앙값 목표 여력 `0.210%` vs 구조 손절 거리 `1.077%` / RR PASS `0` | - | 3봉 상승 베이스로 구조를 좁혔으나 여전히 가장 가까운 직전 분봉 고점을 고정 목표가(`target`)로 사용하여 `96/96`건 모두 비용 반영 진입 가능 구간이 공집합이 됨. |

**결론:** 기존 A/B의 개별 필터나 임계값을 조금씩 수정하는 작업은 구조적 모순을 해결할 수 없으므로 완전히 중단하고, 매수·청산 철학이 일관된 두 독립 전략으로 교체한다.

---

## P1. 매수·청산 철학 재설계 (`research/ab_replacement_strategies.py`)

Murphy(추세·지지/저항·측정목표치), O'Neil(타이트한 베이스 피벗 돌파 및 추격 제한), Elder(가치 구간 조정 후 회복 및 리스크 한도), Way of the Turtle(돌파 후 추세 보유 및 트레일링 청산)의 핵심 원칙을 한국 주식 장중 데이트레이딩 비용 구조(왕복 수수료·거래세·슬리피지 예비비 약 `0.73%`)에 맞게 재설계하였다.

### 1. 추세 돌파·지속형 (`TrendBreakoutHoldResearch`, 기존 A 대체)
- **진입 조건:**
  - `ACTUAL_RECEIVED`로 검증된 연속 3개 완성 1분봉(`b0, b1, b2`)이 **저점 상승형(`b0.low < b1.low < b2.low`)** 또는 **타이트한 변동성 수축 베이스(`(level - base_low) / base_low <= 2.5%`)**를 형성하고 `b2.close >= b0.open`을 유지.
  - 현재가(`price`)가 베이스 고점 피벗(`level = max(b.high)`)을 돌파하고, 실행 가능한 매도호가(`ask`)가 추격 한도(`level * (1 + max_chase)`) 및 스프레드 한도(`max_spread`) 이내일 것.
- **무효화 및 비상 손절:**
  - `structural_stop = floor_tick(min(b1.low, b2.low) * (1 - structural_buffer))`: 최근 2개 베이스 봉 저점 이탈 시 종가 기준 추세 가설 무효화.
  - `emergency_stop = floor_tick(structural_stop - noise)`: 3봉 진폭(`THREE_BAR_RANGE`) 또는 인과적 직전 60초 체결/호가 점프(`PRETRIGGER_60S_PREFIX`)만큼 분리된 즉시 비상 손절선.
- **목표가 상한 제거 및 일관된 추세 청산 (`evaluate_post_entry_path`):**
  - 가까운 장중 미세 고점(`confirmed_overhead`)을 최종 익절 상한으로 사용하지 **않는다**. 단, 돌파 지점 바로 위에 주요 일봉/세션 저항(`major_resistance`)이 존재하여 최소 `1.5R_net` 생존 목표가(`required_1_5R_target`)를 가로막는 경우(`level < major_resistance < required_1_5R_target`)에만 진입을 거부한다.
  - **4단계 청산 규칙:**
    1. `EMERGENCY_STOP`: 체결가 또는 신선한 매수호가(`bid`)가 `emergency_stop` 이하 도달 시 즉시 청산.
    2. `STRUCTURAL_INVALIDATION`: 완성 1분봉 종가(`close`)가 `structural_stop` 이하 마감 시 청산.
    3. `BREAKEVEN_PROTECT_EXIT`: 진입 후 최대 유리 변위(`MFE`)가 `+1.0R_net` 이상 도달 시 손절 하한을 왕복 거래비용 포함 본전가(`breakeven_price`)로 상향.
    4. `TRAILING_3BAR_LOW_EXIT`: 진입 후 `MFE`가 `+1.5R_net` 이상 도달 시 직전 완성 3분봉 최저가(`floor_tick(min(low[-3:]) * (1 - structural_buffer))`)를 따라 트레일링하며 완성봉 종가가 이를 하향 이탈할 때 추세 청산.

### 2. 지지 회복형 (`SupportRecoveryHoldResearch`, 기존 B 대체)
- **진입 조건:**
  - 이전에 확정된 3봉 피벗 고점(`target = peak.price`) 형성 이후, 시작 저점보다 높은 지지(`support = min(left.low, right.low) > origin_low`)에서 조정을 방어(`right.close > support` 및 양봉/보합 마감).
  - 완성봉 고점 재돌파까지 기다리지 않고, 지지 방어 직후 미시적 눌림(`dip <= pivot`)에서 실제 회복(`price > pivot`)이 발생할 때 매도호가(`ask`) 기준으로 직전 확정 고점(`target`)까지 순보상비율(`net_RR >= 1.5`)이 남아 있는 경우에만 진입.
- **구조 무효화 vs 비상 손절·노이즈 모델 독립 검증:**
  - `structural_stop = floor_tick(support * (1 - structural_buffer))`: 완성 1분봉 종가가 `structural_stop` 이하로 마감할 때만 구조 무효화 청산(장중 일시적 아래꼬리가 `structural_stop`을 하회하더라도 `emergency_stop` 위에서 지지를 회복해 종가 마감하면 조기 손절하지 않음).
  - `emergency_stop = floor_tick(structural_stop - noise)`:
    - 모델 A (`THREE_BAR_RANGE`): 직전 3개 완성봉의 최대 진폭(`max(high - low)`).
    - 모델 B (`PRETRIGGER_60S_PREFIX`): 트리거 직전 60초간 실제 수신된 인접 체결가 점프 및 호가 스프레드 최대값(수신 연속성 미충족 시 `UNKNOWN` 처리).
  - 수량 산정 시 일반 리스크 수량(`ordinary_quantity`)과 직전 점프 1회 추가 슬리피지를 반영한 스트레스 상한 수량(`stress_capped_quantity`)을 함께 산출.

---

## P2. 10월 23일 및 10월 26일 수신 데이터 실증 검증

### 1. 후보별 진입·무효화·수량·청산·MFE/MAE 검증 결과 (`CASES.csv`)

| 일자 | 종목 | 전략 | 노이즈 모델 | 최초 진입 가능 시각 | 진입가(`ask`) | 무효화(`struct` / `emerg`) | 목표가 | `net_RR` | 수량(일반/스트레스) | 청산 규칙 (시각 / 가격) | MFE / MAE | 주문 가능 판정 (`orderability`) |
| :--- | :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :--- |
| 10-23 | `001510` | 지지회복 | `THREE_BAR_RANGE` | `13:23:01.827` | `2,045` | `2,025` / `1,970` | `2,140` | `0.8888` | `15` / `13` | `CONFIRMED_COMPLETE_CLOSE` (`13:37:00` / `2,025`) | `+0` / `-30` (`-1.47%`) | `REJECT` (`net_RR < 1.5`) |
| 10-23 | `001510` | 지지회복 | `PRETRIGGER_60S` | `13:23:01.827` | `2,045` | `2,025` / `2,010` | `2,140` | **`1.5997`** | `28` / `21` | `CONFIRMED_COMPLETE_CLOSE` (`13:37:00` / `2,025`) | `+0` / `-30` (`-1.47%`) | **`UNKNOWN`** (WATCH/계좌 게이트 및 호가 연속성 미증명) |
| 10-23 | `289220` | 지지회복 | `THREE_BAR_RANGE` | `13:38:04.274` | `1,807` | `1,798` / `1,764` | `1,857` | `0.6531` | `25` / `21` | `CONFIRMED_COMPLETE_CLOSE` (`13:40:00` / `1,796`) | `+5` / `-17` (`-0.94%`) | `REJECT` (`net_RR < 1.5`) |
| 10-23 | `289220` | 지지회복 | `PRETRIGGER_60S` | `13:38:04.274` | `1,807` | `1,798` / `1,789` | `1,857` | `1.1753` | `39` / `35` | `CONFIRMED_COMPLETE_CLOSE` (`13:40:00` / `1,796`) | `+5` / `-17` (`-0.94%`) | `REJECT` (`net_RR < 1.5`) |
| 10-26 | `047770` | 추세돌파 | `THREE_BAR_RANGE` | `09:04:31.004` | `2,980` | `2,805` / `2,605` | `3,700` | **`1.7591`** | `3` / `-` | `HOLDING_AT_HORIZON` (`09:29:59` / `2,940`) | **`+150` (`+5.03%`)** / `-150` (`-5.03%`) | **`UNKNOWN`** (`depth_available=false`, 15초 표본) |
| 10-26 | `047770` | 추세돌파 | `THREE_BAR_RANGE` | `09:06:16.147` | `3,095` | `2,920` / `2,780` | `3,665` | **`1.6206`** | `4` / `-` | `HOLDING_AT_HORIZON` (`09:29:59` / `2,940`) | `+35` (`+1.13%`) / `-160` (`-5.17%`) | **`UNKNOWN`** (`depth_available=false`, 15초 표본) |
| 10-26 | `396300` | 추세돌파 | `THREE_BAR_RANGE` | `09:13:27.546` | `9,830` | `9,680` / `9,550` | `10,440` | **`1.5275`** | `3` / `-` | `HOLDING_AT_HORIZON` (`09:29:47` / `9,930`) | **`+140` (`+1.42%`)** / `-150` (`-1.53%`) | **`UNKNOWN`** (`depth_available=false`, 15초 표본) |
| 10-26 | `396300` | 추세돌파 | `THREE_BAR_RANGE` | `09:28:46.005` | `9,900` | `9,760` / `9,640` | `10,480` | **`1.5252`** | `3` / `-` | `HOLDING_AT_HORIZON` (`09:29:47` / `9,930`) | `+50` (`+0.51%`) / `-60` (`-0.61%`) | **`UNKNOWN`** (`ask_qty=55`, 15초 표본으로 연속 노이즈 미검증) |
| 10-26 | `396300` | 추세돌파 | `THREE_BAR_RANGE` | `09:29:47.640` | `9,940` | `9,810` / `9,690` | `10,500` | **`1.5082`** | `3` / `-` | `HOLDING_AT_HORIZON` (`09:30:00` / `9,930`) | `+10` (`+0.10%`) / `-100` (`-1.01%`) | **`UNKNOWN`** (`ask_qty=213`, 15초 표본으로 연속 노이즈 미검증) |

### 2. 개선 여부 및 자료 부족(Evidence Sufficiency) 엄격 판정
1. **구조적·수치적 결함 해소 여부 (개선 확인):**
   - **추세 돌파·지속형 (`TrendBreakoutHoldResearch`):** 기존 A와 `InitialBaseResearch`에서 `100%` 진입 불가(`maximum_entry < minimum_trigger_entry`)를 유발했던 "인접 분봉 고점(`confirmed_overhead`) 목표가 상한 고정"을 제거하고 트레일링 추세 보유 구조로 전환한 결과, 2026-10-26 09:00~09:30 구간에서 비용 차감 후 `net_RR >= 1.5`를 만족하는 돌파 후보 5건(`047770` 2건, `396300` 3건)이 확인되었으며, 초기 진입 후보(`047770` 09:04:31 `2,980`원 → 최고 `3,130`원 `+5.03%`, `396300` 09:13:27 `9,830`원 → 최고 `9,970`원 `+1.42%`)에서 양(+)의 사후 유리 변위(`MFE`)가 관측되었다.
   - **지지 회복형 (`SupportRecoveryHoldResearch`):** 기존 B의 "재돌파 목표가 중첩(`target <= trigger`)" 결함을 제거하고 구조 종가 무효화(`structural_stop`)와 비상 손절(`emergency_stop`)을 분리함으로써, 2026-10-23 `001510`(`13:23:01`, `ask=2,045`, `target=2,140`, `structural_stop=2,025`)에서 장중 `2,015`원(`structural_stop` 아래 10원)까지의 일시적 흔들림에 조기 손절되지 않고 14분간 보유 후 완성봉 종가 이탈(`13:37:00`, `bid=2,025`, 잔량 `2,119`주)로 계획된 손실 한도 내에서 보호 청산됨을 확인했다.
2. **실제 주문 가능 여부 및 플러스 기대값 판정 (`UNKNOWN` / 자료 부족):**
   - 2026-10-26 추출 자료는 15초 주기의 `OPPORTUNITY_SAMPLE`과 `COMPLETED_BAR` 표본이며, 초기 3건은 체결 시점에 호가 가격 불일치(`BOOK_PRICE_MISMATCH`, `depth_available=false`) 상태였고 후반 2건(`396300` 09:28:46, 09:29:47)은 L1 잔량(`ask_qty=55, 213`)이 확인되었으나 15초 표본 사이의 연속 틱 테이프가 없어 모두 **`UNKNOWN`**으로 분류하였다.
   - 2026-10-23 `001510`(`PRETRIGGER_60S`) 1건은 가격·비용·L1 호가 잔량(`ask_qty=601 >= 28`)을 통과했으나, 당시 `SHADOW` 관찰 종목이어서 실시간 `WATCH` 자격 및 계좌 최종 게이트가 미증명(`UNKNOWN`)이며 사후 경로도 본전/소폭 손실 청산이었다.
   - **최종 판정:** 새 교체 전략 코드는 기존 A/B의 수학적 불능 상태를 해결했으나, 실제 거래비용 차감 후 반복 가능한 플러스 기대값(Positive Expectancy)을 증명하기에는 **실제 L1 호가·WATCH 자격이 완비된 연속 체결 표본이 부족(`EVIDENCE_INSUFFICIENT / UNKNOWN`)**하므로 즉시 LIVE에 승격하지 않고 장 종료 후 전체 연속 틱 로그 리플레이로 추가 검증한다.

---

## P3. 2026-10-26 개장 CPU / 메모리 / 이벤트 루프 지연 원인 규명 및 최적화 설계

### 1. 관측된 성능 장애 지표 (2026-10-26 08:58~09:43, i3-6100U 2C/4T, 8 GiB RAM)
- **CPU 포화:** 08:58~09:12 중앙값 `81.2%`, p95 `98.6%`, 최대 `100.0%` (`09:18~09:30` 최대 `99.988%`).
- **메모리 고갈:** `09:21:42` 시스템 RAM 사용률 `95%` (가용 메모리 약 `378 MiB`), SATA SSD 페이지파일 스와핑 유발.
- **이벤트 루프 지연 및 큐 적체:** 루프 지연 중앙값 `19.5ms`, p95 `803.2ms`, 최대 `6.178s` (개장 초반 `3.057s`, `09:02:21` `2.150s`), 수신 큐 최대 `60`건.
- **실시간성 상실 및 워밍업 초기화:** `09:10:20` `WS_SOURCE_SILENT`(`exchange_tick_age=12.51s`, `receive_age=7.56s`) 발생; 09:00~09:28 사이 8개 WATCH 종목(`028050` 완성봉 12개→0개, `138360` 완성봉 14개→0개 등)에서 `observation_start` 및 완성봉 워밍업이 초기화됨.

### 2. 4대 병목 구간별 코드 위치 및 구체적 최적화 방안

| 병목 영역 | 실측 증거 (2026-10-26) | 코드 원인 위치 | 보호·주문을 해치지 않는 구체적 최적화 설계 |
| :--- | :--- | :--- | :--- |
| **1. WATCH/SHADOW 구독 반복 (Subscription Churn)** | `09:02:14`~`09:02:28` (14초) 동안 `SUBSCRIPTION_REFRESH` 4회 발생 (`001440, 039860, 246690, 290720` 제거 후 **1.5초 뒤 재추가**, **2.4초 뒤 재제거**). 09:43까지 `subscription_acks = 298`회, 8개 종목 워밍업 초기화. | `automation/runner.py:2005-2153`, `automation/runner.py:301-310` (`SubscriptionState.refresh`) | **[구독 디바운스 및 히스테리시스]** ① 보유 포지션·미체결 주문 종목(`critical`)의 구독 추가는 즉시 실행하되, 단순 `SHADOW`/`WATCH` 후보 탈락(`removed`)에 의한 웹소켓 구독 해제는 최소 `15~30초`의 디바운스(cooldown) 주기로 배치 처리한다. ② `QUALIFIED_PROGRESS` 중간 보고(`runner.py:2005`)에서는 신규 승격만 추가하고 기존 구독 종목 해제를 보류하여 1~2초 간격의 탈락·재진입 진동과 `mark_coverage_gap`에 의한 완성봉 초기화를 원천 차단한다. |
| **2. 전략 평가 중복 (Redundant Evaluations)** | 09:43까지 `SHADOW_EVALUATED = 103,626`회 + `WATCH_EVALUATED = 65,229`회 (합계 `168,855`회, A/B/C 개별 시험 약 50만 회). 이 중 절반 이상(`SHADOW` A `52,612`, B `69,487`, C `75,252`; `WATCH` A `14,268`, B `24,538`, C `29,088`)이 `INSUFFICIENT_USABLE_BARS` 상태에서 매 틱 반복 실행됨. | `automation/patterns.py:400-545`, `automation/runner.py:1361-1425`, `automation/shadow_observation.py:141-200` | **[완성봉 리비전 게이트 및 2단계 Fast-Reject]** ① `Detector.ingest`로 틱/호가를 정상 적재하되, 완성봉 개수(`len(usable_bars) < 2`)가 최소 요건 미달이거나 현재 분봉(`minute`) 내에서 완성봉 구조(`_entry_geometry_cache`)가 불변이고 현재가(`t.price`)가 돌파/회복 트리거 반경(`breakout * 0.995`) 아래에 있을 때는 무거운 `pullback_analysis`, `symbol_context`, `setup_trials` dict 생성을 건너뛰고 캐시된 `NO_BUY` 요약을 즉시 반환한다. ② `runner.py:1419`에서 `detector.signal()` 직후 `detector.evaluation_gates()`를 중복 호출하는 대신 `detector.last_eval[t.symbol]['gates']`를 재사용한다. |
| **3. 진단·기록 I/O 과부하 (Recording I/O)** | 43분간 `trade_review_capture` `443,624`행 기록(약 `300 MB`, 33.5 MB 파일 9개 회전), `diagnostic_event_writer` `15,777`건 기록, 매 1초마다 1,330줄(`~50 KB`) 크기의 `heartbeat.json` 전체 직렬화 및 `os.replace` 수행(`PermissionError` 8회 발생). | `automation/runner.py:2213-2259`, `automation/trade_review_capture.py:105-113` | **[Heartbeat 경량화 및 캡처 스로틀]** ① 매 1초 기록하는 `heartbeat.json`에는 운영 핵심 상태(`readiness`, `positions`, `pending`, `counts`, `lag`)만 포함하고, 수백 개 종목의 `first_stage_times`와 12개 상세 `examples`를 담은 `entry_funnel` 전체 덤프는 15~30초 주기의 `candidate_summary.json`으로 분리한다. ② `trade_review_capture`에서 미보유 `WATCH` 종목의 `BOOK` 프레임은 최우선 호가(`best_bid`, `best_ask`)가 변경되거나 주문 평가가 트리거될 때만 기록하여 디스크 쓰기량을 70% 이상 절감한다. |
| **4. GUI 및 외부 프로세스 자원 경합** | `09:02:21` 지연(`2.150s`) 당시 `Worker`(`17.69%` CPU) 외에 `ChatGPT.exe` 3개 프로세스가 CPU `22.03%` + 디스크 쓰기 `2.06 MB/s` 점유, `operator_gui.py`(`9.07%` CPU), `explorer.exe`(`8.32%` CPU)가 경합하며 09:21 RAM `95%` 도달. | `operator_gui.py`, 외부 프로세스 환경 | **[프로세스 우선순위 및 GUI 폴링 완화]** ① 장중 LIVE 실행 중에는 무거운 외부 앱/브라우저/오프라인 분석 스크립트 동시 실행을 금지한다. ② `operator_gui.py`의 상태 파일 폴링 주기를 `1s`에서 `2~3s`로 완화하고, 파일 잠금 충돌(`PermissionError`) 방지를 위해 읽기 전용 공유 모드 및 백오프를 유지한다. |
