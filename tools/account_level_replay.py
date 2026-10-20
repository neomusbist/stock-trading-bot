"""Account scenarios use actual Engine functions and measured paired L1.
Use explicit --config/--automation/--ticks/--capture inputs. Cache-only mode
cannot claim five executions, insolvency or a guaranteed loss cap."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_replay import main

if __name__=='__main__':main(account_default=True)
