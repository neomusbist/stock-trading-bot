"""Captured 2026-10-12 read-only NXT frames; no network or account mutation."""
import json,unittest
from datetime import datetime,timedelta
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,patch
from core.settings import Config,KST
from core.realtime import Feed,parse_tick,parse_book,stream_symbol
from core.util import code
from automation.patterns import PatternDetector
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.runner import SubscriptionState

BASE=datetime(2026,10,12,8,4,22,tzinfo=KST)
TICK={'20':'080421','10':'-258500','15':'+1','13':'313030',
      '27':'-258500','28':'-258000','290':'2','9081':'NXT'}
BOOK={'21':'080422','41':'-258500','51':'-258000','61':'6523','71':'39703','121':'142689','125':'270524'}

class MondayApiContractTests(unittest.TestCase):
    def test_captured_exchange_codes_parse_without_relaxing_order_symbol_validation(self):
        for suffix in ('_NX','_AL'):
            tick=parse_tick({'item':'005930'+suffix,'values':TICK},BASE)
            self.assertEqual(tick.symbol,'005930');self.assertEqual(tick.venue,'NXT')
            self.assertEqual(tick.price,258500)
            with self.assertRaises(ValueError):code('005930'+suffix)
        for bad in ('005930_BAD','005930_NX_AL','X005930_NX'):
            with self.assertRaises(ValueError):stream_symbol(bad)

    def test_original_book_time_is_not_replaced_by_response_time(self):
        book=parse_book({'values':BOOK},BASE+timedelta(seconds=30))
        self.assertEqual(book['time'],BASE.isoformat())
        self.assertEqual(book['received_at'],(BASE+timedelta(seconds=30)).isoformat())
        tick=parse_tick({'item':'005930_AL','values':TICK},BASE+timedelta(seconds=30),book)
        self.assertFalse(tick.depth_available);self.assertEqual(tick.ask_qty,0)
        self.assertEqual(tick.depth_source,'BOOK_STALE')

    def test_after_real_frames_reach_both_observation_and_position_queues(self):
        stamp=BASE.replace(hour=16);cfg=replace(Config(),after_enabled=True)
        with patch('core.realtime.now',return_value=stamp):
            feed=Feed(SimpleNamespace(cfg=cfg),['005930'])
            sink=Mock();feed.configure_observation({'005930'},{'005930'},sink)
            feed.emit({'trnm':'REAL','data':[{'type':'0D','item':'005930_AL','values':{**BOOK,'21':'160422'}}]})
            feed.emit({'trnm':'REAL','data':[{'type':'0B','item':'005930_AL','values':{**TICK,'20':'160422'}}]})
        tick,book=feed.entry_snapshot('005930')
        self.assertEqual(tick.ask_qty,6523);self.assertEqual(tick.bid_qty,39703)
        self.assertTrue(tick.depth_available);self.assertGreaterEqual(book['ask_qty'],6)
        sink.assert_called_once();self.assertEqual(feed.q.qsize(),2)
        msg=feed.q.get_nowait()[1];self.assertEqual(msg['data'][0]['item'],'005930')
        self.assertEqual(msg['data'][0]['_subscription_item'],'005930_AL')

    def test_phase_change_refreshes_exchange_without_releasing_history(self):
        cfg=replace(Config(),after_enabled=True);rest=SimpleNamespace(cfg=cfg)
        with patch('core.realtime.now',return_value=BASE):feed=Feed(rest,['005930'])
        feed.logged_in=True;feed.socket=Mock();feed.latest_ticks['005930']=Mock();feed.orderbooks['005930']={'old':True}
        sub=SubscriptionState(['005930']);engine=SimpleNamespace(connected=True,account_ok=True,event=Mock())
        detector=SimpleNamespace(history={'005930':[1,2]},detached={})
        stamp=BASE.replace(hour=16)
        with patch('core.realtime.now',return_value=stamp):
            self.assertTrue(sub.refresh(engine,detector,feed,{'005930'},stamp))
            self.assertFalse(sub.refresh(engine,detector,feed,{'005930'},stamp))
            feed.emit({'trnm':'REAL','data':[{'type':'0B','item':'005930','values':{**TICK,'20':'160422'}}]})
        self.assertEqual(detector.history['005930'],[1,2]);self.assertIn('005930',sub.waiting)
        self.assertEqual(feed.latest_ticks,{});self.assertEqual(feed.orderbooks,{})
        self.assertTrue(engine.connected);self.assertTrue(engine.account_ok)
        data=json.loads(feed.socket.send.call_args.args[0])['data']
        self.assertEqual(data[0]['item'],['005930_AL']);self.assertEqual(data[1]['type'],['00','0s'])
        self.assertFalse(any(c.args[0]=='OBSERVATION_RELEASED' for c in engine.event.call_args_list))

    def test_preopen_nxt_regular_flag_cannot_create_strategy_bars(self):
        cfg=Config();auto=AutomationConfig();detector=PatternDetector(cfg,auto,Costs(.00015,.00015,.002,.002,.003))
        tick=parse_tick({'item':'005930_NX','values':TICK},BASE)
        self.assertTrue(tick.regular);self.assertFalse(detector.ingest(tick,BASE))
        self.assertEqual(detector.history,{});self.assertEqual(detector.buffers,{})

    def test_default_login_has_only_one_reg_for_groupless_production_ack(self):
        import websocket
        rest=SimpleNamespace(cfg=Config(),get_token=lambda:'FAKE');feed=Feed(rest,['005930']);sent=[]
        class Socket:
            def __init__(self,url,**handlers):self.handlers=handlers
            def send(self,msg):sent.append(json.loads(msg))
            def close(self):pass
            def run_forever(self,**kwargs):
                self.handlers['on_open'](self)
                for msg in ({'trnm':'LOGIN','return_code':0},{'trnm':'REG','return_code':0}):
                    self.handlers['on_message'](self,json.dumps(msg))
                feed.stop.set()
        with patch.object(websocket,'WebSocketApp',Socket):feed.run()
        self.assertEqual([s['trnm'] for s in sent],['LOGIN','REG'])
        self.assertEqual(feed.q.get_nowait()[1],{'trnm':'REG','return_code':0})

if __name__=='__main__':unittest.main()
