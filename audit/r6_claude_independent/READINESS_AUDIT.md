# READINESS_AUDIT (주문권한 동기화 및 엔진-GUI 안정성 검증)

## 1. 주문권한 10대 시나리오 검증 결과

본 매트릭스는 엔진의 실제 주문 가능 여부(`order_ready`)와 GUI의 표시 상태가 일치하는지, 장애 상황 시 지체 없이 차단(Fail-Closed)되는지 검증한 결과입니다 ([`tests/test_order_ready_synchronization.py`](file:///C:/Users/MJS/Documents/주식자동매매_개발/v15-frequency-capacity-r5-20261014/tests/test_order_ready_synchronization.py)).

| 번호 | 시나리오 조건 | 기대 판정 | 엔진 판정 (`order_ready`) | GUI 표시 상태 | 검증 결과 |
| :---: | :--- | :---: | :---: | :---: | :---: |
| 1 | **정상 시장 틱 수신 (4초 무틱, WS 정상)** | `True` | `True` | 초록색 (정상 준비) | **PASS** |
| 2 | **일시적 시장 소강 (7초 무틱, WS 정상)** | `True` | `True` (10초 유예 적용) | 초록색 (정상 유지) | **PASS** |
| 3 | **12초 무틱 초과 (WS 정상)** | `False` | `False` (시장 동결 차단) | 회색 (주문 비활성) | **PASS** |
| 4 | **20초 무틱 초과 (WS 정상)** | `False` | `False` (완전 타임아웃) | 회색 (주문 비활성) | **PASS** |
| 5 | **WebSocket 물리 단절** | `False` | `False` (즉각 차단) | 빨간색 경고 | **PASS** |
| 6 | **계좌 잔고 조회 지연 (15초 초과)** | `False` | `False` (계좌 미확인 차단) | 주황색 경고 | **PASS** |
| 7 | **이벤트 루프 지연 (1.5초 지연)** | `False` | `False` (유예 불허, 즉각 차단) | 주황색 경고 | **PASS** |
| 8 | **피드 큐 적체 (대기 8건)** | `False` | `False` (유예 불허, 즉각 차단) | 주황색 경고 | **PASS** |
| 9 | **개별 종목 틱 지연 (시장 정상, 종목 7초)** | `False` | 종목 진입 기각 (`runner.py:584`) | 주문 미발생 | **PASS** |
| 10 | **엔진-GUI 상태 시차 동기화** | 일치 | `operator_service` 동기화 | 불일치 0건 | **PASS** |

---

## 2. 세부 메커니즘 분석 및 검증

### (1) 시장 정적 유예와 개별 종목 신선도 이중 방어선
- **시장 레벨 (Market Level)**:
  - 10초 이내의 일시적 무틱은 웹소켓 연결이 유지되고 이벤트 루프 지연(`event_loop_lag <= 1.0s`) 및 큐 적체(`feed_queue_size <= 5`)가 없는 경우에 한해 `WS_SOURCE_SILENT`로 분류되어 주문권한 깜빡임(Blinking)을 방지합니다 (`runner.py:660-666`).
- **종목 레벨 (Symbol Level)**:
  - 시장 전체가 `order_ready=True`라 하더라도, 주문 직전 해당 종목의 마지막 틱이 5.0초(`stale_seconds`)를 초과한 경우 `runner.py:584`에서 `TICK_STALE`로 주문 생성이 원천 차단됩니다.
- **Fail-Closed 안전 원칙**:
  - 이벤트 루프 지연이나 큐 버퍼 적체는 시스템 부하 증거이므로, 무틱 유예를 절대 적용하지 않고 즉시 `order_ready=False`로 차단합니다.

### (2) 엔진과 GUI의 동기화 상태 일치성
- `runner.py`의 `readiness_snapshot`에서 산출된 `order_ready` 필드는 하트비트(`heartbeat.json`)를 통해 `operator_service.py:263` 및 `operator_ui_service.py:509`로 500ms 주기로 완벽히 전달됩니다.
- 데이터가 오래되었는데 GUI만 ON으로 표시되거나, 엔진은 주문 가능한데 GUI가 OFF로 표시되는 상태 불일치 현상은 완벽히 차단되었습니다.
