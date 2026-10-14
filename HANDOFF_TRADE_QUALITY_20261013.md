# V1.5 opening-response checkpoint (2026-10-14)

BUILD: `V1.5-risk2-watch12-bc20M-opening-r3-20261014`.
Base: `536f685`; branch `fix/v1.5-night-quality-20261014`.
Entry strategy version remains `V1.5-structural-continuation-20261014`.
Operating exit policy remains AE3. No entry threshold, structural invalidation,
position sizing, daily budget, LIVE receipt or final HTTP guard is relaxed.

## Actual discovery change

The existing 08:00 preopen control window, regular-only detector ingestion,
09:00 immediate radar/account request, 60-second radar cycle, bounded SHADOW
observations, fast FOCUS notifications and bidirectional observation handoff
already exist. They are reused. The confirmed delay is in batch publication:
newly verified RAW candidates cannot enter the main FOCUS pool until every
unrelated status/history query in the compression batch finishes. A diagnostic
readiness notification does not repair a candidate absent from that main pool.

Only during 09:00-09:10, the radar now publishes cumulative verified candidates
after their existing compression checks finish. Main FOCUS uses exactly the
existing tiers, score hysteresis, source fallback and WATCH capacity. A partial
batch preserves unprocessed incumbents; UNKNOWN, REJECT, unaffordable, protected
or RAW candidates never gain WATCH permission. The full final packet still owns
the complete scan result. Fresh received candles, liquidity, support/overhead,
reward/risk, account risk, authorization and execution rechecks remain required.

The two-packet radar channel coalesces progress at at most four packets/second;
the 60-second ranking interval and 20-status-query budget do not change. A safe
nonblocking queue operation also handles concurrent main-thread draining without
manufacturing RADAR_UNAVAILABLE. No extra rank/status/history API request is
made by progress delivery. Local synthetic profiling of 300 candidates measured
2.76 ms mean FOCUS processing; 32 timing updates measured 1.45 ms mean. These are
CPU measurements, not live network or market latency guarantees. A mover first
appearing between scans still has up to a 60-second phase wait plus query time.

## Point-in-time comparison and limits

The same source-only replay ran against actual R2 source and the improved source,
using genuine synthetic receiver ticks and a guarded fake broker. The modeled
batch collects rankings by second 2, verifies the new candidate by second 3 and
finishes unrelated compression by second 16. A blocked metadata worker is an
explicit stress assumption, not a claim about today's market.

| Mock snapshot | R2 unwarmed candidate | Improved unwarmed candidate |
| --- | --- | --- |
| 09:00 | Not yet discovered | Not yet discovered |
| 09:01 | WATCH since 09:00:16; zero usable bars | WATCH since 09:00:03; one usable bar |
| 09:03 | Two usable bars; no order | Three usable bars; mock order at 09:02:05 |
| 09:05 | Four usable bars; no order | Five usable bars; the same single mock order |

A preselected candidate in both versions receives the first open tick, completes
its first candle at 09:01 and produces the same fake-broker order at 09:02:05.
Already successful preparation is preserved. Actual configured C-base/FOCUS/
structural flags were separately tested with normal A/B/C receiver cases. No
premarket, indicative, future, incomplete or REST-restored signal candle is used.
Synthetic symbols are not real winning stocks; actual earlier opportunity rate
and profitability are unproven.

## Chronology

The day-specific diagnostic timeline now separates subscription request/group
ACK, trade time, receiver time, consumer time, execution-fresh tick, completed
candle start and when that candle became known, first signal and local risk
eligibility. Group REG ACK is not falsely labeled a per-symbol ACK. SHADOW
observation before WATCH is explicit; missing or nonordered delays stay null.
Historical backlog does not become a current execution-fresh quote. Opening
stage events contain timestamps and small evidence, never order permission.
The persisted timeline restores chronology only, never warmup, ticks or bars.

## Opening exit comparison: observation only

The separate OPENING_FAILED_REBOUND_OBSERVATION hypothesis requires an opening
REGULAR A entry, gain below one price-risk unit, a completed weak long-wick burst,
a subsequent completed lower rebound and a close below that retained floor. A
single wick or initial pullback is insufficient. Incomplete entry minutes, gaps,
missing volume, future bars and a peak outside the complete sequence stay UNKNOWN.
Fresh bid, full-quantity depth, costs and the same estimated slippage are required
to compute hypothetical net proceeds. This is not a live exit instruction.

In a synthetic three-share failure, the candidate's 09:04 modeled net is +50.74,
while unchanged AE3 requests exit on the broken completed close at 09:06 with
modeled net -817.39. Another failure is already cost-negative at the earlier
decision (-188.74). A counterexample has exactly the same 09:04 evidence but
then recovers: early exit would keep +50.74 while the unchanged AE3 horizon bid
mark is +829.06. That mark is not a realized fill. The two paths cannot be
distinguished using their identical 09:04 history. No profitability claim or
operating AE3 activation is justified. Production quote records currently lack
full bid depth in some paths; such observations leave executable net UNKNOWN.

## Verification and operational boundary

Full regression: 1,234 PASS, including all 1,212 R2 cases and 22 new opening cases;
zero FAIL/ERROR/SKIP, syntax errors, network attempts or real orders. The original
1,203 cases are retained. Operating flags, budgets, journals, protected holdings
and receipts must match their preflight hashes. Source-only deployment requires
STOP/OFF, a backup and hash verification. A new direct user LIVE approval is
required for this BUILD; no previous receipt is copied and no worker is started.
Fresh broker/WS/open-market readiness remains a user's approved preflight task.

---

# Preserved V1.5 preopen continuation-proof checkpoint (2026-10-14)

BUILD: `V1.5-risk2-watch12-bc20M-nightquality-r2-20261014`.
Base: `c6450f1`; same branch `fix/v1.5-night-quality-20261014`.
Strategy version and thresholds remain `V1.5-structural-continuation-20261014`.

The requested preopen audit reproduced a final-order source mismatch. Local
confirmed continuation is qualified from received decision candles, but the
engine recomputed it from reference candles. A benign same-time REST/live volume
or OHLC revision refused a valid local signal. Conversely, missing or incomplete
decision snapshots could pass when reference candles remained valid.

The minimal engine correction selects the same source as the detector: received
`decision_bar_snapshot` for `LOCAL_CONFIRMED_CONTINUATION`, reference snapshots for
the existing reference-qualified continuation mode. Identical continuation proof
comparison remains mandatory. Structural support/stop/ceiling evidence still uses
the original references; stale/missing structure, future receipt, incomplete live
signal, stale executable quote, insufficient RR and a target above the known
session ceiling remain rejected. No strategy, threshold, exit, risk or account
policy is relaxed.

Nine dedicated cases cover matching sources, independent volume/OHLC revisions
through the actual receiver and fake broker, missing/stale structure, old REST
metadata with fresh complete bars, incomplete received candles, future receipt,
stale live quotes, and session-ceiling/RR rejection at the receiver/order boundary.
Failure-before evidence is private: all nine run against c6450f1 reproduce the
three expected source-parity failures. Final full regression: 1,212 PASS, zero
FAIL/ERROR/SKIP/network attempts, including the original 1,203 plus nine new cases.

The invalid-source acceptance is a safety defect, so the correction has a distinct
R2 BUILD for the user's next direct approval. Preserve all protected operating
files and remain STOP/OFF. Deployment/hash/Git verification is recorded privately;
this checkpoint does not grant LIVE approval or claim fresh broker readiness.

---

# Preserved V1.5 overnight judgment checkpoint (2026-10-14)

BUILD: `V1.5-risk2-watch12-bc20M-nightquality-r1-20261014`.
Branch: `fix/v1.5-night-quality-20261014`; base `d602fc8`.
Strategy identity: `V1.5-structural-continuation-20261014`. Operating exit policy
remains E/AE3; A-D hypotheses remain observation only.

## Confirmed entry decision change

The three-completed-bar rising continuation check already existed, but its caller
only generated Setup A above the observed session high. A sustained new local
rise below an older high could never reach that check. With the existing structural
A flag ON, the detector now qualifies three genuinely received completed candles
with rising lows/closes, the existing minimum gain and nondecreasing positive
volume, then requires a breakout of the recent complete reference range. The
range uses the existing twelve-candle pattern horizon. Prior references bound
support/overhead; they never manufacture received warmup or a live signal.

All existing structural support confirmation, noise/spread emergency allowance,
known resistance, net reward/risk, daily structure, liquidity, freshness and final
order/risk sizing checks remain required. No fixed-percent stop was widened or
narrowed. A known nearby ceiling, weaker rebound, flat lows, incomplete/future
history, insufficient turnover or stale execution quote remains blocked.
An earlier observed session high also bounds the new local target even when it
is outside the reference window. With entry 10,040 and known high 10,060, the
pre-fix target 10,330 inflated RR to 1.5263 and allowed entry. The corrected target
is capped at 10,060, RR 0.1053, and the existing reward/risk check refuses entry.
A distant old high at 11,000 preserves the valid local-rise example below.
This is a known price ceiling, not an invented confirmed pivot or widened stop.
The new local entry is explicitly sustained continuation and does not inherit
the AFTER burst-failure policy merely because its short acceleration also passes.

An identical controlled receiver stream produces no baseline buy candidate and
one new fake-broker buy at 10,040, local breakout 10,030, structural line 9,870,
emergency stop 9,850 and target 10,330 (price RR 1.5263). With approved capital
81,300 and the existing risk rules it sizes one share. The real receiver/detector/
engine/mock-order/fill/plan path holds a soft touch, then exits a confirmed broken
close. This demonstrates a changed judgment and invoked integration, not a real
historical fill, earlier discovery or measured profitability.

## Historical checks and evidence limits

Frozen evidence has 22 trade fragments; 14 retain entry candles, eight do not.
No complete historical received stream including earlier quotes/warmup is
available. On the stored price geometry, baseline and new gates are identical:
three recorded positive entries remain price-qualified and one recorded losing
entry remains qualified. The earlier declining/reused C-box and unconfirmed B
support/overhead rejections remain in force. Those were already fixed in d602fc8,
not newly prevented by this patch. Missing earlier A references cannot prove a
named historical losing trade would be prevented. No saved snapshot qualifies the
new three-candle continuation route. Earlier actual opportunities, missed-winner
rate, and expectancy therefore remain UNKNOWN.

Post-entry review preserves the completed-OHLCV receiver wiring, confirmed-close
versus separate emergency stop, observed higher-low protection and AFTER failed
rebound checks. The stored Exgate exit was triggered by global disconnect/consumer
lag; the original socket cause is unresolved. The later Raon reference suggests a
failed-rebound signal at 16:59, but it is counterfactual and cannot establish an
alternative executable fill. Their observed sub-1R gains do not justify enabling
an arbitrary breakeven or fixed-profit exit. No exit trading rule changed tonight.

## Evidence corrections

Trade lineage now merges valid same-day persisted RAW/WATCH/first-signal evidence
with current-process observation. Restart/rejoin can no longer replace known day
discovery chronology in the trade while the GUI retains the older time. This does
not restore ticks, candle warmup, risk eligibility or approval state.

The offline exit comparator now labels mixed older-build and manual outcomes as
historical outcomes. A manual close or missing AE3 execution proof is not counted
as an actual AE3 trade. No imaginary A-D fills or net profits are created. Current
AE3 superiority/expectancy and alternative early-winner retention remain unproved.

## Verification and operating boundary

Seventeen dedicated cases exercise changed qualification, normal counterexamples,
81,300-capital receiver/order/fill/close handling, strict freshness, persisted day
chronology and policy attribution. Final integrated regression: 1,203 PASS, zero
FAIL/ERROR/SKIP, 142 valid syntax files and zero network attempts. The three prior identity assertions
only change to the new release identity; no test is removed or reorganized.

Production must remain STOP, Worker stopped, order permission OFF, with no BOT,
pending or UNKNOWN orders before applying source. Preserve config, approvals,
protected holdings, day override and loss ledger byte-for-byte. Cached cash 21,675
is an old broker snapshot marked not fresh, not a confirmed sale-credit omission.
Do not restore LIVE from that cash value. PermissionError locking process remains
unidentified; existing bounded atomic-write/shared-read handling is preserved.

Remote publication, source backup/deployment/hash proof and the fresh STOP/OFF GUI
check are recorded separately in the private release artifacts after this source
commit. Private replay inputs, reports and captures stay outside Git. User approval
of the new BUILD is required before LIVE; this checkpoint does not grant it.

---

# Preserved V1.5 exit GUI follow-up checkpoint

BUILD: `V1.5-risk2-watch12-bc20M-exitgui-20261013`.
Branch: `fix/v1.5-exit-plan-20261013`; preserves `dbea214` and `9e7217b`.
Strategy identity and trading rules remain `V1.5-structural-exitplan-20261013` / AE3.

The position panel now separates summary, active-condition table and detailed
evidence/history. It shows immutable signal target, current recorded target and
change reason, exact engine initial-R activation, actual profitable protection,
explicit unarmed status, completed-close versus immediate emergency, legacy C
trailing/giveback, AFTER readiness/failure evidence, deadlines and actual orders.
Target touch is a record/observation comparison, not an automatic sell or target
revision. Missing evidence remains UNKNOWN. A-D observers still cannot order.

The pure engine projection is the price/condition source. GUI/service code formats
it without recomputing targets, protection or trading decisions. Snapshot combines
the latest saved order/latch state even when the last projected price expired and
excludes protected/external holdings from BOT plans. Fractional-fill activation
uses actual `initial_r`; presentation history is bounded without trimming ledger
history. No configuration, approval, journal or risk-setting change is required.

Thirty new tests in `tests/test_exit_plan_display.py` cover target immutability,
original versus cost-modeled RR, 1R without profitable support, A/B and C paths,
AFTER scope/freshness, target touch without sell, stale partial orders and user
exits, read-only real service flow, protected ownership and widget selection.
Final offline regression: 1,186 PASS, zero FAIL/ERROR/SKIP/network attempts.
Historical replay/UI examples stay outside Git. They do not inject positions into
an operating journal or imply alternative fills/profits. Profitability and the
benefit of observer policies remain unproved.

Deployment scope is engine-owned display projection, order-state projection,
GUI/service and BUILD identity. Worker remains STOP, no new LIVE approval or order.
Before a future LIVE start, the user must approve the new BUILD through the GUI.

---

# Preserved previous exit plan and completed-bar wiring checkpoint

BUILD: `V1.5-risk2-watch12-bc20M-exitplan-20261013`, based on preserved AE3 `718a7ae`.
The operating policy remains AE3. Targets are entry/reassessment hypotheses, not
resting sell orders or automatic profit-taking instructions.

## Actual defects and fixes

The real receiver checked only `confirmed_structure_protection` before supplying
protection bars. The installed AE3 setting had that flag OFF while completed-close
invalidation and AFTER failed-rebound protection were ON. It also supplied the
legacy `completed` high/low buffer rather than PatternDetector's genuine completed
OHLCV history. Direct method tests did not expose either wiring defect.

The corrected receiver delivers only usable, actually observed, completed post-fill
OHLCV bars. A fresh decision cannot use delayed/future/partial candles. The complete
receiver-to-mock-broker path now confirms both close invalidation with the old flag
OFF and the AFTER failed rebound below one-R. Quote-only protection now uses the
same separately risk-sized emergency line as the tick/close policy; touching a
nearer soft line with a book alone cannot substitute for close confirmation.

The engine creates an exit plan before an order, rebases it to actual average fills,
and stores the active lines, activation/one-R prices, highest observed trade, MFE,
conditional failures, deadline, fresh-bid estimated net PnL and order safety state.
The GUI reads that state, distinguishes hypotheses from triggers, expires stale
prices and never presents an old decision-log price as a fresh executable quote.
The plan panel is read-only. User/deadline exits do not assert trend failure; a stale display preserves a latched exit and its reason. Completed fills preserve their plans and diagnostics;
realized net PnL continues to use the broker-authoritative AE3 accounting.

## Hypotheses and evidence limits

A: target full exit. B: integer partial exit and remaining structure tracking.
C: target reassessment with confirmed structure. D: higher-low trailing before
one-R. E: current AE3. A-D are observation/replay only: no broker order path exists.
One share cannot be split; two/three shares are diagnosed as 1+1 or 1+2. Actual
fixed/minimum cost and executable alternative fills remain unknown.

Frozen evidence covers 22 trades on four dates. Six have broker-confirmed costs,
with six recorded positive trades across the complete mixed-cost sample. No initial
target was reached by the saved observed high; incomplete price coverage prevents
claiming the target never traded. Only two later-received reference timelines are
available, both explicitly counterfactual. The AFTER reference failure first appears
at 16:59; exact executable alternative prices/profits are unknown. No new policy's
expectancy or historical winner-retention rate is established.

The optional cost-inclusive RR guard refuses 21/22 historical entries, including
all six recorded winners under the current-cost counterfactual. It remains OFF.
Tests also show a before-one-R pivot break followed by normal strong recovery:
early structure trailing is therefore not enabled just because it is implementable.

## Verification and operating handoff

Release regression: 1,156 PASS; FAIL/ERROR/SKIP 0; 140 Python syntax files valid;
network attempts 0; actual orders 0; operating program starts 0. The 45 dedicated
cases include genuine receiver wiring, stable/old quotes, accounting provenance,
normal recovery counterexamples, quantity constraints and six real-widget checks.
The old mocked runner fixture was changed to use the actual OHLCV getter; it no
longer assumes the legacy high/low container has a close. No test was removed.

Before application require stopped/flat/no-pending/no-UNKNOWN state, protected
holding comparison, immutable ledger/config/approval hashes, source backup and
baseline comparison. Preserve all risk budgets and user receipts. Apply only the
reviewed source delta and verify file hashes and effective flags in a fresh GUI.
Do not approve LIVE, start a worker, copy a receipt or reset a journal. The direct
new-BUILD LIVE approval remains the user's action. Runtime evidence/backups stay
outside Git. File-lock PermissionError root cause remains a separate unresolved
operating limitation; passing offline tests does not prove its absence in LIVE.

---

# Broker PnL settlement checkpoint

BUILD `V1.5-risk2-watch12-bc20M-structuralAE3-20261013` preserves structuralAE2's entry, AFTER failure and explicit external-close recovery changes. Its new accounting path makes reported broker fees and taxes authoritative after a confirmed matching fill. Entry-risk reservations still use modeled costs until actual charges exist.

The user's final cost directive was prompted by Raon 3 shares at 17,460 then 17,500. The configured 0.015% buy/sell commission contributed 7.857 + 7.875 = 15.732 modeled fees. The broker reported both commissions zero and sell tax 105: gross120 minus105 yields **+15**, replacing modeled **-0.732**. Tax and commission are read, not recomputed from a guessed rounding rule.

`automation/broker_pnl.py` validates account/day, order identity uniqueness, side/symbol, filled quantity and amount. Explicit zero is valid; missing, nonfinite, negative, duplicated or mismatched charges remain unconfirmed. Confirmed prior cost provenance is preserved if a later query omits a fee. Partial-order growth only replaces the newly booked modeled increment; repeat queries cannot double-credit cash. Trades link their exit order directly, with a unique fill-time fallback for old records. Partial closes allocate known charges without duplicating entry fees. Historical modeled PnL is retained for audit while compatible realized-PnL fields and performance cost totals use broker costs.

The original six Oct13 closes were compared to the 19:59 broker query: realized results -1,086 / -676 / -169 / -574 / -1,114 / +15. Gross losing-trade accumulation becomes **3,619**, and net day result **-3,604**. The original absolute risk ceiling **5,222.7945** is unchanged. This is a documented fee correction, not a risk-budget reset; protected holdings, entry counters and approval/config remain intact. Other days without a matching dated broker record are not invented as settled.

13 dedicated accounting cases cover zero costs, tax precedence, subsequent broker revisions, repeat-query idempotence, partial quantities, missing/invalid/duplicate fields, identity/day mismatch, old exit matching and actual performance cost labels. Full release results are saved in the private regression report. No actual test orders are permitted. Direct user approval of the new BUILD remains required; outside operating hours the program must wait for the next supported session.

Official field identifiers are confirmed in the [Kiwoom REST API guide](https://openapi.kiwoom.com/m/guide/apiguide?dummyVal=0); the monetary comparisons are the user's actual broker query, not a universal brokerage-fee claim.

---

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
# Oct 14 opening R4: live observation and Setup C repair

Base: opening R3 commit `2574146`. Work branch: `fix/v1.5-opening-live-20261014`.
BUILD: `V1.5-risk2-watch12-bc20M-opening-r4-20261014`.

The live opening path was connected and order-enabled, but candidate churn repeatedly
discarded actual observation. Consumer-time validation also rejected healthy receiver-time
history in the SHADOW and prewarm routes. WATCH evaluations were missing from FOCUS
readiness because WATCH ticks are routed to main, while selection read only SHADOW results.
Radar separately recalculated a compact two-gate record and lost its full B/C tier.

The repair preserves genuine receiver-time candles without publishing stale signals;
merges fresh full main evaluations into ranking; preserves ranked subscription order;
and treats incomplete warmup as UNKNOWN. UNKNOWN and measured weak candidates compete
on the existing discovery score and incumbent hysteresis. Actual momentum/geometry/signal
readiness retains priority. Status, affordability, source qualification, entry gates and
the final executable quote/order guard remain required. No faster API polling was added.

Setup C now obeys the already enabled confirmed-invalidation contract. Its base must
have a completed support confirmation. A completed expanding impulse cannot supply the
missing fifth consolidation bar. Structural price is buffered by actual spread/tick,
with a separate emergency line derived from observed base ranges. Reward/risk and integer
quantity use that emergency line; missing support or insufficient room refuses entry.
Known overhead caps the target. The final order boundary reproduces the same candle proof,
ceiling and stop. This uses no fixed percentage widening, no future candles and no new
AE3 profit rule. The core emergency and completed-close exit paths are reused.

The position cockpit uses a scrollable detail body and a stable single-row table height,
so an opening position cannot force the table and detail labels to paint over each other.
Completed sells with unconfirmed charges now receive bounded broker settlement reads,
including at restart. These run at the existing radar interval when no order is pending;
active-order reconciliation retains its existing frequency. Missing charges remain UNKNOWN.

Validation: 25 dedicated cases plus the existing 1,234 regressions, 1,259 total. The cases
cover the actual pre-entry C candle counterexample, a normal valid C trade, full mock order
proof, structural touch vs complete close, immediate emergency, stale data, queue history,
ranking, final quote refusal and actual GUI geometry. Two BUILD identity expectations
and one outdated SHADOW-history expectation were corrected without deleting tests.

Limits: no complete receiver tape exists for every missed opening candidate. Saved pool
re-evaluation is not a new subscription/BUY replay and cannot establish reduced churn,
early executable entries or improved profitability. Actual post-approval tick freshness,
candidate residence, complete-bar coverage and useful signal latency still need measurement.
Private live logs, account snapshots, receipts, GUI captures and deployment backups stay
outside Git. LIVE was stopped through the official STOP path; no account-risk reset or
automatic LIVE approval is authorized. Deploy source only after regression and preservation
checks. A changed BUILD requires the user's direct GUI approval before LIVE can resume.

## 2026-10-14 R5: verified observation continuity and radar recovery

BUILD: `V1.5-risk2-watch12-bc20M-observation-r5-20261014`.
The isolated development branch incorporates the tested selection/high-reference
changes from local `e0d2f7a`; those source changes were absent from the running R4.
Its unvalidated default pullback-volume relaxation is excluded: 0.75 is preserved.

ObservationRetention and ShadowObserver share the already-defined residence needed
for the longest completed-bar pattern, including its initial partial minute.
Demoted WATCH observation survives a one-cycle ranking change within the existing
subscription footprint. Critical positions/orders retain priority. Retained
observations do not acquire entry permission. A genuine unsubscribe/data gap still
resets history; no missing or historical reference bar is converted to a received
complete signal bar.

A failed radar refresh keeps only a previously verified, same-day regular-session
snapshot inside the existing three-scan qualification TTL. Errors do not refresh
its timestamp. First-cycle, preopen, future and expired snapshots still block.
Per-symbol source/status, fresh ticks/books, pattern, resistance, risk/reward,
quantity, account, HALT and approval checks remain in the real order path.

Malformed ranking identities are rejected per row with an API/ordinal reason.
Valid rows retain their original rank and official source identity; unknown
exchange suffixes are never normalized into a tradable identity. Whole source
response failures remain errors. Worker errors now include stage, exact exception
message and trace instead of only the class name. The old ValueError records do
not establish its original underlying cause.

Validation: 158 targeted cases; all 1,280 regressions, including all 1,259 R4
cases, six inherited selection tests and 15 dedicated continuity/cache/parser
cases. Network attempts zero. Received-bar role-turnover and real-gap resets
verified; no real orders or live approval during tests. Actual candidate-pool
counterfactuals are independent decisions with past information, not a complete
tick/subscription replay or evidence of profitable execution.

AE3, structural invalidation, account risk parameters, order lifecycle, runtime
journals, broker settlement, manual-protection records and approvals are preserved.
Release backup/hash verification precedes source deployment. A new BUILD requires
the user to approve and start through the GUI. Never reuse a receipt or auto-start.
