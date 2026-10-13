# AFTER burst exit checkpoint

BUILD: `V1.5-risk2-watch12-bc20M-structuralAE2-20261013`.
Extends committed structuralAE checkpoint `7efbd16`. Production application is separately recorded in the private deployment report; this document does not assert deployment or LIVE approval.

## Confirmed operating blind spot

The retained Oct13 Raon position entered Setup A in AFTER at 17,460 with initial stop 17,180 and post-entry high 17,700. The 240-point favorable excursion did not reach the 280-point one-R threshold, so existing profit protection remained unarmed. Initial price invalidation is not a sufficient sole measure of whether a burst continues. A chart's later decline cannot establish that the exact high was sellable when it occurred.

## Actual decision change

`after_momentum_failure_enabled` defaults false. When enabled together with the validated new A/B invalidation policy, only an AFTER A burst uses this exit. The separately proven sustained-continuation entry mode is outside its scope.

A failed burst rebound is confirmed by:

- An observed post-fill peak above entry; a partial entry minute cannot supply that peak without a real post-fill tick timestamp.
- A later completed rebound whose close reclaims the preceding candle's high but whose high remains below the observed peak.
- Three later fresh consecutive completed candles with strictly falling highs, lows and closes, ending below that rebound's low.

That failure latches an exit before the initial structural/emergency stop is necessarily reached. One-R activation is not required. Volume is retained in the evidence; fading volume alone cannot cause an exit and expanding sell volume cannot veto confirmed price failure. Invalid volume, missing/shifted/stale/future/partial candles and unconfirmed recovery produce UNKNOWN or HOLD, not an exit.

A first controlled pullback, retained higher low, recovered close or new higher peak remains HOLD unless the existing structural/emergency protection independently applies. This does not promise capturing the high. It needs at least three decline confirmations after a failed rebound and may still exit before a subsequent successful recovery. A fast outright collapse remains covered by the independently sized emergency stop.

Only new positions carrying validated invalidation proof use this policy. Existing holdings receive no retroactive stop widening or new unapproved exit rule. Exit evidence is frozen when requested, and order/account/duplicate/quantity safeguards are unchanged.

## Validation and limits

20 dedicated cases in `tests/test_after_momentum.py` passed before full regression. They include failure below one-R but above the initial stop, normal pullback/recovery retention, expanding sell volume, malformed/late/future candles, valid partial-minute peak provenance, equal-peak retest reset, strict flags, legacy position preservation and a single mock SELL with frozen evidence. One uses anonymized public market OHLCV values; no account/order/authentication capture is included.

The prior structuralAE checkpoint had 1,072 PASS. The final structuralAE2 operating release regression has **1,098 PASS, FAIL/ERROR/SKIP 0**, 133 syntax files without errors, zero network attempts, zero actual orders and no operating program start. The extra six cases verify explicit recovery after an already executed manual SELL, refusing missing/wrong/duplicate/old fill proof, retained bot quantity, pending orders and protected-holding mismatch. No real test orders are allowed. The new policy is an implementation hypothesis, not a textbook quotation or proven positive expectancy. Exact Oct13 alternative executable exit price and net PnL require the actual completed-bar and quote record; chart pixels or later broker references alone do not prove a historical executable order.

Oct13 completed SOR reference replay, received later at 19:16 and strictly cut at each decision time, first confirms failure at 16:59. The 16:52 lower rebound floor was 17,520; the 16:56/57/58 closes were 17,500/17,460/17,450. Peak 17,700 remained below the legacy 17,740 profit activation. This is a reference-bar counterfactual, not an actual order/quote replay. No hypothetical fill price or net PnL is asserted. The actual stored entry candles still pass the new A structural check: this release does not claim that every ultimately disappointing burst should have been refused initially.

## Integration

Preserve structuralAE's meaningful support, overhead resistance cap, account-risk sizing and early affordable RAW status admission. Keep WATCH12 and API request budgets. Include `automation/after_momentum.py` in the source allowlist and enable the four independent release flags only after approved flat-state deployment. Back up source/config/journals, reconcile manual fills and protected holdings, preserve the day's losses and override, and request direct user GUI approval for the new BUILD. Do not copy approval receipts or reset HALT indiscriminately.

The user subsequently authorized operating deployment and recovery after the manual sale. A fresh broker query confirmed the full 3-share SELL at 17,500, no remaining bot holding and no pending orders. The already executed external fill was reviewed in a stopped isolated model, then journaled under official idle/control locks with the original entry, fee model and day ledger preserved. Existing approval/config hashes stayed unchanged. Explicit recovery requires a fresh matching broker fill and protected holding comparison; this is not an automatic HALT bypass. Gateway recovery reads the fill record only for this specific external-order incident. Operating backup/application/preflight and direct new-BUILD user approval are separately evidenced in the private deployment report; do not infer approval from the source commit.


---

# V1.5 entry-quality development result — 2026-10-13

## Structural AE checkpoint

BUILD `V1.5-risk2-watch12-bc20M-structuralAE-20261013` extends the preserved B checkpoint.
The release regression has 1,072 PASS, FAIL/ERROR/SKIP 0, 130 syntax files checked,
and no network attempts, operating program starts or real test orders. There are
35 new independent tests in `tests/test_structural_a_exit.py`.

Actual archived evidence showed an affordable rising RAW candidate repeatedly
blocked by STATUS_SCAN_LIMIT before WATCH evaluation. Existing FIFO admission is
retained; at most four status slots favor objectively moving or recently discovered
rising candidates after incumbent deadlines. The original API status quota,
affordability, security/status and final order checks remain mandatory.

`IntradayContextWorker` uses the existing shared auth/rate session in read-only
diagnostic mode, at most one request every five seconds and fourteen targets:
the twelve WATCH slots plus at most two fresh preliminary SHADOW setups. This
does not enlarge subscriptions, RAW or WATCH. Reference history is bounded to
120 completed bars, matched to the requested venue, and never restores realtime
tick buffers, completed signal bars or warmup. Empty NXT responses remain
NO_HISTORY rather than fake candles. Receipt time and market candle time differ.

New A proof requires known prior structure, an independently confirmed range
floor/recovery, executable buffer and an older uncleared resistance cap. A sustained
route can replace the burst-ratio prerequisite only with three completed rising
closes, strictly rising lows, nondecreasing positive volume, structural proof and
unchanged fresh quote/liquidity/order/risk gates. It is not a generic ratio relaxation.

New A/B positions separate a completed-close structural line from an immediate
emergency line. Quantity and reward/risk use the latter plus existing costs and
modeled exit slippage. Valid structure may exceed the old three-percent price
distance ceiling, but integer quantity must still fit account and daily risk budgets;
even one affordable share is refused if its risk is too large. Legacy price caps are
unchanged when proof/flags are absent. No existing position is retroactively widened.
Confirmed profitable post-entry pivots, not one tick or one candle low, may raise
protection. Quote/trade timestamps are checked before an emergency trigger.
The GUI distinguishes the structural close line and current protective emergency line.

The late-entry incident is refused both with inadequate original history and with
completed prior market history: available reward at the old high is too small for
the confirmed range risk. Earlier sustained structure can qualify in a market-only
counterfactual, but missing historical live quotes, tick windows and status completion
mean an actual early order or profit is not established. Stored market-only replay
and raw broker evidence stay outside Git. Independent profitable A/B sample size
and realized expectancy remain insufficient. C entry/exit policy and market-index
regime behavior are outside this release's new policy.

Release integration must preserve journal, approval records, account settings and
current daily risk override; require flat/no-pending/no-UNKNOWN broker comparison,
backup, hash verification and the user's direct approval for the new BUILD.
The private handoff and deployment evidence contain operational status.

## Structural B checkpoint

Branch: `fix/v1.5-structural-b-20261013`, based on `d4c0647`.
BUILD: `V1.5-risk2-watch12-bc20M-structuralB-20261013`.
Dedicated gate: `structural_pullback_enabled`, default false; the authorized
deployment explicitly enables it. Existing chase/resilient/exit-policy flags
are not implicitly enabled, and no fixed minimum stop percentage is raised.

The retained incident reused the minimum of a still-unconfirmed pullback as
support and projected the impulse beyond already known overhead. The opt-in B
gate requires a completed higher-low recovery after the last floor touch, with
a close through that floor candle's reaction high. A live uptick cannot confirm
its own candle. Recorded lower highs plus lower lows require a completed reclaim.
References are bounded, predecision observations; unknown earlier trends are
not invented. The initial floor must retain the upward impulse's base.

The support buffer is derived from the existing structural buffer, one price
tick and the actual bid/ask spread. Risk must exceed the observed median
pullback-candle range and spread plus modeled exit slippage. Insufficient
room causes rejection rather than arbitrary stop widening. Target is capped
at the nearest uncleared confirmed local high or the impulse peak above entry.
RR and cost efficiency are recalculated before account-risk sizing. Production
order boundaries recompute the normalized bar proof, stop and target ceiling,
and verify the newest receiver book; changed/stale/missing quotes cannot reuse
an older trade quote to enter.

Validation: **1,037 PASS**, zero failures/errors/skips, 127 syntax files, zero
syntax errors/network attempts/actual orders. Twelve new tests cover the
incident's normalized geometry, confirmed shallow/narrow legitimate support,
unconfirmed one-bar floors, observed execution noise, bad/future/partial bars,
proof tampering, latest-book changes, and actual synthetic broker order routing.
Existing tests were retained; release-identity expectations were updated.

Frozen replay: nineteen records on three dates, twelve with decision bars and
seven UNKNOWN. Three recorded B decisions become ineligible; no observed
winner with usable bars is newly blocked. All saved A/B/C trials were checked:
two of those decisions have no alternative ready setup; one still has a ready A.
Thus rejecting B is not claimed to eliminate every whole trade. Independent
profitable B observations remain insufficient. Waiting for a completed recovery
can miss fast V-shaped rebounds; losing an individual trade does not prove its
original support was intrinsically meaningless. Expectancy remains unproven.

Before deployment preserve the latest user-approved daily override and loss
journal, broker holdings/orders, and existing ownership protection. New BUILD
approval uses the official direct-user GUI path, never copied receipts. Detailed
trade and broker evidence, backup, deployed hashes, PID and flags stay private.

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
