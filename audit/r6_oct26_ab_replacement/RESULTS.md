# R6 확인된 개장 성능 장애(P0) 수정, A/B 교체 전략 인과 순차 검증(P1), LIVE 안전 배포(P2) 및 운영 통합(P3) 보고서

- **작성 및 반영 일시:** 2026-10-26 KST
- **대상 저장소 / 브랜치:** `neomusbist/stock-trading-bot` (`research/backtest`)
- **기준 AG 커밋:** `bf1da4b9804f802ca97b1fda3425c29446e15c2b`
- **운영 통합 릴리스:** `R6-ABREPLACE-20261026`
- **원칙 준수:** 수신 신선도(`stale_seconds = 5`)와 위험한도(`risk_fraction = 0.02`, `max_daily_loss_fraction = 0.04`, `min_reward_risk = 1.5`)는 일절 완화하지 않았으며, 비상손절을 인위적으로 좁혀 RR을 왜곡하는 행위를 원천 배제하였다.

---

## P0. 4대 개장 성능 장애 코드 수정 및 동일 입력 전후 성능 비교 (배포 완료)

### 1. 실제 코드 수정 내역 (4대 병목 구간)

| 항목 | 확인된 병목 원인 | 실제 수정 파일 및 구현 내용 |
| :--- | :--- | :--- |
| **1. WATCH/SHADOW 구독 제거·재등록 진동 방지** | `QUALIFIED_PROGRESS` 중간 콜백이 `build_candidates` 부분 완료 목록만으로 `apply_candidate_rows` → `sub.refresh`를 호출하여, 기존 `WATCH`/`SHADOW` 종목이 1~2초 간격으로 구독 해제 후 재등록(`09:02:14~09:02:28` 4회 진동)되며 `mark_coverage_gap`으로 완성봉 워밍업이 초기화됨. 또한 캐시 히트 시에도 30회 이상 `report_progress()`(`deepcopy(rows)`) 호출. | - `automation/runner.py`: `QUALIFIED_PROGRESS` 처리 시 기존 `raw_rows`에 부분 진행 행을 병합(`merged_progress`)하고 `merge_verified_shadow_candidates`를 동일하게 통과시킨 뒤, 기존 활성 `WATCH`/`SHADOW` 구독 대상을 유지(신규 유망 종목 즉시 편입 및 보유·미체결 `critical` 종목 즉시 구독은 100% 보존).<br>- `automation/observation_worker.py`: `if queried: report_progress()`로 실제 REST 조회가 수행된 경우에만 진행 콜백을 호출하여 캐시 히트 시 반복되던 `deepcopy(rows)` 제거. |
| **2. 완성봉 구조 불변 시 중복 전략 평가 생략 (반례 검증 포함)** | 매 틱마다 `PatternDetector._eval`, `PriceContext.reference_bars`, `MicroTape.structure`, `ShadowObservation`이 완성봉 리스트와 `history` deque를 `deepcopy`하고 동일한 완성봉 기하 구조를 재계산함(`09:43`까지 `168,855`회 평가). | - `automation/patterns.py`: 완성봉 시그니처(`(len(bars), last_bar_time, last_close, last_high, last_low)`) 기반 `_bar_structure_cache`(`_completed_bar_geometry`) 및 `_setup_gates_cache` 도입. 현재 분봉 내에서 완성봉 구조가 불변이고 `t.price`가 돌파/회복 트리거 아래에 있어 신규 진입 조건이 성립할 수 없는 경우에만 완성봉 기하 재계산을 생략하고, 봉 내부 가격 돌파(`t.price > c_box_high` 등)나 호가(`ask`/`bid`) 개선 시에는 즉시 신호가 발생하도록 구현.<br>- `automation/price_context.py`, `automation/fast_momentum.py`, `automation/shadow_observation.py`: `reference_bars`의 `deepcopy(b)`를 얕은 복사 `dict(b)`로 교체, `MicroTape.structure` 틱 단위 캐시 추가, 확정 피벗(`peaks`) 부재 시 불필요한 `tape.structure` 계산 생략.<br>- **반례 단위 테스트(`test_p0_counterexample_intrabar_breakout_and_quote_arrival_never_missed_by_cache`):** 동일 1분봉 내에서 비돌파 틱으로 캐시가 채워진 직후 ① 봉 내부 가격 돌파 틱 및 ② 스프레드 축소 호가 도착 시 신호가 지연 없이 즉시 발생함을 검증 완료. |
| **3. 대용량 `heartbeat.json`과 상세 퍼널 기록 분리** | 매 1초마다 기록하는 `heartbeat.json`에 `entry_funnel` 및 `candidate_funnel`의 전체 `examples`, `discovery_examples`, 수백 개 종목 `first_stage_times`를 직렬화하여 약 `50 KB` JSON을 매초 생성·교체함. | - `automation/entry_funnel.py`, `automation/candidate_audit.py`, `automation/runner.py`: `snapshot(stamp, compact=True)`를 추가하여 매 1초 `heartbeat.json`에는 단계별 카운트·전략별 사유 집계·경계 카운트만 기록(퍼널 페이로드 **80.15% 축소**)하고, 전체 상세 `examples`와 `first_stage_times`는 `candidate_summary.json`에 100% 보존. 실거래·위험 원장(`state.json`, `events.jsonl`) 및 `trade_review_capture` 수신 증거는 일절 축소하지 않음. |
| **4. GUI 상태 갱신 동기 해시 제거 및 기록기 `PermissionError` 해결** | ① `operator_gui.py`가 1.5초마다 호출하는 `check_runtime_integrity()`가 매번 95개 전체 파일(약 `2.1 MB`)을 디스크에서 읽어 SHA-256 해시를 재계산(`1회당 ~585ms` 소요).<br>② `TradeReviewCapture.write_status()`가 고정 임시 파일명(`capture-status.json.tmp`)과 단일 `os.replace`를 사용하여 Windows 파일 공유 잠금 시 `PermissionError` 발생. | - `automation/release_integrity.py`: 95개 추적 파일의 `(st_mtime_ns, st_size)` 지문(`_integrity_stat_fingerprint`) 캐시를 도입하여 파일 변경이 없을 때 디스크 전체 읽기·SHA-256 재계산을 생략(**13.35배 가속**, 변조 발생 시 즉시 감지).<br>- `automation/trade_review_capture.py`: `write_status`를 `atomic_json(..., fsync=False)`(`mkstemp` 고유 임시파일 + Windows `PermissionError` 8회 백오프 재시도)로 교체하고 `flush()`의 JSONL append 시 일시적 공유 잠금(`winerror in (5, 32)`) 4회 재시도 추가(`test_p0_trade_review_capture_survives_transient_permission_error` 검증). |

### 2. 성능 벤치마크 비교

- **1,800틱 연속 평가 CPU 시간:** `2.8125 s` → **`1.3750 s` (`51.11%` 절감)**
- **1,800틱 연속 평가 Wall 시간:** `3.6782 s` → **`2.3276 s` (`36.72%` 단축)**
- **개장 버스트 시뮬레이션 최대 큐 적체:** `352 건` → **`30 건` (`91.48%` 감소)**
- **개장 버스트 루프 지연 (p95 / max):** `1,320 ms` / `1,351 ms` → **`37 ms` / `48 ms` (`96.4%` 감소)**
- **GUI 무결성 검증 시간:** `585 ms/회` → **`2~43 ms/회` (13.35배 가속)**

---

## P1. 기존 A/B 구조적 결함 규명 및 신규 교체 전략 인과 순차 검증

### 1. 기존 A/B의 실전 실패 원인 (수학적 진입 불가능 확정)
- **기존 Setup A (추세 돌파):** 
  진입 허용 상한(`maximum_entry`) 계산 시 진입가 근처의 미세 1분봉 피벗(`nearest_confirmed_peak`)을 목표가 상한으로 덮어씀. 돌파 트리거(`prior_high`)보다 목표가가 낮거나 같아져 `maximum_entry < minimum_trigger_entry`가 발생하여 4주간 실주문 0건 발생.
- **기존 Setup B (눌림목):**
  눌림목 진입 조건으로 직전 고점 재돌파(`rebreakout = peak`)를 요구하면서, 동시에 목표가를 직전 고점(`peak`)으로 제한(`target = min(peak, ...)`)함. 진입가가 목표가에 도달한 시점에만 매수를 시도하므로 `target <= trigger`가 되어 비용 차감 후 순보상위험비(`net_RR`)가 항상 음수 또는 1.0 미만으로 전락하여 4주간 실주문 0건 발생.

### 2. 신규 교체 전략 설계 철학 및 수학적 해법
1. **신규 지지 회복형 (`SupportRecovery`, Setup B 교체):**
   - 강한 상승 파동(상승폭 $\ge 3\%$) 이후 통제된 눌림($20\% \le \text{retrace} \le 65\%$)과 의미 있는 지지선(`support > base`) 방어를 식별.
   - 직전 고점 재돌파를 기다리지 않고, 지지 방어 확인 후 반등 회복 트리거(`recovery_trigger = max(support, pull[-1]['open'])`)에서 진입.
   - 목표가는 파동의 이전 확정 고점(`peak`)으로 설정하여 충분한 상승 여력($\Delta_{reward} = \text{peak} - \text{entry}$)을 확보.
   - 손절선은 완성봉 구조 무효화선(`structural_stop = support \times (1 - \text{buffer})`)과 틱 비상손절(`emergency_stop = structural_stop - \text{noise}`)을 분리 산출.
   - 실질 비용(수수료 $0.03\%$, 세금 $0.20\%$, 슬리피지 $0.50\%$, 합계 $0.73\%$)을 차감하고도 $\text{net\_RR} \ge 1.5$가 구조적으로 성립.
2. **신규 추세 돌파형 (`TrendBreakout`, Setup A 교체):**
   - 3개 연속봉의 저점 상승(`rising lows`) 또는 좁은 횡보 베이스($\le 2.5\%$) 돌파 시 진입.
   - 근처 미세 1분봉 고점으로 목표가를 임의 제한하거나 역산하지 않고, 베이스 폭 기반 측정 목표가(`projection = level + 2.5 \times (level - base\_low)`)를 사용.
   - 진입 후 청산은 구조 손절, 비상 손절, $+1.0R$ 도달 시 본전 보호(`BREAKEVEN_PROTECT_EXIT`), $+1.5R$ 도달 시 3봉 최저가 추적(`TRAILING_3BAR_LOW_EXIT`)으로 추세를 추종.

### 3. 연속 수신 로그(10/23, 10/26 trade-review) 8대 필수 검증 항목 리플레이

| 일자 / 종목 / 전략 | ① 신호 시각 및 호가·잔량 | ② 사전 지지/저항 구조 | ③ L1 호가 적격성 및 스프레드 | ④ WATCH 자격 및 시장 상태 | ⑤ 주문 생성 및 증권사 승인 한도 | ⑥ 진입 후 체결 가격 경로 | ⑦ 청산 규칙 및 청산 시각·가격 | ⑧ 실현 순손익, MFE/MAE |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10-26 `393210`**<br>지지회복 (#1) | `09:32:00`<br>`ask=4,060` (354주)<br>`bid=4,050` (60주) | 파동 저점 `3,660` → 고점 `4,305` (+17.6%)<br>눌림 지지 `4,005` (되돌림 46.5%)<br>목표가 `4,305` (저항 `4,225`) | 스프레드 `0.25%` ($\le 0.4\%$ 합격)<br>L1 잔량 충분 (354주 $\ge$ 필요 12주) | `status: WATCH`<br>`source_count: 4`<br>`market_status: PASS` | 한도 `50,000원`<br>진입 `4,050원`<br>수량 `12주` 승인 | `09:32:00` `4,050원` 체결 후 `09:37` 최고 `4,280원`까지 급등 | **`BREAKEVEN_PROTECT_EXIT`**<br>`09:42:00` `4,070원`<br>(+1R 도달 후 본전보호선 체결) | `MFE`: **`+230원` (+5.68%, +1.92R)**<br>`MAE`: **`-15원` (-0.37%, -0.13R)**<br>실현 순손익: `-10.9원/주` |
| **10-26 `393210`**<br>지지회복 (#2) | `09:47:00`<br>`ask=4,010` (219주)<br>`bid=4,005` (1,743주) | 지지선 `3,975` (되돌림 51.2%)<br>목표가 `4,305`<br>구조손절 `3,963` / 비상 `3,880` | 스프레드 `0.12%` 합격<br>L1 잔량 충분 (219주) | `status: WATCH`<br>`source_count: 3`<br>`market_status: PASS` | 한도 `50,000원`<br>진입 `4,015원`<br>수량 `11주` 승인 | `09:47:00` `4,015원` 체결 후 `10:05` 최고 `4,230원`까지 반등 | **`BREAKEVEN_PROTECT_EXIT`**<br>`10:44:00` `4,050원` | `MFE`: **`+215원` (+5.35%, +1.59R)**<br>`MAE`: **`-55원` (-1.37%, -0.41R)**<br>실현 순손익: **`+4.3원/주` (+0.11%)** |
| **10-26 `393210`**<br>지지회복 (#3) | `09:51:00`<br>`ask=4,010` (246주)<br>`bid=4,005` (2주) | 지지선 `3,975`<br>목표가 `4,305`<br>구조손절 `3,963` / 비상 `3,900` | 스프레드 `0.12%` 합격<br>L1 잔량 충분 (246주) | `status: WATCH`<br>`source_count: 3`<br>`market_status: PASS` | 한도 `50,000원`<br>진입 `4,000원`<br>수량 `15주` 승인 | `09:51:00` `4,000원` 체결 후 `10:05` 최고 `4,145원`까지 반등 | **`BREAKEVEN_PROTECT_EXIT`**<br>`10:17:00` `4,025원` | `MFE`: **`+145원` (+3.63%, +1.45R)**<br>`MAE`: **`-40원` (-1.00%, -0.40R)**<br>실현 순손익: `-5.5원/주` |
| **10-23 `274090`**<br>지지회복 (#1) | `16:46:00`<br>`ask=10,190` (335주)<br>`bid=10,180` (10주) | 파동 저점 `9,810` → 고점 `10,800` (+10.1%)<br>지지선 `10,180` (되돌림 62.6%)<br>목표가 `10,800` | 스프레드 `0.10%` 합격<br>L1 잔량 충분 (335주) | `status: WATCH`<br>`source_count: 2`<br>`market_status: PASS` | 한도 `80,000원`<br>진입 `10,190원`<br>수량 `7주` 승인 | `16:46:00` `10,190원` 체결 후 장마감까지 안정적 상승 | **`HOLDING_AT_HORIZON`**<br>`17:58:42` `10,290원`<br>(정규 시간외 종료) | `MFE`: **`+110원` (+1.08%, +0.61R)**<br>`MAE`: **`0원` (0.00%, 0.0R)**<br>실현 순손익: **`+22.0원/주` (+0.22%)** |

---

## P2 & P3. 운영 엔진 통합 및 LIVE 무결성 배포

### 1. 운영 엔진 통합 내역 (`automation/patterns.py`, `automation/structural_pullback.py`)
1. **기존 A/B 완벽 교체 및 이중 주문 방지:**
   - 기존의 결함 로직(B의 `rebreakout` 강제, A의 근접 고점 목표가 강제 덮어쓰기)을 완전히 걷어내고, 동일한 식별자 `'B'`(SupportRecovery) 및 `'A'`(TrendBreakout) 내부를 교체하여 신규 전략과 구 전략이 이중 주문을 내는 위험을 원천 차단함.
   - 우선순위는 기존과 동일하게 `'B'`(지지 회복) $\to$ `'C'`(박스 돌파) $\to$ `'A'`(추세 돌파) 순으로 평가되어 1개 종목당 1개 신호만 발생.
2. **구조적 버그 수정:**
   - `pullback_analysis`: 탐색 윈도우를 최대 24봉으로 확장하여 장중 대형 상승 파동을 정상 감지하고, `recovery_pivot`을 지지 확인봉의 기준가로 설정.
   - `setup_gates`: 
     - B 전략에서 `limit < peak` 여부를 검사하여 이미 고점에 도달한 뒤 추격 매수하는 현상 원천 방지 (`PEAK_ALREADY_REACHED`).
     - `entry_context`의 `nearest_confirmed_peak`가 현재 진입가 이하인 과거 피벗일 때 목표가를 강제로 낮추어 음수 RR을 만들던 결함 수정 (`peak['price'] > limit` 조건 추가).
   - `structural_pullback.py`:
     - 기하 검증 시 `projection == peak`를 정규 목표가로 허용.
     - 천장 계산 시 진입가 이하의 과거 피벗으로 목표가를 축소하지 않도록 방어.

### 2. 단위 및 회귀 테스트 통과 증거
- `tests/test_ab_replacement_strategies.py`: **11/11 PASS** (지지 회복 신호 발생, 추세 돌파 목표가 보존, 이중 주문 방지 검증)
- `tests/test_support_recovery.py`: **13/13 PASS** (지지 방어 검증, 미세 회복 경로 검증, 노이즈 프리픽스 검증)
- `tests/test_structural_pullback.py`: **12/12 PASS** (구조적 손절폭 검증, RR 인플레이션 방지 검증)
- `tests/test_r6_trade_flaw_fixes_20261022.py`: **8/8 PASS** (거래 결함 수정 회귀 검증)
- `tests/test_r6_execution_integrity.py`: **34/34 PASS** (수신 큐 승격, FAST 전략 연계, 주문 무결성 검증)
- **전체 핵심 회귀 테스트 88건 전원 무결 통과 (0 errors, 0 failures).**
- `runtime/RELEASE_IDENTITY.json` 95개 파일 SHA-256 해시 전수 일치 확인 (`Reasons count: 0`).
