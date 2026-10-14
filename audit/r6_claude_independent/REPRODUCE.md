# R6 Independent Audit Reproduction Guide

## 1. Overview
This document specifies the exact environment, prerequisites, commands, and expected verification metrics required for an independent third party to reproduce the R6 FAST Momentum replay audit, defect fixes, and test results from a clean checkout.

---

## 2. Prerequisites & Environment

- **Operating System**: Windows 10 / 11 (PowerShell or cmd)
- **Python**: 3.10+ with standard library (`json`, `csv`, `unittest`, `dataclasses`, `hashlib`, `math`, `pathlib`)
- **Virtualenv**: Recommended to activate the repository virtualenv:
  ```powershell
  & "C:\Users\MJS\Documents\PythonEnvs\stock-trading-bot\Scripts\Activate.ps1"
  ```
- **Git Branch**: `feat/v1.6-fast-momentum-r6-20261014`
- **Working Directory**: `C:\Users\MJS\Documents\주식자동매매_개발\v15-frequency-capacity-r5-20261014`

---

## 3. Data Integrity & Input Provenance

The replay strictly consumes the immutable historical event log recorded during live shadow observation on 2026-10-14:

- **File Path**: `C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce`
- **File Size**: `67,132,914 bytes` (64.02 MB)
- **SHA-256 Digest**: `0c698c6ef4c7d9815c83a081d5d81d66205588e120c2e07caaf3f10f2050b50c`
- **Time Range**: `2026-10-14 14:21:21.079035+09:00` ~ `2026-10-14 14:52:49.552486+09:00` (31.47 minutes)
- **Total Lines**: 9,435 lines (6,072 `ENTRY_EVAL` events, 178 distinct symbols)

Verification Command (PowerShell):
```powershell
Get-FileHash -Algorithm SHA256 "C:/Users/MJS/Desktop/주식자동매매/runtime/automation-production/archive/events.jsonl.20261014-145249-570438.7232.32e27bce"
```
Expected Output:
```
0C698C6EF4C7D9815C83A081D5D81D66205588E120C2E07CAAF3F10F2050B50C
```

---

## 4. Execution Step 1: Independent Replay

Run the standalone audit replay script:
```powershell
python audit/r6_claude_independent/independent_replay.py
```

### Outputs Generated:
1. `audit/r6_claude_independent/SYMBOL_IDENTITY_AUDIT.csv`
2. `audit/r6_claude_independent/TRADE_LEDGER.csv`
3. `audit/r6_claude_independent/LEGACY_REPRODUCTION_LEDGER.csv`
4. `audit/r6_claude_independent/EQUITY_CURVE.csv`
5. `audit/r6_claude_independent/SLIPPAGE_SENSITIVITY.csv`
6. `audit/r6_claude_independent/COOLDOWN_SENSITIVITY.csv`
7. `audit/r6_claude_independent/REPLAY_SUMMARY.json`

### Key Verification Metrics to Validate:
- `identity_mismatches`: **0 / 23** (all 6-digit codes correctly mapped to authentic KRX company names)
- `corrected_baseline` (SAME_SNAPSHOT, 0-tick):
  - Completed trades: **7**
  - Unresolved trade at tape close (15:30 KST): **1** (042510 라온시큐어, 4 shares, holding time 633s)
  - Gross PnL: **+5,330.0 KRW**
  - Commission & Tax: **1,216.76 KRW** (Entry 0.015%, Exit 0.215%)
  - Base Slippage (0.1% each leg): **2,643.32 KRW**
  - Net PnL: **+1,469.92 KRW** (Win Rate: 57.14% / 4 wins, 3 losses / Profit Factor: 1.688)
- **Adverse Tick Sensitivity (SAME_SNAPSHOT)**:
  - 0-tick: **+1,469.92 KRW** (PF 1.688)
  - **-1 tick**: **-278.17 KRW** (PF 0.910 -> **TURNS NEGATIVE**)
  - -2 tick: **-2,116.29 KRW** (PF 0.453)
  - -3 tick: **-2,894.39 KRW** (PF 0.071)
- **Delay Sensitivity (NEXT_SNAPSHOT, 1-snapshot latency)**:
  - 0-tick: **-61.41 KRW** (TURNS NEGATIVE EVEN AT 0-TICK)
  - -1 tick: **-1,430.04 KRW**
- **Legacy Reproduction (Forced exit at tape boundary)**:
  - Matches previous report: 8 trades, Net PnL **+1,104.82 KRW** @ 0-tick, **-723.16 KRW** @ -1 tick.

---

## 5. Execution Step 2: Unit & Regression Tests

Run the dedicated R6 audit test suite:
```powershell
python -m unittest tests/test_r6_independent_audit.py -v
```
Expected Result:
- **16 tests run, 0 failures, 0 errors, ALL PASS** (~1.2s)
- Verifies:
  - Defect A fix: Costs dataclass conversion & get attribute fallback
  - Defect B fix: MicroTape starvation resolved (ingest_tick called immediately on tick)
  - Defect C fix: TickLogger bounded memory buffer, incremental backup rotation, timestamp separation, thread safety
  - MicroTape VWAP / Buy ratio calculation
  - Order state machine: SUBMITTED -> PENDING_CANCEL -> CANCELLED / FILLED
  - Symbol identity integrity

Run the full regression test suite:
```powershell
python -m unittest discover tests -v
```
Expected Result:
- All suites pass without regressions.

---

## 6. Audit Verdict Summary
- **Code Defects**: Fully remediated and verified with automated regression tests.
- **Symbol Identity**: 100% verified against KRX master codes; historical naming discrepancy explained.
- **Replay Viability**: **FAIL**. The strategy fails the -1 tick adverse slippage test (-278.17 KRW) and fails under 1-snapshot execution delay (-61.41 KRW). LIVE capital deployment is prohibited. Shadow data collection is ready.
