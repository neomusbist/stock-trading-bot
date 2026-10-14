# REPLAY_PROVENANCE (데이터 출처 및 무결성 감사 보고서)

## 1. 원본 데이터 파일 식별 정보

| 항목 | 상세 정보 |
| :--- | :--- |
| **파일 경로** | `C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce` |
| **파일 크기** | **67,132,914 bytes (64.02 MB)** |
| **SHA-256 해시** | `0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c` |
| **기록 생성 시각** | 2026-10-14 14:21:21.079035 ~ 14:52:49.552486 KST (총 31분 28초) |
| **기록 생성 프로세스** | PID 7232 (운영 BUILD: `V1.5-risk2-watch12-bc20M-observation-r5-20261014`) |
| **총 이벤트 라인 수** | 9,435 라인 (전체 파일 라인) |
| **ENTRY_EVAL 이벤트 수** | **6,072건** |
| **유니크 종목 수** | **178개 종목** |
| **A/B/C 전략 실결정** | NO_BUY 6,072건 (0건 BUY) |

---

## 2. 사용 필드 및 데이터의 본질적 한계

### (1) 사용 필드
- `symbol`: 종목코드 (6자리 문자열 정합성 확인)
- `name`: 원본 이벤트에 기록된 종목명 (한글 명칭)
- `current_price`: 현재가
- `gates.spread.values.bid` / `ask`: 최우선 매수/매도 호가
- `gates.acceleration.values.gain` / `ratio`: 모멘텀 가속도 지표
- `gates.liquidity.values.window_turnover`: 구간 거래대금

### (2) 데이터 결손 및 미검증 영역 (Critical Gap)
- **Bid/Ask 결손율**: 총 6,072건의 `ENTRY_EVAL` 중 **4,540건(74.8%)**에는 `bid`/`ask` 호가 필드가 기록되어 있지 않고 `current_price`만 존재합니다.
- **원시 틱(Raw Ticks) 및 호가창 잔량(Book Depth) 부재**:
  - 원본 데이터는 1ms 단위의 연속 체결 틱이 아니라, 감시 종목을 주기적으로 폴링 평가한 **이벤트 스냅샷**입니다.
  - 따라서 호가창 큐 우선순위(Queue Priority)와 슬리피지 없는 지정가 체결 가능성은 **원천적으로 검증 불가능**합니다.
- **체결 가능성 판정**: `UNKNOWN_NO_RAW_TICKS_OR_DEPTH` (원시 틱 부재로 실체결 입증 불가)
