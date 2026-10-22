from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json,queue,random,tempfile,unittest
from unittest.mock import patch
from core.realtime import Feed
from core.settings import Config,KST
from core.util import atomic_json
from automation.runner import _protected_feed_work_pending
from automation.engine import AutonomousEngine
from automation.policy import Policy

AT=datetime(2026,10,22,10,20,tzinfo=KST)

class FeedControlHotpath(unittest.TestCase):
    def feed(self):return Feed(SimpleNamespace(cfg=Config()),['999990'])

    def test_repeated_priority_queries_do_not_rescan_queued_payloads(self):
        class Counted(dict):
            reads=0
            def get(self,key,*args):
                if key=='data':self.reads+=1
                return super().get(key,*args)
        feed=self.feed()
        rows=[Counted(trnm='REAL',data=[{'type':'0B','item':'999990'}]) for _ in range(100)]
        for row in rows:feed.q.put((AT,row))
        for row in rows:row.reads=0
        for _ in range(50):self.assertFalse(_protected_feed_work_pending(feed,set()))
        self.assertEqual(sum(row.reads for row in rows),0)

    def test_priority_index_matches_original_fifo_predicate(self):
        feed=self.feed();expected=[];rng=random.Random(42)
        for _ in range(250):
            if expected and rng.randrange(3)==0:
                self.assertIs(feed.q.get_nowait()[1],expected.pop(0))
            else:
                row={'trnm':rng.choice(['REAL']*6+['REG','ERROR','DISCONNECTED']),
                     'data':[{'type':rng.choice(['0B','0D','00','0s']),
                              'item':rng.choice(['999990','999991'])}]}
                feed.q.put((AT,row));expected.append(row)
            for critical in (set(),{'999990'},{'999991'}):
                old=any(m.get('trnm') in {'REG','ERROR','DISCONNECTED'} or
                    any(i.get('type') in {'00','0s'} or i.get('item') in critical for i in m.get('data',[]))
                    for m in expected)
                self.assertEqual(_protected_feed_work_pending(feed,critical),old)
        while expected:self.assertIs(feed.q.get_nowait()[1],expected.pop(0))
        self.assertFalse(_protected_feed_work_pending(feed,{'999990'}))

    def test_generic_queue_fallback_still_defers_protected_messages(self):
        feed=SimpleNamespace(q=queue.Queue())
        feed.q.put((AT,{'trnm':'REAL','data':[{'type':'00'}]}))
        self.assertTrue(_protected_feed_work_pending(feed,set()))

    def test_unchanged_controls_do_not_reopen_policy_on_every_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(Path(tmp)/'policy.json')
            engine=SimpleNamespace(policy=policy,control_revision=policy.state['revision'],
                s={'automation':{},'positions':{}},active_orders=lambda *a,**k:[],event=lambda *a,**k:None)
            from automation import policy as module
            with patch.object(module,'read_json_retry',wraps=module.read_json_retry) as read:
                for _ in range(20):self.assertFalse(AutonomousEngine.controls(engine))
            self.assertLessEqual(read.call_count,1)

    def test_policy_replacement_stop_and_malformed_state_are_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(Path(tmp)/'policy.json');policy.refresh_if_changed()
            value={**policy.state,'mode':'STOP','revision':1}
            atomic_json(policy.path,value)
            self.assertEqual(policy.refresh_if_changed()['mode'],'STOP')
            policy.path.write_text('{bad',encoding='utf-8')
            with self.assertRaises(json.JSONDecodeError):policy.refresh_if_changed()

    def test_default_refresh_always_rereads_for_order_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(Path(tmp)/'policy.json')
            from automation import policy as module
            with patch.object(module,'read_json_retry',wraps=module.read_json_retry) as read:
                policy.refresh();policy.refresh()
            self.assertEqual(read.call_count,2)

    def test_replacement_during_read_never_binds_old_state_to_new_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy=Policy(Path(tmp)/'policy.json')
            from automation import policy as module
            original=module.read_json_retry
            def replacing(path):
                row=original(path)
                atomic_json(path,{**row,'mode':'STOP','revision':row['revision']+1})
                return row
            with patch.object(module,'read_json_retry',side_effect=replacing):policy.refresh_if_changed()
            self.assertEqual(policy.refresh_if_changed()['mode'],'STOP')

if __name__=='__main__':unittest.main()
