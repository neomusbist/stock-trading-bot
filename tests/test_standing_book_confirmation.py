"""198440 quiet book: preserve source time; fresh matched REST confirmation for held stock only."""
import copy,unittest
from dataclasses import replace
from datetime import timedelta
from tests import test_position_data_health as fixture
BASE=fixture.BASE;SYM=fixture.SYM

class StandingBookTests(unittest.TestCase):
    setUp=fixture.PositionHealthTests.setUp
    advance=fixture.PositionHealthTests.advance
    sell_count=fixture.PositionHealthTests.sell_count
    def prepare(self):
        self.e.a=replace(self.e.a,stable_position_book_confirmation=True)
        self.advance(32)
        self.book={'time':BASE.isoformat(),'received_at':self.clock.isoformat(),'best_bid':7170,'best_ask':7180,
            'bid_qty':390,'ask_qty':1,'depth_available':True,'source':'ka10004','venue':'KRX',
            'requested_symbol':SYM,'regular':True,'status':{'pass':True}}
        self.feed.orderbooks[SYM]={**self.book,'source':'0D','received_at':BASE.isoformat()}
    def test_exact_reconfirmed_standing_book_keeps_position_and_original_market_time(self):
        self.prepare();before=copy.deepcopy(self.e.s['positions'][SYM])
        self.assertTrue(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.e.timer(self.e.prices)
        self.assertEqual(self.sell_count(),0);self.assertEqual(self.e.s['halt'],'')
        health=self.e.s['positions'][SYM]['data_health']
        self.assertEqual(health['reason_code'],'TRADE_QUIET_REST_BOOK_CONFIRMED')
        self.assertEqual(health['quote_age'],32);self.assertEqual(health['snapshot_confirmation_age'],0)
        self.assertEqual(self.h.quotes[SYM]['time'],BASE.isoformat())
        for key in ('last_tick','high','low','stop'):self.assertEqual(self.e.s['positions'][SYM][key],before[key])
    def test_disabled_flag_does_not_accept_old_quote(self):
        self.prepare();self.e.a=replace(self.e.a,stable_position_book_confirmation=False)
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
    def test_mismatch_missing_depth_wrong_route_or_old_receipt_does_not_confirm(self):
        self.prepare()
        for change in ({'bid_qty':389},{'ask_qty':0},{'best_bid':7160},{'requested_symbol':SYM+'_AL'},
                       {'received_at':BASE.isoformat()},{'received_at':None}):
            self.assertFalse(self.h.accept_book(SYM,{**self.book,**change},self.clock,'ka10004'))
    def test_disconnected_or_unhealthy_account_cannot_be_excused_by_matched_snapshot(self):
        self.prepare();self.e.connected=False
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.e.connected=True;self.e.account_ok=False
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
    def test_expired_confirmation_requires_new_probe_then_existing_blind_timeout(self):
        self.prepare();self.assertTrue(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.advance(48);self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),0)
        self.advance(64);self.e.timer(self.e.prices);self.assertEqual(self.sell_count(),1)
    def test_after_route_must_match_held_venue(self):
        self.prepare();self.clock=self.clock.replace(hour=18)
        self.feed.last_message_at=self.clock;self.e.after_enabled=True
        self.book.update(time=(self.clock-timedelta(seconds=32)).isoformat(),received_at=self.clock.isoformat(),
                         regular=False,session='AFTER',venue='SOR',requested_symbol=SYM+'_AL')
        self.feed.orderbooks[SYM]={**self.book,'source':'0D','received_at':self.book['time']}
        self.assertTrue(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.assertFalse(self.h.accept_book(SYM,{**self.book,'venue':'NXT','requested_symbol':SYM+'_NX'},self.clock,'ka10004'))
    def test_entry_provider_cannot_use_position_snapshot_confirmation(self):
        self.prepare();self.assertTrue(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.assertEqual(self.feed.orderbooks[SYM]['time'],BASE.isoformat())
        self.assertNotIn('snapshot_confirmed_at',self.feed.orderbooks[SYM])
    def test_new_flag_is_strict_and_changes_approval_fingerprint(self):
        original=self.e.a
        self.assertFalse(original.stable_position_book_confirmation)
        self.assertNotEqual(original.fingerprint(),replace(original,stable_position_book_confirmation=True).fingerprint())
        with self.assertRaises(ValueError):replace(original,stable_position_book_confirmation=1).validate()
    def test_missing_subscription_stale_ws_or_account_blocks_confirmation(self):
        self.prepare();self.sub.symbols=set()
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.sub.symbols={SYM};self.feed.last_message_at=BASE-timedelta(seconds=100)
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))
        self.feed.last_message_at=self.clock;self.e.last_account-=100
        self.assertFalse(self.h.accept_book(SYM,self.book,self.clock,'ka10004'))

if __name__=='__main__':unittest.main()
