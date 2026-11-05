import json
import tempfile
import unittest
from pathlib import Path
from tools.stream_trade_evidence import analyze

class StreamingTradeEvidenceTests(unittest.TestCase):
    def test_filters_symbol_day_and_does_not_invent_missing_market_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=root/'state.json';events=root/'events.jsonl'
            state.write_text(json.dumps({'trades':[{'symbol':'290690','opened':'2026-11-05T09:08:00+09:00',
                'closed':'2026-11-05T09:08:34+09:00','entry':3655,'exit':3610,'qty':16,
                'MFE':20,'MAE':45,'pnl_source':'ESTIMATED','net_pnl':-835,'signal':{}}]}),encoding='utf8')
            rows=[{'symbol':'290690','time':'2026-11-05T09:08:34+09:00','event':'FILL','side':'SELL'},
                  {'symbol':'OTHER','time':'2026-11-05T09:08:35+09:00','event':'FILL'},
                  {'symbol':'290690','time':'2026-11-04T09:08:35+09:00','event':'FILL'}]
            events.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf8')
            r=analyze(state,[events],'290690','2026-11-05')
            self.assertEqual(len(r['recorded_events']),1)
            self.assertIsNone(r['trades'][0]['net_pnl'])
            self.assertIn('UNKNOWN',r['trades'][0]['post_exit_path'])
            self.assertEqual(len(r['sources'][0]['sha256']),64)

    def test_post_exit_uses_received_trade_time_and_labels_coverage_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=root/'state.json';prints=root/'prints.jsonl'
            state.write_text(json.dumps({'trades':[{'symbol':'290690','opened':'2026-11-05T09:08:00+09:00',
                'closed':'2026-11-05T09:08:34+09:00','entry':3655,'exit':3610,'qty':16,'signal':{}}]}),encoding='utf8')
            rows=[{'event':'TRADE','symbol':'290690','received_at':'2026-11-05T09:08:33+09:00','price':3700},
                  {'event':'TRADE','symbol':'290690','received_at':'2026-11-05T09:09:00+09:00','price':3630},
                  {'event':'TRADE','symbol':'290690','received_at':'2026-11-05T09:10:00+09:00','price':3670},
                  {'event':'BOOK','symbol':'290690','received_at':'2026-11-05T09:09:01+09:00','price':9999}]
            prints.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf8')
            r=analyze(state,[],'290690','2026-11-05',[prints]);d=r['trades'][0]['post_exit_observed']
            self.assertEqual((d['60']['count'],d['60']['max']),(1,3630))
            self.assertEqual((d['180']['count'],d['180']['max']),(2,3670))
            self.assertEqual(d['180']['coverage'],'UNKNOWN')

if __name__=='__main__':unittest.main()
