# R6 evidence research handoff — 2026-10-20

Branch: `research/r6-evidence-shadow-20261020`.
Base: `40bbfe0b8d410377666e221bcc6aa064c270852c`.
Scope: offline evidence audit and recording/reporting fixes, not a LIVE release.
No broker queries, orders, live source replacement, restart, policy/ledger changes,
new trading rules, threshold changes or GitHub push are part of this work.

Read `audit/r6_evidence_shadow_20261020/SHADOW_PROTOCOL.md` for the policy contrasts,
episode definition, causal clocks, missingness, executable-quote model and budgets.
Policy 1 and the new prospective episode/tail collector are designs, not deployed
implementations. Existing profit observers are component hypotheses, not full
account/order simulators or proof of expectancy.

Changed production-module source in this **research copy only**:

* `automation/trade_review_capture.py`: preserve actual ABC `calculated_rr` / FAST
  `net_rr` with their basis. Add closed/receipt/processing knowledge clocks while
  retaining the historical field. No order, detector or subscription change.
* `automation/performance.py`: unknown/non-finite holding durations do not crash
  summary, count their missingness instead of inventing zero or a reconcile time.
* `automation/runner.py`: unchanged opening qualified-progress selections do not
  emit false WATCH-change events. Selection, subscriptions and trading unchanged.

New offline tool: `python -m research.r6_evidence_audit --state-snapshot <private
snapshot.json> --capture-folder <existing trade-review> --dates YYYY-MM-DD ...
--output <new private output.json>`. No new output inside the input runtime and
no overwrite of an existing output. Input prefix sizes are frozen, total read
extent capped64MiB; missing data never filled from charts. Keep all outputs
outside Git because they contain private trade details.

Tests: `tests/test_evidence_audit_20261020.py` and
`tests/test_opening_report_20261020.py`, plus existing capture and Lunit unit tests.
One existing full FakeBroker pipeline fixture requires the integrated release
manifest absent in this new research worktree. It was blocked by integrity on
first run and is explicitly NOT RUN in the final unit suite. No guard was
mocked, expected result relaxed or production manifest fabricated. No whole
operational regression was run or claimed for this research change.

Private evidence/results (not committed): sibling directory
`r6-evidence-shadow-20261020-private`, notably `REVIEW_20261020.md`,
`OPENING_READONLY_CHECK.md`, `EVIDENCE_AUDIT.json`, `CAUSAL_PREFIX_CHECKS.json`,
`TARGETED_FINAL.log/json`, `LIVE_READONLY_VERIFICATION.json`.

No deployable ZIP or new BUILD manifest was issued. The source retains the
baseline BUILD string; that is not release approval for these research edits.
The opening main-loop delay and WebSocket timeouts are still unresolved. Report
and false Focus-event fixes do not resolve queue lag. Before any later deployment
request, separately reproduce/review them with a sealed candidate, safe account
preflight and the user's explicit deployment boundary.
