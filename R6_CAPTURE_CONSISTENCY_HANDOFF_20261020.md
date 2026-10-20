# R6 capture consistency candidate — 2026-10-20

This development branch starts at `5a4264cfae4cd77b02ff1efce8d45f7240001f01`,
the verified 92-file LIVE source baseline. Its parent includes the ledger-write
repair `5dc2d0d522f5e8751069067591ab1063c32bdb58`. No LIVE file, process,
account configuration, ledger, authorization or risk limit is changed here.

## Confirmed defects and changes

1. **Promotion scope mismatch.** `run()` passes the observation universe after
   removing the new WATCH set. `prewarm_handoff()` previously intersected new
   WATCH with that disjoint universe, so queued prewarm trades were discarded
   during a real promotion. It now uses the receiver's preceding routing scope
   too. It retains FIFO print history, never replays an old signal as permission.
2. **Known gaps treated as continuity.** Subscription detach and local queue
   loss now mark an entry coverage barrier. Previously confirmed history stays
   stored, but a partial resumed minute cannot validate an entry. FAST's broken
   micro history resets. Genuine quiet trading on a continuously subscribed
   stream does not trigger this barrier. Protected position/order routing stays
   first; control/position messages preempt background prewarm processing.
3. **Reference-to-signal provenance leak.** REST bars could indirectly enter
   `Detector.history` through `PriceContext` preload. Source provenance now
   survives references. Only certified receiver-built references can preload;
   REST/unknown references remain available for resistance/context, not warmup.
4. **Status admission starvation.** Fresh cache hits reuse the same original
   TTL. Actual WATCH deadlines retain first priority. Four of the existing
   admission budget slots go to older eligible waiters; the other spare slots
   keep mover priority. The 20 status and chart query budgets are not increased.
5. **Book clock lost at the decision boundary.** A parsed tick now retains its
   book's own receipt timestamp/counter. FAST and the fallback order boundary
   use them, so a new trade or wall-clock correction cannot revive an old book.
   UNKNOWN records include the exact book/price/depth cause. No TTL is increased.

Operational source files: `automation/engine.py`, `fast_momentum.py`,
`observation_worker.py`, `patterns.py`, `price_context.py`, `radar.py`,
`runner.py`, `trade_review_capture.py`; `core/market_time.py`, `realtime.py`,
`strategy.py`. An eventual payload must also include its newly sealed
`runtime/RELEASE_IDENTITY.json` (12 files total). Do not copy the full branch.

## Evidence and boundaries

The independently extracted half-open 11:00–11:30 KST window contains 171 RAW
symbols, 40 evaluated symbols, 1,256 ENTRY_EVAL records and zero recorded signals.
FAST has 732 REJECT and 524 UNKNOWN records. Source-count-rule exclusions are
zero; 30 different single-source symbols reached WATCH. STATUS_SCAN_LIMIT affects
126 symbols at least once, not six across the whole window; eight have no other
status evidence in the retained cycle summaries. These categories overlap.

A/B/C formed frames are 39/10/168, involving 15/6/30 symbols. Under the explicit
consecutive-formed hypothesis grouping these are 20/6/48 episodes, **not proof
of independent profitable opportunities**. Every formed A frame fails reward
and costs; every formed B frame has not rebroken out. No single-gate ablation,
nor reducing the recorded turnover floor to 5/10/20/30 million while keeping
other gates, produces a passing formed frame. This does not prove zero market
opportunities; unobserved/missing data prevents that conclusion.

`research/r6_capture_audit.py` performs the bounded, reproducible intersections.
`research/r6_capture_replay.py` separates actual-tape component replay from causal
synthetic counterexamples. Actual tape contains 102,997 archived SHADOW trades,
not a complete WATCH/order tape. Missing original BOOK events and receipt
counters are UNKNOWN, never invented. Neither replay submits orders.

`research/r6_entry_experiment.py` is an **offline-only** A/C confirmed-range
target hypothesis. It does not relax liquidity, spread, warmup, structure,
invalidation, chase, final net RR or costs. It requires actual received bars and
their availability times; missing provenance produces UNKNOWN. It is not
imported into the LIVE entry path and is not a validated strategy.

Private original inputs/results are outside Git at
`C:\Users\MJS\Documents\주식자동매매_개발\r6-capture-consistency-20261020-private`.
They must not be added to this repository. The original 11:00 window and tape
hashes, before/after JSON, full test logs, sealed payload and apply/rollback
instructions are kept there. The final private validation report is authoritative
for final test totals and deployment readiness; preliminary test runs are not.

## Test-contract changes requiring review

- The existing queue-loss regression now separately asserts retained history
  and **unusable** entry bars after a known gap. Retention is not continuity.
- Two positive preload fixtures explicitly identify their synthetic bars as
  ACTUAL_RECEIVED. New negative tests reject REST and missing provenance.
- Radar mock fixtures now provide the actual affordability/lock interfaces;
  cache reuse is tested ahead of new HTTP, without consuming query budget.
- No negative poor-RR, wide-spread, emergency-stop or order-authorization
  expected result is changed to permit a trade.

## Reproduce (offline development directory only)

```powershell
& 'C:\Users\MJS\Documents\PythonEnvs\stock-trading-bot\Scripts\python.exe' -X utf8 -B -m unittest tests.test_capture_consistency_20261020 tests.test_entry_experiment_20261020
```

Do not deploy or restart LIVE from this document. User deployment approval,
fresh broker/ledger reconciliation, an empty order/position workload, file hashes,
rollback backup and a separate LIVE trading approval remain required. Never
copy config, policy, authorization receipts, `.env`, account files or state.
