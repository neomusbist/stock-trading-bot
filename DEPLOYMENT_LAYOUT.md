# Separation of operating, development and source trees
Production needs the launcher, 37 current local Python modules, operating settings/approval/book/log data, retained dependencies and user guides. Tests, source comparisons, benchmarks, original API documents and backups belong outside the production tree.

The current widget GUI imports QtCore, QtGui and QtWidgets. QtTest is used only by GUI regression. A minimal deployment may retain their DLL transitive dependencies, platform/image/style plugins and licenses; QML/WebEngine/tool assets are not required by current application code. Never apply that assumption to a new import without re-auditing and full regression. Runtime storage is not expendable cache.

Full private backup precedes any cleanup. Move archival material only after byte/hash verification. Rebuildable Python caches can be removed; historical account records and backups are preserved rather than discarded. Qt package pruning uses a reviewed dependency manifest and isolated Windows/offscreen checks, not filename guesses.

The GitHub repository contains no vendor or actual runtime. Public docs replace private incident evidence; synthetic fixtures replace source market/API originals. The source tree remains usable with full requirements on another machine.
