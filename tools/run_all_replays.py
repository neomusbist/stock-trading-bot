"""Audit both inherited caches without inventing four account simulations."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.causal_replay import main as replay

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache-dir',type=Path,default=Path(__file__).resolve().parents[1]/'audit_data')
    p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args(argv)
    for day in ('20261019','20261020'):
        replay(['--cache',str(a.cache_dir/('price_context_cache_'+day+'.json')),
                '--output',str(a.output_dir/('CACHE_'+day+'.json'))])

if __name__=='__main__':main()
