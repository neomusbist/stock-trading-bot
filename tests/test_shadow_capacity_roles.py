"""RAW phase must not evict a committed WATCH from the independent observer."""
import unittest
from datetime import timedelta
from tests import test_shadow_observation as fixtures

class ShadowCapacityRoleTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.ShadowTests();f.setUp()
        self.f=f;self.s=f.s;self.start=f.start;self.s.limit=2
        self.s.detector.continuous_observation=True
    def row(self,sym,role,score):
        return {**self.f.row(2,role,sym),'discovery_score':score}
    def test_raw_phase_retains_watch_history_across_repeated_capacity_pressure(self):
        self.s.update([self.row('001210','WATCH',1),self.row('999980','SHADOW',2)],self.start)
        for sec in range(71):self.s.tick(self.f.tick(sec),self.start+timedelta(seconds=sec))
        buf=self.s.detector.buffers['001210'];epoch=self.s.detector.observation_start['001210']
        for sec in (71,72,78):
            self.s.update([self.row('001210','RAW',1),self.row('999970','RAW',100),self.row('999960','RAW',90)],self.start+timedelta(seconds=sec))
            self.s.tick(self.f.tick(sec),self.start+timedelta(seconds=sec))
            self.assertIn('001210',self.s.rows)
            self.assertIs(buf,self.s.detector.buffers['001210'])
            self.assertEqual(epoch,self.s.detector.observation_start['001210'])
        self.assertFalse(any(k in {'SHADOW_ENDED','SHADOW_DATA_RESET','SHADOW_CAPACITY'} and v['symbol']=='001210' for k,v in self.f.events))
        self.assertFalse(hasattr(self.s,'broker'))
        self.assertTrue(all(v.get('SHADOW_ONLY') for _,v in self.f.events))
    def test_committed_demotion_is_not_pinned_forever(self):
        self.s.update([self.row('001210','WATCH',1)],self.start)
        self.s.update([self.row('001210','RESERVE',1),self.row('999970','WATCH',100),self.row('999960','WATCH',90)],self.start+timedelta(seconds=1))
        self.assertNotIn('001210',self.s.rows)
        self.assertEqual(set(self.s.rows),{'999970','999960'})
    def test_raw_never_creates_watch_permission(self):
        self.s.update([self.row('001210','RAW',10)],self.start)
        self.s.tick(self.f.tick(),self.start)
        evaluation=self.s.latest['001210']
        self.assertEqual(evaluation['discovery_status'],'RAW')
        self.assertIs(evaluation['actual_order_permission'],False)
        event=next(v for k,v in reversed(self.f.events) if k=='SHADOW_EVAL')
        self.assertIs(event['SHADOW_ONLY'],True)

if __name__=='__main__':unittest.main()
