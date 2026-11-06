import json
import tempfile
import unittest
from pathlib import Path
from tools.replay_fast_structure import replay

class FastStructureReplayTests(unittest.TestCase):
    def test_unprocessed_or_future_receipt_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'ticks.jsonl'
            rows=[{'event':'TRADE','symbol':'290690','exchange_at':'2026-11-05T09:07:40+09:00',
                   'receiver_at':'2026-11-05T09:07:40.100000+09:00','processed_at':'2026-11-05T09:07:40.200000+09:00',
                   'price':3610,'volume':1,'cumulative':10,'bid':3605,'ask':3610,'regular':True},
                  {'event':'TRADE','symbol':'290690','exchange_at':'2026-11-05T09:07:41+09:00',
                   'receiver_at':'2026-11-05T09:07:41.100000+09:00','processed_at':'2026-11-05T09:07:45+09:00',
                   'price':3650,'volume':1,'cumulative':11,'bid':3645,'ask':3650,'regular':True}]
            p.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf8')
            result=replay(p,'290690','2026-11-05T09:07:40+09:00','2026-11-05T09:07:42+09:00')
            self.assertEqual(result['counts']['accepted'],1)
            self.assertFalse(result['structure']['pass'])
            self.assertEqual(result['coverage'],'UNKNOWN')

if __name__=='__main__':unittest.main()
