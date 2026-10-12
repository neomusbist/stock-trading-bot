# Separation of operating, development and source trees
Production needs the launcher, 37 current local Python modules, operating settings/approval/book/log data, retained dependencies and user guides. Tests, source comparisons, benchmarks, original API documents and backups belong outside the production tree.

The current widget GUI imports QtCore, QtGui and QtWidgets. QtTest is used only by GUI regression. A minimal deployment may retain their DLL transitive dependencies, platform/image/style plugins and licenses; QML/WebEngine/tool assets are not required by current application code. Never apply that assumption to a new import without re-auditing and full regression. Runtime storage is not expendable cache.

Full private backup precedes any cleanup. Move archival material only after byte/hash verification. Rebuildable Python caches can be removed; historical account records and backups are preserved rather than discarded. Qt package pruning uses a reviewed dependency manifest and isolated Windows/offscreen checks, not filename guesses.

The GitHub repository contains no vendor or actual runtime. Public docs replace private incident evidence; synthetic fixtures replace source market/API originals. The source tree remains usable with full requirements on another machine.

## Operating-folder layout confirmed 2026-10-13

The operating folder was reorganized separately from the V1.4 source checkout. Preserve that layout when preparing a later, explicitly approved V1.5 deployment:

- Normal GUI entry point: root `주식자동매매_실행.cmd` -> root `gui_menu.cmd`.
- Administrative launchers: `관리자도구/`. Scripts invoking root Python modules must resolve the parent application directory; do not restore obsolete root wrappers from the source tree indiscriminately.
- User documentation: `docs/` (including `GUI_GUIDE.md`). Source documentation may remain at its repository location; deployment requires an explicit destination mapping.
- Historical reports and retired wrappers: `archive_reports/` and `archive_reports/_removed_wrappers/`. Preserve them and the separately created pre-cleanup backup.

This is not a relocation of active diagnostic report contracts. `main.py` still writes root `connection_check.json`; `automation/runner.py` writes root `automation_check.json` during readiness/smoke checks. Operator services read the new root reports and validate their current-run timestamps. Fresh checks may recreate these root files. Never make readiness or authorization fall back to archived PASS reports, and never restore old reports as current evidence.

The external cleanup transcript reports dry-run and import checks but provides no completed full-regression result. Its checks do not establish V1.5 strategy validation or LIVE readiness. Source-only offline regression and later approved deployment validation remain separate obligations. Development must not execute operating START, STOP, KILL, authorization or account commands to verify this layout.
