# SHADOW_DEPLOYMENT (내일 09시 개장 무손실 데이터 수집 및 안전 운영 매뉴얼)

## 1. 운영 기본 원칙 및 안전 보장

- **목적**: 2026-10-15 09:00 개장 시 한국 시장의 실제 체결 틱과 최우선 호가 잔량을 1ms 단위로 무손실 수집하여, 1호가 불리 체결을 극복할 수 있는 진입 구조 설계 기반 마련.
- **제1원칙**: **실주문 원천 차단 (`actual_order_permission=False` 하드코딩)**.
- **제2원칙**: 기존 운영 프로세스(PID 14884)와 완전 분리된 독립 프로세스로 구동.

---

## 2. 틱 기록기(`TickLogger`) 강화 사양 및 검증 결과

이번 독립 감사에서 발견된 틱 로거 결함을 수정하여 다음 사양이 완벽히 구현되었습니다:

1. **거래소 체결시각과 로컬 수신시각 분리**:
   - `time` (거래소 체결시각)과 `received` (로컬 수신시각)을 각각 ISO-8601 형식으로 독립 저장하여 네트워크 지연을 실측 가능.
2. **호가 잔량 및 시장 메타데이터 전수 기록**:
   - `symbol`, `time`, `received`, `price`, `volume`, `cumulative` (누적거래량), `bid`, `ask`, `bid_qty` (매수잔량), `ask_qty` (매도잔량), `depth_available`, `venue`, `regular`, `vi_active` 기록.
3. **로테이션 파일 보존 (무삭제 원칙)**:
   - 64MB 초과 시 기존 `.1` 파일을 덮어쓰거나 삭제하지 않고, `.1`, `.2`, `.3` 순서로 안전하게 증분 로테이션.
4. **기록 실패 시 버퍼 보존 및 누락 통계 추적**:
   - 디스크 쓰기 오류 발생 시 버퍼를 즉시 비우지 않고 메모리 큐(최대 20만 건)에 보존 후 1초 뒤 재시도.
   - 누락 및 오류 통계(`rows_logged`, `rows_written`, `rows_dropped`, `write_errors`, `rotations`)를 `stats()`로 집계하여 `shadow_snapshot.json`의 `performance.tick_logger`에 실시간 노출.
5. **프로세스 종료 시 잔여 버퍼 강제 플러시**:
   - `close()` 시 워커 스레드 종료를 대기(`join`)한 후 잔여 버퍼를 `flush(force=True)`로 파일에 완전 기록.

---

## 3. 개장 전 사용자 실행 절차 (08:50 ~ 09:00)

### 1단계: 사전 검증 테스트 실행 (08:50)
```bash
python -m unittest tests.test_r6_independent_audit
```
*(16개 테스트 전원 PASS 확인)*

### 2단계: 런타임 디렉터리 확인
```bash
powershell -Command "New-Item -ItemType Directory -Force -Path runtime"
```

### 3단계: SHADOW 수집 모드 시작 (08:52)
```bash
# 독립 가상환경에서 SHADOW 수집 프로세스 가동
python -m automation.runner --mode=OBSERVE_ONLY
```

---

## 4. 장중 모니터링 및 종료 절차

- **수집 파일 크기 및 누락율 확인**:
  ```bash
  powershell -Command "Get-Item runtime/shadow_ticks*.jsonl | Select-Object Name, Length, LastWriteTime"
  ```
- **정상 종료 (15:35 이후)**:
  - 프로세스에 `Ctrl + C`를 1회 전송하면 워커 스레드가 안전하게 종료되고 잔여 틱 버퍼가 `shadow_ticks.jsonl`에 플러시됩니다.
