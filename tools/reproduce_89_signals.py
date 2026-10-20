"""Corrected entry point. The original 89-trade result is invalid performance.
Original code remains at Git 8174b5d. Nothing executes on import. Without real
receipt/bar/book inputs, report UNKNOWN; do not create synthetic trades."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_replay import main

if __name__=='__main__':main()
