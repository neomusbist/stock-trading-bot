> 결론: A 저항 정책과 판단 기록·실제 엔진 PAPER 코드를 수정했습니다. 실제 수신 자료의 지정 종목/구간에서는 추가 주문 적격 신호가 0건이며, 당일 전체 시장과 누락 구간은 UNKNOWN입니다. 관측 패키지는 준비했지만 LIVE에는 적용하지 않았습니다. 실행 명령과 판정 기준은 RUNBOOK.md에 있습니다.

> 결론: A 저항 정책과 판단 기록·실제 엔진 PAPER 코드를 수정했습니다. 실제 수신 자료의 지정 종목/구간에서는 추가 주문 적격 신호가 0건이며, 당일 전체 시장과 누락 구간은 UNKNOWN입니다. 관측 패키지는 준비했지만 LIVE에는 적용하지 않았습니다. 실행 명령과 판정 기준은 RUNBOOK.md에 있습니다.

# R6 entry evidence and production-path PAPER result

Completed 2026-10-28. Development baseline `408f8bbe671dd34549e78db68b36a1901d85922d` on the existing `tech-stability/market-open-0900` branch. No branch switch, LIVE installation, restart, API connection or real order.

## Confirmed code changes

- A resistance is classified against the fixed structural breakout, never the moving decision ask. A ceiling above that breakout remains binding even when the decision print overshoots it. Only a resistance at/below the structural breakout can retire after a real trade above the breakout. Independently justified projection is unchanged; a daily-bar search above the fixed trigger finds the next known ceiling and retains the nearest still-binding known ceiling. An ask-only crossing cannot retire resistance. The original named failing test retains its expected 1040 target.
- Entry evidence binds the actual merged quote and the WATCH snapshot used by the detector. It records structure episodes, source/receipt IDs, event/processing times, freshness, completed-bar coverage, all five setup statuses, actual price/stop/target/cost/risk gates and final outcome. It runs before ordinary display throttling, suppresses unchanged rows, and uses the existing bounded asynchronous capture queue/rotation/free-space guard. Episodes and price fingerprints are not independent trades. Missing fields remain UNKNOWN or NOT_REACHED.
- Native mandatory gates are separated from observation-only failures. PAPER requires matching native selected setup, READY trial, positive recorded pre-intent quantity, mandatory gate PASS, identical trade/book receipt linkage, identical signal prices and fresh observed account/risk/cost context. The actual detector, runner, AutonomousEngine, durable INTENT, L1 FakeBroker, fill handling and protective exits are reused. An isolated fake account cannot turn missing real authority into permission.
- No new strategy, wider target, lower RR, tighter unsupported stop, expanded subscription or LIVE risk change.

## Actual-source replay, not actual trading

The frozen 2026-10-27 09:44–10:18 extraction covers **253590 only**, plus its available scope/stream controls. Source byte ranges, line hashes and file extent hashes remain private. Receiver-only prints are not silently treated as consumed strategy input.

| Item | Native transitions / legacy samples | Every consumed print, offline research |
|---|---:|---:|
| Preserved TRADE records | 7,646 | same |
| Confirmed processed TRADE records ingested | 3,822 | same |
| Processing/receipt-unknown TRADE records | 3,824 | same |
| Independent BOOK records | 3,844 | same |
| Legacy opportunity samples | 26 | same |
| Actual entry evaluation calls executed | 15 | 6,947 |
| Distinct price-structure fingerprints | 2 | 6 |
| Setup-ready evaluations / PAPER BUY / fills | 0 / 0 / 0 | 0 / 0 / 0 |

The two default-mode fingerprints are A structures. B/C/D/E2 produced no ready signal here; D/E2 and FAST remain disabled in the explicit profile. The every-print count is research re-evaluation, not a claim that LIVE performed those calls or had 6,947 opportunities. Default mode follows existing decision transitions or legacy samples, avoiding per-print entry-research load on the operating PC. Protection still receives every admitted print.

Concrete A examples, using only contemporaneously available recorded context and the research common-cost model:

| First observed | Ask | Structural line | Emergency | Known target | Maximum entry | Net RR |
|---|---:|---:|---:|---:|---:|---:|
| 10:05:01.850776 | 17,350 | 16,930 | 16,850 | 17,330 | 16,910 | -0.234829 |
| 10:06:01.459266 | 17,380 | 16,930 | 16,850 | 17,540 | 17,000 | 0.049381 |

The target is already below the first ask. Neither changing quantity nor ignoring fees creates a legitimate entry. Native-sample replay also records acceleration failure 15 times, warmup/usable-bar failures 8 times each, RR/economic failures 3 times each and liquidity failures twice. These reasons overlap, include repeated evaluations, and are not independent missed opportunities.

Quantity was not reached in these rejected cases. It is incorrect to call it a computed zero-risk quantity. The extraction starts with capture loss counter **42,707**; the time/symbol range of those earlier losses is unknown. New replay loss/write-error counts after flush are both zero, which does not repair source omissions. Missing historical final account/authority gates prevent certifying full LIVE orderability. The 26 legacy samples are not all evaluations. Unknown windows and the rest of the market remain UNKNOWN. The explicit profile preserves available LIVE settings and fills only absent research fields with documented inactive defaults; it is not the installed legacy engine. Frozen default additions and full typed settings stay private.

## C economic incompatibility discovered during regression

With the actual profile `box_max_range=0.015`, C entry >= box top, emergency <= box low and target <= top + 2 box widths, the most optimistic common-cost net RR is **1.004646**, below 1.5. This ignores buffers, noise and overshoot, so they cannot improve the bound. Costs: buy/sell commissions 0.015% each, tax 0.20%, entry slippage reserve 0.20%, exit reserve 0.30%.

This is a diagnostic bound for the unified **research** calculation. Installed 93bdf9a retains its legacy gate and must not be described as running this model. The original gross-positive C synthetic fixture now correctly verifies cost rejection. A/B/FAST positive order/fill/protection tests remain; no C target/width/cost/RR was changed to manufacture a positive case. C needs a separate economic-design decision, outside this task.

## Order origin and tests

The preserved 396300 A trade on 2026-10-15 entered 7,570 x 3 at 11:58:29.405864. Eighteen preserved state sources contain no matching BUY INTENT/broker order ID. Available archived execution events do not cover that entry. Stored order_sent/build metadata and later external-sell reconciliation are insufficient to identify the BUY sender. **Origin UNKNOWN; exclude from autonomous success counts.** This is not a finding that it was definitely manual.

- Broad related regression: 232 executed, 230 passed, 2 skipped (optional private-ledger prerequisites), no failure. Final observer admission/receipt/queue/BUY-NO_BUY-exit suite: 12 passed, including added final authority checks.
- Synthetic PAPER E2 path: BUY -> persisted INTENT -> fake acceptance/fill -> engine protective SELL. Net +1,209.575 KRW, MFE 2,100 KRW/share, MAE 0 in the chosen artificial path. This validates code only; it is neither a market trade nor profitability evidence.
- Synthetic A/B lifecycle exercises partial/duplicate fills and protection. Old missing-target risk/freshness fixtures now explicitly carry the same target already used in their efficiency calculation; expected risk limits and rejection behavior were preserved.
- Transaction safety: 11 synthetic install/rollback tests passed. A/B recording compatibility passed against a private copy of the installed native sources with zero recorder errors and durable pre-transport INTENT evidence. No network.
- A 4-second read-only EOF-tail smoke on 2026-10-28 read zero new rows: CLI/close behavior only, not real-time market performance. No opening-load capacity claim.

## Minimum observation deployment, not yet installed

The prepared observation-only overlay is R6-ENTRYTRACE-20261027-4c756a1b2bb2: exactly `automation/decisions.py`, `automation/engine.py`, `automation/runner.py`, `automation/trade_review_capture.py`, new `automation/entry_evidence.py`, and `runtime/RELEASE_IDENTITY.json`.

It is built from exact installed bytes. It excludes patterns.py, A resistance research changes, ABRR and all new strategies. The release pins 10 existing Python dependencies omitted by the old 92-file identity; those dependencies are checked, not replaced. Native old-model maximum entry may remain UNKNOWN_NOT_EMITTED_BY_NATIVE_MODEL; the exact known price/stop/target/cost inputs permit a separately labelled common-model offline reconstruction. Legacy native decision RR and net economic-score RR are labelled separately.

Default observer limits: 32 MiB/file, 64 MiB/day, 256 MiB total, 32 GiB free reserve; bounded queue, 1-second polling, Windows idle priority, 50 ms soft CPU budget per poll, transitions rather than every-print strategy recomputation. These are protective engineering limits, not measured capacity guarantees. Existing capture loss counters expose overload; critical order/ledger durability is unchanged.

Approval and a fresh STOP/no-running-target-process/flat-resolved-bot-ledger check are required before installation. External backup precedes exact file replacement, source/release/protected-state hashes are verified, and STOP remains. Rollback restores only these source/identity files; it removes the approved new module only if its installed hash still matches and never restores an old ledger or approval receipt. Changed files or failed guards stop the tool. A running held position must not be interrupted to install logging.

Current read-only check on 2026-10-28 19:24 KST: LIVE release R6-LIVE-OPS-20261021-10cf8d2 / 93bdf9a, all 92 declared hashes match, frozen configuration fields unchanged, policy STOP. Heartbeat is from 2026-10-27, so this is **not** a claim of a running, connected or approved current-day trading session. No installation was performed.

Next actionable step: separately approve the six-file observation overlay if desired, then collect actual native decision transitions with matching account/receipt evidence. Until then the sidecar can read existing journals but must keep missing native order authority UNKNOWN. No automatic activation or real order is part of this delivery.
