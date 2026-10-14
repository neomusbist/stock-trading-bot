# REPRODUCE (클린 환경 재현 가이드)

본 문서는 제3자 감사관 또는 사용자가 독립된 클린 환경에서 동일한 결과를 100% 오차 없이 재현할 수 있는 절차를 기술합니다.

---

## 1. 사전 요구 사항 및 환경

- **운영체제**: Windows 10/11 x64
- **Python 버전**: Python 3.13.14 (tags/v3.13.14:fd17997)
- **가상환경 경로**: `C:\Users\MJS\Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe`
- **저장소 디렉터리**: `C:\Users\MJS\Documents\주식자동매매_개발\v15-frequency-capacity-r5-20261014`
- **작업 브랜치**: `feat/v1.6-fast-momentum-r6-20261014`

---

## 2. 재현 단계별 명령어

### 1단계: 브랜치 및 저장소 무결성 확인
```bash
git checkout feat/v1.6-fast-momentum-r6-20261014
git status --porcelain
```
*(출력 결과: 아무것도 출력되지 않아야 클린 상태임)*

### 2단계: 공식 감사 리플레이 단독 실행
```bash
python audit/r6_final_20261014/audit_replay.py
```

### 3단계: 생성 파일 확인
실행 후 `audit/r6_final_20261014/` 디렉터리에 다음 3개 파일이 생성됩니다:
1. `TRADE_LEDGER.csv`: 8건 거래의 매수/매도 시각, 원본 라인 번호, 체결가, 제비용, 순손익
2. `EQUITY_CURVE.csv`: 17개 시점의 예수금, 포지션 가치, 총 자산 시계열
3. `SLIPPAGE_SENSITIVITY.csv`: 0틱, 1틱, 2틱, 3틱 불리 체결 시의 손익 및 승률 민감도

### 4단계: 신규 단위 테스트 실행 (주문권한 동기화 및 틱 기록기)
```bash
# 1. 6대 주문권한 시나리오 동기화 검증 (6 tests)
python -m unittest tests/test_order_ready_synchronization.py

# 2. SHADOW 원시 틱 기록기 및 로테이션 검증 (4 tests)
python -m unittest tests/test_shadow_tick_logger.py
```

### 5단계: 저장소 전체 회귀 테스트 실행
```bash
python -m unittest discover tests
```
