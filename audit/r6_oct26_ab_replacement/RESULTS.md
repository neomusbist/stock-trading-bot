# R6 확인된 개장 성능 장애(P0) 수정, A/B 교체 전략 인과 순차 검증(P1), LIVE 안전 배포(P2) 및 운영 통합(P3) 보고서

- **작성 및 반영 일시:** 2026-10-26 KST
- **대상 저장소 / 브랜치:** `neomusbist/stock-trading-bot` (`research/backtest`)
- **기준 AG 커밋:** `e3ae0aaf2158508c22572893f852f17294f888e7`
- **설치된 Windows LIVE 기준 릴리스:** `R6-GUIIO-20261026-1b9ad46415aa` (`b56511f10f2dc3b766103072c297933fbd268466`)
- **원칙 준수:** 수신 신선도(`stale_seconds = 5`)와 위험한도(`risk_fraction = 0.02`, `max_daily_loss_fraction = 0.04`, `min_reward_risk = 1.5`)는 일절 완화하지 않았으며, 목표가 역산(`required_1_5R_target`)을 실제 기대보상으로 취급하는 로직을 제거하였다.

---

## P0. 4대 개장 성능 장애 코드 수정 및 동일 입력 전후 성능 비교

### 1. 실제 코드 수정 내역 (4대 병목 구간)

| 항목 | 확인된 병목 원인 | 실제 수정 파일 및 구현 내용 |
| :--- | :--- | :--- |
| **1. WATCH/SHADOW 구독 제거·재등록 진동 방지** | `QUALIFIED_PROGRESS` 중간 콜백이 `build_candidates` 부분 완료 목록(`eligible` 중 조회 완료된 일부 종목)만으로 `merge_verified_shadow_candidates`를 거치지 않고 `apply_candidate_rows` → `sub.refresh`를 호출하여, 기존 `WATCH`/`SHADOW` 종목이 1~2초 간격으로 구독 해제 후 재등록(`09:02:14~09:02:28` 4회 진동)되며 `mark_coverage_gap`으로 완성봉 워밍업이 초기화됨. 또한 `observation_worker.py`가 캐시 히트 시에도 매 행마다 `report_progress()`(`deepcopy(rows)`)를 호출함. | - `automation/runner.py`: `QUALIFIED_PROGRESS` 처리 시 기존 `raw_rows`에 부분 진행 행을 병합(`merged_progress`)하고 `merge_verified_shadow_candidates`를 동일하게 통과시킨 뒤, 기존 활성 `WATCH`/`SHADOW` 구독 대상을 유지(신규 유망 종목 즉시 편입 및 보유·미체결 `critical` 종목 즉시 구독은 100% 보존).<br>- `automation/observation_worker.py`: `if queried: report_progress()`로 실제 REST 조회가 수행된 경우에만 진행 콜백을 호출하여 캐시 히트 시 30회 이상 반복되던 `deepcopy(rows)` 제거. |
| **2. 완성봉 구조 불변 시 중복 전략 평가 생략 (반례 검증 포함)** | 매 틱마다 `PatternDetector._eval`, `PriceContext.reference_bars`, `MicroTape.structure`, `ShadowObservation`이 완성봉 리스트와 `history` deque를 `deepcopy`하고 동일한 완성봉 기하 구조를 재계산함(`09:43`까지 `168,855`회 평가). | - `automation/patterns.py`: 완성봉 시그니처(`(len(bars), last_bar_time, last_close, last_high, last_low)`) 기반 `_bar_structure_cache`(`_completed_bar_geometry`) 및 `_setup_gates_cache` 도입. 현재 분봉 내에서 완성봉 구조가 불변이고 `t.price`가 돌파/회복 트리거 아래에 있어 신규 진입 조건이 성립할 수 없는 경우에만 완성봉 기하 재계산을 생략하고, 봉 내부 가격 돌파(`t.price > c_box_high` 등)나 호가(`ask`/`bid`) 개선 시에는 즉시 신호가 발생하도록 구현.<br>- `automation/price_context.py`, `automation/fast_momentum.py`, `automation/shadow_observation.py`: `reference_bars`의 `deepcopy(b)`를 얕은 복사 `dict(b)`로 교체, `MicroTape.structure` 틱 단위 캐시 추가, 확정 피벗(`peaks`) 부재 시 불필요한 `tape.structure` 계산 생략.<br>- **반례 단위 테스트(`test_p0_counterexample_intrabar_breakout_and_quote_arrival_never_missed_by_cache`):** 동일 1분봉 내에서 비돌파 틱으로 캐시가 채워진 직후 ① 봉 내부 가격 돌파 틱 및 ② 스프레드 축소 호가 도착 시 신호가 지연 없이 즉시 발생함을 검증 완료. |
| **3. 대용량 `heartbeat.json`과 상세 퍼널 기록 분리** | 매 1초마다 기록하는 `heartbeat.json`에 `entry_funnel` 및 `candidate_funnel`의 전체 `examples`, `discovery_examples`, 수백 개 종목 `first_stage_times`를 직렬화하여 약 `50 KB` JSON을 매초 생성·교체함. | - `automation/entry_funnel.py`, `automation/candidate_audit.py`, `automation/runner.py`: `snapshot(stamp, compact=True)`를 추가하여 매 1초 `heartbeat.json`에는 단계별 카운트·전략별 사유 집계·경계 카운트만 기록(퍼널 페이로드 **80.15% 축소**)하고, 전체 상세 `examples`와 `first_stage_times`는 `candidate_summary.json`에 100% 보존. 실거래·위험 원장(`state.json`, `events.jsonl`) 및 `trade_review_capture` 수신 증거는 일절 축소하지 않음. |
| **4. GUI 상태 갱신 동기 해시 제거 및 기록기 `PermissionError` 해결** | ① `operator_gui.py`가 1.5초마다 호출하는 `check_runtime_integrity()`가 매번 95개 전체 파일(약 `2.1 MB`)을 디스크에서 읽어 SHA-256 해시를 재계산(`1회당 ~585ms` 소요).<br>② `TradeReviewCapture.write_status()`가 고정 임시 파일명(`capture-status.json.tmp`)과 단일 `os.replace`를 사용하여 Windows 파일 공유 잠금 시 `PermissionError` 발생. | - `automation/release_integrity.py`: 95개 추적 파일의 `(st_mtime_ns, st_size)` 지문(`_integrity_stat_fingerprint`) 캐시를 도입하여 파일 변경이 없을 때 디스크 전체 읽기·SHA-256 재계산을 생략(**13.35배 가속**, 변조 발생 시 즉시 감지).<br>- `automation/trade_review_capture.py`: `write_status`를 `atomic_json(..., fsync=False)`(`mkstemp` 고유 임시파일 + Windows `PermissionError` 8회 백오프 재시도)로 교체하고 `flush()`의 JSONL append 시 일시적 공유 잠금(`winerror in (5, 32)`) 4회 재시도 추가(`test_p0_trade_review_capture_survives_transient_permission_error` 검증). |

### 2. 동일 입력 전후 성능 벤치마크 비교 (`scratch/run_tests_and_p0_benchmark.py`, `scratch/run_burst_queue_lag_sim.py`)

| 측정 영역 (동일 입력 조건) | 수정 전 (Before) | 수정 후 (After) | 개선율 / 비고 |
| :--- | :---: | :---: | :--- |
| **1,800틱 연속 평가 CPU 시간 (`PatternDetector` + `PriceContext` + `MicroTape`)** | `2.8125 s` | **`1.3750 s`** | **CPU 시간 `51.11%` 절감** (신호 결과 동일) |
| **1,800틱 연속 평가 Wall 시간** | `3.6782 s` (`2.04 ms/tick`) | **`2.3276 s` (`1.29 ms/tick`)** | **처리 시간 `36.72%` 단축** |
| **300틱 구간 피크 힙 메모리 할당 (`tracemalloc` Peak)** | `88.58 KB` | **`56.15 KB`** | **피크 동적 할당 `36.61%` 감소** |
| **개장 버스트(500틱, 625 ticks/s) 시뮬레이션 최대 큐 적체 (`max_queue_depth`)** | `352 건` | **`30 건`** | **큐 적체 `91.48%` 감소** |
| **개장 버스트 시뮬레이션 루프 지연 (`median` / `p95` / `max` loop lag)** | `811.58 ms` / `1,320.60 ms` / `1,351.55 ms` | **`3.72 ms` / `37.04 ms` / `48.64 ms`** | **중앙값 `99.54%`, p95 `97.20%`, 최대 지연 `96.40%` 감소** |
| **`heartbeat.json` 퍼널 스냅샷 100회 직렬화 시간 및 크기** | `85.64 ms` / `19,553 B` | **`29.64 ms` / `3,881 B`** | **직렬화 시간 `65.39%` 단축, 페이로드 `80.15%` 축소** |
| **GUI 1.5초 주기 `check_runtime_integrity` 20회 연속 호출 시간** | `11,705.66 ms` (`585.3 ms/회`) | **`876.78 ms` (`43.8 ms/회`, 캐시 히트 시 `~2 ms`)** | **13.35배 가속 (`92.51%` I/O 시간 절감)** |

---

## P1. 교체 전략의 경제성·시장근거 검증 (목표가 역산 제거 및 전체 수신 로그 인과 순차 리플레이)

### 1. 목표가 역산(`required_1_5R_target`) 제거 및 추세 보유 기대값 검증 (`research/ab_replacement_strategies.py`)
- 기존 `TrendBreakoutHoldResearch.structure()`에서 `viable_target = max(measured_target, required_1_5R_target)`로 목표가를 역산하여 `net_RR >= 1.5`를 통과시키던 로직을 **완전히 제거**하고, 오직 시장 구조(베이스 높이 `level - base_low`의 2.5배 측정목표치 `measured_target = floor_tick(level + 2.5 * (level - base_low))`)만을 `viable_target`으로 사용하도록 수정하였다(`test_trend_breakout_hold_never_inflates_target_from_required_1_5R` 검증).
- 그 결과, 2026-10-26 오전 `047770`(2건) 및 `396300`(3건) 후보 5건의 시장 구조 기반 `measured_target`은 각각 `3,385원`(`net_RR=0.9654`), `3,500원`(`net_RR=1.1320`), `10,110원`(`net_RR=0.5903`), `10,190원`(`net_RR=0.6534`), `10,170원`(`net_RR=0.4864`)으로 산출되어, **`THREE_BAR_RANGE` 노이즈 기준 5건 모두 `net_RR < 1.5`로 `REJECT`**됨을 확인했다(이전 보고서의 `3,700`, `3,665`, `10,440`, `10,480`, `10,500` 목표가는 역산값이었음을 정정).
- 나아가 고정 목표가 없이 순수 추세 청산(`EMERGENCY_STOP` / `STRUCTURAL_INVALIDATION` / `BREAKEVEN_PROTECT_EXIT` / `TRAILING_3BAR_LOW_EXIT`)만으로 보유했을 때 실제 플러스 기대값이 나오는지 확인하기 위해, 2026-10-26 전체 수신 스트림(`trade-review-20261026-0001..0021.jsonl`: `047770` `25,788`행, `396300` `67,424`행, `09:00~11:15`)과 2026-10-23 전체 수신 스트림(`trade-review-20261023-0001..0015.jsonl`: `001510` `31,690`행)을 실제 수신 시각(`received_at` / `known_at`) 순서대로 전수 리플레이하였다.

### 2. 종목·시점별 6대 필수 검증 항목 상세 표 (`CASES.csv` 동기화)

| # | 일자 / 종목 / 전략 | ① 진입 시각(`received_at`) 및 호가·잔량 (`ask` / `bid`, `ask_qty` / `bid_qty`) | ② 구조 무효화 / 비상 손절 / 주당 실제 위험액(`risk/sh`) | ③ 일반 수량 vs 점프 반영 스트레스 수량 (최대 역방향 점프) | ④ 진입 후 시간순 청산 사유·시각·가격 (`exit_bid`, 잔량) | ⑤ 비용·슬리피지 차감 후 주당 순손익 (`MFE` / `MAE`) | ⑥ 최종 판정 (`ACTUAL_ORDERABLE` / `REJECT` / `UNKNOWN`) 및 사유 |
| :-: | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1** | **10-26 `047770`**<br>추세돌파 (`#1`) | `09:04:31.004`<br>`ask=2,980` / `bid=2,975`<br>`ask_qty=879` / `bid_qty=762`<br>(`BOOK_PRICE_MISMATCH=True`) | `struct = 2,805`<br>`emerg(3bar) = 2,605` (`risk=394.82원`)<br>`emerg(60s) = 2,745` (`risk=255.14원`)<br>`measured_target = 3,385` (`RR=0.9654 / 1.4981`) | 일반: `3주` (60s: `5주`)<br>스트레스: `3주` (60s: `5주`)<br>(직전 60s 최대 역점프 `25원`) | **`HOLDING_AT_STREAM_END`**<br>`11:15:40.088` 기준 미청산 보유 중 (`2,805` 종가 이탈 없음, `MFE +150원`으로 본전보호 트리거 `+395원` 미달)<br>종단 `bid=2,940` (`bid_qty=1,360`) | 평가손익(MTM): **`-46.79원/주`** (3주 `-140.4원`)<br>`MFE`: **`+150원`** (`+5.03%`, `+0.38R` @ `09:07:01` `3,130원`)<br>`MAE`: **`-150원`** (`-5.03%`, `-0.38R` @ `09:04:35` `2,830원`) | **`REJECT`**<br>① `WATCH` 일봉 저항(`2,970원`) < 진입 호가(`2,980원`)<br>② 시장 구조 목표가(`3,385원`) 기준 `net_RR = 0.9654 < 1.5`<br>③ 체결 프레임 `BOOK_PRICE_MISMATCH` |
| **2** | **10-26 `047770`**<br>추세돌파 (`#2`) | `09:06:16.147`<br>`ask=3,095` / `bid=3,090`<br>`ask_qty=261` / `bid_qty=2,102`<br>(`BOOK_PRICE_MISMATCH=False`) | `struct = 2,920`<br>`emerg(3bar) = 2,780` (`risk=335.97원`)<br>`emerg(60s) = 2,870` (`risk=246.17원`)<br>`measured_target = 3,500` (`RR=1.1320 / 1.5313`) | 일반: `4주` (60s: `5주`)<br>스트레스: `3주` (60s: `5주`)<br>(직전 60s 최대 역점프 `25원`) | **`STRUCTURAL_INVALIDATION`**<br>`10:42:28.472` (`10:41` 완성봉 종가 `2,920 <= 2,920` 확인 시점)<br>청산 당시 `bid=2,920` (`bid_qty=1,701`) | 실현 순손익: **`-181.74원/주`** (4주 `-727.0원`, 3주 `-545.2원`)<br>`MFE`: **`+35원`** (`+1.13%`, `+0.10R` @ `09:07:01` `3,130원`)<br>`MAE`: **`-175원`** (`-5.65%`, `-0.52R` @ `10:09:12` `2,920원`) | **`REJECT`**<br>① `WATCH` 일봉 저항(`2,970원`) < 진입 호가(`3,095원`)<br>② 3봉 노이즈 기준 `net_RR = 1.1320 < 1.5`<br>③ 60초 프리픽스 호가 연속성 미증명 |
| **3** | **10-26 `396300`**<br>추세돌파 (`#1`) | `09:13:27.546`<br>`ask=9,830` / `bid=9,820`<br>`ask_qty=209` / `bid_qty=227`<br>(`BOOK_PRICE_MISMATCH=True`) | `struct = 9,680`<br>`emerg(3bar) = 9,550` (`risk=350.32원`)<br>`emerg(60s) = 9,610` (`risk=290.46원`)<br>`measured_target = 10,110` (`RR=0.5903 / 0.7082`) | 일반: `3주` (60s: `3주`)<br>스트레스: `3주` (60s: `3주`)<br>(직전 60s 최대 역점프 `60원`) | **`STRUCTURAL_INVALIDATION`**<br>`10:42:03.129` (`10:41` 완성봉 종가 `9,630 <= 9,680` 확인 시점)<br>청산 당시 `bid=9,630` (`bid_qty=51`) | 실현 순손익: **`-222.18원/주`** (3주 `-666.5원`)<br>`MFE`: **`+250원`** (`+2.54%`, `+0.71R` @ `09:36:06` `10,080원`)<br>`MAE`: **`-230원`** (`-2.34%`, `-0.66R` @ `10:42:03` `9,600원`) | **`REJECT`**<br>① 시장 구조 목표가(`10,110원`) 기준 `net_RR = 0.5903 < 1.5`<br>② 체결 프레임 `BOOK_PRICE_MISMATCH`<br>③ 최고 `+250원`(`10,080원`) 상승 후 본전보호 트리거(`10,190원`, `+1.0R`) 도달 전 반락하여 손절 마감 |
| **4** | **10-26 `396300`**<br>추세돌파 (`#2`) | `09:28:46.005`<br>`ask=9,900` / `bid=9,880`<br>`ask_qty=55` / `bid_qty=705`<br>(`BOOK_PRICE_MISMATCH=False`) | `struct = 9,760`<br>`emerg(3bar) = 9,640` (`risk=330.93원`)<br>`emerg(60s) = 9,700` (`risk=271.07원`)<br>`measured_target = 10,190` (`RR=0.6534 / 0.7972`) | 일반: `3주` (60s: `3주`)<br>스트레스: `3주` (60s: `3주`)<br>(직전 60s 최대 역점프 `30원`) | **`STRUCTURAL_INVALIDATION`**<br>`10:04:04.144` (`10:03` 완성봉 종가 `9,750 <= 9,760` 확인 시점)<br>청산 당시 `bid=9,750` (`bid_qty=9`) | 실현 순손익: **`-172.45원/주`** (3주 `-517.3원`)<br>`MFE`: **`+180원`** (`+1.82%`, `+0.54R` @ `09:36:06` `10,080원`)<br>`MAE`: **`-160원`** (`-1.62%`, `-0.48R` @ `10:03:59` `9,740원`) | **`REJECT`**<br>시장 구조 목표가(`10,190원`) 기준 `net_RR = 0.6534 < 1.5` (고점 추격 진입 후 `10,080원`에서 반락하여 구조 손절) |
| **5** | **10-26 `396300`**<br>추세돌파 (`#3`) | `09:29:47.640`<br>`ask=9,940` / `bid=9,930`<br>`ask_qty=213` / `bid_qty=121`<br>(`BOOK_PRICE_MISMATCH=False`) | `struct = 9,810`<br>`emerg(3bar) = 9,690` (`risk=321.27원`)<br>`emerg(60s) = 9,760` (`risk=251.43원`)<br>`measured_target = 10,170` (`RR=0.4864 / 0.6203`) | 일반: `3주` (60s: `3주`)<br>스트레스: `3주` (60s: `3주`)<br>(직전 60s 최대 역점프 `30원`) | **`STRUCTURAL_INVALIDATION`**<br>`09:48:00.710` (`09:47` 완성봉 종가 `9,810 <= 9,810` 확인 시점)<br>청산 당시 `bid=9,810` (`bid_qty=2,038`) | 실현 순손익: **`-152.58원/주`** (3주 `-457.7원`)<br>`MFE`: **`+140원`** (`+1.41%`, `+0.44R` @ `09:36:06` `10,080원`)<br>`MAE`: **`-160원`** (`-1.61%`, `-0.50R` @ `09:47:03` `9,780원`) | **`REJECT`**<br>시장 구조 목표가(`10,170원`) 기준 `net_RR = 0.4864 < 1.5` |
| **6** | **10-23 `001510`**<br>지지회복 (`#1`) | `13:23:01.827`<br>`ask=2,045` / `bid=2,040`<br>`ask_qty=601` / `bid_qty=200`<br>(`BOOK_PRICE_MISMATCH=False`) | `struct = 2,025`<br>`emerg(3bar) = 1,970` (`risk=89.54원`, `RR=0.8888`)<br>`emerg(60s) = 2,010` (`risk=49.75원`, **`RR=1.5997`**)<br>`target = 2,140` (확정 피벗 고점) | 3bar: `15주` / 스트레스 `13주`<br>60s: `28주` / 스트레스 `21주`<br>(직전 60s 최대 역점프 `15원`) | **`STRUCTURAL_INVALIDATION`**<br>`13:37:00.717` (`13:36` 완성봉 종가 `2,025 <= 2,025`; `WATCH` 완성봉 기준 `13:39:44.090` `13:38` 봉 종가 `2,020`)<br>청산 당시 `bid=2,025` (`bid_qty=2,119`) | 실현 순손익: **`-24.66원/주`** (28주 `-690.5원`, 21주 `-517.9원`; `2,020원` 청산 시 `-29.65원/주`)<br>`MFE`: **`+30원`** (`+1.47%`, `+0.60R` @ `13:26:20` `2,075원`)<br>`MAE`: **`-30원`** (`-1.47%`, `-0.60R` @ `13:29:18` `2,015원`) | 3bar: **`REJECT`** (`net_RR=0.8888 < 1.5`)<br>60s: **`UNKNOWN`** (`13:23:01` 진입 시점 `SHADOW` 역할로 `WATCH` 자격 미보유, 호가 스트림 연속성 미증명) |

### 3. P1 경제성 검증 핵심 결론
1. **역산 목표가 제거 효과:** `required_1_5R_target` 역산을 제거하자 10월 26일 오전 돌파 후보 5건은 모두 3분봉 베이스 대비 손절폭(`321~395원/주`, 약 `3.2%~13.2%`)이 넓어 시장 구조 목표가(`measured_target`) 기준 `net_RR`이 `0.4864 ~ 1.1320`에 불과함이 드러났다.
2. **순수 추세 청산 리플레이 결과:** 만약 `net_RR` 게이트 없이 위 5건을 진입시켜 추세 청산 규칙만으로 보유했다면, `047770` `#1`(`2,980원` → 최고 `3,130원`, `+0.38R`)과 `396300` `#1`(`9,830원` → 최고 `10,080원`, `+0.71R`) 모두 본전보호 활성화 기준(`+1.0R`)에 도달하지 못한 채 10시 이후 장중 하락 반전하여 **4건은 `STRUCTURAL_INVALIDATION` 실손실(`-152.58 ~ -222.18원/주`), 1건은 평가손실(`-46.79원/주`)**로 마감되었다.
3. **지지 회복형(`001510`) 리플레이 결과:** 진입(`2,045원`) 후 `13:26:20`에 `2,075원`(`+30원`, `+0.60R`)까지 반등하고 `13:29:18` 장중 일시적 저점 `2,015원`에서 조기 손절을 피했으나, 목표가(`2,140원`)까지 도달하지 못하고 `13:37:00` 완성봉 종가 이탈(`2,025원`)로 **`-24.66원/주`(`-0.50R`) 소폭 손실 청산**되었다.
4. **결론:** 교체 전략 또한 현재 표본에서는 **비용 차감 후 양(+)의 실현 기대값이 입증되지 않았으므로(`ACTUAL_ORDERABLE = 0건`)**, 실주문 권한을 절대 부여하지 않고 P3의 비주문(`actual_order_permission = False`) 완성봉 관측 경로로만 축적한다.

---

## P3. 신규 전략의 운영 통합 방식 판정 및 기존 A/B 퇴출 계획

1. **운영 통합 방식 (`observe_replacement_ab_on_completed_bar`):**
   - `research/ab_replacement_strategies.py`에 구현된 `observe_replacement_ab_on_completed_bar()`는 **틱마다 실행되지 않고 완성 1분봉이 확정될 때만(종목당 분당 최대 1회)** 호출되도록 설계되어 CPU 부하가 `0.05ms` 미만이다.
   - 모듈 상수 `LIVE_ENABLED = False`, `ACTUAL_ORDER_PERMISSION = False` 및 반환 레코드의 `'actual_order_permission': False`가 하드코딩되어 있어 어떠한 경우에도 주문 엔진(`engine.enter`)이나 계좌 상태에 영향을 주지 않는다(`test_p3_completed_bar_non_ordering_hook` 검증 완료).
2. **기존 A/B 퇴출 및 교체 전략 승격 조건:**
   - **기존 A/B:** 구조적 모순(`maximum_entry < minimum_trigger_entry`, `target <= trigger`)이 확정되었으므로 필터 미세 조정을 영구 중단한다.
   - **교체 전략(`TrendBreakoutHoldResearch`, `SupportRecoveryHoldResearch`) 실주문 승격 전 필수 관문:**
     1. 최소 5거래일 이상의 장중 실시간/리플레이 표본에서 `ACTUAL_ORDERABLE` 조건(`WATCH` 적격 + 호가 일치 + `ask_qty >= stress_capped_quantity` + 시장 구조 목표가 기준 `net_RR >= 1.5`)을 충족하는 표본 확보.
     2. 역산 목표가 없이 순수 추세·구조 청산만으로 거래비용·슬리피지 차감 후 누적 순기대값(`Net Expectancy > 0`) 확인.
     3. 사용자의 명시적인 전략 교체 및 실주문 재승인.
