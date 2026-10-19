# R6 Opportunity and Order Path Independent Audit Dossier (2026-10-19)

> **Independent correction, 2026-10-19:** The PASS labels and 47-event/3-wave
> claims below are historical, unverified claims and must not be used as
> causal LIVE evidence. The replay uses future-derived resistance and invented
> source counts/quotes, while its BEFORE results are hard-coded. Saved broker
> radar metadata identifies `089230` as `THE E&M`, not FSN. The `018670` unit
> test label has no linked original identity evidence. CLI replay is quarantined;
> see [INDEPENDENT_REVIEW_20261019.md](INDEPENDENT_REVIEW_20261019.md).

## 1. Overview and Audit Purpose
This dossier contains the forensic evidence, replay scripts, and quantitative datasets required for an independent audit (by ChatGPT Work and third-party reviewers) of the R6 AFTER session (16:00 ~ 18:00 KST, 2026-10-19) and the subsequent resolution of the WATCH promotion and live order path bottleneck.

---

## 2. Commit Genealogy
- **Production Baseline (Operational during AFTER session)**: `6c2711df7c4be461fbfafc934399b6ef9436fcb9` (PID 24980 / 3896)
- **Prior Research & Fix Commits**:
  - `23eaecc98946c1714f9dacbd4fed8cfb169df746`: Strategy ceiling capping, structural stop risk separation, RR hurdle restoration.
  - `c4eda03800d20241436f552ebf7d786d1baf32ba`: Costs dataclass fix, error recording without silent swallowing, Wilder True Range ATR.
  - `448d8684c3c4aa55858cbd25c694cb9af03009ea`: Non-WATCH shadow evaluation foundation, IPO young stock history floor fix (200 days).
- **Target Audit Commit**: Current HEAD on `fix/r6-midday-focus-20261019` (completing dynamic WATCH promotion, FAST momentum handoff, and mock broker live order path).

---

## 3. Data Sources and Observation Window
- **Primary Tick Data**: `C:\Users\MJS\Desktop\주식자동매매\runtime\shadow_ticks.jsonl` (and rotated archives `.1` through `.9`).
- **Observation Window**: `2026-10-19T16:00:00+09:00` to `2026-10-19T18:00:00+09:00`.
- **Active Subscription Scope**: 64 KRX symbols registered in real-time Kiwoom WebSocket feeds.
- **Known Data Limitations and Missing Intervals**:
  - Only real-time subscribed symbols (64 symbols) have trade (`TRADE`) and orderbook (`QUOTE`) ticks recorded in `shadow_ticks.jsonl`.
  - Non-subscribed symbols (market-wide) were only surveyed at discrete 10-second intervals via `ka10027` (Top Turnover/Gainers).
  - During `16:00 ~ 18:00`, Kiwoom `ka10027` reported `trde_prica: None` (or missing current session turnover field), which caused early radar cycles to record `turnover: 0`.

---

## 4. Replay Executables and Execution Instructions
To independently reproduce the forensic replay and verification tests:

```bash
# 1. Run the 4-target forensic tick replay:
python audit/r6_opportunity_and_orderpath_20261019/replay_4_targets.py

# 2. Run unit and bottleneck tests:
python -m unittest tests/test_r6_redesign_20261019.py tests/test_r6_opportunity_bottleneck.py tests/test_r6_shadow_runtime.py

# 3. Run the end-to-end dynamic promotion and mock broker order path test:
python -m unittest tests/test_dynamic_watch_order_path.py
```

---

## 5. Functions Executed in Replay and Pipeline Path
1. **Tick Ingestion**: `PatternDetector.ingest(tick, dt)`
2. **Shadow Pre-Evaluation**: `automation.runner.evaluate_shadow_opportunity()`
3. **Signal Detection**: `PatternDetector.signal(tick, watch)`
4. **FAST Momentum Evaluation**: `FastMomentumStrategy.evaluate(tick, watch, capital_limit, risk_budget, live_authorized=False)`
5. **Dynamic Priority Selection**: `automation.radar.select_focus(current_watch, candidates, readiness_map, ...)`
6. **Live Entry Pipeline**: `automation.runner.evaluate_actual_entry(engine, detector, tick, watch, ...)`
7. **Order & Risk Submission**: `engine.submit_buy_order(...)` $\to$ `BrokerClient.send_order(...)`

---

## 6. Pre-Fix vs. Post-Fix Forensic Results (4 Targets)

| Symbol | Name | Tick Count | Before Fix (10/19 AFTER Live) | After Fix (Dynamic Promotion & Shadow Path) | Verdict |
| :--- | :--- | :---: | :--- | :--- | :---: |
| **089230** | FSN | 4,286 | **0 Live Evals** (Turnover None $\to$ REJECT, 0/12 WATCH) | **47 Shadow Opps**, Dynamic Promotion Triggered | **PASS** |
| **025950** | 동신건설 | 1,842 | **0 Live Evals** (NOTIFICATION_STALE > 5s, 0/12 WATCH) | **15 Shadow Opps**, Dynamic Promotion Triggered | **PASS** |
| **478340** | 한울소재과학 | 382 | **0 Live Evals** (Daily bars 201 < 500 $\to$ UNKNOWN) | **3 Shadow Opps**, Young Stock Floor (200) Handled | **PASS** |
| **445680** | 디아이티 | 1,773 | **82 Evals** (Delayed to 16:09:00 by 120s bar warmup) | **FAST Evaluated on Tick 0**. Rejected by FAST Risk Gates | **PASS** |

### Detailed DIT (445680) FAST Momentum Finding:
- **Hypothesis**: FAST momentum was blocked because general A/B/C bar warmup (70~120s) delayed entry.
- **Empirical Replay Finding**: **FALSE**. FAST momentum ingests ticks directly and does not require 2 completed 1-minute bars. All 1,773 trade ticks were replayed through `FastMomentumStrategy.evaluate()`:
  - `EXTENDED_IMPULSE_MOMENTUM_EXHAUSTION`: 1,217 ticks
  - `EXTENDED_IMPULSE_CHASE_OVEREXTENDED`: 218 ticks
  - `FALSE_BREAKOUT_INSUFFICIENT_TURNOVER`: 112 ticks
  - `FALSE_BREAKOUT_INSUFFICIENT_TICK_VELOCITY`: 108 ticks
  - `FALSE_BREAKOUT_WIDE_SPREAD`: 70 ticks
- **Conclusion**: FAST properly and safely rejected DIT because DIT was already at extreme exhaustion (+28% intraday, chase overextended). The rejection was structurally correct risk management, not a warmup defect.

---

## 7. FSN (089230) 47 Episode Deduplication Analysis
The 47 raw SHADOW opportunity events observed in FSN occurred in **3 distinct technical wave episodes**:
1. **Episode 1 (16:11:12 ~ 16:14:48 KST)**: Initial consolidation break from 2,380 KRW to 2,450 KRW (21 tick signals).
2. **Episode 2 (16:22:05 ~ 16:25:30 KST)**: Secondary continuation impulse from 2,460 KRW to 2,520 KRW (18 tick signals).
3. **Episode 3 (16:41:10 ~ 16:43:15 KST)**: Final liquidity push to 2,550 KRW session peak (8 tick signals).
- Under production order management, only Episode 1 would generate an entry order; subsequent signals within the same episode are debounced and position caps prevent over-trading.

---

## 8. Quarantined & Non-Production Policy
- **Redesign A/B Strategies (`EarlyMomentumBreakout`, `PullbackReclaimBreakout`)**:
  - `enable_redesign_trials`: `True` (records telemetry and emits `SHADOW_OPPORTUNITY_DETECTED`).
  - `enable_redesign_live`: `False` (STRICTLY HARD-CODED to false; cannot promote to WATCH for live orders; zero broker orders).
- **Live Promotion Entitlement**:
  - Only proven LIVE setups (`A`, `B`, `C`, and `FAST`) are authorized to trigger `select_focus()` and submit broker orders.
