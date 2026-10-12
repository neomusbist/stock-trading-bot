# 최신 감사: 2026-10-12 연속 운용과 진입 지연

상태: 운영 최소 복구 완료, 추가 관측 안정화 전체870 PASS. 추가 운영 반영은 사용자 승인 대기. 아래 과거 보고는 역사 기록이며 현재 완료 판정이 아니다.

## 확인된 문제와 수정 범위

1. 정상 SELL 체결이 조회에 먼저 나타나고 로컬 체결 장부가 늦게 반영되는 경합을 합성 입력으로 재현했다. 체결 전 시작한 잔고 조회는 fresh 증거가 아니며, 자기 미체결 SELL로 설명되는 수량 감소만 제한된 시간 동안 재대조한다. UNKNOWN/설명 불가/초과 수량/시간초과 HALT는 유지한다. 실제 과거 원본 잔고 응답은 저장되지 않아 삼표 사고의 정확한 패킷 내용은 UNKNOWN이다.
2. WebSocket REG 전송의 closed-transport 예외가 worker를 종료할 수 있었다. transport 예외만 정상 disconnect/reconnect 경로로 넘기고 같은 cycle에서 주문을 즉시 차단한다. 프로그래밍 오류/PermissionError를 복구 성공으로 숨기지 않는다.
3. WATCH 역할 변경이 실제 구독 제외로 이어져 관측을 잃었다. 라온의 12:06:06에는 이미 124초/완성봉2개였지만, 12:06:28 release 후 12:06:34 rejoin에서 실제 구독 공백5.418723초로 reset됐다. 이후 새 epoch12:06:44, 12:08:38에는 16,550원/112초/완성1봉이었다. 이 가격차가 곧 실행 가능한 수익기회였다는 뜻은 아니다.
4. 기존 WATCH 상태정보는 180초까지 유효하나 갱신 지연으로 신호를 차단할 수 있다. 실제 WATCH는 기존 radar 한 cycle 먼저 갱신하고, main entry가 radar가 이미 갱신한 immutable metadata를 사용한다. 주문측 TTL/20회 status budget은 완화하지 않는다. main thread의 동기 REST JIT 호출은 추가하지 않는다.
5. 현재 A/C target은 일봉 저항과 패턴 projection의 작은 값을 사용한다. 가까운 장중 고점이 반드시 반영되는 구조가 아니다. 새 독립 ENTRY_CONTEXT_AUDIT는 양쪽의 연속·완성 1분봉으로 확인된 국소 고점, 원래/재검토 target·RR·Economic·5/15분 문맥을 기록한다. 이 진단은 주문 조건을 바꾸지 않는다. snapshot 밖의 장중 고점은 UNKNOWN으로 남긴다.

## 세 거래의 당시 판단

| 사례 | 실제 signal | entry / initial invalidation | 기존 target / 가격 RR | 완성봉에서 확인한 가까운 고점 | 진단 RR | 판정 |
|---|---|---|---|---|---|---|
| 금호건설 | C, 11:09:58 | 13,800 / 13,640 | 14,050 / 1.5625 | 저장된 마지막12봉에는 10:38의13,950원 없음 | 해당 고점 확인 시0.9375 | 전일 일봉과 짧은 snapshot만으로 온전한 장중 저항을 판단할 수 없음 |
| 라온시큐어 오후 | C, 12:28:04 | 16,530 / 16,330 | 16,860 / 1.65 | 12:21 국소고점16,560 | 0.15 | 관측 reset 때문에 급등 초기에 데이터 준비 부족. 고점 cap은 이후 수익 거래도 거절함 |
| 삼표시멘트 | C, 12:42:29 | 7,590 / 7,460 | 7,800 / 1.6154 | 12:33 고점7,630, 좌우 완성봉으로 당시 확인 가능 | 0.3077 | 작은 box breakout만으로 큰 하락 흐름의 무효화를 확인하는 구조가 아님 |

오늘 네 실제 매수 체결은 모두 C였다. A acceleration을 낮추는 것이 이 사례들의 입증된 해결책은 아니다. 삼표의 실제 exit7,450은 초기 invalidation7,460 아래이며, 가격 -2% 고정손절을 임의 적용한 결과가 아니다. 5분/15분 trend는 현재 진단이며 hard entry gate가 아니다. 삼표 MIXED/MIXED, 금호 UP/MIXED, 오후 라온 MIXED/UNKNOWN이었다.

최근 국소고점 cap만 기존 RR1.5와 비교하면 삼표 손실 진입과 두 라온 이익 진입을 함께 거절한다. 따라서 이 단일 변경의 기대값 개선을 주장할 수 없다. 개발 V1.4의 '+15%와 15분 MIXED/DOWN' 차단, snapshot 양 끝을 swing peak로 인정하는 로직, 09:00~09:05 Setup O는 검증되지 않아 운영에 병합하지 않았다. 장중 새 상승은 Setup O로 해결되지 않는다.

## 탐색 범위와 편향 (09:00~13:30 저장된288 radar cycles)

- 필터 후 고유 RAW330, 한 번 이상 affordability 통과272, radar row 기준 WATCH53, RESERVE57. 단계별 고유집합은 중복되며 합산할 수 없다. fast-focus WATCH까지의 완전한 합집합으로 해석하지 않는다.
- cycle RAW 최소137/평균143.24/최대152. 전체 상장시장 전수검사나 0168과 동일한 후보집합이 아니다.
- API별 필터 후 고유 기여: ka10029 92 / ka10032 161 / ka10027 151 / ka00198 78 / ka10031 28. 반환 원본에서 보통주 필터 전에 제외된 개수는 로그만으로 UNKNOWN이다. configured rank multiplier2, 기존 첫 응답 page만 사용, RAW 상한300, radar60초, WATCH12.
- 한 번 이상 매수 가능: 3만~5만원22종목, 5만~81,300원18종목. 수천/1만원대 전용 상한은 없다. 가격대 이동에 따라 집계집합은 중복될 수 있다.
- 예상체결(ka10029) 단독으로 관측된 종목79, 그 중 radar WATCH12. 단독 관측 가격이 저장 구간에서 하나뿐인 종목46, 그 중 WATCH7. 장중 실제 상승/거래량 증가를 보장하는 source가 아니라는 진단 필요. 이를 현재가라고 무조건 믿거나 순위 API5개를 독립적인 실시간 상승증거5개로 해석하면 안 된다.
- 기존 거래 이력 자체를 점수에 더하는 코드는 없다. 동일tier 기존 WATCH +2 hysteresis, higher tier 교체, 이미 봉이 준비된 후보의 tier 우선, active WATCH 상태조회 우선 및20회 조회 budget이 간접적인 재선택 편향을 만들 수 있다. 이 때문에 새 종목이 더 좋았다고 단정할 수는 없다.

키움 REST 공식 이용안내: 국내 조회TR/주문TR 각각 초당5회, 토큰당 세션1개, 실시간 시세200종목. OpenAPI+ 화면수 한도와 혼동하지 않는다. 공식출처: https://openapi2.kiwoom.com/intro?dummyVal=0 . 공유 API rate 간격/조회budget/구독상한을 이번 패치에서 임의 확대하지 않았다.

## 지연과 시간축 해석

13:30까지 실제300 latency samples: cycle 평균13.287초/p95 23.856초/최대43.139초, rank평균2.839초, compression10.427초, status8.575초, history1.426초, API wait4.945초. 하위구간은 겹칠 수 있어 합산 금지. Python 신호 계산 시간과 HTTP 대기를 혼동하지 않는다.

70초 baseline+acceleration, A2/B4/C5는 데이터 준비/형상 정의의 일부이며 고정 pilot sleep과 다르다. 현재 C는 상승 재개 여부를 5/15분 문맥에서 강제 검증하지 않으므로 하락 중 작은 박스도 geometry에 들어올 수 있다. 필요 조건을 단순 완화하기 전에 실제 관측 손실/worker restart/status latency를 제거한다. 정확한 과거 raw tick·호가 전체가 저장되지 않아 조기 주문 가능 시각/가상 체결·MFE/MAE·전략 기대값의 완전한 counterfactual replay는 미검증이다.

## 연속 거래와 안전 상태

- 첫849 회귀 최소 복구 패치는 운영에 반영됐다. 사용자 직접 새 BUILD LIVE 승인 후 연결/계좌/OPEN_READY 확인. HALT를 직접 JSON 편집해 해제하지 않았다.
- 두 라온 정상 이익청산 후 다음 거래가 이어졌다. 모든 거래 뒤 HALT가 걸렸다는 주장과는 다르다. 금호 PermissionError HALT는 11:11:43으로 실제 exit11:13:49보다 먼저다. 삼표 quantity mismatch HALT는12:58:00, local fill 반영은12:58:04다.
- cutoff까지 WORKER_FATAL19 중 WS closed 예외18/PermissionError1. worker 시작 횟수와 실제 재시작 원인은 로그 환경을 분리해 집계한다. 과거 fatal에는 stack/filename이 없어 PermissionError의 정확한 경로를 소급 단정하지 않는다.
- 합성 브로커 실제 Engine 경로로5회 연속 BUY/fill/SELL/fill/reconcile 후 정상대기 복귀를 검증했다. 정상 이익/손실, 다른 종목 및 기존 시간 cooldown 경과 후 같은 종목 재진입을 포함한다. 실제 주문 테스트0. 이를 실전 HALT율0이나 수익성 입증으로 간주하지 않는다.
- cap은 실제 사용자 승인값, risk2/daily4/WATCH12/maxpositions1 유지. 일일/동일종목/연속손실 count limit은 운영 설정null, 거래횟수는 기록. 시간 cooldown900초는 별도 기존 정책으로 보존했다. 수익이 gross daily_loss를 되돌리지 않는 현재 예산 정책도 바꾸지 않았다.
- Windows live log reader는 READ/WRITE/DELETE 공유로 rotation을 막지 않는다. 열린 reader 동안 lossless rotate가 성공하는 신규 검증을 추가했다. 이것이 과거 PermissionError의 유일 원인이었다고 주장하지 않는다.

## 남은 인수 조건

추가 BUILD observation 패치는 전체검사/commit/push 및 구체적인 사용자 배포승인 필요. 운영 LIVE를 유지하며 개발한다. 새로운 LIVE 승인은 사용자 직접 수행하며 receipt복사/대신 승인 금지. 운영 raw account/credential/runtime/거래 JSON은 Git 제외. 기대값 검증, 전시장 발견지연 및 미수집 후보의 신호 가능성, 장시간 queue/열 부하는 아직 UNKNOWN이다.

# 역사 기준: 2026-10-07 Discovery/WATCH 복원

기존 Work preopen 계획을 재개하지 않고 de5fc0c 뒤의 미커밋 후보 관찰 작업만 복원했다. 현재 위험 실험 2%/일일4%·승인자본20,000·포지션1·진입3과 기존 전략/청산/승인 정책을 보존했다. 최종 전체 551 PASS이며 실제 주문/네트워크/운영본 배포는 없다. 아래 V1.1 감사는 역사 기록이다. 이번 변경의 상세 판정은 마지막 2026-10-07 절에 있다.

# V1.1 code audit and observation design
Build: V1.1-audit-20261002. This public report contains code findings and synthetic verification only, not private trading logs or account results.

Observation and permission are separate. Affordable, officially normal, unprotected source-one candidates can accumulate SHADOW data. Actual BUY still requires at least two current sources or the existing explicitly qualified source-one WATCH path, a valid A/B/C Setup, risk quantity, confirmed costs, account/market/session checks and explicit LIVE authorization. Virtual SHADOW signals cannot enter the real order path.

Sole resistance-room failures are deferred to breakout A/C evaluation; B and other structure failures remain unchanged. Continuous valid observations survive source qualification changes. Missing minutes, disconnections and actual unsubscribe reset invalid data. Metadata and tick work use separate queues; shared CPU/API/WebSocket load still requires real-session verification.

The original order/fill/recovery engine remains. Initial structural invalidation is stored at fill. Protection only rises. Exit intentions persist across failures; partial fills, cancellation confirmation and reconciliation prevent duplicate selling. Local stops are not broker-native stop orders and do not guarantee maximum realized loss.

Risk remains 0.3%; authorization caps never increase automatically. Entry cutoff 15:05, flatten 15:10, support end 15:20, overnight disallowed. HARD_LOCK and external holding protections remain. Synthetic regression is not evidence of profitability or actual fill success.


## 2026-10-07 interrupted Discovery patch restoration

Verified starting HEAD: de5fc0c, main. No later commits or staged changes existed. Twelve unstaged files were preserved byte-for-byte before editing. Their purpose matches the interrupted Discovery work, but Git evidence alone cannot prove author identity.

Already implemented: unified focus competition, same-tier +2 incumbent hysteresis, better-quality replacement, max_watchlist default/example 8. Incomplete: radar still rejected source-one with installed min_source_count=2 and reused an empty-slot-only fallback path; fast promotion updated engine qualification without updating WATCH row flags; the fresh-structure signal recomputation discarded that flag. The interrupted diff also removed the actual source floor from order guards. That order-floor change was not needed for observation and has been restored to the prior boundary.

Completion: source-one is compressed once and competes in the same pool, with basic safety/economic/status/structure checks intact. Explicit qualified WATCH markers propagate through the real signal recheck. SHADOW-only flags remain blocked. Strategy thresholds, sizing formula, risk experiment, exit/STOP/session policies were not changed.

Connected synthetic approval/order regression found a separate required safety invariant failure: durable own INTENT was rejected by the daily authorization critical-state check. A narrowly scoped current-order context now excludes only that exact current BUY record during guard evaluation. Foreign INTENT, UNKNOWN, HALT, mismatched disk records, account identity and duplicate guards remain fail-closed. Read-only authorization still sees all INTENT records.

Replay scope: 163 saved candidate cycles for six requested symbols. All six contain source-one historical rejections. No source-only rejection remains at the patched observation entrance. Other recorded economic/weakening gates remain; missing point-in-time official status remains UNKNOWN. Closed daily history ends 2026-10-06 and is recomputed only at recorded prices. This is a partial-universe entrance replay, not proof of actual WATCH allocation, Setup, BUY, execution or profitability. Full raw logs remain private and unmodified. No broker was instantiated by replay.

Deployment/push are not part of this restoration. Desktop installation, personal configuration, approvals and runtime are untouched. Applying source code requires a controlled restart; an explicit saved WATCH limit of 5 still overrides the new default 8.
