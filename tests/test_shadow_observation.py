import unittest,tempfile,ast
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta
from unittest.mock import Mock
from core.settings import Config,KST
from core.strategy import Tick
from automation.policy import AutomationConfig,Policy
from automation.capital import Costs
from automation.radar import DynamicRadar
from automation.shadow_observation import ShadowObserver
class ShadowTests(unittest.TestCase):
 def setUp(self):
  self.cfg=Config();self.auto=AutomationConfig();self.events=[];self.risk=Mock(return_value={'quantity':1});self.s=ShadowObserver(self.cfg,self.auto,Costs(.00015,.00015,.002,.002,.003),lambda k,**v:self.events.append((k,v)),self.risk);self.start=datetime(2026,10,2,11,22,tzinfo=KST)
 def row(self,n=1,status='REJECT',sym='001210'):
  return {'code':sym,'name':'합성','source_count':n,'status':status,'reason':'소스 교집합 부족','reference_price':9990,'affordability':{'entry_eligible':True},'shadow_view':{'status':'ELIGIBLE','market_status':{'pass':True},'resistance':12000}}
 def tick(self,sec=0):return Tick('001210',self.start+timedelta(seconds=sec),10000,100,100+sec,10010,9990)
 def test_single_source_collects(self):
  self.s.update([self.row()],self.start);self.s.tick(self.tick(),self.start);self.assertIn('001210',self.s.detector.last);self.assertTrue(all(v['SHADOW_ONLY'] for _,v in self.events))
 def test_no_order_surface_or_live_dispatch(self):
  self.assertFalse(hasattr(self.s,'broker'));self.assertFalse(hasattr(self.s,'engine'));self.assertFalse(hasattr(self.s,'enter'))
  root=Path(__file__).resolve().parents[1];s=(root/'automation/runner.py').read_text(encoding='utf-8');self.assertIn('received,real_symbols,lookup',s);self.assertNotIn('engine.enter(', (root/'automation/shadow_observation.py').read_text(encoding='utf-8'))
 def test_source_changes_preserve_fresh_buffers_and_completed_bars(self):
  self.s.update([self.row()],self.start);self.s.tick(self.tick(),self.start);buf=self.s.detector.buffers['001210'];self.s.update([self.row(2,'WATCH')],self.start+timedelta(seconds=1));self.s.update([self.row()],self.start+timedelta(seconds=2));self.assertIs(buf,self.s.detector.buffers['001210']);self.assertEqual(self.events[-1][1]['data_action'],'RETAIN_FRESH')
  self.setUp();self.s.update([self.row()],self.start)
  for sec in range(121):self.s.detector.ingest(self.tick(sec),self.start+timedelta(seconds=sec))
  bars=self.s.detector.history['001210'];self.assertGreaterEqual(len(bars),2)
  self.s.update([self.row(2,'WATCH')],self.start+timedelta(seconds=121));self.s.update([self.row()],self.start+timedelta(seconds=122));self.assertIs(bars,self.s.detector.history['001210'])
 def test_stale_gap_resets(self):
  self.s.update([self.row()],self.start);self.s.tick(self.tick(),self.start);self.s.tick(self.tick(10),self.start+timedelta(seconds=10));self.assertEqual(len(self.s.detector.buffers['001210']),1);self.assertTrue(any(k=='SHADOW_DATA_RESET' for k,_ in self.events))
 def test_shadow_exclusions_matrix(self):
  for sym in ['005930','017900','052260','267320']:self.s.update([self.row(sym=sym)],self.start);self.assertEqual(self.s.rows,{})
  row=self.row();row['shadow_view']['market_status']['pass']=None;self.s.update([row],self.start);self.assertEqual(self.s.rows,{})
  row2=self.row();row2['affordability']['entry_eligible']=False;self.s.update([row2],self.start);self.assertEqual(self.s.rows,{})
 def test_all_abc_failure_records(self):
  self.s.update([self.row()],self.start);self.s.tick(self.tick(),self.start);e=self.events[-1][1]
  # Optional diagnostic trials do not remove the three required legacy records.
  legacy=[x for x in e['setup_trials'] if x['setup'] in {'A','B','C'}]
  self.assertEqual([x['setup'] for x in legacy],['A','B','C']);self.assertFalse(e['actual_order_permission']);self.assertTrue(all(x['failed_reasons'] for x in legacy))
 def test_virtual_risk_no_order_state(self):
  self.s.update([self.row()],self.start)
  with unittest.mock.patch.object(self.s.detector,'box',return_value={'pattern':'C','breakout':10000,'support':9900,'projection':10200}):self.s.tick(self.tick(),self.start)
  self.assertTrue(self.risk.called);self.assertEqual(self.events[-1][1]['setup_trials'][2]['virtual_risk_quantity'],{'quantity':1});self.assertFalse(self.events[-1][1]['actual_order_permission'])
 def test_real_source_rule_unchanged(self):
  with tempfile.TemporaryDirectory() as tmp:
   radar=DynamicRadar(self.cfg,self.auto,Policy(Path(tmp)/'policy.json'));radar.available_capital=20000
   item={'code':'001210','name':'합성','reference_price':9990,'source_details':{'one':{'observed_rank':1,'values':{}}}}
   fn=Mock(return_value={'status':'ELIGIBLE','overhead_share_estimate':0})
   rows,watch,_=radar.update([item],fn,self.start);self.assertEqual(watch,[]);fn.assert_not_called();self.assertEqual(rows[0]['reason'],'소스 교집합 부족')
 def test_iso_payload_timestamp(self):
  self.s.update([self.row()],self.start.isoformat());self.assertIn('001210',self.s.rows)
 def test_virtual_evaluation_preserves_real_engine_state(self):
  import copy
  from automation.engine import AutonomousEngine
  try:
   from test_automation import FakeBroker
  except ImportError:
   from tests.test_automation import FakeBroker
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   broker=FakeBroker();engine=AutonomousEngine(self.cfg,self.auto,Policy(Path(tmp)/'policy.json'),Path(tmp)/'engine',broker,lambda:self.start)
   before=copy.deepcopy(engine.s);s=ShadowObserver(self.cfg,self.auto,engine.costs,lambda *a,**kw:None,risk=lambda sig:engine.quantity_details(sig,20000,20000,60))
   s.update([self.row()],self.start)
   with patch.object(s.detector,'box',return_value={'pattern':'C','breakout':10000,'support':9900,'projection':10200}):s.tick(self.tick(),self.start)
   self.assertEqual(before,engine.s);self.assertEqual(broker.orders,[]);self.assertEqual(broker.cancels,[])

if __name__=='__main__':unittest.main()
