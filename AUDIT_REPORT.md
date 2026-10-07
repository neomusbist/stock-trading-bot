# 현재 기준: 2026-10-07 Discovery/WATCH 복원

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
