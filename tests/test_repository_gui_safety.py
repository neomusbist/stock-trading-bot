"""새 설치 GUI의 안전 기본값과 서비스 경계. 실제 연결/주문 없이 검사한다."""
import ast,contextlib,io,json,os,subprocess,sys,tempfile,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from core.settings import ROOT,KST
from operator_ui_service import UserOperations
import operator_gui as gui
from PySide6.QtWidgets import QApplication

class RepositoryGuiSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]); gui.configure_font(cls.app)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        for name in ('config.example.json','automation.example.json'):
            (self.root/name).write_bytes((ROOT/name).read_bytes())
        self.gateway=Mock(); self.service=UserOperations(self.root,clock=lambda:datetime(2026,10,1,10,0,tzinfo=KST),gateway=self.gateway)
    def test_fresh_clone_gui_opens_without_local_credentials_or_runtime(self):
        status=self.service.snapshot(); window=gui.MainWindow(self.service,auto_refresh=False)
        try:
            window.apply_status(status); window.show(); self.app.processEvents()
            self.assertEqual(status['mode'],'ARMED'); self.assertFalse(status['live_mode']); self.assertEqual(status['cap'],0)
            self.assertFalse(status['costs_confirmed']); self.assertFalse(status['live_approved']); self.assertFalse(status['trading_on'])
            self.assertEqual(window.cards['cap'].text(),'0원'); self.assertIn('OFF',window.notice.text()); self.assertTrue(window.isVisible())
            self.assertEqual(sorted(p.name for p in self.root.iterdir()),['automation.example.json','config.example.json'])
            self.assertEqual(self.gateway.mock_calls,[])
        finally: window.allow_close=True; window.close()
    def test_clone_ready_guard_cap_zero_never_queries_account(self):
        with self.assertRaisesRegex(ValueError,'승인한도가 0원'): self.service.prepare_capital()
        self.assertEqual(self.gateway.mock_calls,[]); self.assertFalse((self.root/'runtime/automation_policy.json').exists())
    def test_clone_no_live_authorization_from_confirmation_alone(self):
        with self.assertRaises(ValueError): self.service.authorize_capital('0','실제매매승인')
        status=self.service.snapshot(); self.assertFalse(status['live_approved']); self.assertFalse(status['live_mode'])
        self.assertEqual(self.gateway.mock_calls,[]); self.assertFalse((self.root/'runtime/automation_policy.json').exists())
    def test_gui_has_no_broker_endpoint_or_order_import(self):
        tree=ast.parse((ROOT/'operator_gui.py').read_text(encoding='utf-8-sig'))
        imports=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): imports.extend(a.name for a in node.names)
            if isinstance(node,ast.ImportFrom): imports.append(node.module or '')
        self.assertFalse(any(x in {'requests','urllib','websocket'} or x.startswith(('core.broker','core.engine','core.strategy','automation.engine')) for x in imports))
        text=(ROOT/'operator_gui.py').read_text(encoding='utf-8-sig')
        self.assertNotIn('/api/dostk/',text); self.assertNotIn('api.kiwoom.com',text)
        self.assertFalse(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in {'buy','sell','_http','submit_order'} for n in ast.walk(tree)))
    def test_missing_pyside6_real_import_uses_console_fallback(self):
        script='''import sys,socket,importlib.abc
sys.path.insert(0,sys.argv[1])
def denied(*a,**k): raise AssertionError('network forbidden')
socket.socket.connect=denied; socket.create_connection=denied
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='PySide6' or fullname.startswith('PySide6.'): raise ModuleNotFoundError('PySide6 missing in isolated test')
sys.meta_path.insert(0,NoQt())
import gui_launch
seen=[]
assert gui_launch.launch(console=lambda:(seen.append('console') or 0))==0
assert seen==['console']
'''
        result=subprocess.run([sys.executable,'-I','-X','utf8','-c',script,str(ROOT)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PySide6 missing in isolated test',result.stdout); self.assertIn('기존 콘솔로 전환합니다',result.stdout)
    def test_dependency_declared_without_binary_requirement(self):
        req=(ROOT/'requirements.txt').read_text(encoding='utf-8-sig').splitlines()
        self.assertIn('PySide6==6.11.2',req); self.assertFalse(any('.dll' in x or '.whl' in x or 'gui_vendor' in x for x in req))
