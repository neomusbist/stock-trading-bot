# 주식매매 프로토콜 V1.1

한국 주식 정규장 모멘텀 단타의 후보 수집, 장기·최근 가격구조 검토, A/B/C 조건 평가, RiskGate, 주문·체결·보호·복구를 연결하는 개인용 프로그램입니다. 코드 회귀검사 통과는 수익성 또는 실제 체결 검증 완료를 의미하지 않습니다. 새로운 전략이나 시간외/NXT 거래는 포함하지 않습니다.

## 시간의 의미

| 구분 | 의미 |
|---|---|
| KRX 주식 정규장 | 통상 09:00~15:30. 실제 개장일·특별 일정과 시장 상태는 별도 확인 |
| 프로그램/메뉴/로컬 진단 | 시간 제한 없음. 16:00에도 정상 실행·상태 조회 가능 |
| V1.1 자동매매 연결·실주문 지원 | 평일 한국시간 09:00:00 이상 15:20:00 미만 + 시장/계좌/정책 gate |
| 일반 신규 BUY | 위 조건에 더해 15:05:00 미만. 실제 전략·위험 조건도 통과해야 함 |
| 봇 포지션 청산 시작 | 15:10:00. ALLOW_OVERNIGHT=false |
| 15:20 | 거래소 폐장이 아니라 **V1.1 운영정책 종료**. 미청산/UNKNOWN은 수동 확인 필요 |

거래소 기준: [KRX 주식 거래 안내](https://global.krx.co.kr/contents/GLB/01/0109/0109000000/guide_to_trading_in_the_korean_stock_market.pdf).
지원시간 밖 기본 실행은 로컬 진단으로 정상 종료합니다. 시간 차단만으로 HALT/ERROR를 만들지 않습니다. 기존 HALT는 지우지 않습니다. `--diagnostics`는 설정, 장부 읽기, 별도 로컬 heartbeat 및 파일 I/O를 검사하며 네트워크를 사용하지 않습니다. 감시 프로세스/시세 heartbeat와 별개입니다.

## 안전한 첫 설치

Python 3.11 이상과 Git이 필요합니다. 이 소스 폴더를 기존 실계좌 설치본 위에 덮어쓰지 마세요.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tools/setup_local.py
.\.venv\Scripts\python.exe main.py --diagnostics
.\.venv\Scripts\python.exe operator_console.py --self-check
.\.venv\Scripts\python.exe main.py --self-test
.\.venv\Scripts\python.exe main.py --config config.example.json --demo
```

예시는 live_mode=false, 승인 cap=0, costs_confirmed=false입니다. LIVE 승인/실제 계좌/주문/인증정보가 없습니다. 로컬 설정이 없을 때도 예시만 읽습니다. `setup_local.py`는 기존 설정을 덮어쓰지 않습니다. 운영 설정은 `config.json`, `automation.json`, 승인은 `runtime/automation_policy.json`에 보관하며 모두 Git 제외입니다.

자격증명은 로컬 `.env` 또는 OS 환경변수 `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`에만 입력합니다. `.env.example`은 YOUR_* 자리표시자만 포함합니다. OAuth 토큰은 메모리에만 보관합니다. 실제 secret은 Private 저장소에도 넣지 마세요. 새 clone은 안전 설정을 생성한 뒤에도 다른 바탕화면 설치의 인증파일을 자동 읽지 않습니다. 기존 설치에만 Git 제외 legacy_credentials_enabled.local 표식을 두어 기존 인증파일 읽기를 보존합니다. 새 소스에는 이 표식이 없습니다. account_equity/paper_cash는 합성 시험용 예시이며 승인 자본이 아닙니다. 이 API 구현은 별도 계좌번호 필드를 요구하지 않습니다. 사용자가 다른 정책/도구에 계좌번호를 추가하면 로컬 파일에만 두어야 합니다.

`--diagnostic-account`는 사용자가 명시 선택하는 계좌 조회 진단입니다. 인증 및 kt00018/kt00001/ka10075/ka10076 읽기만 허용하고 주문 API는 차단합니다. 운영시간 밖 API가 허용하는지는 미검증이며, 거절 시 실제 코드/문구를 로컬 runtime에 보존합니다. 일반 감시의 지원시간은 확대하지 않습니다.

## 사용자 운영 흐름

`operator_menu.cmd`를 더블클릭합니다. 최초 150초 안전 시운전 → PASS 확인 → 계속 감시 시작을 사용합니다. 150초 시운전은 하루 운용 대신 쓰는 기능이 아닙니다. ARMED continuous는 조건/NO_BUY/HOLD만 기록하고 주문/가상 포지션을 만들지 않습니다. LIVE는 비용 확인·사용자 cap·live_mode·명시 승인·계좌 reconcile·RiskGate가 모두 필요합니다. 실행 중 설정 변경은 허용하지 않고 안전 중단 후 승인/재시작합니다. 작은 자본으로 억지 거래하지 않습니다.

- STOP: 신규진입 차단/매수 미체결 정리, 남은 봇 포지션 보호 유지 후 종료.
- STOP_FLAT: 봇 포지션 청산 요청 후 종료.
- KILL: 기존 비상정지·청산 절차. 브로커 체결/취소의 성공을 보장하지 않음.

삼성전자 005930/광전자 017900 HARD_LOCK과 현대바이오랜드 052260 BREAKEVEN_EXIT 보호 상수는 실계좌 보호 약화를 피하기 위해 현재 소스/회귀검사에 유지합니다. 현재 잔고 snapshot·승인 cap·체결이력은 포함하지 않습니다. 개인 보호정책의 종목코드·5,834원 기준·보호수량 상한13은 소스에 남으므로 Private 업로드 전 이 정책 정보도 검토해야 합니다. 나인테크 시험매도 사용자 기능은 종료/비활성입니다. 과거 계약의 고립된 synthetic 테스트는 유지합니다.

## 소스와 운영자료

설치본 전체를 Git에 추가하지 마세요. 검토한 `github_source` 폴더만 업로드합니다. [업로드 절차](REPOSITORY_GUIDE.md), [secret 사고 대응](SECURITY.md), [외부 코드 안내](THIRD_PARTY_NOTICES.md)를 확인하세요. `tools/repo_guard.py`는 파일 허용목록·인증 패턴·실제 staged 바이트를 검사하며 발견값을 출력하지 않습니다. 자동 검사는 완전한 보안 보증이 아닙니다.

원본 공식 문서/예제, vendor, runtime, releases, 실제 거래보고서, 개인 운영문서는 제외합니다. 계약 fixture는 필요한 필드 이름만, CSV fixture는 합성 250행만 포함합니다. 전략·주문·체결·부분체결·보호·복구 상태기계와 기존 회귀검사는 유지했습니다. 이번 작업의 검사는 네트워크 없는 합성 검증이며, 실제 신규주문·실제 청산·수익성·시간외 계좌 API 성공은 미검증입니다.

2026-10-01 최신 오프라인 회귀검사: 기존 371개 유지 + 신규 33개 = 404 PASS / FAIL 0 / ERROR 0 / SKIP 0. 설치본 자체 Python 50파일 및 업로드 소스 46파일의 문법 오류 0. secret/local 설정 없는 소스 유사 환경에서도 같은 404개 통과 (의존성은 저장소 밖에서 오프라인 제공). 실제 체결/수익성 검증 완료를 의미하지 않습니다.
