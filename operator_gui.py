"""읽기 중심 PySide6 운영 화면. 실행/설정은 UserOperations 서비스에만 위임한다."""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parent
for folder in ('vendor','gui_vendor'):
    sys.path.insert(0,str(ROOT/folder))
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtGui import QFont,QFontDatabase,QAction,QKeySequence
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QLabel,QPushButton,
    QVBoxLayout,QHBoxLayout,QGridLayout,QGroupBox,QSplitter,QTabWidget,QTableWidget,
    QTableWidgetItem,QHeaderView,QPlainTextEdit,QMessageBox,QDialog,QLineEdit,QFormLayout,QInputDialog,QProgressBar,QMenu)
from operator_ui_service import (UserOperations, CANDIDATE_FIELDS, GATE_NAMES, money, presentation,
    start_block, human_decision, result_text, candidate_display, candidate_wait_reason,
    format_observation_age, human_blocker_reason, extract_important_events, context_description,exit_plan_text,exit_plan_presentation)
from operator_service import decision_line, read_json
from local_diagnostics import redact

def configure_font(app):
    # offscreen 검사에서도 Windows 기본 한글 글꼴을 실제 로드한다.
    # 네이티브 플랫폼에서 이미 등록된 글꼴은 중복 등록하지 않는다.
    if 'Malgun Gothic' not in QFontDatabase.families():
        fonts=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
        for name in ('malgun.ttf','malgunbd.ttf'):
            path=fonts/name
            if path.exists(): QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont('Malgun Gothic',10))

class Signals(QObject):
    done=Signal(object)
    failed=Signal(str)

class Work(QRunnable):
    def __init__(self,operation):
        super().__init__(); self.operation=operation; self.signals=Signals()
    def run(self):
        try: self.signals.done.emit(self.operation())
        except Exception as error:
            self.signals.failed.emit(redact(type(error).__name__+': '+str(error)))

def clean_tsv_value(val):
    if val is None:
        return ''
    return str(val).replace('\r\n', ' ').replace('\n', ' ').replace('\t', ' ')

def table_to_tsv(widget, row_indices=None):
    col_count = widget.columnCount()
    if col_count == 0:
        return ''
    headers = []
    for c in range(col_count):
        item = widget.horizontalHeaderItem(c)
        h_text = item.text() if item is not None else f'열 {c + 1}'
        headers.append(clean_tsv_value(h_text))
    
    total_rows = widget.rowCount()
    if total_rows == 0:
        return ''
    
    if row_indices is None:
        target_rows = list(range(total_rows))
    else:
        target_rows = sorted({r for r in row_indices if 0 <= r < total_rows})
    
    if not target_rows:
        return ''
    
    lines = ['\t'.join(headers)]
    for r in target_rows:
        row_vals = []
        for c in range(col_count):
            item = widget.item(r, c)
            val = item.text() if item is not None else ''
            row_vals.append(clean_tsv_value(val))
        lines.append('\t'.join(row_vals))
    return '\n'.join(lines)

def copy_table_tsv(widget, row_indices=None):
    tsv = table_to_tsv(widget, row_indices)
    if not tsv:
        return ''
    app = QApplication.instance()
    if app:
        cb = app.clipboard()
        if cb:
            cb.setText(tsv)
    return tsv

def create_table_context_menu(widget, pos=None):
    menu = QMenu(widget)
    total_rows = widget.rowCount()
    selected_indexes = widget.selectionModel().selectedIndexes() if widget.selectionModel() else []
    selected_rows = sorted({i.row() for i in selected_indexes})
    if pos is not None:
        idx = widget.indexAt(pos)
        if idx.isValid() and idx.row() not in selected_rows:
            widget.selectRow(idx.row())
            selected_rows = [idx.row()]
    has_rows = total_rows > 0
    has_selection = has_rows and len(selected_rows) > 0
    act_sel = menu.addAction('선택 행 복사')
    act_sel.setEnabled(has_selection)
    act_sel.triggered.connect(lambda: copy_table_tsv(widget, selected_rows))
    act_all = menu.addAction('표 전체 복사')
    act_all.setEnabled(has_rows)
    act_all.triggered.connect(lambda: copy_table_tsv(widget, None))
    return menu, act_sel, act_all

def setup_table_copy_menu(w):
    w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    def on_custom_context_menu(pos):
        menu, _, _ = create_table_context_menu(w, pos)
        menu.exec(w.viewport().mapToGlobal(pos))
    w.customContextMenuRequested.connect(on_custom_context_menu)
    
    copy_act = QAction(w)
    copy_act.setShortcut(QKeySequence.Copy)
    copy_act.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
    def on_copy_shortcut():
        if w.rowCount() == 0:
            return
        selected_indexes = w.selectionModel().selectedIndexes() if w.selectionModel() else []
        selected_rows = sorted({i.row() for i in selected_indexes})
        copy_table_tsv(w, selected_rows if selected_rows else None)
    copy_act.triggered.connect(on_copy_shortcut)
    w.addAction(copy_act)
    w._copy_action = copy_act
    
    w.to_tsv = lambda rows=None: table_to_tsv(w, rows)
    w.copy_selected = lambda: copy_table_tsv(w, sorted({i.row() for i in w.selectionModel().selectedIndexes()}) if w.selectionModel() else [])
    w.copy_all = lambda: copy_table_tsv(w, None)

DEFAULT_COL_WIDTHS = {
    '종목': 120,
    '상태': 150,
    '현재가': 95,
    '최근 평가': 105,
    '주요 차단 / 대기 사유': 320,
    '보유수량 (Setup)': 120,
    '진입 평균가': 95,
    '초기 무효화': 95,
    '현재 보호선': 95,
    '대응 상태': 120,
    '청산 조건': 130,
    '시각': 135,
    '구분': 80,
    '종목 / 대상': 120,
    '상세 내용': 380,
    '판단 / 실제 사유': 380,
    '방향': 60,
    '수량 / 체결': 90,
    '가격': 85,
    '이유': 90,
    '손익': 75,
    '전략버전': 80,
}

def table(headers):
    w = QTableWidget(0, len(headers))
    w.setHorizontalHeaderLabels(headers)
    w.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    w.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    w.setAlternatingRowColors(True)
    w.verticalHeader().hide()
    # 열 너비 자동 변동(Jitter) 방지: Interactive 모드로 설정하여 수동 조절 가능 및 갱신 시 너비 보존
    header = w.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    for col, h in enumerate(headers):
        w.setColumnWidth(col, DEFAULT_COL_WIDTHS.get(h, 100))
    w.setTextElideMode(Qt.TextElideMode.ElideRight)
    setup_table_copy_menu(w)
    return w

def rows_to_table(widget, rows):
    values = tuple(tuple('미확인' if v is None else str(v) for v in row) for row in rows)
    if getattr(widget, '_rendered_values', None) == values and widget.rowCount() == len(values):
        return
    scroll = widget.verticalScrollBar().value()
    selected_rows = [i.row() for i in widget.selectionModel().selectedRows()] if widget.selectionModel() else []

    widget.setUpdatesEnabled(False)
    try:
        target_len = len(rows)
        if widget.rowCount() != target_len:
            widget.setRowCount(target_len)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                val_str = '미확인' if value is None else str(value)
                item = widget.item(i, j)
                if item is None:
                    item = QTableWidgetItem(val_str)
                    item.setToolTip(val_str)
                    widget.setItem(i, j, item)
                else:
                    if item.text() != val_str:
                        item.setText(val_str)
                    if item.toolTip() != val_str:
                        item.setToolTip(val_str)
        if selected_rows and widget.selectionModel():
            for r in selected_rows:
                if r < target_len:
                    widget.selectRow(r)
    finally:
        widget.setUpdatesEnabled(True)

    widget.verticalScrollBar().setValue(scroll)
    widget._rendered_values = values


def full_stamp(row): return row.get('timestamp') or row.get('time') or row.get('closed') or row.get('opened') or '시각 미확인'

class SettingsDialog(QDialog):
    def __init__(self,window):
        super().__init__(window); self.window=window; self.setWindowTitle('자본 / 설정'); self.resize(630,460)
        layout=QVBoxLayout(self); s=window.status
        layout.addWidget(QLabel('승인한도는 사용자가 허용한 상한입니다. 현금·매도대금 증가로 자동 증액하지 않습니다.'))
        layout.addWidget(QLabel(f"현재 승인한도 {money(s['cap'])} / 주문가능현금 {money(s['cash'])}\n조회시각: {s.get('cash_time') or '미확인'} / 가용자본 {money(s['available'])}"))
        form=QFormLayout(); self.amount=QLineEdit(str(s['cap'])); self.amount.setMaxLength(12)
        form.addRow('새 승인한도 (원)',self.amount); layout.addLayout(form)
        self.change=QPushButton('승인한도 변경'); self.change.clicked.connect(self.change_cap); layout.addWidget(self.change)
        layout.addWidget(QLabel('평소에는 메인 시작 버튼을 한 번 누르면 현재 승인한도로 안전검사와 시작을 진행합니다.'))
        manual=QWidget();manual_layout=QVBoxLayout(manual)
        toggle=QPushButton('고급 수동 준비 / 승인 보기');toggle.setCheckable(True);toggle.toggled.connect(manual.setVisible);layout.addWidget(toggle)
        self.ready=QPushButton('실제매매 준비 검사 (조회만)'); self.ready.clicked.connect(self.prepare); manual_layout.addWidget(self.ready)
        self.authorize=QPushButton('실제매매 명시 승인'); self.authorize.clicked.connect(self.approve); manual_layout.addWidget(self.authorize)
        layout.addWidget(manual);manual.hide()
        idle=not s['process']['running'] and not(s['positions'] or s['pending'] or s['uncertain'] or s['halt'])
        self.change.setEnabled(idle); self.ready.setEnabled(idle and s.get('control_supported',s['supported']) and s['costs_confirmed']); self.authorize.setEnabled(idle and s.get('control_supported',s['supported']) and s['costs_confirmed'])
        self.change.setToolTip('감시 중지·봇 보유/미체결/불명확 주문·HALT 없음이 필요합니다.' if not idle else '새 원 단위 한도를 입력하고 변경 확인을 선택하세요.')
        reason='감시·장부 안전상태 확인 필요' if not idle else '운영시간 밖' if not s.get('control_supported',s['supported']) else '비용 설정 미확인' if not s['costs_confirmed'] else '기존 안전조건을 다시 검사합니다.'
        self.ready.setToolTip(reason); self.authorize.setToolTip(reason)
        layout.addWidget(QLabel('한도 변경 시 실제매매 승인은 해제됩니다. 준비 검사 → 승인한도 확인·승인 버튼 → 별도 시작이 필요합니다.\n실행 중에는 변경할 수 없습니다. 비용 미확인이면 실제매매 승인을 차단합니다.'))
        self.result=QPlainTextEdit(); self.result.setReadOnly(True); layout.addWidget(self.result)
        advanced=QPushButton('고급 설정 보기 (읽기 전용)'); advanced.clicked.connect(lambda:window.details(s.get('advanced',{}),'고급 설정 — 읽기 전용')); layout.addWidget(advanced)
        costs=QPushButton('실제 확인한 비용 입력 (고급 / 감시 중지 필요)'); costs.clicked.connect(self.confirm_costs); layout.addWidget(costs)
        close=QPushButton('닫기'); close.clicked.connect(self.accept); layout.addWidget(close)
    def output(self,result): self.result.setPlainText(result_text(result)); self.window.refresh()
    def change_cap(self):
        amount=self.amount.text().strip(); old=self.window.status['cap']
        if not amount.isascii() or not amount.isdigit(): self.window.problem('승인한도는 0 이상의 원 단위 정수로 입력하세요.'); return
        if self.window.confirm(f'승인한도를 {money(old)} → {money(int(amount))}으로 변경합니다.\n실주문 상한에 영향을 줍니다. 변경 후 실제매매는 꺼집니다.', '변경 확인'):
            self.window.submit(lambda:self.window.service.change_cap(amount,True,expected_cap=old),self.output)
    def prepare(self): self.window.submit(self.window.service.prepare_capital,self.output)
    def approve(self):
        amount,ok=QInputDialog.getText(self,'실제매매 승인','현재 승인한도를 원 단위 숫자로 입력하세요:')
        if not ok: return
        self.window.submit(lambda:self.window.service.authorize_capital(amount.strip(),True),self.output)
    def confirm_costs(self):
        values=[]
        for label in ('매수 수수료율','매도 수수료율','매도 세금률'):
            value,ok=QInputDialog.getText(self,'실제 비용 확인',label+' (% 숫자):')
            if not ok: return
            values.append(value)
        if self.window.confirm('증권사에서 실제 확인한 비용입니까?\n입력값 (%): '+', '.join(values),'비용 확인'):
            self.window.submit(lambda:self.window.service.confirm_costs(*values,True),self.output)

class MainWindow(QMainWindow):
    progress_update=Signal(str)
    def __init__(self,service=None,auto_refresh=True):
        super().__init__(); self.service=service or UserOperations(); self.status=None
        self.reading=False; self.working=False; self.wait_close=False; self.allow_close=False
        self.jobs=set(); self.read_pool=QThreadPool(self); self.read_pool.setMaxThreadCount(1)
        self.command_pool=QThreadPool(self); self.command_pool.setMaxThreadCount(1)
        self.emergency_pool=QThreadPool(self); self.emergency_pool.setMaxThreadCount(1)
        self.setWindowTitle('주식자동매매 V1.5'); self.resize(1220,820); self.setMinimumSize(980,700)
        body=QWidget(); self.setCentralWidget(body); outer=QVBoxLayout(body); outer.setContentsMargins(20,16,20,16)
        header=QHBoxLayout(); title=QLabel('주식자동매매 V1.5'); title.setObjectName('title'); header.addWidget(title)
        header.addSpacing(14)
        self.mode_badge=QLabel('요청 모드: 확인 중'); self.mode_badge.setObjectName('modeBadge'); header.addWidget(self.mode_badge)
        self.permission_badge=QLabel('주문 권한: 확인 중'); self.permission_badge.setObjectName('permissionBadge'); header.addWidget(self.permission_badge)
        header.addStretch(); self.clock=QLabel('저장된 상태 확인 중'); header.addWidget(self.clock); outer.addLayout(header)
        grid=QGridLayout(); self.cards={}
        for i,(key,label) in enumerate((('operation','운영상태'),('market','시장 / 운영시간'),('safety','안전상태'),('account','계좌 / 서버'),
            ('cap','승인한도'),('cash','주문가능현금'),('available','자동매매 가용자본'),('inventory','봇 보유 / 미체결'))):
            box=QGroupBox(label); boxlayout=QVBoxLayout(box); value=QLabel('확인 중'); value.setWordWrap(True)
            value.setObjectName('cardValue'); boxlayout.addWidget(value); self.cards[key]=value; grid.addWidget(box,i//4,i%4)
        outer.addLayout(grid)
        self.notice=QLabel('안전 상태를 읽기 전에는 자동매매를 시작할 수 없습니다.'); self.notice.setWordWrap(True); self.notice.setObjectName('notice'); outer.addWidget(self.notice)
        self.check_progress=QProgressBar();self.check_progress.setRange(0,0);self.check_progress.hide();outer.addWidget(self.check_progress)
        self.check_started=None;self.check_stage='';self.progress_update.connect(self._check_stage)
        self.check_timer=QTimer(self);self.check_timer.setInterval(250);self.check_timer.timeout.connect(self._check_elapsed)
        split=QSplitter(Qt.Orientation.Horizontal); outer.addWidget(split,1)
        left=QWidget(); controls=QVBoxLayout(left); controls.setContentsMargins(0,0,15,0); self.buttons={}; auxiliary=QGridLayout()
        for key,label,callback in (('start','자동매매 시작',self.start),('stop','신규진입 중지',lambda:self.send_stop('STOP')),
            ('flat','봇 보유 전량청산 후 중지',lambda:self.send_stop('STOP_FLAT')),('kill','긴급정지',lambda:self.send_stop('KILL')),
            ('reset_kill','비상정지 복구',self.reset_kill_action),
            ('after_live','AFTER LIVE 승인',self.after_live_action),
            ('diagnostic','상태 / 진단',self.diagnostic),('settings','설정 / 승인한도',self.settings),
            ('records','거래기록',lambda:self.tabs.setCurrentIndex(1)),('logs','상세로그',lambda:self.tabs.setCurrentIndex(3))):
            button=QPushButton(label); button.setMinimumHeight(43 if key in {'start','stop','flat','kill','reset_kill','after_live'} else 36); button.setObjectName(key)
            button.clicked.connect(callback); self.buttons[key]=button
            if key in {'start','stop','flat','kill','reset_kill','after_live'}: controls.addWidget(button)
            else:
                index=('diagnostic','settings','records','logs').index(key); auxiliary.addWidget(button,index//2,index%2)
        controls.addLayout(auxiliary)
        protect=QGroupBox('자동매매 제외 / 기존보유 보호'); protect.setToolTip('읽기 전용: 기존 HARD_LOCK / BREAKEVEN_EXIT 정책을 표시합니다.'); p=QVBoxLayout(protect)
        self.protection=QLabel('보호정책 확인 중'); self.protection.setWordWrap(True); p.addWidget(self.protection); controls.addWidget(protect)
        controls.addStretch(); split.addWidget(left)
        right=QWidget(); content=QVBoxLayout(right); content.setContentsMargins(0,0,0,0)
        self.tabs=QTabWidget(); content.addWidget(self.tabs,1)

        # Tab 0: 트레이딩 칵핏
        cockpit_widget=QWidget(); cockpit_layout=QVBoxLayout(cockpit_widget); cockpit_layout.setContentsMargins(0,4,0,0)
        cockpit_split=QSplitter(Qt.Orientation.Vertical); cockpit_layout.addWidget(cockpit_split)
        self.cockpit_split=cockpit_split;self._plan_positions_visible=False

        bot_box=QGroupBox('봇 운용 포지션'); bot_b_layout=QVBoxLayout(bot_box)
        self.bot_board=table(['종목','보유수량 (Setup)','진입 평균가','현재가','최초 구조 무효화','비상선 / 체결가 보호선','대응 상태','청산 조건'])
        self.bot_board.setMaximumHeight(80)
        self.bot_board.setToolTip('예상손실은 최초 체결가부터 현재 보호선까지 비용·설정 슬리피지를 포함한 추정액입니다. 급락 실제손실 상한은 아닙니다.')
        bot_b_layout.addWidget(self.bot_board)
        self.bot_empty_label=QLabel('현재 봇이 보유 중인 포지션이 없습니다. (신규 진입 탐색 대기)')
        self.bot_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.bot_empty_label.setStyleSheet('color:#64748b;font-size:13px;padding:12px;')
        bot_b_layout.addWidget(self.bot_empty_label)
        self.exit_plan_panel=QWidget();plan_layout=QVBoxLayout(self.exit_plan_panel);plan_layout.setContentsMargins(0,0,0,0)
        self.exit_plan_heading=QLabel('보유종목의 기대 가격과 실제 매도 조건');plan_layout.addWidget(self.exit_plan_heading)
        grid=QGridLayout();grid.setSpacing(6);self.exit_plan_summary={};self.exit_plan_titles={}
        keys=('entry','current','initial_target','current_target','protection','activation','net','profit_status')
        for i,key in enumerate(keys):
            box=QWidget();layout=QVBoxLayout(box);layout.setContentsMargins(8,4,8,4);layout.setSpacing(2)
            title=QLabel();title.setStyleSheet('color:#52657d;font-size:11px;');layout.addWidget(title)
            value=QLabel();value.setWordWrap(True);value.setStyleSheet('font-weight:600;color:#18334f;');layout.addWidget(value)
            box.setStyleSheet('background:#f4f7fb;border-radius:4px;');grid.addWidget(box,i//4,i%4)
            self.exit_plan_summary[key]=value;self.exit_plan_titles[key]=title
        plan_layout.addLayout(grid)
        self.exit_plan_policy=QLabel();self.exit_plan_policy.setWordWrap(True)
        self.exit_plan_policy.setStyleSheet('color:#36536a;font-size:12px;');plan_layout.addWidget(self.exit_plan_policy)
        self.exit_plan_expected=QLabel();self.exit_plan_expected.setWordWrap(True)
        self.exit_plan_expected.setStyleSheet('background:#fff5dd;color:#775016;padding:5px;font-weight:600;');plan_layout.addWidget(self.exit_plan_expected)
        self.exit_plan_next=QLabel();self.exit_plan_next.setWordWrap(True);plan_layout.addWidget(self.exit_plan_next)
        self.exit_plan_orders=QLabel();self.exit_plan_orders.setWordWrap(True);plan_layout.addWidget(self.exit_plan_orders)
        self.exit_plan_tabs=QTabWidget();plan_layout.addWidget(self.exit_plan_tabs,1)
        self.exit_conditions=table(['실제 엔진 조건','상태','판정 조건 / 미발동 이유'])
        self.exit_conditions.setWordWrap(True);self.exit_conditions.setMinimumHeight(150)
        self.exit_conditions.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.exit_conditions.setColumnWidth(0,165);self.exit_conditions.setColumnWidth(1,80)
        self.exit_plan_tabs.addTab(self.exit_conditions,'현재 청산 조건')
        self.exit_plan_details=QPlainTextEdit();self.exit_plan_details.setReadOnly(True)
        self.exit_plan_details.setMinimumHeight(150)
        self.exit_plan_details.setToolTip('엔진이 저장한 청산 계획입니다. 목표가·종가 구조선·즉시 비상선은 서로 다른 조건입니다.')
        self.exit_plan_tabs.addTab(self.exit_plan_details,'상세 근거 / 변경 이력')
        bot_b_layout.addWidget(self.exit_plan_panel)
        self._exit_plan_rows=[];self._exit_plan_permission=False
        self.bot_board.itemSelectionChanged.connect(self.render_exit_plan)
        cockpit_split.addWidget(bot_box)

        cand_box=QGroupBox('실시간 감시 후보 종목 (WATCH / SHADOW)'); cand_b_layout=QVBoxLayout(cand_box)
        self.board=table(['종목','상태','현재가','최근 평가','주요 차단 / 대기 사유']); self.board.setMinimumHeight(160)
        cand_b_layout.addWidget(self.board)
        self.candidate_empty_label=QLabel('현재 감시 대상 후보 종목이 없습니다.')
        self.candidate_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.candidate_empty_label.setStyleSheet('color:#64748b;font-size:13px;padding:12px;')
        cand_b_layout.addWidget(self.candidate_empty_label)
        cockpit_split.addWidget(cand_box)

        event_box=QGroupBox('최근 중요 이벤트 (체결 · 주문 · RiskGate 차단 · 상태 전이)'); event_b_layout=QVBoxLayout(event_box)
        self.event_feed=table(['시각','구분','종목 / 대상','상세 내용']); self.event_feed.setMinimumHeight(140)
        event_b_layout.addWidget(self.event_feed)
        self.event_empty_label=QLabel('최근 기록된 중요 이벤트가 없습니다.')
        self.event_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.event_empty_label.setStyleSheet('color:#64748b;font-size:13px;padding:12px;')
        event_b_layout.addWidget(self.event_empty_label)
        cockpit_split.addWidget(event_box)
        cockpit_split.setSizes([110,250,180])
        self.tabs.addTab(cockpit_widget,'트레이딩 칵핏')

        # Tab 1: 거래기록
        self.trades=table(['시각','종목','방향','수량 / 체결','가격','이유','구분','상태','손익','전략버전'])
        self.tabs.addTab(self.trades,'거래기록')

        # Tab 2: 상세 진단 / 통계
        diag_widget=QWidget(); diag_b_layout=QVBoxLayout(diag_widget); diag_b_layout.setContentsMargins(4,4,4,4)
        pipe_box=QGroupBox('후보 선정 파이프라인 (Funnel)'); pipe_layout=QVBoxLayout(pipe_box)
        self.funnel=QLabel('Raw → 현재자본 매수가능 → SHADOW → WATCH → Setup → RiskGate → 주문가능'); self.funnel.setWordWrap(True)
        pipe_layout.addWidget(self.funnel)
        self.wait_reason=QLabel('최신 집계 대기 중'); self.wait_reason.setWordWrap(True)
        pipe_layout.addWidget(self.wait_reason)
        diag_b_layout.addWidget(pipe_box)

        summary=QGroupBox('후보 통계 — 최신 저장된 판단'); summarylayout=QGridLayout(summary); self.stats={}
        for i,(key,label) in enumerate(CANDIDATE_FIELDS.items()):
            line=QLabel(label+'  -'); self.stats[key]=line; summarylayout.addWidget(line,i//2,i%2)
        self.summary_stamp=QLabel('최신 후보 집계가 없습니다.'); self.summary_stamp.setWordWrap(True); summarylayout.addWidget(self.summary_stamp,5,0,1,2)
        self.statistics_toggle=QPushButton('탈락 통계 / 처리시간 자세히 보기'); self.statistics_toggle.setCheckable(True)
        self.statistics_toggle.toggled.connect(summary.setVisible)
        diag_b_layout.addWidget(self.statistics_toggle); diag_b_layout.addWidget(summary); summary.hide()

        dec_box=QGroupBox('개별 후보 탈락 / 판단 이력'); dec_l=QVBoxLayout(dec_box)
        self.decisions=table(['시각','종목','판단 / 실제 사유']); dec_l.addWidget(self.decisions)
        diag_b_layout.addWidget(dec_box,1)
        self.tabs.addTab(diag_widget,'상세 진단 / 통계')

        # Tab 3: 시스템 로그 / 오류
        logs_widget=QWidget(); logs_b_layout=QVBoxLayout(logs_widget); logs_b_layout.setContentsMargins(4,4,4,4)
        log_split=QSplitter(Qt.Orientation.Vertical)
        log_gbox=QGroupBox('시스템 이벤트 로그 (JSONL)'); log_gl=QVBoxLayout(log_gbox)
        self.logs=QPlainTextEdit(); self.logs.setReadOnly(True); log_gl.addWidget(self.logs)
        log_split.addWidget(log_gbox)
        err_gbox=QGroupBox('오류 / HALT 상태'); err_gl=QVBoxLayout(err_gbox)
        self.errors=QPlainTextEdit(); self.errors.setReadOnly(True); err_gl.addWidget(self.errors)
        log_split.addWidget(err_gbox)
        log_split.setSizes([380,220]); logs_b_layout.addWidget(log_split)
        self.tabs.addTab(logs_widget,'시스템 로그 / 오류')

        split.addWidget(right); split.setSizes([290,880])
        self.footer=QLabel('상태 갱신은 저장파일을 읽습니다. 창 종료와 자동매매 중지는 별개입니다.'); outer.addWidget(self.footer)
        self.setStyleSheet('QMainWindow {background:#f4f6fa;} QLabel#title {font-size:24px;font-weight:700;color:#162b48;} '
            'QGroupBox {background:white;border:1px solid #d8e0eb;border-radius:7px;margin-top:9px;padding:12px 8px 6px;} '
            'QGroupBox::title {subcontrol-origin:margin;left:12px;color:#56657a;} QLabel#cardValue {font-size:17px;font-weight:600;} '
            'QLabel#notice {background:#e8eef8;color:#223e66;padding:10px;border-radius:5px;} '
            'QPushButton {padding:8px;border:1px solid #bdcada;border-radius:5px;background:white;} '
            'QPushButton:hover {background:#e3edf9;} QPushButton:disabled {color:#8995a5;background:#edf0f4;} '
            'QPushButton#start {background:#245ba5;color:white;} QPushButton#start:disabled {background:#b9c3d1;} '
            'QPushButton#kill {background:#b43838;color:white;} '
            'QPushButton#reset_kill {background:#d97706;color:white;font-weight:700;} '
            'QTableWidget,QPlainTextEdit {background:white;border:1px solid #dae1ec;}')
        self.buttons['start'].setEnabled(False); self.buttons['stop'].setEnabled(False); self.buttons['flat'].setEnabled(False); self.buttons['reset_kill'].hide(); self.buttons['after_live'].hide()
        self.timer=QTimer(self); self.timer.setInterval(1500); self.timer.timeout.connect(self.refresh)
        self.tabs.currentChanged.connect(lambda _: self.apply_status(self.status) if self.status else None)
        if auto_refresh: self.timer.start(); self.refresh()
    def _work(self,pool,operation,success,failure):
        job=Work(operation); self.jobs.add(job)
        def done(value):
            self.jobs.discard(job); success(value)
        def failed(message):
            self.jobs.discard(job); failure(message)
        job.signals.done.connect(done); job.signals.failed.connect(failed); pool.start(job)
    def refresh(self):
        if self.reading: return
        self.reading=True
        self._work(self.read_pool,self.service.snapshot,self._read_done,self._read_failed)
    def _read_done(self,s):
        self.reading=False; self.apply_status(s)
        if self.wait_close and not self.working and not s['process']['running'] and not(s['positions'] or s['pending'] or s['uncertain']):
            self.allow_close=True; self.close()
    def _read_failed(self,message):
        self.reading=False; self.buttons['start'].setEnabled(False); self.cards['safety'].setText('주의 — 상태 읽기 오류')
        self.notice.setText('상태를 읽을 수 없습니다. 시작은 차단하며 중지·비상정지는 사용할 수 있습니다. '+message)
    def render_exit_plan(self):
        """Read-only widgets consume the engine projection and saved order states."""
        if not self._exit_plan_rows:
            self.exit_plan_details.clear();self.exit_conditions.setRowCount(0)
            for value in self.exit_plan_summary.values():value.clear()
            for label in (self.exit_plan_policy,self.exit_plan_expected,self.exit_plan_next,self.exit_plan_orders):label.clear()
            return
        index=self.bot_board.currentRow()
        row=self._exit_plan_rows[index if 0<=index<len(self._exit_plan_rows) else 0]
        panel=exit_plan_presentation(row,self._exit_plan_permission)
        self.exit_plan_heading.setText(str(row.get('name') or row.get('symbol'))+' · '+str(row.get('quantity','?'))+'주 · Setup '+str(row.get('setup') or 'UNKNOWN'))
        for key,title,value in panel['summary']:
            self.exit_plan_titles[key].setText(title);self.exit_plan_summary[key].setText(value)
        self.exit_plan_policy.setText('실제 정책 E (AE3) · '+panel['policy'])
        self.exit_plan_expected.setText('예상 익절 조건: '+panel['expected'])
        self.exit_plan_next.setText('다음 청산 조건: '+panel['next_exit'])
        self.exit_plan_orders.setText(panel['orders'])
        rows_to_table(self.exit_conditions,[[c['label'],c['status'],c['detail']] for c in panel['conditions']])
        self.exit_conditions.resizeRowsToContents()
        self.exit_plan_details.setPlainText(exit_plan_text(row,self._exit_plan_permission))

    def apply_status(self,s):
        self.status=s; operation,market,safety=presentation(s)
        self.clock.setText(s['time'][:19].replace('T',' ')+' / '+s.get('build_id',s['strategy_version']))
        mode=s.get('mode','STOP')
        if mode=='LIVE':
            if s.get('current_session')=='AFTER' and not s.get('after_live_approved'):
                m_text='[요청 모드] 실제매매 (LIVE) — AFTER 미승인'
                m_style='background:#fef3c7;color:#92400e;border:1px solid #fde68a;font-weight:700;padding:4px 10px;border-radius:4px;'
            elif s.get('current_session')=='AFTER' and s.get('after_live_approved'):
                m_text='[요청 모드] AFTER 실제매매 (LIVE)'
                m_style='background:#dcfce7;color:#166534;border:1px solid #86efac;font-weight:700;padding:4px 10px;border-radius:4px;'
            else:
                m_text='[요청 모드] 실제매매 (LIVE)'
                m_style='background:#fef3c7;color:#92400e;border:1px solid #fde68a;font-weight:700;padding:4px 10px;border-radius:4px;'
        elif mode=='ARMED':
            m_text='[요청 모드] 모의/관찰 (ARMED)'
            m_style='background:#dbeafe;color:#1e40af;border:1px solid #bfdbfe;font-weight:700;padding:4px 10px;border-radius:4px;'
        elif mode=='KILL':
            m_text='[요청 모드] 긴급정지 (KILL)'
            m_style='background:#fee2e2;color:#991b1b;border:1px solid #fecaca;font-weight:700;padding:4px 10px;border-radius:4px;'
        else:
            m_text='[요청 모드] 정지 (STOP)'
            m_style='background:#f1f5f9;color:#475569;border:1px solid #cbd5e1;font-weight:700;padding:4px 10px;border-radius:4px;'
        self.mode_badge.setText(m_text); self.mode_badge.setStyleSheet(m_style)
        trading_on=bool(s.get('trading_on'))
        if trading_on:
            p_text='[주문 권한] 실제 주문 허용 (ON)'
            p_style='background:#dcfce7;color:#166534;border:1px solid #86efac;font-weight:700;padding:4px 10px;border-radius:4px;'
        else:
            if mode=='LIVE':
                p_text='[주문 권한] 주문 전송 차단 (OFF - 조건 대기)'
                p_style='background:#fee2e2;color:#991b1b;border:1px solid #fecaca;font-weight:700;padding:4px 10px;border-radius:4px;'
            else:
                p_text='[주문 권한] 주문 전송 차단 (OFF)'
                p_style='background:#f8fafc;color:#64748b;border:1px solid #cbd5e1;font-weight:700;padding:4px 10px;border-radius:4px;'
        self.permission_badge.setText(p_text); self.permission_badge.setStyleSheet(p_style)
        self.cards['operation'].setText(operation); self.cards['market'].setText(market); self.cards['safety'].setText(safety)
        self.cards['safety'].setStyleSheet('color:#b43838;' if s.get('halt') else 'color:#8a620c;' if safety.startswith('주의') else 'color:#56657a;' if safety.startswith('로컬 상태') else 'color:#24684a;')
        self.cards['account'].setText(('현재 조회 정상' if s.get('account_fresh') else '최근 조회값만 있음' if s['cash'] is not None else '조회 미확인')+'\n'+s['websocket'].split(' — ')[0])
        for key in ('cap','cash','available'): self.cards[key].setText(money(s[key]))
        self.cards['cash'].setToolTip('조회시각: '+str(s.get('cash_time') or '미확인')+' / 기록값은 현재 잔고를 보장하지 않음')
        self.cards['available'].setToolTip('min(실제 주문가능현금, 사용자 승인한도). 과거 조회값이면 현재 매수 가능액을 보장하지 않습니다.')
        self.cards['inventory'].setText(f"{s['position_count']}종목 / {s['pending']}건\n불명확 주문 {s['uncertain']}건")
        block=start_block(s); running=s['process']['running']
        if mode=='KILL':
            self.buttons['start'].setEnabled(False)
            self.buttons['start'].setToolTip('비상정지(KILL) 상태에서는 시작할 수 없습니다. 비상정지를 먼저 복구하세요.')
            self.buttons['stop'].setEnabled(False); self.buttons['stop'].setToolTip('비상정지 상태입니다.')
            self.buttons['flat'].setEnabled(False); self.buttons['flat'].setToolTip('비상정지 상태입니다.')
            self.buttons['kill'].setEnabled(False); self.buttons['kill'].setToolTip('이미 비상정지(KILL) 상태입니다.')
            self.buttons['reset_kill'].show(); self.buttons['reset_kill'].setEnabled(not self.working)
            self.buttons['reset_kill'].setToolTip('비상정지(KILL) 상태를 대기(STOP) 모드로 안전하게 복구합니다.')
        else:
            self.buttons['reset_kill'].hide()
            self.buttons['start'].setEnabled(not block and not self.working); self.buttons['start'].setToolTip(block or '클릭하면 현재 승인한도로 오늘 운용을 승인합니다. 안전검사 후 감시를 시작하며, 한도 0원은 관찰만 합니다.')
            self.buttons['start'].setText('자동매매 시작 (시간 밖)' if not s.get('control_supported',s['supported']) else '사전 준비 / 자동매매 시작' if not s['supported'] else '자동매매 시작')
            self.buttons['stop'].setEnabled(running)
            self.buttons['stop'].setToolTip('신규 진입을 중지하고 기존 봇 포지션 보호만 유지합니다.' if running else '감시가 중지되어 있습니다.')
            self.buttons['flat'].setEnabled(bool(running or s['positions'] or s['pending']))
            self.buttons['flat'].setToolTip('봇 포지션 청산 후 중지를 요청합니다. 보호종목은 기존 정책을 유지합니다.' if self.buttons['flat'].isEnabled() else '감시·봇 포지션·미체결이 없습니다. 이 기능도 보호종목과 외부보유 정책을 유지합니다.')
            self.buttons['kill'].setEnabled(True)
            self.buttons['kill'].setToolTip('비상 상황 시 신규 주문을 즉시 차단하고 비상정지(KILL) 모드로 전환합니다.')
        need_after=bool(s.get('current_session')=='AFTER' and s.get('after_supported') and s.get('live_approved') and not s.get('after_live_approved') and mode!='KILL' and not s.get('halt'))
        self.buttons['after_live'].setVisible(need_after); self.buttons['after_live'].setEnabled(need_after and not self.working)
        self.buttons['after_live'].setToolTip('AFTER 시장 실제주문을 별도로 승인합니다. 기존 승인한도를 그대로 사용합니다.')
        self.buttons['settings'].setEnabled(not running and not self.working)
        self.buttons['settings'].setToolTip('감시 중에는 설정을 변경할 수 없습니다. 먼저 정상 중지하세요.' if running else '현재 작업 완료 후 사용할 수 있습니다.' if self.working else '명시 확인으로 승인한도를 변경합니다.')
        policy_names={'005930':'삼성전자','017900':'광전자','052260':'현대바이오랜드'}
        self.protection.setText('\n\n'.join(policy_names.get(r['symbol'],r['symbol'])+' '+r['symbol']+'\n'+r['policy']+
            (f" / 기준 {money(r['breakeven'])}" if r.get('breakeven') else '') for r in s.get('protection_rows',[]) if r['symbol'] in policy_names))
        counts,stamp=candidate_display(s)
        for key,label in CANDIDATE_FIELDS.items(): self.stats[key].setText(label+'  '+str(counts[key]))
        performance=s.get('candidate_performance') or {}
        latency=(' / 후보 처리 평균 '+str(round(performance['cycle_mean_ms']/1000,2))+'초 · 최대 '+str(round(performance['cycle_max_ms']/1000,2))+'초') if performance.get('cycle_mean_ms') is not None else ' / 처리시간 실측 대기'
        self.summary_stamp.setText(stamp+latency if counts.get('raw_candidates')!='-' else stamp)
        summary=s.get('candidate_summary') or {}
        self.funnel.setText(' → '.join(label+' '+str(summary.get(key,'-') if counts.get('raw_candidates')!='-' else '-') for key,label in
            [('raw_candidates','Raw'),('affordable','매수가능'),('shadow','SHADOW'),('watch','WATCH'),('setup_candidates','Setup 형상'),('risk_reached','RiskGate'),('final_entry_eligible','주문가능')]))
        self.funnel.setToolTip('Raw는 5개 ranking의 일반주 합집합입니다. 원시 갱신 '+str(s.get('candidate_refresh_seconds',60))+'초 + 실제 API 처리시간. 탈락 gate는 중복 가능합니다.')
        self.wait_reason.setText(candidate_wait_reason(s))

        bot_pos=s.get('bot_positions',[])
        bot_rows=[]
        for r in bot_pos:
            name=str(r.get('name') or r.get('symbol') or '')
            qty_setup=f"{r.get('quantity','?')}주 ({r.get('setup','')})" if r.get('setup') else f"{r.get('quantity','?')}주"
            entry_p=money(r.get('entry'))
            curr_p=money(r.get('current'))
            inval_p=money(r.get('invalidation'))
            view=r.get('exit_state') or {};lines=view.get('lines') or {}
            prot_p=('비상 ' if lines.get('close_based') else '체결 ')+money(r.get('protection'))
            hold_st=str(r.get('hold_reason') or '보유 중')
            next_ex=str(r.get('next_exit') or '-')
            bot_rows.append([name,qty_setup,entry_p,curr_p,inval_p,prot_p,hold_st,next_ex])
        self.bot_board.blockSignals(True);rows_to_table(self.bot_board,bot_rows);self.bot_board.blockSignals(False)
        self.bot_board.setVisible(bool(bot_pos))
        self.bot_empty_label.setVisible(not bool(bot_pos))
        self._exit_plan_rows=bot_pos;self._exit_plan_permission=bool(s.get('trading_on'))
        self.exit_plan_panel.setVisible(bool(bot_pos));self.render_exit_plan()
        if bool(bot_pos)!=self._plan_positions_visible:
            self.cockpit_split.setSizes([470,180,100] if bot_pos else [110,250,180])
            self._plan_positions_visible=bool(bot_pos)

        board=s.get('candidate_board',[])
        items=[]
        try: snap_dt=datetime.fromisoformat(str(s.get('time','')))
        except Exception: snap_dt=None
        for row in board:
            geometry=any(trial.get('candidate_generated',bool(trial.get('setup') and trial.get('gates'))) for trial in (row.get('setup_trials') or []))
            state=('SETUP 형상 / ' if geometry else '')+{'SHADOW':'관찰만 (SHADOW)','WATCH':'진입검토 (WATCH)','RESERVE':'감시대기 (RESERVE)'}.get(row['state'],row['state'])
            if row.get('fallback_source1') or row.get('watch_type')=='FALLBACK_SOURCE1':
                state=state.replace('진입검토 (WATCH)','진입검토 (WATCH·보조)')
            if row.get('data_fresh') is False: state+=' / 틱 미확인'
            sym_name=str(row.get('name') or row['symbol'])+' '+row['symbol']
            price_val=money(row.get('current_price'))
            age_val=format_observation_age(row.get('timestamp'),clock=snap_dt,is_stale=(row.get('data_fresh') is False))
            blocker_val=human_blocker_reason(row)
            context_val=context_description(row)
            if context_val:blocker_val+=" | "+context_val
            if row.get('data_fresh') is False and row.get('data_note'):
                blocker_val=f"{row.get('data_note')}; {blocker_val}"
            items.append([sym_name,state,price_val,age_val,blocker_val])
        rows_to_table(self.board,items)
        self.board.setVisible(bool(items))
        self.candidate_empty_label.setVisible(not bool(items))

        events=s.get('important_events')
        if events is None:
            events=extract_important_events(s.get('log_rows',[]))
        rows_to_table(self.event_feed,list(reversed(events or [])))
        self.event_feed.setVisible(bool(events))
        self.event_empty_label.setVisible(not bool(events))

        decisions=s.get('decision_rows',[])
        rows_to_table(self.decisions,[[full_stamp(r),r.get('name') or r.get('symbol'),human_decision(r)+' ['+str(r.get('primary_reason_code') or r.get('reason_code') or '미확인')+']'] for r in reversed(decisions)])
        trades=[]
        for r in s.get('orders',[]):
            trades.append([full_stamp(r),r.get('name') or r.get('symbol'),r.get('side'),str(r.get('qty','?'))+' / '+str(r.get('filled','?')),r.get('limit') or r.get('price'),
                r.get('reason'),r.get('execution') or ('PAPER' if str(r.get('id','')).startswith('PAPER-') else 'REAL' if r.get('id') else 'NOT_SUBMITTED'),r.get('status'),r.get('pnl_estimate'),r.get('strategy_version')])
        for r in s.get('trades',[]):
            trades.append([full_stamp(r),r.get('symbol'),'SELL',r.get('qty'),r.get('exit'),r.get('reason'),r.get('execution'),
                '청산 기록',r.get('net_pnl',r.get('pnl_estimate')),r.get('strategy_version')])
        rows_to_table(self.trades,list(reversed(trades[-100:])))
        # Populate once for diagnostics/export compatibility. Afterwards render the large
        # JSON view only while visible; current safety cards and backend checks still refresh.
        if not getattr(self,'_logs_rendered',False) or self.tabs.currentIndex()==3:
            self.logs.setPlainText('\n'.join(json.dumps(r,ensure_ascii=False) for r in s.get('log_rows',[])))
            self._logs_rendered=True
        info=s.get('halt_info',{})
        recover='기존 엔진의 조건부 I/O 복구 대상 (GUI에서 해제하지 않음)' if info.get('classification')=='RECOVERABLE_IO' else '자동복구 불가 / 수동 확인 필요' if info.get('classification')=='MANUAL_REVIEW' else '미확인 — GUI에서 자동 해제하지 않음'
        self.errors.setPlainText('현재 HALT: '+(s.get('halt') or '없음')+'\n발생시각: '+str(info.get('occurred_at') or '기록 없음')+
            '\n자동복구: '+recover+'\n분류: '+str(info.get('classification') or '미확인')+
            '\n현재 오류: '+s['current_error']+'\n\n과거 오류 (현재 오류와 구분):\n'+s['error']+
            '\n\n보조 관찰의 최근 오류 기록 (현재 오류 판정과 별개):\n'+json.dumps((s.get('shadow_diagnostics') or {}).get('last_error_record'),ensure_ascii=False))
        permission='실제매매 조건부 ON' if s['trading_on'] else '실제매매 OFF / 관찰 또는 정지'
        guidance=('ARMED 관찰 실행 중입니다. 「신규매수 중지」로 정상 중지 후 「자동매매 시작」에서 검사·명시 승인을 진행하세요.'
            if running and s['mode']=='ARMED' and not s['live_approved'] else
            'AFTER 자동매매를 시작하려면 별도의 AFTER LIVE 승인이 필요합니다. (정규장 LIVE만 승인됨)'
            if running and s.get('current_session')=='AFTER' and not s.get('after_live_approved') else
            '시작 버튼 선택으로 현재 승인한도 내 오늘 운용을 승인합니다. 필요한 안전검사를 자동 수행합니다.'
            if not running and s['mode']=='ARMED' and s['cap']>0 and not s['live_approved'] else
            block if not running and block else s.get('last_decision','판단 기록 없음'))
        self.notice.setText('안전검사 진행 중 · '+self.check_stage if self.working and self.check_progress.isVisible() else permission+' · '+guidance)
        self.footer.setText('현금 조회시각: '+str(s.get('cash_time') or '미확인')+' · 창만 닫으면 별도 감시는 계속됩니다. 중지는 중지 버튼을 사용하세요.')
    def confirm(self,message,title='확인'):
        return QMessageBox.question(self,title,message,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Yes
    def problem(self,message): QMessageBox.warning(self,'작업을 진행하지 않았습니다',message)
    def submit(self,operation,callback=None,emergency=False):
        if self.working and not emergency: self.problem('이전 작업이 진행 중입니다. 완료 후 다시 선택하세요.'); return
        if not emergency: self.working=True; self.buttons['start'].setEnabled(False); self.buttons['settings'].setEnabled(False)
        self.notice.setText('요청을 처리하고 있습니다. 상태 확인과 비상정지는 계속 사용할 수 있습니다.')
        def success(result):
            if not emergency: self.working=False
            if callback: callback(result)
            else: self.show_result(result)
            self.refresh()
        def failure(message):
            if not emergency:
                self.working=False;self.check_timer.stop();self.check_progress.hide()
            self.problem(message); self.refresh()
        def recorded_operation():
            try: return operation()
            except Exception as error:
                # 기존 오류 저장 경로에 traceback/path를 보존. 상태 refresh는 읽기만 한다.
                try: self.service.save_error(error)
                except Exception as record_error:
                    raise RuntimeError(str(error)+' / 오류 기록 실패: '+str(record_error)) from error
                raise
        self._work(self.emergency_pool if emergency else self.command_pool,recorded_operation,success,failure)
    def start(self):
        if not self.status: return
        reason=start_block(self.status)
        if reason: self.problem(reason); return
        if hasattr(self.service,'prepare_start'):
            self.check_progress.setRange(0,0);self.check_progress.show();self.check_started=None
            # The deliberate start click authorizes only the cap shown at that click.
            # Backend readiness/hash/account guards still run before authorization/start.
            clicked_cap=int(self.status['cap'])
            self.submit(lambda:self.service.prepare_start(self.progress_update.emit),
                lambda result:self._prepared_start(result,clicked_cap=clicked_cap));return
        real=self.status['live_mode'] and self.status['live_approved']
        self.submit(lambda:self.service.start_user(confirmed=bool(real)),self._start_finished)
    def _check_stage(self,text):
        self.check_stage=text;self.check_started=time.monotonic()
        self.check_progress.setRange(0,0);self.check_timer.start()
        self.notice.setText('안전검사 진행 중 · '+text)
    def _check_elapsed(self):
        if self.check_started is None:return
        elapsed=int(time.monotonic()-self.check_started)
        self.check_progress.setRange(0,0)
        self.notice.setText('안전검사 결과 정리 중 / 증거 확인 '+str(elapsed)+'초 · '+self.check_stage)
    def _prepared_start(self,result,clicked_cap=None):
        self.check_timer.stop();self.check_progress.hide()
        verdict=result.get('verdict')
        if verdict=='COST_CONFIRMATION_REQUIRED':
            values=[]
            for label in ('매수 수수료율','매도 수수료율','매도 세금률'):
                value,ok=QInputDialog.getText(self,'비용 설정 미확인',label+' (% 숫자). 증권사에서 실제 확인한 값만 입력하세요:')
                if not ok:return
                values.append(value)
            if self.confirm('증권사에서 실제 확인한 비용입니까?\n입력값 (%): '+', '.join(values),'비용 명시 확인'):
                self.submit(lambda:self.service.confirm_costs(*values,True),lambda _:self.start())
            return
        if verdict in {'NEEDS_LIVE_APPROVAL','READY_LIVE'}:
            if clicked_cap is None or clicked_cap<=0 or result.get('cap')!=clicked_cap:
                self.problem('시작 버튼 선택 이후 승인한도가 달라졌거나 명시적 시작 선택이 없습니다. 상태를 확인하고 다시 시작 버튼을 누르세요.');return
        if verdict=='NEEDS_LIVE_APPROVAL':
            def approve_start():
                self.service.authorize_capital(str(clicked_cap),True)
                return self.service.start_user(confirmed=True)
            self.submit(approve_start,self._start_finished);return
        if verdict in {'READY_LIVE','READY_OBSERVE','READY_EXISTING'}:
            self.submit(lambda:self.service.start_user(confirmed=verdict=='READY_LIVE'),self._start_finished);return
        self.show_result(result)  # Readiness FAIL/INCONCLUSIVE must not disappear silently.
    def _start_finished(self,result):
        if result.get('verdict')=='STARTED':
            self.notice.setText('감시 시작 요청이 처리되었습니다. 실제 주문권한과 준비 상태는 상단에서 확인하세요.')
        else:
            self.show_result(result)  # Failed/inconclusive backend results remain visible.
    def send_stop(self,command):
        if command=='STOP_FLAT' and not self.confirm('봇 보유 포지션 전량 청산 후 중지를 요청합니다.\n삼성전자·광전자 등 외부보유는 절대 자동매도하지 않습니다.','청산 후 중지'): return
        if command=='KILL' and not self.confirm(
            '⚠️ [비상정지(KILL) 경고]\n\n'
            '모든 신규 주문을 즉시 차단하고 비상정지 상태로 래치합니다.\n'
            '기존 LIVE / AFTER 승인이 즉시 폐기됩니다.\n'
            '(봇 보유 포지션은 계좌 대조 후 안전 청산만 허용됩니다)\n\n'
            '정말 비상정지하시겠습니까?','긴급 정지 실행'): return
        self.submit(lambda:self.service.stop(command),emergency=True)
    def reset_kill_action(self):
        if not self.confirm(
            '현재 비상정지(KILL) 상태를 대기(STOP) 모드로 복구하시겠습니까?\n\n'
            '• 모든 주문 권한은 OFF(차단) 상태가 유지됩니다.\n'
            '• LIVE / AFTER 승인은 완전히 해제된 상태로 전환됩니다.\n'
            '• 실제 주문을 재개하려면 사전 준비검사와 명시 승인이 필요합니다.\n\n'
            '비상정지를 복구하시겠습니까?','비상정지 복구 확인'): return
        self.submit(lambda:self.service.reset_kill('RESET KILL'),emergency=True)
    def after_live_action(self):
        if not self.status: return
        cap=int(self.status.get('cap') or 0)
        if cap<=0: self.problem('승인한도가 0원입니다.'); return
        self.submit(lambda:self.service.activate_after_live(str(cap),True))
    def details(self,value,title='상태 / 진단'):
        dialog=QDialog(self); dialog.setWindowTitle(title); dialog.resize(850,620); layout=QVBoxLayout(dialog)
        content=QPlainTextEdit(); content.setReadOnly(True); content.setPlainText(json.dumps(redact(value),ensure_ascii=False,indent=2)); layout.addWidget(content)
        close=QPushButton('닫기'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()
    def show_result(self,result): QMessageBox.information(self,'요청 / 검사 결과',result_text(result))
    def diagnostic(self):
        d=QDialog(self); d.setWindowTitle('상태 / 진단'); d.resize(650,360); layout=QVBoxLayout(d)
        layout.addWidget(QLabel('상태 확인은 저장파일 조회입니다. 운영시간 밖에서도 이용할 수 있습니다.\n고급 안전 준비검사은 최초 0원 조회 검사이며 현재 승인을 자동 변경하지 않습니다.'))
        for label,call in [('상세 상태 보기 (읽기 전용)',lambda:self.details(self.status)),
            ('최근 요청 / 검사 결과 (기록시각 확인)',lambda:self.show_result((self.status or {}).get('recent_result',{}))),
            ('로컬 진단 (네트워크 없음)',lambda:self.submit(self.service.local_diagnostic)),
            ('고급 진단: 최초 증거 기반 안전 준비검사',self.smoke),
            ('운영 설정 유지: 증거 기반 재자격 조회 검사',self.recover_pilot)]:
            b=QPushButton(label); b.clicked.connect(call); layout.addWidget(b)
        close=QPushButton('닫기'); close.clicked.connect(d.accept); layout.addWidget(close); d.exec()
    def recover_pilot(self):
        if self.confirm("승인한도·비용 설정을 보존하고 증거 기반 조회로 증빙을 다시 취득합니다.\nARMED·실제매매 OFF·감시 중지·보유/주문 없음이 필요합니다.\n실제 주문과 LIVE 승인은 수행하지 않습니다.","재자격 조회 확인"):
            self.submit(lambda:self.service.recover_pilot(True))
    def smoke(self):
        if self.confirm('최초 안전 시운전 조건(0원·조회 대기·실주문 꺼짐·비용 미확인)을 먼저 검사합니다.\n현재 설정을 자동으로 바꾸지 않으며 필수 증거를 확인하면 즉시 끝납니다.','증거 기반 안전 준비검사'):
            self.submit(lambda:self.service.pilot())
    def settings(self):
        if self.status: SettingsDialog(self).exec()
    def close_choice(self):
        box=QMessageBox(self); box.setWindowTitle('감시가 실행 중입니다'); box.setText('창 종료와 자동매매 중지는 별개입니다.\n창만 닫으면 감시·보호관리는 계속됩니다.')
        cancel=box.addButton('취소',QMessageBox.ButtonRole.RejectRole)
        only=box.addButton('창만 닫기',QMessageBox.ButtonRole.AcceptRole)
        stop=box.addButton('자동매매 중지 후 닫기',QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel); box.exec()
        return 'window' if box.clickedButton()==only else 'stop' if box.clickedButton()==stop else 'cancel'
    def closeEvent(self,event):
        if self.allow_close: self.timer.stop(); event.accept(); return
        if self.working: self.problem('설정·검사 작업이 진행 중입니다. 완료 후 닫으세요. 비상정지는 사용할 수 있습니다.'); event.ignore(); return
        # 창을 닫는 순간의 실제 프로세스도 읽기 전용으로 확인한다.
        try: running=self.service.running()
        except Exception: self.problem('감시 상태를 확인하지 못했습니다. 콘솔에서 상태를 확인하세요.'); event.ignore(); return
        if running:
            choice=self.close_choice()
            if choice=='window': self.timer.stop(); event.accept(); return
            if choice=='stop':
                self.wait_close=True; self.submit(lambda:self.service.stop('STOP'),lambda _:self.notice.setText('중지를 요청했습니다. 기존 보호·주문 처리가 종료될 때까지 창을 유지합니다.'),emergency=True)
            event.ignore(); return
        self.timer.stop(); event.accept()

def run(service=None):
    app=QApplication.instance() or QApplication(sys.argv); configure_font(app)
    window=MainWindow(service); window.show(); return app.exec()

if __name__=='__main__': raise SystemExit(run())
