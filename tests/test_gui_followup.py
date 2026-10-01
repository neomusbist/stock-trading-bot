"""표시 변경만 검사하는 오프라인 GUI 회귀. 기존 검사는 변경하지 않는다."""
import copy, unittest
from test_gui import synthetic_status, FakeService
from operator_ui_service import candidate_display, presentation, CANDIDATE_FIELDS, start_block
import operator_gui as gui
from PySide6.QtWidgets import QApplication, QGroupBox

class UiPresentationFollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]); gui.configure_font(cls.app)
    def setUp(self):
        self.service=FakeService(); self.window=gui.MainWindow(self.service,auto_refresh=False)
    def tearDown(self):
        self.window.allow_close=True; self.window.close()
    def test_local_safety_explicit_unverified(self):
        self.assertEqual(presentation(synthetic_status())[2],'로컬 상태 정상 / 실계좌 미확인')
    def test_verified_safety_requires_both(self):
        self.assertEqual(presentation(synthetic_status(account_fresh=True,websocket='현재 연결 정상'))[2],'정상 / 계좌·실시간 연결 확인')
    def test_account_alone_not_verified(self):
        self.assertIn('실계좌 미확인',presentation(synthetic_status(account_fresh=True))[2])
    def test_websocket_alone_not_verified(self):
        self.assertIn('실계좌 미확인',presentation(synthetic_status(websocket='현재 연결 정상'))[2])
    def test_outside_never_strong_normal(self):
        self.assertIn('실계좌 미확인',presentation(synthetic_status(supported=False,account_fresh=True,websocket='현재 연결 정상'))[2])
    def test_safety_warning_priorities_preserved(self):
        for changes,expected in [({'halt':'TEST'},'HALT'),({'hard_ok':False},'보호정책'),({'uncertain':1},'불명확 주문'),({'active_error':True},'현재 오류')]:
            self.assertIn(expected,presentation(synthetic_status(**changes))[2])
    def test_stale_process_still_warning(self):
        s=synthetic_status(process={'running':True,'managed':True,'process_heartbeat_fresh':False})
        self.assertIn('프로세스 응답 지연',presentation(s)[2]); self.assertTrue(start_block(s))
    def test_outside_single_message_and_dash_counters(self):
        self.window.apply_status(synthetic_status(supported=False))
        self.assertEqual(self.window.summary_stamp.text(),'현재 운영시간 밖입니다. 최신 후보 집계가 없습니다.')
        for key,label in CANDIDATE_FIELDS.items(): self.assertEqual(self.window.stats[key].text(),label+'  -')
    def test_empty_summary_one_unknown_message(self):
        self.window.apply_status(synthetic_status(candidate_summary={}))
        self.assertEqual(self.window.summary_stamp.text().count('미확인'),1)
        self.assertTrue(all(x.text().endswith('  -') for x in self.window.stats.values()))
    def test_fresh_numbers_unchanged(self):
        s=synthetic_status(); counts,stamp=candidate_display(s)
        for key in CANDIDATE_FIELDS: self.assertEqual(counts[key],s['candidate_summary'][key])
        self.assertIn(s['candidate_summary']['timestamp'],stamp)
    def test_old_and_future_summaries_hidden(self):
        for timestamp in ('2026-10-01T09:00:00+09:00','2026-10-01T09:30:01+09:00','bad'):
            s=synthetic_status(); s['candidate_summary']['timestamp']=timestamp
            self.assertTrue(all(x=='-' for x in candidate_display(s)[0].values()))
    def test_existing_scan_period_used_for_display(self):
        s=synthetic_status(candidate_refresh_seconds=15); s['candidate_summary']['timestamp']='2026-10-01T09:29:15+09:00'
        self.assertEqual(candidate_display(s)[0]['raw_candidates'],24)
        s['time']='2026-10-01T09:30:01+09:00'; self.assertEqual(candidate_display(s)[0]['raw_candidates'],'-')
    def test_previous_process_cycle_hidden(self):
        s=synthetic_status(process={'running':True,'started':'2026-10-01T09:29:30+09:00'})
        self.assertEqual(candidate_display(s)[0]['raw_candidates'],'-')
        s['candidate_summary']['cycle_started']='2026-10-01T09:29:31+09:00'
        self.assertEqual(candidate_display(s)[0]['raw_candidates'],24)
    def test_partial_summary_missing_values_are_dash(self):
        s=synthetic_status(); del s['candidate_summary']['affordable']; s['candidate_summary']['risk_zero_qty']=None
        counts,_=candidate_display(s); self.assertEqual(counts['affordable'],'-'); self.assertEqual(counts['risk_zero_qty'],'-'); self.assertEqual(counts['raw_candidates'],24)
    def test_display_does_not_mutate_source_or_call_service(self):
        s=synthetic_status(); before=copy.deepcopy(s); self.window.apply_status(s)
        self.assertEqual(s,before); self.assertEqual(self.service.calls,[]); self.assertEqual(self.service.snapshot_calls,0)
    def test_disabled_start_reason_visible_and_conditions_unchanged(self):
        for s in [synthetic_status(supported=False),synthetic_status(),synthetic_status(uncertain=1),synthetic_status(halt='TEST'),synthetic_status(time='2026-10-01T15:05:00+09:00')]:
            self.window.apply_status(s); self.assertEqual(self.window.buttons['start'].isEnabled(),not bool(start_block(s)))
        self.window.apply_status(synthetic_status(supported=False)); self.assertIn('시간 밖',self.window.buttons['start'].text()); self.assertIn('운영시간 밖',self.window.buttons['start'].toolTip())
    def test_disabled_stop_settings_flat_reasons(self):
        self.window.apply_status(synthetic_status()); self.assertIn('중지되어',self.window.buttons['stop'].toolTip()); self.assertIn('없습니다',self.window.buttons['flat'].toolTip())
        self.window.apply_status(synthetic_status(process={'running':True})); self.assertFalse(self.window.buttons['settings'].isEnabled()); self.assertIn('먼저 정상 중지',self.window.buttons['settings'].toolTip())
    def test_protection_title_clear_policy_unchanged(self):
        self.window.apply_status(synthetic_status())
        self.assertIn('자동매매 제외 / 기존보유 보호',[box.title() for box in self.window.findChildren(QGroupBox)])
        self.assertEqual(self.window.protection.text().count('HARD_LOCK / 자동매매 금지'),2)
        self.assertIn('BREAKEVEN_EXIT / 기준 5,834원',self.window.protection.text())

    def test_settings_disabled_explanations_no_enable_change(self):
        for s,reason in [(synthetic_status(supported=False),'운영시간 밖'),(synthetic_status(costs_confirmed=False),'비용 설정 미확인'),(synthetic_status(process={'running':True}),'감시·장부 안전상태 확인 필요')]:
            self.window.apply_status(s); dialog=gui.SettingsDialog(self.window)
            self.assertFalse(dialog.ready.isEnabled()); self.assertFalse(dialog.authorize.isEnabled())
            self.assertEqual(dialog.ready.toolTip(),reason); self.assertEqual(dialog.authorize.toolTip(),reason)
            dialog.close()
    def test_local_safety_is_neutral_and_verified_is_green(self):
        self.window.apply_status(synthetic_status())
        self.assertIn('#56657a',self.window.cards['safety'].styleSheet())
        self.window.apply_status(synthetic_status(account_fresh=True,websocket='현재 연결 정상'))
        self.assertIn('#24684a',self.window.cards['safety'].styleSheet())
