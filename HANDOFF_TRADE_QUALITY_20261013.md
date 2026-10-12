# Trade quality handoff — 2026-10-13

Status: development changes complete; integration and deployment are pending.
Base: `7693c563d1d42d4c4467f17a85564743df0c4824`.
Branch: `fix/trade-quality-night-20261013`.
Source commit: `df56fc284ea33bef5eebf9195db02d28406a9a78`.

## Ownership and operating boundary

This branch owns trading quality only. Antigravity owns the baseline 949-test
inventory/deduplication/performance and storage audit/cleanup. Neither of those
two tasks was continued after the ownership change. Existing audit drafts stay
in their original development workspace. No operating credentials, account,
orders or runtime were used after the scope change. There were no real orders,
operating restarts, deployments or approval-record mutations.

## Changes from the base

- `automation/trade_quality.py`: reject malformed/partial/stale minute sequences.
  C declining endpoints still reject a mere live breakout. A completed higher-low
  recovery that closes over the intervening reaction high can qualify, subject
  to the unchanged geometry, breakout, chase, RR, costs and risk checks. Raising
  a structural profit stop requires a current complete sequence; the original
  invalidation and emergency exits remain active.
- `automation/price_context.py` and `automation/entry_context_audit.py`: retire
  a confirmed peak only after a later valid completed close above it; retain the
  clearance evidence. A live wick cannot clear it, and an older role-handoff copy
  cannot restore it. A completed close received during a role handoff also clears
  an older destination peak. Historical snapshots before clearance retain it.
  A newly confirmed peak remains a potential resistance.
- `automation/engine.py`: verify the claimed resistance/target proof again at
  the order boundary; record the original exit-trigger evidence separately from
  later receiver/health updates. Eligibility timing requires nonzero risk-sized
  quantity and passing engine safety gates.
- `automation/position_health.py`: reject regressing REST receipts; invalidate
  an old standing-book cross-check when a later receiver book conflicts. A stale
  conflicting quote requests verification rather than immediately submitting a
  sale. Preserve the blind-interval start through journal/object reconstruction.
  Persist interval start and resolution once each so cleared deadlines cannot
  revive after reconstruction. Fresh validated trades also resolve the interval
  through the runner, including when the entry detector rejects the observation.
  Stale/nonregular/future/invalid trades cannot resolve it. Probe evidence separates
  market quote time, HTTP receipt time and main-thread processing time.
- `automation/entry_timing.py` and `automation/runner.py`: separate first known
  RAW/WATCH, observation, detector signal, engine eligibility and latest radar
  refresh. Never substitute a refresh timestamp for first discovery. Missing
  earlier process history is explicitly UNKNOWN; bounded to 512 symbols/day.
- `tools/trade_quality_replay.py`: explicit frozen-input offline replay only.
  Does not read operating config/policy/runtime or connect to a broker. Raw
  outputs belong outside Git. Future, previous-day and naive RAW/WATCH/observation
  timestamps are UNKNOWN; they cannot become first-discovery evidence.
- `tests/test_trade_quality_night.py`: dedicated new tests only. Existing test
  files and shared pytest configuration have not been edited.

The BUILD/strategy constants and feature-flag defaults were deliberately left
at the base values. This is source awaiting integration, not a new deployable
release. Never deploy this branch under the old BUILD or reuse old approval.

## Frozen replay and limits

Eight retained trades were compared using only known predecision snapshots and
already captured timestamped resistance references. The candidate source may
have been visible preopen; that does not establish uninterrupted intraday WATCH
observation. Detector signal availability is distinct from account/order safety
eligibility. The first fully executable opportunity is UNKNOWN where its full
guard evidence was not retained.

Combined price gates block six cases: four historical losses and two historical
winners. The count is unchanged from the base experiment. Two remaining losses
still pass entry price gates. The revision removes demonstrably already-cleared
resistance and preserves completed reversal bases, but no additional historical
trade in this eight-case sample is thereby proved executable. No future outcome
was used to exempt a winning trade from an unbroken resistance. This sample does
not establish improved expected value.

The eight entry snapshots cannot reproduce complete post-entry minute bars,
receiver messages and REST receipt times. Revised exit prices/P&L remain UNKNOWN.
One quiet-book incident's final health row came after its exit latch, so it must
not be used to infer the original cause. A probe processing time is not an HTTP
receipt time. Functional quiet-book protection passes synthetic broker tests;
the historical transaction cannot be claimed prevented from missing evidence.

The inherited confirmed-structure policy gives a normal profitable pullback
more room than the MFE giveback stop. It can give back more profit or retain the
initial stop until a valid profitable higher low forms. Do not call that a proven
better exit policy. Market-regime filtering, larger universes and earlier entry
thresholds were not added or weakened.

## Targeted validation

- New dedicated tests: 42 PASS, 0 FAIL/ERROR, no socket attempts or real orders.
- Related targeted safety/lifecycle/continuity suites: 134 PASS, 0 FAIL/ERROR.
- Baseline quality/context suites: 42 cases, 37 PASS, 5 conflicts below.
- Changed Python syntax: 10 files parsed. No baseline 949-case/full integrated
  regression was run; that belongs after the two branches merge.

Do not describe the five conflicts as passing or delete them merely to turn the
suite green. Their current expectations are incompatible with the new explicit
freshness/minute-boundary requirement. The dedicated file proves current-bar
runner delivery, stop persistence and mock restart; it also retains stale-bar
rejection as a separate boundary.

A separate reviewer reproduced four final boundary defects (handoff clearance,
persisted quote/trade recheck resolution and future discovery timestamps). All were fixed,
with six new regression cases included in the 42-case dedicated suite. The
related 134 checks were rerun after these fixes. This is targeted validation,
not a substitute for integrated regression or proof of trading profitability.
Final read-only review found no additional blocking defect in these corrected
boundaries; the reviewer did not independently rerun the test suite. The 42-case
baseline comparison was also rerun on the final source: its five documented
conflicts remain, with no additional failures.

### Test conflicts for the integration owner

Four `test_trade_quality.StructureProtectionTests` cases use 13:33–13:35 bars at
13:37:34 and expect a stop raise despite no 13:36 complete bar:

1. `test_completed_bars_and_raised_stop_persist_in_existing_journal`
2. `test_confirmed_post_entry_higher_low_raises_and_real_breach_exits`
3. `test_real_runner_delivers_completed_bars_before_tick_protection`
4. `test_restart_with_same_approved_settings_preserves_structural_stop`

`test_price_context.ContextOrderTests.test_context_rr_blocks_real_pattern_before_fake_order`
creates bars starting at arbitrary `:05` seconds. Such rows no longer enter the
minute reference store. Preserve the blocked-order scenario using minute-aligned
inputs, and keep an explicit malformed timestamp rejection case.

The dedicated new file reuses baseline fixture APIs from `test_setup_acceleration`,
`test_standing_book_confirmation`, `test_trade_quality` and `test_price_context`.
If Antigravity centralizes/removes those fixtures, adapt these imports in the
integration branch rather than silently dropping the new tests.

## Integration and resume

1. Merge/cherry-pick this strategy branch with the independent test/storage work.
   Resolve the five documented fixture expectations intentionally. Preserve all
   safety and order-lifecycle assertions.
2. Run the integrated regression and sequential mock-broker trade scenarios.
   Verify HTTP receipt evidence against actual response adapters in an authorized
   read-only smoke. Do not test real orders.
3. Assign a distinct BUILD/strategy identity and release manifest. Feature flags
   are still OFF by default: `entry_context_enabled`, `confirmed_box_direction`,
   `confirmed_structure_protection`, `stable_position_book_confirmation`.
   Decide their operating activation using the documented tradeoffs and fresh
   evidence, not eight-case hindsight or this branch's commit status.
4. Only after checks and the user's deployment approval, back up the operating
   source and reconcile positions/orders/UNKNOWN. New LIVE authorization must be
   given by the user through the GUI; never copy an older approval receipt.
5. For additional replay, explicitly supply the frozen evidence folder:
   `python -B tools/trade_quality_replay.py --evidence-dir FROZEN_DIR --output-dir PRIVATE_DIR`.
   The four required filenames are in the tool docstring. Private handoff records
   exact local paths, result logs and evidence hashes. Do not recapture runtime
   under the current instruction.

Remaining substantive work: verify observation coverage before the initial surge,
measure first signal blocked by stale status versus by held-position/cash/risk,
and evaluate candidate diversity/trend-aware entries on a larger point-in-time
sample. The base already prioritizes WATCH status refresh; its latency is not
re-proved here against a live API. Earlier profitable fills are not promised.
