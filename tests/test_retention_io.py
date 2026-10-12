import unittest,tempfile,json
from pathlib import Path
from datetime import datetime,timedelta
from core.settings import KST
from core.util import shared_binary_reader
from core.event_log import append_jsonl
from automation.observation_retention import ObservationRetention
import tests.test_subscription_refresh as fixtures

class RetentionTests(unittest.TestCase):
    def test_capacity_and_roles_never_authorize_retained_or_drop_critical(self):
        stamp=datetime(2026,10,12,12,6,tzinfo=KST);r=ObservationRetention()
        r.select({'old'},{'held'},{'old','new','extra'},stamp,60,3)
        desired,observed,retained=r.select({'new'},{'held'},['extra','next'],stamp+timedelta(seconds=6),60,3)
        self.assertEqual(desired,{'new','held','old','extra'});self.assertEqual(observed,{'old','extra'})
        self.assertEqual(retained,{'old'});self.assertEqual(len(desired),4) # Existing baseline also used four slots.
        desired,_,retained=r.select({'new'},{'held'},{'extra'},stamp+timedelta(seconds=61),60,3)
        self.assertEqual(retained,set());self.assertEqual(desired,{'new','held','extra'})
    def test_full_budget_grace_uses_diagnostic_slots_in_existing_priority_order(self):
        stamp=datetime(2026,10,12,12,6,tzinfo=KST);r=ObservationRetention()
        r.select({'old'},{'held'},[],stamp,60,3)
        desired,_,grace=r.select({'new'},{'held'},['preferred','weaker'],stamp+timedelta(seconds=6),60,3)
        self.assertEqual(desired,{'new','held','old','preferred'})
        self.assertEqual(grace,{'old'})
        # Three old roles do not expand a full two-symbol diagnostic baseline.
        r.last_watch.update({'old2':stamp,'old3':stamp})
        desired,_,_=r.select({'new'},{'held'},['preferred','weaker'],stamp+timedelta(seconds=7),60,3)
        self.assertEqual(len(desired),4);self.assertTrue({'new','held'}<=desired)
    def test_next_day_never_retains_previous_watch(self):
        r=ObservationRetention();stamp=datetime(2026,10,12,12,tzinfo=KST)
        r.select({'old'},set(),set(),stamp,60,2)
        self.assertEqual(r.select(set(),set(),set(),stamp+timedelta(days=1),60,2),(set(),set(),set()))

class RetentionPathTests(unittest.TestCase):
    setUp=fixtures.SubscriptionRefreshTests.setUp
    tick=fixtures.SubscriptionRefreshTests.tick
    warm=fixtures.SubscriptionRefreshTests.warm
    refresh=fixtures.SubscriptionRefreshTests.refresh
    def test_five_second_focus_demotion_rejoin_has_no_release_and_keeps_70s(self):
        self.warm();r=ObservationRetention();start=self.d.observation_start[fixtures.SYM]
        bars=self.d.history[fixtures.SYM];r.select({fixtures.SYM},{fixtures.SYM},set(),self.clock,60,2)
        desired,_,_=r.select({fixtures.OTHER},{fixtures.OTHER},set(),self.clock+timedelta(seconds=1),60,2)
        self.refresh(desired)
        for sec in range(121,128):self.d.ingest(self.tick(sec=sec),fixtures.START+timedelta(seconds=sec))
        self.clock+=timedelta(seconds=7)
        desired,_,_=r.select({fixtures.SYM},{fixtures.SYM},set(),self.clock,60,2);self.refresh(desired)
        self.assertIs(bars,self.d.history[fixtures.SYM]);self.assertEqual(start,self.d.observation_start[fixtures.SYM])
        self.assertNotIn(fixtures.SYM,self.sub.detached);self.assertEqual(self.broker.orders,[])
        self.assertGreaterEqual(self.d.base_gates(self.d.last[fixtures.SYM],self.watch)['warmup']['values']['elapsed_seconds'],70)

class SharedLogTests(unittest.TestCase):
    def test_live_reader_does_not_block_lossless_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'events.jsonl';append_jsonl(path,{'n':0})
            with shared_binary_reader(path) as reader:
                append_jsonl(path,{'n':1},max_bytes=1)
                self.assertEqual(json.loads(reader.read()),{'n':0})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'n':1})
            archives=list((path.parent/'archive').iterdir());self.assertEqual(len(archives),1)
            self.assertEqual(json.loads(archives[0].read_text(encoding='utf-8')),{'n':0})

if __name__=='__main__':unittest.main()
