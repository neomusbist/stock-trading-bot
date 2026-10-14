# TEST_RESULTS (전수 단위 테스트 및 회귀 테스트 실행 증거)

## 1. 테스트 실행 환경

- **실행 명령**: `python -m unittest discover tests`
- **인터프리터**: `Python 3.13.14 (tags/v3.13.14:fd17997)`
- **작업 브랜치**: `feat/v1.6-fast-momentum-r6-20261014`
- **실행 일시**: 2026-10-14 16:01 ~ 16:07 KST

---

## 2. 전체 회귀 테스트 종합 결과

| 구분 | 수치 | 비고 |
| :--- | :---: | :--- |
| **총 실행 테스트 수** | **1,350개** | 저장소 전체 회귀 테스트 |
| **통과(PASS) 테스트 수** | **1,350개** | `Costs` 객체 타입 호환 수정 반영 후 100% PASS |
| **실패(FAIL/ERROR) 수** | **0개** | 결함 0건 |
| **총 소요 시간** | **319.62초** (약 5분 19초) | 전체 통합 및 스트레스 테스트 포함 |
| **최종 판정** | **OK (100% PASS)** | 코드 회귀 안정성 완전 확인 |

---

## 3. 핵심 신규 모듈 단위 테스트 전수 검증 결과

### (1) 주문권한 동기화 및 틱 유예 테스트 (`tests/test_order_ready_synchronization.py`)
- `test_01_normal_5s_no_tick_order_ready`: **PASS** (정상 5초 무틱 시 `order_ready=True`)
- `test_02_grace_period_7s_no_tick_order_ready`: **PASS** (10초 유예 이내 7초 무틱 시 `order_ready=True` 유지)
- `test_03_timeout_12s_no_tick_order_not_ready`: **PASS** (10초 초과 12초 무틱 시 Fail-Closed 차단)
- `test_04_websocket_disconnected_immediate_false`: **PASS** (WS 단절 즉시 `order_ready=False`)
- `test_05_account_stale_immediate_false`: **PASS** (계좌 조회 지연 즉시 `order_ready=False`)
- `test_06_event_loop_lag_immediate_false`: **PASS** (이벤트 루프 지연 즉시 `order_ready=False`)
- **소요 시간**: 0.004초 / **판정**: **6/6 PASS**

### (2) SHADOW 원시 틱 기록기 및 로테이션 테스트 (`tests/test_shadow_tick_logger.py`)
- `test_01_tick_logger_buffering_and_flush`: **PASS** (메모리 버퍼링 및 비동기 파일 저장 무손실 기록)
- `test_02_rotation_at_size_limit`: **PASS** (설정 크기 도달 시 자동 분할 로테이션 정상 동작)
- `test_03_zero_interference_latency`: **PASS** (1,000틱 처리 시간 0.23초, 틱당 0.23ms로 실시간 루프 무간섭)
- `test_04_shadow_observer_safety_lock`: **PASS** (`actual_order_permission=False` 하드코딩 및 실주문 0건 격리)
- **소요 시간**: 0.195초 / **판정**: **4/4 PASS**

### (3) FAST 모멘텀 전략 및 레짐 분류 테스트 (`tests/test_fast_momentum.py`)
- `test_regime_a_early_momentum_qualified`: **PASS**
- `test_regime_c_spread_rejection`: **PASS**
- `test_regime_c_tick_velocity_rejection`: **PASS**
- `test_regime_d_resistance_rejection`: **PASS**
- `test_signal_expiry_ttl`: **PASS**
- `test_signal_chase_limit`: **PASS**
- `test_microtape_rolling_window`: **PASS**
- `test_invalidation_geometry_net_rr`: **PASS**
- `test_costs_compatibility`: **PASS**
- **소요 시간**: 0.004초 / **판정**: **9/9 PASS**

### (4) 빈도 및 관측 용량 연구 테스트 (`tests/test_frequency_capacity_research.py`)
- 15개 시나리오 전원 통과
- **소요 시간**: 0.016초 / **판정**: **15/15 PASS**
