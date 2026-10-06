"""HTS식 표 TSV 복사 및 우클릭 메뉴 기능 회귀테스트."""
import unittest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem
import operator_gui as gui
from test_gui import synthetic_status, FakeService

class TableCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        gui.configure_font(cls.app)

    def setUp(self):
        self.service = FakeService()
        self.window = gui.MainWindow(self.service, auto_refresh=False)

    def tearDown(self):
        self.window.allow_close = True
        self.window.close()

    def test_clean_tsv_value(self):
        self.assertEqual(gui.clean_tsv_value(None), '')
        self.assertEqual(gui.clean_tsv_value('정상 텍스트'), '정상 텍스트')
        self.assertEqual(gui.clean_tsv_value('줄바꿈\r\n포함\n텍스트'), '줄바꿈 포함 텍스트')
        self.assertEqual(gui.clean_tsv_value('탭\t포함\t텍스트'), '탭 포함 텍스트')

    def test_empty_table_returns_empty_tsv_and_does_not_modify_clipboard(self):
        t = gui.table(['종목', '상태', '현재가'])
        self.assertEqual(t.rowCount(), 0)
        self.assertEqual(gui.table_to_tsv(t), '')
        self.assertEqual(t.to_tsv(), '')
        
        self.app.clipboard().setText('기존클립보드')
        result = gui.copy_table_tsv(t)
        self.assertEqual(result, '')
        self.assertEqual(self.app.clipboard().text(), '기존클립보드')

    def test_table_to_tsv_formatting(self):
        t = gui.table(['종목', '상태', '현재가', '관찰 경과', '주요 차단 / 대기 이유'])
        rows = [
            ['빛과전자 069540', '진입검토 (WATCH)', '4,945원', '0초 전', '가속, Setup'],
            ['HLB제약 047920', '진입검토 (WATCH)', '12,750원', '0초 전', '가속, Setup']
        ]
        gui.rows_to_table(t, rows)
        
        expected_header = "종목\t상태\t현재가\t관찰 경과\t주요 차단 / 대기 이유"
        expected_row0 = "빛과전자 069540\t진입검토 (WATCH)\t4,945원\t0초 전\t가속, Setup"
        expected_row1 = "HLB제약 047920\t진입검토 (WATCH)\t12,750원\t0초 전\t가속, Setup"
        expected_tsv = f"{expected_header}\n{expected_row0}\n{expected_row1}"
        
        self.assertEqual(t.to_tsv(), expected_tsv)
        
        # 클립보드 복사 검증
        t.copy_all()
        self.assertEqual(self.app.clipboard().text(), expected_tsv)

    def test_table_to_tsv_selected_subset(self):
        t = gui.table(['코드', '이름', '가격'])
        gui.rows_to_table(t, [
            ['005930', '삼성전자', '60,000원'],
            ['000660', 'SK하이닉스', '180,000원'],
            ['035420', 'NAVER', '170,000원']
        ])
        
        # 1행만 복사
        tsv_row1 = gui.table_to_tsv(t, [1])
        expected_row1 = "코드\t이름\t가격\n000660\tSK하이닉스\t180,000원"
        self.assertEqual(tsv_row1, expected_row1)
        
        # 0행과 2행 다중 선택 복사
        tsv_multi = gui.table_to_tsv(t, [0, 2])
        expected_multi = "코드\t이름\t가격\n005930\t삼성전자\t60,000원\n035420\tNAVER\t170,000원"
        self.assertEqual(tsv_multi, expected_multi)

    def test_context_menu_states_for_empty_table(self):
        t = gui.table(['A', 'B'])
        menu, act_sel, act_all = gui.create_table_context_menu(t)
        self.assertEqual(act_sel.text(), '선택 행 복사')
        self.assertEqual(act_all.text(), '표 전체 복사')
        self.assertFalse(act_sel.isEnabled())
        self.assertFalse(act_all.isEnabled())

    def test_context_menu_states_with_rows_unselected(self):
        t = gui.table(['A', 'B'])
        gui.rows_to_table(t, [['1', '2'], ['3', '4']])
        t.clearSelection()
        menu, act_sel, act_all = gui.create_table_context_menu(t)
        self.assertFalse(act_sel.isEnabled())
        self.assertTrue(act_all.isEnabled())

    def test_context_menu_right_click_selects_row(self):
        t = gui.table(['A', 'B'])
        gui.rows_to_table(t, [['행0-A', '행0-B'], ['행1-A', '행1-B']])
        t.selectRow(0)
        
        # 행 1 위에서 우클릭 발생 시뮬레이션
        row1_pos = QPoint(5, t.rowViewportPosition(1) + 5)
        menu, act_sel, act_all = gui.create_table_context_menu(t, row1_pos)
        self.assertTrue(act_sel.isEnabled())
        self.assertTrue(act_all.isEnabled())
        
        # 선택이 행 1로 갱신되었는지 확인
        act_sel.trigger()
        clipboard_text = self.app.clipboard().text()
        self.assertIn('행1-A\t행1-B', clipboard_text)
        self.assertNotIn('행0-A', clipboard_text)

    def test_context_menu_right_click_preserves_multi_selection(self):
        from PySide6.QtCore import QItemSelectionModel
        t = gui.table(['A', 'B'])
        gui.rows_to_table(t, [['행0', 'V0'], ['행1', 'V1'], ['행2', 'V2']])
        
        # 행 0과 행 1 동시 선택 (Ctrl+클릭 다중 선택 시뮬레이션)
        sel_flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        t.selectionModel().select(t.model().index(0, 0), sel_flags)
        t.selectionModel().select(t.model().index(1, 0), sel_flags)
        self.assertEqual(sorted({i.row() for i in t.selectionModel().selectedRows()}), [0, 1])
        
        # 이미 선택된 행 1 위에서 우클릭 시 다중 선택 유지
        row1_pos = QPoint(5, t.rowViewportPosition(1) + 5)
        menu, act_sel, act_all = gui.create_table_context_menu(t, row1_pos)
        act_sel.trigger()
        
        clipboard_text = self.app.clipboard().text()
        self.assertIn('행0\tV0', clipboard_text)
        self.assertIn('행1\tV1', clipboard_text)

    def test_cockpit_tables_copy_features_configured(self):
        # 칵핏 메인 표들이 복사 액션 및 핸들러를 보유하는지 검증
        for table_widget in (self.window.board, self.window.event_feed, self.window.trades, self.window.decisions, self.window.bot_board):
            self.assertEqual(table_widget.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
            self.assertTrue(hasattr(table_widget, '_copy_action'))
            self.assertEqual(table_widget._copy_action.shortcut(), QKeySequence.Copy)
            self.assertTrue(callable(table_widget.to_tsv))
            self.assertTrue(callable(table_widget.copy_selected))
            self.assertTrue(callable(table_widget.copy_all))

    def test_cockpit_candidate_board_copy_e2e(self):
        s = synthetic_status()
        s['candidate_board'] = [
            {'symbol': '069540', 'name': '빛과전자', 'state': 'WATCH', 'source_count': 2, 'current_price': 4945, 'timestamp': s['time'], 'gates': {}},
            {'symbol': '047920', 'name': 'HLB제약', 'state': 'WATCH', 'source_count': 2, 'current_price': 12750, 'timestamp': s['time'], 'gates': {}}
        ]
        self.window.apply_status(s)
        self.assertEqual(self.window.board.rowCount(), 2)
        
        tsv = self.window.board.to_tsv()
        lines = tsv.split('\n')
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "종목\t상태\t현재가\t관측 경과\t주요 차단 / 대기 사유")
        self.assertIn("빛과전자 069540\t진입검토 (WATCH)\t4,945원", lines[1])
        self.assertIn("HLB제약 047920\t진입검토 (WATCH)\t12,750원", lines[2])
        
        # 1개 행 선택 복사
        self.window.board.selectRow(0)
        copied = self.window.board.copy_selected()
        self.assertEqual(self.app.clipboard().text(), copied)
        self.assertIn("빛과전자 069540", copied)
        self.assertNotIn("HLB제약", copied)

    def test_cockpit_event_feed_copy_e2e(self):
        s = synthetic_status()
        s['important_events'] = [
            ('09:05:01', '후보 전이', '빛과전자 069540', 'WATCH 승격 (2개 랭킹 포착)'),
            ('09:10:15', '주문', '빛과전자 069540', 'BUY 100주 주문 접수')
        ]
        self.window.apply_status(s)
        self.assertEqual(self.window.event_feed.rowCount(), 2)
        
        tsv = self.window.event_feed.to_tsv()
        lines = tsv.split('\n')
        self.assertEqual(lines[0], "시각\t구분\t종목 / 대상\t상세 내용")
        self.assertIn("09:10:15\t주문\t빛과전자 069540\tBUY 100주 주문 접수", lines[1])
        self.assertIn("09:05:01\t후보 전이\t빛과전자 069540\tWATCH 승격 (2개 랭킹 포착)", lines[2])

    def test_cockpit_bot_board_copy_e2e(self):
        s = synthetic_status()
        s['bot_positions'] = [
            {'name': '빛과전자', 'symbol': '069540', 'quantity': 50, 'setup': 'A', 'entry': 4900, 'current': 5000,
             'invalidation': 4750, 'protection': 4850, 'hold_reason': '보호선 상회', 'next_exit': '15:10 당일 청산'}
        ]
        self.window.apply_status(s)
        self.assertEqual(self.window.bot_board.rowCount(), 1)
        
        tsv = self.window.bot_board.to_tsv()
        lines = tsv.split('\n')
        self.assertEqual(lines[0], "종목\t보유수량 (Setup)\t진입 평균가\t현재가\t초기 무효화\t현재 보호선\t대응 상태\t청산 조건")
        self.assertIn("빛과전자\t50주 (A)\t4,900원\t5,000원\t4,750원\t4,850원\t보호선 상회\t15:10 당일 청산", lines[1])

    def test_cockpit_trades_copy_e2e(self):
        s = synthetic_status()
        s['orders'] = [
            {'timestamp': '2026-10-06T09:15:00+09:00', 'name': '빛과전자', 'symbol': '069540', 'side': 'BUY',
             'qty': 100, 'filled': 100, 'limit': 4950, 'reason': 'Setup A', 'execution': 'PAPER',
             'status': 'FILLED', 'pnl_estimate': 0, 'strategy_version': 'V1.1'}
        ]
        self.window.apply_status(s)
        self.assertGreaterEqual(self.window.trades.rowCount(), 1)
        
        tsv = self.window.trades.to_tsv()
        lines = tsv.split('\n')
        self.assertEqual(lines[0], "시각\t종목\t방향\t수량 / 체결\t가격\t이유\t구분\t상태\t손익\t전략버전")
        self.assertIn("빛과전자\tBUY\t100 / 100\t4950\tSetup A\tPAPER\tFILLED", lines[1])

    def test_cockpit_decisions_copy_e2e(self):
        s = synthetic_status()
        s['decision_rows'] = [
            {'time': '2026-10-06T09:12:00+09:00', 'name': 'HLB제약', 'symbol': '047920',
             'primary_reason_code': 'ONE_SHARE_UNAFFORDABLE', 'reason': '자본 부족'}
        ]
        self.window.apply_status(s)
        self.assertEqual(self.window.decisions.rowCount(), 1)
        
        tsv = self.window.decisions.to_tsv()
        lines = tsv.split('\n')
        self.assertEqual(lines[0], "시각\t종목\t판단 / 실제 사유")
        self.assertIn("2026-10-06T09:12:00+09:00\tHLB제약\t", lines[1])
        self.assertIn("ONE_SHARE_UNAFFORDABLE", lines[1])

if __name__ == '__main__':
    unittest.main()
