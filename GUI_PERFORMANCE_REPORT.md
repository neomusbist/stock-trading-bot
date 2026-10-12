# 주식자동매매 GUI 렉 및 리소스 최적화 보고서

> **작성 시각**: 2026-10-13 08:08 (KST)  
> **대상 브랜치**: `perf/gui-latency-optimization`  
> **기준 커밋**: `9464649` (`test/audit-and-storage-cleanup`)  
> **최적화 모듈**: `operator_gui.py`, `tools/benchmark_gui_performance.py`  
> **안전 원칙 준수**: 실거래 주문·전략·위험관리 로직 100% 불변, LIVE 엔진(PID 8260) 무간섭 격리

---

## 1. 개요 및 배경

주식자동매매 GUI(PySide6 기반) 운용 중, 실시간 체결 틱과 관측 상태 업데이트가 빈번하게 수신될 때 다음과 같은 치명적인 UX 및 성능 저하 현상이 발생했습니다.

1. **열 너비 지터(Column Width Jitter)**: 
   - 새 틱/평가 결과가 수신될 때마다 표(Table)의 컬럼 너비가 좌우로 덜덜 떨리며 진동(Jitter)하는 현상 발생.
   - 사용자가 컬럼 경계선을 마우스로 드래그하여 보기 편한 너비로 조절해 두어도, 다음 1.5초 갱신 시 즉시 초기화되어 풀리는 현상 발생.
2. **렌더링 스톰 및 GUI 렉(Lag)**:
   - 1개 종목의 현재가 10원 또는 경과 시간 1초만 변경되어도 `setRowCount(len(rows))`를 호출하여 C++ 힙에서 기존 모든 `QTableWidgetItem`을 파괴하고 새로 할당하는 전체 재생성 반복.
   - 사용자가 테이블의 특정 행을 마우스로 클릭해 선택(Selection)해 두어도 갱신 즉시 선택이 해제됨.
   - 다수의 위젯이 동시에 리페인트 큐를 점유하며 순간적인 프레임 드롭(버벅임) 유발.

---

## 2. 병목 원인 규명

코드 정밀 분석 및 `tools/benchmark_gui_performance.py` 실측을 통해 다음 두 가지 명확한 기술적 원인을 규명했습니다.

1. **`ResizeToContents` 모드의 무차별 레이아웃 재계산**:
   - `operator_gui.py` line 141:
     ```python
     w.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
     ```
   - 셀 내 텍스트(예: `13,800원` → `13,850원`, 또는 경과 시간 `1초 전` → `12초 전`, 상태 메시지 등)의 글자 폭이 1픽셀이라도 달라질 때마다 Qt 레이아웃 엔진이 모든 열 너비를 즉시 재계산하고 테이블 전체를 다시 그렸습니다.
   - **실측치**: 100회 업데이트 중 `board`(실시간 감시 후보)에서 무려 **99회(99%)의 열 너비 변동**이 발생했습니다.

2. **전체 행 파괴 및 C++ 객체 재할당 (`rows_to_table`)**:
   - `operator_gui.py` line 150-154:
     ```python
     widget.setRowCount(len(rows))
     for i, row in enumerate(rows):
         for j, value in enumerate(row):
             item = QTableWidgetItem(...)
             widget.setItem(i, j, item)
     ```
   - 변경되지 않은 수십~수백 개의 셀까지 포함하여 전체 위젯 아이템을 매 갱신 주기마다 C++ 힙에서 삭제하고 재생성했습니다.
   - 이로 인해 메모리 단편화, GC/C++ 바인딩 오버헤드, 행 선택 유실 및 렌더링 스톰이 유발되었습니다.

---

## 3. 최적화 구현 내용

### 3.1. 표준 열 너비 지정 및 `Interactive` 모드 전환 (지터 100% 제거)
- 헤더별 최적 기본 너비를 `DEFAULT_COL_WIDTHS` 딕셔너리로 사전 정의하여 초기 1회 명시적으로 부여.
- `QHeaderView.ResizeMode.Interactive`로 전환:
  - 데이터 갱신 시 Qt가 열 너비를 자동으로 재계산하지 않으므로 **열 너비 지터가 0회로 완전 수렴**.
  - **사용자가 마우스 드래그로 조절한 열 너비가 다음 틱 갱신 후에도 100% 영구 보존**.
- `setStretchLastSection(True)`: 창 크기를 조절할 때 마지막 상세/사유 컬럼이 남은 여백을 유연하게 채움.
- `setTextElideMode(Qt.TextElideMode.ElideRight)`: 열 너비보다 긴 텍스트는 깔끔하게 말줄임(`...`) 처리되며, `setToolTip()`을 통해 마우스 오버 시 전체 내용을 즉시 확인 가능.

### 3.2. 인플레이스 셀 업데이트 (In-Place Cell Update)
- `widget.rowCount() != target_len`일 때만 행 크기를 조정.
- 기존 셀에 `QTableWidgetItem`이 이미 존재하면 객체를 파괴하지 않고 재사용:
  - 텍스트가 실제로 변경된 경우에만 `item.setText(val_str)` 및 `item.setToolTip(val_str)` 호출.
  - 텍스트가 동일하면 C++ 속성 변경 호출을 생략하여 CPU 연산 최소화.

### 3.3. 렌더링 스톰 방지 및 선택/스크롤 보존
- `rows_to_table()` 전체 갱신 구간을 `widget.setUpdatesEnabled(False)` ~ `finally: widget.setUpdatesEnabled(True)`로 래핑하여 중간 렌더링 스톰을 완벽히 차단.
- 갱신 직전 사용자가 선택한 행 번호(`selectedRows()`)와 스크롤 위치를 기록하고, 인플레이스 갱신 직후 오차 없이 복원.

---

## 4. 정량적 성능 벤치마크 결과

실제 운용 환경과 동일한 100회 및 500회 연속 고빈도 틱/상태 업데이트 부하 테스트(`tools/benchmark_gui_performance.py`) 결과입니다.

### 4.1. 전후 정량 비교 요약표

| 평가 항목 | 최적화 전 (Baseline) | 최적화 후 (100회) | 고빈도 스트레스 (500회) | 개선 효과 |
| :--- | :---: | :---: | :---: | :---: |
| **`board` 열 너비 변동 횟수** | **99회 / 100회 (99%)** | **0회 (0%)** | **0회 (0%)** | **Jitter 100% 제거 (완전 안정)** |
| **`decisions` 열 너비 변동 횟수** | **9회 / 100회** | **0회 (0%)** | **0회 (0%)** | **Jitter 100% 제거** |
| **기타 3개 테이블 변동 횟수** | 0회 | 0회 | 0회 | **완전 안정 유지** |
| **사용자 수동 조절 너비 보존** | **실패 (1회 만에 리셋)** | **성공 (100% 유지)** | **성공 (100% 유지)** | **사용자 UX 결함 해결** |
| **행 선택(Selection) 보존율** | 불안정 (파괴 재생성) | **100% 보존 (0회 손실)** | **100% 보존 (0회 손실)** | **사용자 관측 연속성 확보** |
| **업데이트당 평균 지연 시간** | **5.94 ms** | **2.88 ms** | **2.96 ms** | **처리 지연 51.5% 단축** |
| **초당 화면 처리량 (Throughput)** | **168.5 fps** | **347.8 fps** | **337.3 fps** | **2.06배 향상 (부드러운 UI)** |
| **말줄임 (ElideRight) 및 툴팁** | 미적용 | **정상 작동** | **정상 작동** | **정보 가독성 극대화** |

---

## 5. 안전성 및 회귀 검증

- **실거래 엔진 무간섭**: 
  `operator_gui.py`의 프레젠테이션 계층만 수정하였으며, 전략(`strategy`), 리스크게이트(`RiskGate`), 세션 제어, 주문 전송(`order`), 계좌 정합성(`reconcile`) 로직은 일절 수정하지 않았습니다.
- **단위 및 회귀 테스트 100% 통과**:
  - `tests/test_gui.py`: **57개 테스트 전수 통과 (OK, 14.80s)**
  - `tests/test_repository_gui_safety.py` & `test_operator_interface.py`: **39개 테스트 전수 통과 (OK, 18.36s)**
  - 누적 96개 관련 테스트 회귀 0건 입증.
- **레포지토리 보안 검사 (`tools/repo_guard.py`)**:
  - `passed: true, files_checked: 147, findings: [], values_disclosed: false` 완벽 통과.

---

## 6. 결론

본 최적화를 통해 실시간 시세 수신 시 표가 좌우로 출렁이던 현상(Jitter)을 완전히 제거하였으며, 인플레이스 업데이트와 렌더링 스톰 차단으로 GUI 응답 속도를 2배 이상 가속했습니다. 사용자가 조절한 표 너비와 선택 상태가 완벽하게 유지되어 장중 자동매매 모니터링 편의성이 대폭 개선되었습니다.
