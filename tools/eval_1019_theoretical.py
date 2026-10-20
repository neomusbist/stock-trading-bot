"""10/19 cache provenance audit. The former theoretical PnL is not executable evidence."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_replay import main

if __name__=='__main__':
    main(['--cache',str(Path(__file__).resolve().parents[1]/'audit_data/price_context_cache_20261019.json'),
          '--day','2026-10-19',*sys.argv[1:]])
