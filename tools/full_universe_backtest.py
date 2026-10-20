"""Shared causal evaluator entry point; not a whole-market coverage claim."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_replay import main

if __name__=='__main__':main()
