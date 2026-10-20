# R6 FAST evidence and paired SHADOW protocol

Status: DEVELOPMENT / NOT DEPLOYED. No policy winner, profit claim, or order
permission is implied by this document. Reference baseline is commit
40bbfe0b8d410377666e221bcc6aa064c270852c and the integrated 20261020 release.
The operating release, settings, authorization, positions and ledgers are not
changed by this research branch. Private evidence and outputs must stay out of
Git. No unbounded tick subscription or synchronous disk write is proposed.

## 1. Freeze the policy contrast before collecting outcomes

All branches receive exactly the same causal prefix, original fill, size, costs,
structural invalidation, emergency line and account risk budget. Use the
baseline code and config hashes as identity. Historical workers with other
patches are separate cohorts, not current Policy 0 performance.

| Policy | Changed component | Components retained |
|---|---|---|
| 0 | None: current deployed FAST | Complete live structural/emergency/account/data/order guards, micro profit protection, momentum failure |
| 1 | A FAST `MOMENTUM_FAILURE_EXIT` becomes a pending observation. Freeze its floor and failed rebound high. The next causally available, full post-fill minute close at/below that frozen floor confirms failure. A close reclaiming the frozen rebound high cancels that pending hypothesis. Other closes leave it pending. | Original structural loss guard, immediate emergency/account protection, connection/data protection and existing profit lock. No 180-second timeout or minimum hold. |
| 2 | Replace only micro `PROFIT_LOCK_EXIT` with the existing confirmed higher-low-and-recovery observer. Its already confirmed line can be broken only by a subsequently known full close. Never turn an unconfirmed dip into support. | Original structural loss guard and momentum-failure rule, immediate emergency/account/data/order protections. No widening of emergency or account limits. |

Policy 1 is a specified hypothesis, not implemented or validated in LIVE.
Policy 2 can reuse `observe_fast_profit`'s structural-trail component, but needs
an adapter to keep the *shared live loss guards*. The existing B/C standalone
profit-component outputs must not be mistaken for complete policies: a shared
structural or emergency exit still wins when a profit observer says UNKNOWN or
HOLD. Micro profit-line construction and 1R thresholds remain Policy 0 facts,
not evidence that two ticks demonstrate trend failure.

Do not combine both changes for the first comparison. A combined change is a
fourth, separately preregistered experiment. None of the proposed BE+3 ticks,
180 seconds, 6-tick minimum support, ATR x3.5 target, ask+1 tick is adopted.

## 2. One episode is not one repeated ENTRY_EVAL

Introduce a bounded observation-only episode registry, isolated from live
qualification and order state. Key an episode by session/day, symbol, strategy,
frozen structure-basis hash and the initial signal event ID. Save the first
signal clock, receipt counter, original price/TTL/chase ceiling and structure.
Gate changes and repeated evaluations of that same signal append reason
transitions to its ID; they do not create new opportunities. Freeze the basis.

Close the episode on its existing TTL expiry, explicit invalidation, actual
filled exit, or observed subscription end. A subsequent fresh pattern must have
a new signal/basis ID and a parent link. Respect the existing reentry cooldown;
temporally adjacent episodes are not automatically statistically independent.
Use trade_id + opening fill to join partial sells. Missing episode lineage is
UNKNOWN; do not retroactively label sampled chart shapes as opportunities.

Use two different cohorts:

* Exit study: actual filled FAST episodes, paired same entry/time/quantity.
* Missed opportunity study: all frozen formed signals, including rejected and
  expired ones, with their original gates and then-observed price path. Add
  contemporaneous non-formed/non-rising controls by fixed rule before outcomes.

No new entry is considered executable merely because a later chart rose. It
must pass the original live gates, original signal lifetime, contemporaneous
ask/depth, integer size, available cash and concurrent account risk. A replay
without the full portfolio/order state is a component study, not a simulated
day with ten feasible trades.

## 3. Causal evidence and missingness

Order prints by receiver time/counter within the same receiver session. Exchange
HHMMSS alone cannot establish availability or independent order. Keep original
print sequence/cumulative volume. For a completed bar, use the maximum of minute
close, trigger-receipt time and consumer processing time. The historical
`confirmed_at` label held the trigger receipt; never use it alone for backdating.
Retain bar revisions with their own knowledge time and provenance; never apply
the final version to an earlier decision. A reference/REST bar is not proved
to be receiver-built simply by a legacy `reference_bars_used=false` label.

Persist event IDs and references to existing ENTRY_EVAL, PRE_INTENT/PRE_HTTP,
orders, fills and broker-cost records rather than a second conflicting ledger.
Each action needs its exact evidence: breach trade IDs, fresh bid/depth timestamp,
frozen structure, micro line, confirmed pivot/close and common guard decision.
Distinguish signal generated, candidate, qualified order, submission and fill.

Restart, disconnect, subscription gap, loss-counter increase, malformed row,
storage cap or incomplete decision tape censors the affected comparison window.
A sequence-number gap alone does not prove dropped frames: old WATCH rings
were intentionally partial. Periods with no recorded frame cannot be completed
with REST, interpolated ticks, final chart prices, or fabricated zero movement.
Show eligible and excluded episodes by reason, including those with poor
outcomes; exclude the *same* missing-data cases from both policies.

## 4. Executability and accounting

Virtual fills require the first *subsequent* actually observed sell quote after
decision + measured/configured transport delay, before the existing freshness
deadline. Validate exchange and receipt clocks, bid <= ask, venue, known depth,
available bid quantity for the whole remaining size, and connection status.
Do not fill at the decision's already consumed quote, candle high/low or target
touch. Insufficient depth/clock information yields UNKNOWN, not a fictitious fill.

Apply the same broker charge/rounding model and slippage assumption once to all
policies. Separately report zero extra slip, one executable tick adverse, and
the baseline configured slip; this is sensitivity analysis, not actual fees.
Use observed send/ack/fill delays and independently report fixed delay scenarios
when the actual distribution is unavailable. A top-of-book quote is still an
execution hypothesis, never a guaranteed broker fill. The saved actual ledger's
BROKER_QUERY fees/net remain the actual reference; do not overwrite them.

Unclosed alternatives at the last observed quote or tail limit are right-
censored. Do not force an exit, infer a future fill, or count unrealized gains
as realized expectancy. Shared emergency decisions always take priority;
if data are missing their timing/execution cannot be proved by a profit observer.

## 5. Minimal future capture candidate; not enabled

Existing capture already has a receiver hook, bounded WATCH ring and async disk
writer. Reuse it. Add no subscription and preserve the current 12 WATCH limit,
observation capacity, queue budgets, and held-position/order priority.

| Candidate file/function | Minimal prospective addition | Bound/priority |
|---|---|---|
| `automation/trade_review_capture.py:evaluation/event_ref` | Frozen first formed-signal episode; rejection stage/reason and existing event reference; trigger short tail also for a rejected formed signal | Same existing max 8 promoted symbols. Do not promote every failed evaluation. |
| separate research episode worker | Policy 0/1/2 actions and evidence, dedup and finite lifecycle | Separate thread/process; never invoke an order API or mutate live position. Overflow censors research, never trading. |
| existing capture `set_scope` | Continue post-exit tails only while receiver remains subscribed | Existing 120-second tail; unsubscribe/lost depth censors. No keepalive subscription. |
| existing receiver/consumer hooks | Join original receipt and processing IDs; record explicit loss/gap/session counters | No detector recomputation, no unbounded deep copy, no synchronous file write on order path. |

The current 45-second WATCH ring is at most 256 rows/symbol, 1 MiB total; disk
queue 4096 rows / 4 MiB; file/day/total caps 4/16/64 MiB. Keep these caps. All
new schemas and summaries must fit the same budget; no silent cap increase.
Reserve priority for actual holdings, entry basis, shared emergencies and loss
summaries. Stop low-priority rejected tails/samples first, with explicit censor
counts. Do not delete private old evidence automatically. Dedup repeated REG
acknowledgements and unchanged samples with summaries, but retain true transport
barriers and first/last timing. Existing synchronous JSON encoding also costs
CPU despite async disk writes; benchmark enqueue CPU/time separately.

The implemented development changes are intentionally narrower: correct sampled
RR field/basis, add bar knowledge clocks, provide an offline bounded inventory,
handle unknown holding durations, and suppress false no-change Focus events.
No new episode/tail collector or Policy 1 is active. Development code has no
production manifest; a research branch is not a deployment package.

## 6. Next-session measurements and decision criteria

Record by unique episode and by session segment, never raw repeated evaluations:

1. RAW -> first receiver tick -> WATCH -> first formed signal -> PRE_INTENT ->
   PRE_HTTP -> send -> ack -> fill: count, p50/p95/max delays and exact blockers.
   Separate detection lateness, queue latency, TTL expiry and unfavorable price.
2. Coverage: eligible/excluded/censored episodes, subscription gaps, receiver
   drops, writer losses/cap hits, missing depth, missing/revised bars; denominator
   includes untraded signals and controls. No completeness claim from sparse samples.
3. Paired exit actions: P0 reason, P1 pending/confirmed/reclaimed, P2 confirmed
   pivot and later close, immediate common-guard decisions and executable quotes.
4. On the common fully observed cohort: net per trade, win rate, mean win/loss,
   expectancy, holding time, observed MFE capture, MAE, gross/net giveback, one-tick
   and delay sensitivity, emergency/structural exit delay and unresolved exposure.
5. Compare receiver queue p95/max, position-protection latency, order latency,
   recorder enqueue CPU, memory, write bytes and dropped/censored samples against
   the same-load baseline. No research gain can justify degrading protection.

UNKNOWN or censored episodes cannot produce a profit winner. Twenty or thirty
trades are an initial data-quality check, not proof of positive expectancy.
Require a frozen protocol, multiple independent days/market regimes, transparent
missingness, paired out-of-sample results and adverse-execution sensitivity
before considering any separate operating candidate. This document does not
authorize that promotion or LIVE deployment.
