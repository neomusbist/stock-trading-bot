import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.account_level_replay as alr

print("#######################################################")
print("### 2026-10-20 ACCOUNT REPLAY                       ###")
print("#######################################################")
p20 = REPO_ROOT / 'audit_data' / 'price_context_cache_20261020.json'
print("\n--- 1. Max Position = 1 ---")
alr.run_account_replay(p20, max_positions=1)
print("\n--- 2. Max Position = 2 ---")
alr.run_account_replay(p20, max_positions=2)

print("\n#######################################################")
print("### 2026-10-19 ACCOUNT REPLAY                       ###")
print("#######################################################")
p19 = REPO_ROOT / 'audit_data' / 'price_context_cache_20261019.json'
print("\n--- 1. Max Position = 1 ---")
alr.run_account_replay(p19, max_positions=1)
print("\n--- 2. Max Position = 2 ---")
alr.run_account_replay(p19, max_positions=2)
