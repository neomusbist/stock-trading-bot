# SHADOW 실행 절차 — 실접속 조건부

최종 부모 보고서의 실연결·전체 회귀·깨끗한 체크아웃 검증 결과와 함께 사용한다. FAST는 실주문에 연결하지 않는다. 10월14일 22:23 인증 없는 --check는 collision_check PASS/network_calls0/live_mode false/order_surface false/종료0이었다. 다음 날 실행 직전에는 반드시 다시 검사한다. 실제 구독·수신을 확인한 PASS는 아니다.

## 인증 없는 확인

검증한 개발 폴더에서 다음을 실행한다.

```
python -m automation.shadow_fast_runtime --check --source-root "<R5 operation folder>" --output "<isolated SHADOW output folder>"
```

출력의 collision_check PASS·live_mode false·order_surface false·network_calls0을 확인한다. SOURCE_ROOT의 LIVE Worker가 동작하거나 봇 보유·미체결·UNKNOWN이 있으면 진행하지 않고 차단 원인을 표시한다. 사용자가 직접 승인한 LIVE를 이 명령이 임의로 종료하지 않는다.

## 명시적인 실시간 데이터 실행

현재 LIVE Worker STOP·봇 보유0·미체결0·UNKNOWN0이 공식 대조된 후에만 실행한다. 새 인증은 이 명시적 CLI 실행 안에서만 발생하며 기존 승인·설정·손실장부는 쓰지 않는다.

```
python -m automation.shadow_fast_runtime --source-root "<R5 operation folder>" --output "<isolated SHADOW output folder>" --seconds 1200
```

장전 후보 준비가 필요하면08:50경 시작하여09:10까지 기록한다. 이는 당일 실증 예정 절차이며 현재 09:00 검증을 완료했다는 뜻이 아니다. `--symbols 001210 042510` 등 추가 소수 종목을 지정하면 별도로 수신할 수 있지만 강제로 매수 자격을 부여하지 않는다. 기존랭킹 API의60초 이하 과도 반복은 하지 않는다. 후보 metadata에 상태·지지/저항이 부족하면 FAST 신호가 UNKNOWN으로 남을 수 있다.

별도 미리 발급한 SHADOW token file이 있다면 `--token-file`을 추가로 지정할 수 있다. 이 경우에도 source-root를 생략하거나 실행 중인 LIVE 충돌 검사를 우회할 수 없다. token file은 개발·Git·로그에 넣지 않는다. 계좌 번호·비밀번호 입력을 이 CLI가 요구하지 않는다.

## 정상 확인 및 종료

- `runtime/shadow_ticks.jsonl`: TRADE/BOOK, exchange_at/receiver_at/processed_at, depth 상태, record/transport sequence.
- `runtime/shadow_events.jsonl`: 후보 발견, 구독 ACK, FAST 신호·실패, stream reset/queue gap, 기록 오류.
- `runtime/shadow_snapshot.json`: queue 깊이·input/processed sequence·dropped·logger write error·metadata 호출.
- `runtime/shadow_virtual_ledger.jsonl`: 모델의 pending/후속 프레임 진입/청산. 실제 증권사 체결이 아니다.
- `runtime/shadow_result.json`, `shadow_account.json`: 실현/미실현 및 종료 시 보유·미체결 분리. tape 종료로 강제 청산하지 않는다.

Ctrl+C 또는 지정한 초가 끝나면 Feed를 닫고 worker 큐를 drain한 다음 flush한다. close flush 실패·worker 미종료 오류가 나오면 정상 종료로 평가하지 않는다. active.jsonl과 `.1`, `.2` 등 로테이션 파일을 함께 보존한다. `rows_dropped`, queue gap, `write_errors`, `rollback_errors`가0인지 확인한다.0이더라도 증권사~PC 구간 무손실이나 실제 체결 가능성을 입증한 것은 아니다.

LIVE를 재개할 때는 SHADOW 종료·기록 완료를 먼저 확인하고, 기존 인간 승인·주문 경계를 그대로 따른다. 실제 승인 대행·실주문 테스트·예산 리셋은 하지 않는다.
