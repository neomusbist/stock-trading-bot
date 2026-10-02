# Existing GitHub Desktop repository
This source tree is for the existing neomusbist/stock-trading-bot repository. Do not create another github_source, new repository, or copy the entire live installation into Git.

Keep three roles separate: Desktop operating installation (code, dependencies and private runtime), Documents development tree (tests/audits/tools/archives), and the existing GitHub Desktop repository (reviewed code/docs/tests/safe examples/synthetic fixtures).

Before publishing compare current deployed source hashes against reviewed source. Use SOURCE_MANIFEST.json and tools/repo_guard.py. .gitignore defaults to deny; only explicit reviewed paths are allowed. Local actual config/automation/observation, credentials, approval state, logs, account records, vendor/binaries and caches never belong here. Public API fixtures contain field identifiers only and the daily fixture is synthetic.

Review GitHub Desktop changes and staged bytes before committing. A staged scan of zero files does not verify unstaged changes. Commit/push require the user's explicit action; preparation does not publish anything. Local HEAD and a cached remote branch are not proof of the server's current HEAD without authenticated read verification.

Install Python and requirements for development, then use safe examples. No runtime or LIVE approval is distributed. New configuration defaults remain zero-cap / live false / costs unconfirmed. Do not run regression against actual operating state.
