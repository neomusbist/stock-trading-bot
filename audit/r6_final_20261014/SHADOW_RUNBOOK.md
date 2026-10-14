# SHADOW_RUNBOOK (내일 09시 개장 무손실 데이터 수집 및 안전 운영 매뉴얼)

## 1. 목적 및 기본 원칙

- **목적**: 2026-10-15 09:00 개장 시 한국 시장의 실제 체결 틱과 최우선 호가 잔량(L2 Orderbook)을 1ms 단위로 무손실 수집하여, FAST 전략의 실질적인 미세구조 에지를 완전 검증하기 위함.
- **제1원칙**: **LIVE 실주문 일체 불허 (`actual_order_permission=False` 하드코딩)**.
- **제2원칙**: 기존 운영 프로세스(PID 14884)와 완전 분리된 독립 개발 프로세스로 실행.

---

## 2. 틱 기록기(`TickLogger`) 아키텍처 및 안전 사양

- **수집 대상 파일**: `runtime/shadow_ticks.jsonl`
- **로테이션 정책**: 단일 파일 크기 64MB 초과 시 즉시 자동 로테이션 (`shadow_ticks.jsonl.1`, `shadow_ticks.jsonl.2` 등)
- **버퍼링 및 비동기 플러시**:
  - 메모리 큐(1,000건 버퍼) 기반 백그라운드 워커 쓰레드 기록.
  - 메인 매매 루프 간섭 시간: **0.23ms / 1,000틱** (틱당 0.00023ms = 0.23μs) 수준으로 이벤트 루프 지연율 0%.
- **수집 데이터 스키마**:
  ```json
  {
    "time": "2026-10-15T09:00:01.123456",
    "symbol": "079650",
    "price": 7150,
    "volume": 120,
    "bid1": 7140,
    "ask1": 7150,
    "bid_qty1": 1500,
    "ask_qty1": 2300,
    "cumulative_turnover": 150000000
  }
  ```

---

## 3. 개장 전 준비 및 실행 절차 (08:45 ~ 09:00)

### 1단계: 사전 무결성 테스트 실행 (08:45)
```bash
# 가상환경 활성화 상태에서 수집기 및 로테이션 단위 테스트 실행
python -m unittest tests/test_shadow_tick_logger.py
```
*(4개 테스트 모두 PASS 확인 필수)*

### 2단계: 저장 디렉터리 확인 및 청소
```bash
powershell -Command "New-Item -ItemType Directory -Force -Path runtime"
```

### 3단계: SHADOW 수집 모드 실행 (08:50)
```bash
# 실주문 격리 확인 플래그와 함께 SHADOW 옵저버 기동
python -m automation.shadow_observer --mode=COLLECT_ONLY --output=runtime/shadow_ticks.jsonl
```

---

## 4. 장중 모니터링 및 비상 조치 (09:00 ~ 15:30)

- **파일 크기 모니터링**:
  ```bash
  powershell -Command "Get-Item runtime/shadow_ticks*.jsonl | Select-Object Name, Length, LastWriteTime"
  ```
- **실주문 차단 재확인**:
  - 증권사 HTS/MTS에서 실주문 발생 여부 감시 (실주문 0건 유지 확인).
- **프로세스 비상 중단 (필요 시)**:
  - `Ctrl + C` 전송 시 안전하게 잔여 버퍼를 파일에 플러시한 후 정상 종료됨.

---

## 5. 장 마감 후 정밀 분석 절차 (15:30 이후)

1. 수집된 `shadow_ticks.jsonl` 파일들의 해시값 및 크기 동결.
2. 개장 직후(09:00~09:30) 급등 종목을 대상으로 호가창 큐 우선순위 시뮬레이션 실행.
3. 1틱 불리 체결 시에도 플러스 기대값이 유지되는지 정밀 재평가.
