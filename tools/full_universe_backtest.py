"""Full Universe Backtest for LocalBreakoutStrategy.

Exact alias and reproduction entrypoint for reproducing the 89-signal backtest.
Reads audit_data/price_context_cache_20261020.json and evaluates
LocalBreakoutStrategy per symbol independently without portfolio cash/slot limits.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.reproduce_89_signals

if __name__ == '__main__':
    pass
