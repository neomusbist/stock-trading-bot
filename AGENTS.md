# Repository version management

- Before changing versions, tags, Releases or branch roles, read `VERSION_POLICY.md`, `VERSION_HISTORY.md` and `VERSION_INDEX.json`.
- User-facing versions and Release titles contain only the version number, such as `v1.6`, `v1.61`, `v1.7`. Dates, features and long BUILD identifiers belong in technical metadata or the Release description.
- Treat versions as identifier strings. Use the explicit catalog order, not floating-point arithmetic or lexical sorting.
- Do not invent historical versions, release dates, test successes or profitability. Historical source snapshots and unvalidated research must not be promoted as confirmed LIVE releases.
- Keep existing tags immutable. Preserve active worktrees, uncommitted/unpushed work, open PR references and unmerged research.
- The current catalog describes `v1.6` at `eac9bc5`; `v1.7` is a development goal, not a published release. `main` still contains the older program baseline plus management documentation until a separately approved source integration.
- Version housekeeping does not authorize LIVE source replacement, restarts, orders, approval edits or ledger/risk-budget changes. Follow the user's current task authorization.
- Run `python tools/validate_versions.py` before publishing catalog changes, and add `--check-git` after creating the corresponding tags. Keep account data, credentials and private evidence outside commits.
