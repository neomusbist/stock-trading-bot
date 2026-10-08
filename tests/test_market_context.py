"""Context is diagnostic, point-in-time, bounded and independent of permission."""
import copy,unittest
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.settings import KST,Config
from core.realtime import Feed
from automation.market_context import trend_window,symbol_context,IndexContext,DiscoveryContext

START=datetime(2026,10,12,9,0,tzinfo=KST)

class MarketContextTests(unittest.TestCase):
    def bars(self,count=15,step=1):
        return [{'time':START+timedelta(minutes=i),'open':100+i*step,'close':100+(i+1)*step,
                 'low':99+i*step,'high':102+i*step} for i in range(count)]
    def test_no_new_fixed_five_or_fifteen_minute_entry_wait(self):
        t=SimpleNamespace(time=START+timedelta(minutes=2))
        c=symbol_context(self.bars(2),t)
        self.assertEqual(c['trend_5m']['status'],'UNKNOWN')
        self.assertTrue(c['diagnostic_only']);self.assertFalse(c['actual_order_permission'])
        self.assertNotIn('BUY',str(c));self.assertNotIn('entry_block',c)
    def test_incomplete_future_bars_never_enter_context(self):
        t=SimpleNamespace(time=START+timedelta(minutes=5,seconds=30))
        before=trend_window(self.bars(6),t,5)
        future=self.bars(6);future[-1].update(close=999999,high=999999,low=1)
        self.assertEqual(before,trend_window(future,t,5))
        self.assertEqual(before['status'],'UP')
    def test_gap_and_unknown_benchmark_never_invent_relative_strength(self):
        t=SimpleNamespace(time=START+timedelta(minutes=15))
        bars=self.bars();bars[7]['time']+=timedelta(seconds=30)
        self.assertEqual(trend_window(bars,t,15)['reason'],'CONTEXT_GAP')
        self.assertIsNone(symbol_context(self.bars(),t)['relative_return_5m'])
    def test_negative_previous_close_change_can_coexist_with_intraday_up_context(self):
        index=IndexContext()
        for i in range(16):
            stamp=START+timedelta(minutes=i)
            self.assertTrue(index.ingest('001',stamp,str(10000+i*10),stamp,'-1.2'))
        c=index.snapshot(START+timedelta(minutes=15))['KOSPI']
        self.assertEqual(c['raw_change_percent'],'-1.2')
        self.assertEqual(c['trend_15m']['status'],'UP')
    def test_index_relative_comparison_requires_identical_period(self):
        t=SimpleNamespace(time=START+timedelta(minutes=15))
        stock=symbol_context(self.bars(),t)
        index={'trend_5m':{**stock['trend_5m'],'return':.01},'trend_15m':stock['trend_15m']}
        c=symbol_context(self.bars(),t,index)
        self.assertGreater(c['relative_return_5m'],0)
        index['trend_5m']['start']=START.isoformat()
        self.assertIsNone(symbol_context(self.bars(),t,index)['relative_return_5m'])
    def test_index_closed_market_and_stale_samples_report_unknown(self):
        index=IndexContext();index.ingest('101',START,'1000',START,'-1')
        self.assertFalse(index.snapshot(START+timedelta(seconds=16))['KOSDAQ']['fresh'])
        self.assertFalse(index.ingest('101',START.replace(hour=8),'1100',START.replace(hour=8)))
        self.assertEqual(index.snapshot(START.replace(hour=17))['KOSDAQ']['trend_5m']['status'],'UNKNOWN')
    def test_index_stream_has_no_extra_rest_or_order_queue_messages(self):
        rest=Mock(cfg=Config());feed=Feed(rest,['999990'])
        with patch('core.realtime.now',return_value=START):
            feed.emit({'trnm':'REAL','data':[{'type':'0J','item':'001','values':{'20':'090000','10':'250000','12':'-1.2'}}]})
        self.assertTrue(feed.q.empty());rest.query.assert_not_called();rest.submit.assert_not_called()
        self.assertEqual(feed.context_snapshot(START)['KOSPI']['raw_level'],'250000')
        feed.emit({'trnm':'DISCONNECTED'});self.assertFalse(feed.context_snapshot(START)['KOSPI']['fresh'])
    def test_subscription_refresh_keeps_position_and_order_types_with_two_indices(self):
        feed=Feed(Mock(),['999990']);feed.socket=Mock();feed.logged_in=True
        feed.update_symbols(['999990','999980'])
        import json
        data=json.loads(feed.socket.send.call_args.args[0])['data']
        self.assertEqual(data[0]['type'],['0B','0D']);self.assertEqual(data[1]['type'],['00','0s'])
        self.assertEqual(len(data),2) # index diagnostic group is independent of WATCH churn
        with patch('core.realtime.now',return_value=START):
            feed.emit({'trnm':'REG','grp_no':'2','return_code':99})
        self.assertTrue(feed.q.empty())
        feed.emit({'trnm':'REG','grp_no':'1','return_code':99})
        self.assertFalse(feed.q.empty()) # real subscription failure still reaches the engine
    def test_preopen_list_is_not_fixed_after_open_and_counts_bounded(self):
        ctx=DiscoveryContext(limit=2)
        rows=[{'code':'999990','reference_price':100}]
        pre=ctx.update(rows,START.replace(hour=8))[0]['discovery_context']
        self.assertTrue(pre['preopen_prepared']);self.assertFalse(pre['actual_order_permission'])
        opened=ctx.update([{'code':'999990','reference_price':95},{'code':'999980','reference_price':300}],START)
        self.assertEqual(opened[0]['discovery_context']['activity'],'FALLING_RECENTLY')
        self.assertEqual(opened[1]['discovery_context']['activity'],'NEW_DISCOVERY')
        ctx.update([{'code':'999970','reference_price':400}],START+timedelta(minutes=1))
        self.assertEqual(len(ctx.rows),2)
        next_day=ctx.update(rows,START+timedelta(days=1))[0]['discovery_context']
        self.assertEqual(next_day['activity'],'NEW_DISCOVERY');self.assertFalse(next_day['preopen_prepared'])
