import tempfile,unittest
from pathlib import Path
from datetime import date,datetime,timedelta
from unittest.mock import Mock,patch
from automation.policy import AutomationConfig,Policy
from automation.radar import RadarWorker
from automation.observation_worker import ObservationWorker
from automation.capital import Costs
from core.settings import Config,KST
from candidate_builder import history_refresh_required

AT=datetime(2026,10,21,9,0,tzinfo=KST)
SYM='289220'


def history(day):
    return [dict(datetime=(day-timedelta(days=749-i)).isoformat(),open=1000,high=1010,
        low=990,close=1000,volume=2000000) for i in range(750)]


class DailyHistoryRefreshTests(unittest.TestCase):
    def test_weekend_is_a_refresh_trigger_not_a_holiday_calendar(self):
        self.assertFalse(history_refresh_required(history(date(2026,10,23)),date(2026,10,26)))
        self.assertTrue(history_refresh_required(history(date(2026,10,22)),date(2026,10,26)))

    def test_radar_refreshes_missing_previous_session_even_within_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'data/raw'/f'{SYM}_daily.csv';path.parent.mkdir(parents=True);path.touch()
            worker=RadarWorker(Config(),AutomationConfig(),Policy(root/'policy.json'),Mock(),root)
            worker.status_cache[SYM]={'pass':True,'checked_at':AT.isoformat()}
            worker.status_fetched=worker.fetched=0;worker.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.}
            stale=history(AT.date()-timedelta(days=7));fresh=history(AT.date()-timedelta(days=1))
            with patch('automation.radar.now',return_value=AT),patch('automation.radar.bars_from_csv',return_value=stale),patch('automation.radar.fetch_bars',return_value=fresh) as fetch:
                worker.evaluate({'code':SYM,'reference_price':1001})
                self.assertEqual(fetch.call_count,1)
                self.assertEqual(worker.cache[(AT.date(),SYM)][-1]['datetime'],'2026-10-20')
                worker.evaluate({'code':SYM,'reference_price':1002})
                self.assertEqual(fetch.call_count,1)

    def test_radar_chart_budget_blocks_stale_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'data/raw'/f'{SYM}_daily.csv';path.parent.mkdir(parents=True);path.touch()
            worker=RadarWorker(Config(),AutomationConfig(),Policy(root/'policy.json'),Mock(),root)
            worker.status_cache[SYM]={'pass':True,'checked_at':AT.isoformat()}
            worker.status_fetched=0;worker.fetched=worker.a.max_scan_charts
            worker.timings={'status_ms':0.,'history_ms':0.,'structure_ms':0.}
            with patch('automation.radar.now',return_value=AT),patch('automation.radar.bars_from_csv',return_value=history(AT.date()-timedelta(days=7))),patch('automation.radar.fetch_bars') as fetch:
                result=worker.evaluate({'code':SYM,'reference_price':1001})
                self.assertEqual(result['status'],'UNKNOWN')
                fetch.assert_not_called()
                self.assertNotIn((AT.date(),SYM),worker.cache)

    def test_shadow_refreshes_old_daily_history_without_granting_order_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rest=Mock();worker=ObservationWorker(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003),root,rest=rest)
            worker.clock=lambda:AT
            row={'code':SYM,'name':'SYNTHETIC','reference_price':1001,'status':'RAW','source_count':2,
                 'affordability':{'entry_eligible':True},'market_status':{'pass':True,'checked_at':AT.isoformat()}}
            with patch.object(worker,'daily_bars',return_value=history(AT.date()-timedelta(days=7))),patch('automation.observation_worker.fetch_bars',return_value=history(AT.date()-timedelta(days=1))) as fetch:
                rows,_,_=worker.prepare([row],AT,{'entry_budget':71760},publish=False)
                self.assertEqual(fetch.call_count,1)
                self.assertEqual(rows[0]['shadow_view']['history_end'],'2026-10-20')
                self.assertNotIn('actual_order_permission',rows[0]['shadow_view'])
                worker.prepare([dict(row)],AT+timedelta(seconds=31),{'entry_budget':71760},publish=False)
                self.assertEqual(fetch.call_count,1)

    def test_shadow_disabled_refresh_keeps_history_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=ObservationWorker(Config(),AutomationConfig(),Costs(.00015,.00015,.002,.002,.003),Path(tmp),rest=Mock())
            worker.clock=lambda:AT
            row={'code':SYM,'reference_price':1001,'status':'RAW','affordability':{'entry_eligible':True},
                 'market_status':{'pass':True,'checked_at':AT.isoformat()}}
            with patch.object(worker,'daily_bars',return_value=history(AT.date()-timedelta(days=7))),patch('automation.observation_worker.fetch_bars') as fetch:
                rows,_,_=worker.prepare([row],AT,{'entry_budget':71760},allow_metadata=False,publish=False)
                self.assertEqual(rows[0]['shadow_view']['status'],'UNKNOWN')
                self.assertEqual(rows[0]['shadow_view']['reason'],'SHADOW_HISTORY_REFRESH_PENDING')
                fetch.assert_not_called()


if __name__=='__main__':unittest.main()
