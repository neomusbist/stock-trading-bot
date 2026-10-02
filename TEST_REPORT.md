# Offline verification
The preserved V1.1 suite has 620 tests, including original regressions and SHADOW/entry/exit/GUI checks. Previous validation: 620 PASS, 0 FAIL, 0 ERROR, 0 SKIP. Re-run the entire suite after installing dependencies; do not infer current results from this historical count.

Tests use fake brokers, synthetic accounts, temporary files and Qt test services. No real broker orders or account network tests form part of this result. A/B/C paths, source/SHADOW isolation, cap-zero permission, protected holdings, monotone stops, rejection/partial fill/recovery and session boundaries are covered.

Local verification outputs, account data, actual logs, screenshots, performance samples and backup archives are excluded from this repository. QtTest is a development dependency; production GUI modules are QtCore/QtGui/QtWidgets.
