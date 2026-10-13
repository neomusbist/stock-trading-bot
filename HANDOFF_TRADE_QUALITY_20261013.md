# V1.5 entry-quality development result — 2026-10-13

## Feed catch-up checkpoint

Branch: `fix/v1.5-exit-lifecycle-20261013`, based on `e49a18b` (including
`633c09c` and `a43352d`). BUILD: `V1.5-risk2-watch12-bc20M-feedcatchup-20261013`.

The operating source comparison confirmed that SHADOW depth isolation and
the observation continuity changes were absent from the running source. The
receiver cached all books, but diagnostic SHADOW books also occupied the main
order/position queue. The outer runner consumed only one message per iteration,
repeating subscription, account and status work between messages. A retained
data-exit incident recorded consumer lag exceeding forty seconds alongside a
recent receiver heartbeat; the later transport-disconnected state triggered the
existing emergency exit. The exact transport failure was not retained and is
not inferred from the consumer lag.

This checkpoint adds bounded FIFO catch-up (64 messages or 50 milliseconds),
yielding at REG/ERROR/DISCONNECTED barriers. Prices and fills are neither
coalesced nor reordered. Receiver-time history can populate past bars; stale
frames cannot cause current orders or price exits. Account reconciliation and
the existing watchdog run after each bounded batch. This is a processing-path
change, not evidence of improved financial expectancy.

The five-percent pullback reference is now explicitly described as the bounded
usable-bar window, not an exchange day high. Its decision threshold is unchanged.
GUI release labeling preserves V1.5. Three release-identity expectations were
updated without weakening old-approval rejection or risk assertions. The first
full run found a captured default-clock error in the new helper; runtime clock
resolution was corrected, and the final full run must pass before deployment.

Five dedicated tests cover FIFO prices/fills, transport barriers, bounded yield,
stale-history/no-order behavior and fresh evaluation after a backlog. Existing
price-stop, quote-emergency, quiet-book, REST verification, loss-budget and
continuous-trading counterexamples remain required in the full suite.

Frozen price-gate replay covers eighteen completed records across three dates:
eleven have decision bars and seven are UNKNOWN. With the actual operating
origin/direction flags, old and new price gates agree on all eleven; no alternate
fills or replacement profits are inferred. Full post-entry trade/book/bar streams
are not retained, so alternate structural/profit/partial-exit prices and expectancy
remain UNKNOWN. Private evidence and broker/order identifiers stay outside Git.

Final offline regression: **1,025 PASS**, zero failures/errors/skips, 125 syntax
files, zero syntax errors, zero network attempts and zero actual orders. The
operating source, account reconciliation and new BUILD approval are separate
checks; this result alone does not authorize orders.

Deployment preserves current settings, daily loss, orders and the user's latest
date-specific risk override. It must not restore an earlier budget snapshot.
Keep `chase_quality_filter`, `resilient_protection_enabled` and
`confirmed_structure_protection` OFF unless separately validated and approved.
Existing origin/direction/priority flags remain as configured. A new BUILD cannot
reuse or fabricate an old authorization receipt; finish account reconciliation,
backup, source hash verification and read-only readiness before direct user LIVE
approval. Record final regression, commit, deployment and process results in the
private handoff.

Base: V1.4 `abb29815ca114fc0ce50babc4035f15236a64ba5`.
Branch: `feat/v1.5-entry-quality-20261013`.
Status: verified development checkpoint; no operating deployment, LIVE approval, restart or real orders.
The branch is the development identity. Existing production BUILD identifiers are not a claim of V1.5 release readiness; new release stamping and approval belong to separately approved packaging.

## Actual changes in trading decisions

1. `fresh_box_basis_enabled` (default **false**) adds an independent-base check for Setup C. Using the existing five-bar length and 1.5% box range, it finds an already confirmed close above an earlier box. If that breakout candle is still inside the current claimed five-bar base, C fails `BOX_CONTAINS_COMPLETED_BREAKOUT`. Five fully completed bars after the breakout can form a new base. Breaking the old floor retires that earlier hypothesis, allowing a separately evaluated reversal. First live breakout of an initial complete base remains possible. No extra acceleration, percentage-rise or arbitrary elapsed-time threshold was added.
2. The new C check is part of the actual detector's price gates, not merely a log. The engine recomputes its timestamped proof and normalized OHLC digest before an order can leave the process. Missing/changed data is blocked. Unknown, gaps, duplicates, partial and stale candles do not become PASS. B/A geometry, liquidity, risk and protection are unchanged.
3. `setup_aware_watch_priority` (default **false**) makes RAW compression use the already implemented A/B/C readiness tiers. Previously DynamicRadar labeled a valid C/B with acceleration FAIL as FRESH_WEAK although fast focus used setup-specific readiness. Both RAW WATCH ranking and diagnostic residence now recognize a viable independent B/C. Stale observations, failed liquidity/RR/box-origin, hard locks, affordability and normal final order guards remain enforced. WATCH stays 12; no capacity, request budget or rank score expansion.
4. No new exit policy was applied. V1.4's confirmed post-entry protection and matched REST data-health paths are preserved. A longer hold cannot be honestly justified by selected after-the-fact charts without a complete post-entry receiver/quote/REST stream.

## Kumho Construction chronology and cause

Closed archives, verified unchanged by SHA256, show:

| Event | Time | Price / decision |
|---|---|---|
| First retained RAW | 08:36:49 | discovery only |
| First radar WATCH | 08:53:33 | does not prove uninterrupted later observation |
| Earlier B signal | 09:56:05 | 13,990; MARKET_STATUS_STALE |
| Earlier C signal | 11:04:29 | 13,680; MARKET_STATUS_STALE, logged at 11:04:30 |
| Earlier C signal | 11:05:16 | 13,700; MARKET_STATUS_STALE, logged at 11:05:17 |
| Later C signal | 11:08:53 | 13,790; MARKET_STATUS_STALE |
| Accepted C | 11:09:58 | 13,800; order accepted 11:09:59.773954 |

The 09:56 check still referenced 09:52:06 status; the 11:04/11:05 signals referenced 11:01:23. At scan=60 seconds, the hard status TTL is **180 seconds**. 120 seconds is V1.4's refresh trigger, not permission to use stale data. V1.4 already refreshes incumbent WATCH one scan before expiry and publishes status-cache changes immediately to the main evaluator (`test_status_publication.py`). Do not attribute that earlier fix to this V1.5 checkpoint or assert that a simulated fresh status proves an earlier executable order.

At 11:09:58, the rolling five-bar high was 13,790 and low 13,660; width 130 produced target 14,050. Buffered stop was 13,640, giving gross RR=(14,050−13,800)/(13,800−13,640)=1.5625. In the retained preceding bars a lower earlier box had already broken by completed close at 11:04/11:05. The current C base reused those breakout candles, moving the support and projection upward. The new origin gate rejects this late follow-through as a new independent C breakout.

Around 11:00–11:09, radar repeatedly labeled the symbol FRESH_WEAK despite C evaluation not requiring A acceleration. This is a verified priority inconsistency; the exact alternative historical WATCH membership cannot be reconstructed because every competing symbol's contemporaneous readiness is not retained. New ranking behavior is validated on isolated competing candidates, not reported as proven historical earlier fills.

## Eight-trade point-in-time comparison

Actual code `PatternDetector.setup_gates` was rerun with stored decision OHLC, stored breakout/ask, existing geometry and then-known resistance. Default V1.4 and V1.5-origin-only differ only by the new flag. Historical exit results are comparison labels and are not fed into entry logic. Account, approval, quantity, pending orders and hypothetical fills are **not** replayed.

| Date/time | Symbol/setup | V1.4 default price gates | V1.5 origin-only | Observed outcome |
|---|---|---|---|---|
| 10/12 09:58:52 | 042510 C | PASS | PASS | profit |
| 10/12 11:09:58 | 002990 C | PASS | FAIL origin | loss |
| 10/12 12:28:03 | 042510 C | PASS | PASS | profit |
| 10/12 12:42:36 | 038500 C | PASS | PASS | loss |
| 10/12 14:29:19 | 038500 C | PASS | PASS | loss |
| 10/12 16:06:21 | 042510 A | PASS | PASS | loss |
| 10/12 16:55:55 | 002990 A | PASS | PASS | loss |
| 10/12 18:57:44 | 198440 C | PASS | FAIL origin | loss |

The origin experiment blocks two selected losses and zero selected winners. It does **not** establish better expectancy or predict the profit of any replacement trade. With existing context+direction enabled, both profitable Raon entries still fail resistance RR/economic/context. V1.5 origin does not solve that over-blocking tradeoff. A winner's outcome does not make a known resistance invalid. Do not disable that guard silently or count an origin-only PASS as a full strategy PASS.

A separately truncated earlier-minute reconstruction is private and explicitly incomplete: at 11:04 only seven of the required bounded twelve historical bars were retained in the later snapshot. It cannot prove that the full contemporaneous V1.5 origin check would have permitted that earlier order. Same-second events can have different prices and gate outcomes; use `signal_at`, not the first log line in that wall-clock second.

## Selection coverage and remaining limitations

The closed Oct12 radar archive contains 532 cycles, **615 unique RAW symbols** and **78 unique radar WATCH symbols**. Counts cover retained preopen and intraday cycles, not the whole Korean market. The eight executed trades concentrate on four symbols. Compression-cycle reason counts include unaffordable 22,315, status-scan budget 19,936, resistance room+turnover 11,953 and turnover 4,272. These are repeated row evaluations, **not unique lost opportunities**. Status-scan exhaustion is a bounded API workload policy, not a trade count limit; this checkpoint does not raise it.

Oct08 provides ten ENTRY_CANDIDATE records on an independent date. Three C candidates lack decision-bar snapshots and are UNKNOWN for the new origin test; seven A/B records are outside C scope. Thus independent-date strategy validation remains incomplete. No full post-entry quote/REST/bar stream is present to compute a credible alternate exit price, MFE/MAE path or net PnL. Keep the new flags OFF pending broader saved-data or isolated paper evidence.

## Validation and preserved boundaries

- Baseline: 992 PASS, zero failures/errors/skips; new targeted: **12 PASS**.
- Final offline regression: **1004 PASS**, zero failures/errors/skips, 122 syntax files, zero syntax errors, zero network attempts, zero actual orders, operating program not started.
- Tests cover first C breakout, old completed breakout reuse, five-bar rearming, retired old floor, future-price exclusion, invalid histories, default-OFF strict booleans, actual detector gates, engine proof tampering, B/C acceleration independence and failed/stale competing readiness.
- New tests are in `tests/test_v15_entry_quality.py`; existing baseline tests and shared pytest settings were not removed or reorganized.
- Reproduction tool: `tools/trade_quality_v15_replay.py --evidence-dir FROZEN_DIR --independent-events OCT08_FROZEN_JSONL --output PRIVATE_JSON`. Historical evidence/output stay outside Git. Source manifest and staged-byte guard must pass before commit.
- Preserve the operating layout documented in `DEPLOYMENT_LAYOUT.md`; never wholesale-copy the checkout or restore archived readiness PASS files. No active runtime/authentication/approval/account state was used or changed.

## Integration work still required

1. Obtain adequate independent-date C snapshots and competing readiness histories to measure actual selection latency and false exclusions.
2. Evaluate reclaim/acceptance of genuinely cleared near resistance while preserving conservative RR. No new threshold is justified by these two winners alone.
3. Compare normal pullbacks versus structural failure using complete post-entry bars and executable quotes; do not remove emergency exit or widen loss budgets as a substitute.
4. Validate candidate coverage across prices/source intersections without increasing live subscriptions or API budget blindly.
5. Integrate reviewed development commits and other agent results; stamp a new BUILD, package only approved code, and request explicit deployment and direct user LIVE approval separately. This result is **not deployed** and does not claim LIVE readiness or profitability.
