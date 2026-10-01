"""읽기 중심 PySide6 운영 화면. 실행/설정은 UserOperations 서비스에만 위임한다."""
import json
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
for folder in ('vendor','gui_vendor'):
    sys.path.insert(0,str(ROOT/folder))
from PySide6.QtCore import Qt, QTimer, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QLabel,QPushButton,
    QVBoxLayout,QHBoxLayout,QGridLayout,QGroupBox,QSplitter,QTabWidget,QTableWidget,
    QTableWidgetItem,QHeaderView,QPlainTextEdit,QMessageBox,QDialog,QLineEdit,QFormLayout,QInputDialog)
from operator_ui_service import UserOperations, CANDIDATE_FIELDS, money, presentation, start_block, human_decision,result_text,candidate_display
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

def table(headers):
    w=QTableWidget(0,len(headers)); w.setHorizontalHeaderLabels(headers)
    w.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    w.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    w.setAlternatingRowColors(True); w.verticalHeader().hide()
    w.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    w.horizontalHeader().setStretchLastSection(True)
    return w

def rows_to_table(widget,rows):
    scroll=widget.verticalScrollBar().value()
    widget.setRowCount(len(rows))
    for i,row in enumerate(rows):
        for j,value in enumerate(row):
            item=QTableWidgetItem('미확인' if value is None else str(value)); item.setToolTip(item.text())
            widget.setItem(i,j,item)
    widget.verticalScrollBar().setValue(scroll)

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
        self.ready=QPushButton('실제매매 준비 검사 (조회만)'); self.ready.clicked.connect(self.prepare); layout.addWidget(self.ready)
        self.authorize=QPushButton('실제매매 명시 승인'); self.authorize.clicked.connect(self.approve); layout.addWidget(self.authorize)
        idle=not s['process']['running'] and not(s['positions'] or s['pending'] or s['uncertain'] or s['halt'])
        self.change.setEnabled(idle); self.ready.setEnabled(idle and s['supported'] and s['costs_confirmed']); self.authorize.setEnabled(idle and s['supported'] and s['costs_confirmed'])
        self.change.setToolTip('감시 중지·봇 보유/미체결/불명확 주문·HALT 없음이 필요합니다.' if not idle else '새 원 단위 한도를 입력하고 변경 확인을 선택하세요.')
        reason='감시·장부 안전상태 확인 필요' if not idle else '운영시간 밖' if not s['supported'] else '비용 설정 미확인' if not s['costs_confirmed'] else '기존 안전조건을 다시 검사합니다.'
        self.ready.setToolTip(reason); self.authorize.setToolTip(reason)
        layout.addWidget(QLabel('한도 변경 시 실제매매 승인은 해제됩니다. 준비 검사 → 금액·문구 확인 → 별도 시작이 필요합니다.\n실행 중에는 변경할 수 없습니다. 비용 미확인이면 실제매매 승인을 차단합니다.'))
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
        phrase,ok=QInputDialog.getText(self,'최종 확인','실제 주문이 가능해집니다. 승인하려면 실제매매승인 을 정확히 입력하세요:')
        if ok: self.window.submit(lambda:self.window.service.authorize_capital(amount.strip(),phrase.strip()),self.output)
    def confirm_costs(self):
        values=[]
        for label in ('매수 수수료율','매도 수수료율','매도 세금률'):
            value,ok=QInputDialog.getText(self,'실제 비용 확인',label+' (% 숫자):')
            if not ok: return
            values.append(value)
        if self.window.confirm('증권사에서 실제 확인한 비용입니까?\n입력값 (%): '+', '.join(values),'비용 확인'):
            self.window.submit(lambda:self.window.service.confirm_costs(*values,True),self.output)

class MainWindow(QMainWindow):
    def __init__(self,service=None,auto_refresh=True):
        super().__init__(); self.service=service or UserOperations(); self.status=None
        self.reading=False; self.working=False; self.wait_close=False; self.allow_close=False
        self.jobs=set(); self.read_pool=QThreadPool(self); self.read_pool.setMaxThreadCount(1)
        self.command_pool=QThreadPool(self); self.command_pool.setMaxThreadCount(1)
        self.emergency_pool=QThreadPool(self); self.emergency_pool.setMaxThreadCount(1)
        self.setWindowTitle('주식자동매매 V1.1'); self.resize(1220,820); self.setMinimumSize(980,700)
        body=QWidget(); self.setCentralWidget(body); outer=QVBoxLayout(body); outer.setContentsMargins(20,16,20,16)
        header=QHBoxLayout(); title=QLabel('주식자동매매 V1.1'); title.setObjectName('title'); header.addWidget(title)
        header.addStretch(); self.clock=QLabel('저장된 상태 확인 중'); header.addWidget(self.clock); outer.addLayout(header)
        grid=QGridLayout(); self.cards={}
        for i,(key,label) in enumerate((('operation','운영상태'),('market','시장 / 운영시간'),('safety','안전상태'),('account','계좌 / 서버'),
            ('cap','승인한도'),('cash','주문가능현금'),('available','자동매매 가용자본'),('inventory','봇 보유 / 미체결'))):
            box=QGroupBox(label); boxlayout=QVBoxLayout(box); value=QLabel('확인 중'); value.setWordWrap(True)
            value.setObjectName('cardValue'); boxlayout.addWidget(value); self.cards[key]=value; grid.addWidget(box,i//4,i%4)
        outer.addLayout(grid)
        self.notice=QLabel('안전 상태를 읽기 전에는 자동매매를 시작할 수 없습니다.'); self.notice.setWordWrap(True); self.notice.setObjectName('notice'); outer.addWidget(self.notice)
        split=QSplitter(Qt.Orientation.Horizontal); outer.addWidget(split,1)
        left=QWidget(); controls=QVBoxLayout(left); controls.setContentsMargins(0,0,15,0); self.buttons={}; auxiliary=QGridLayout()
        for key,label,callback in (('start','자동매매 시작',self.start),('stop','자동매매 중지',lambda:self.send_stop('STOP')),
            ('flat','봇 포지션 청산 후 중지',lambda:self.send_stop('STOP_FLAT')),('kill','비상정지',lambda:self.send_stop('KILL')),
            ('diagnostic','상태 / 진단',self.diagnostic),('settings','설정 / 승인한도',self.settings),
            ('records','거래기록',lambda:self.tabs.setCurrentIndex(1)),('logs','상세로그',lambda:self.tabs.setCurrentIndex(2))):
            button=QPushButton(label); button.setMinimumHeight(43 if key in {'start','stop','flat','kill'} else 36); button.setObjectName(key)
            button.clicked.connect(callback); self.buttons[key]=button
            if key in {'start','stop','flat','kill'}: controls.addWidget(button)
            else:
                index=('diagnostic','settings','records','logs').index(key); auxiliary.addWidget(button,index//2,index%2)
        controls.addLayout(auxiliary)
        protect=QGroupBox('자동매매 제외 / 기존보유 보호'); protect.setToolTip('읽기 전용: 기존 HARD_LOCK / BREAKEVEN_EXIT 정책을 표시합니다.'); p=QVBoxLayout(protect)
        self.protection=QLabel('보호정책 확인 중'); self.protection.setWordWrap(True); p.addWidget(self.protection); controls.addWidget(protect)
        controls.addStretch(); split.addWidget(left)
        right=QWidget(); content=QVBoxLayout(right); content.setContentsMargins(0,0,0,0)
        summary=QGroupBox('후보 통계 — 최신 저장된 판단'); summarylayout=QGridLayout(summary); self.stats={}
        for i,(key,label) in enumerate(CANDIDATE_FIELDS.items()):
            line=QLabel(label+'  -'); self.stats[key]=line; summarylayout.addWidget(line,i//2,i%2)
        self.summary_stamp=QLabel('최신 후보 집계가 없습니다.'); self.summary_stamp.setWordWrap(True); summarylayout.addWidget(self.summary_stamp,5,0,1,2); content.addWidget(summary)
        self.tabs=QTabWidget(); content.addWidget(self.tabs,1)
        self.decisions=table(['시각','종목','판단 / 실제 사유']); self.tabs.addTab(self.decisions,'현재 판단')
        self.trades=table(['시각','종목','방향','수량 / 체결','가격','이유','구분','상태','손익','전략버전']); self.tabs.addTab(self.trades,'거래기록')
        self.logs=QPlainTextEdit(); self.logs.setReadOnly(True); self.tabs.addTab(self.logs,'시스템 로그')
        self.errors=QPlainTextEdit(); self.errors.setReadOnly(True); self.tabs.addTab(self.errors,'오류 / HALT')
        split.addWidget(right); split.setSizes([290,880])
        self.footer=QLabel('상태 갱신은 저장파일을 읽습니다. 창 종료와 자동매매 중지는 별개입니다.'); outer.addWidget(self.footer)
        self.setStyleSheet('QMainWindow {background:#f4f6fa;} QLabel#title {font-size:24px;font-weight:700;color:#162b48;} '
            'QGroupBox {background:white;border:1px solid #d8e0eb;border-radius:7px;margin-top:9px;padding:12px 8px 6px;} '
            'QGroupBox::title {subcontrol-origin:margin;left:12px;color:#56657a;} QLabel#cardValue {font-size:17px;font-weight:600;} '
            'QLabel#notice {background:#e8eef8;color:#223e66;padding:10px;border-radius:5px;} '
            'QPushButton {padding:8px;border:1px solid #bdcada;border-radius:5px;background:white;} '
            'QPushButton:hover {background:#e3edf9;} QPushButton:disabled {color:#8995a5;background:#edf0f4;} '
            'QPushButton#start {background:#245ba5;color:white;} QPushButton#start:disabled {background:#b9c3d1;} '
            'QPushButton#kill {background:#b43838;color:white;} QTableWidget,QPlainTextEdit {background:white;border:1px solid #dae1ec;}')
        self.buttons['start'].setEnabled(False); self.buttons['stop'].setEnabled(False); self.buttons['flat'].setEnabled(False)
        self.timer=QTimer(self); self.timer.setInterval(1500); self.timer.timeout.connect(self.refresh)
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
    def apply_status(self,s):
        self.status=s; operation,market,safety=presentation(s)
        self.clock.setText(s['time'][:19].replace('T',' ')+' / '+s['strategy_version'])
        self.cards['operation'].setText(operation); self.cards['market'].setText(market); self.cards['safety'].setText(safety)
        self.cards['safety'].setStyleSheet('color:#b43838;' if s.get('halt') else 'color:#8a620c;' if safety.startswith('주의') else 'color:#56657a;' if safety.startswith('로컬 상태') else 'color:#24684a;')
        self.cards['account'].setText(('현재 조회 정상' if s.get('account_fresh') else '최근 조회값만 있음' if s['cash'] is not None else '조회 미확인')+'\n'+s['websocket'].split(' — ')[0])
        for key in ('cap','cash','available'): self.cards[key].setText(money(s[key]))
        self.cards['cash'].setToolTip('조회시각: '+str(s.get('cash_time') or '미확인')+' / 기록값은 현재 잔고를 보장하지 않음')
        self.cards['available'].setToolTip('min(실제 주문가능현금, 사용자 승인한도). 과거 조회값이면 현재 매수 가능액을 보장하지 않습니다.')
        self.cards['inventory'].setText(f"{s['position_count']}종목 / {s['pending']}건\n불명확 주문 {s['uncertain']}건")
        block=start_block(s); running=s['process']['running']
        self.buttons['start'].setEnabled(not block and not self.working); self.buttons['start'].setToolTip(block or '기존 안전 검사 후 별도 감시 프로세스를 시작합니다.')
        self.buttons['start'].setText('자동매매 시작 (시간 밖)' if not s['supported'] else '자동매매 시작')
        self.buttons['stop'].setEnabled(running)
        self.buttons['stop'].setToolTip('실행 중인 감시를 정상 중지합니다. 기존 봇 포지션 보호는 종료될 때까지 유지됩니다.' if running else '감시가 중지되어 있습니다.')
        self.buttons['flat'].setEnabled(bool(running or s['positions'] or s['pending']))
        self.buttons['settings'].setEnabled(not running and not self.working)
        self.buttons['settings'].setToolTip('감시 중에는 설정을 변경할 수 없습니다. 먼저 정상 중지하세요.' if running else '현재 작업 완료 후 사용할 수 있습니다.' if self.working else '명시 확인으로 승인한도를 변경합니다.')
        self.buttons['flat'].setToolTip('봇 포지션 청산 후 중지를 요청합니다. 보호종목은 기존 정책을 유지합니다.' if self.buttons['flat'].isEnabled() else '감시·봇 포지션·미체결이 없습니다.')
        policy_names={'005930':'삼성전자','017900':'광전자','052260':'현대바이오랜드'}
        self.protection.setText('\n\n'.join(policy_names.get(r['symbol'],r['symbol'])+' '+r['symbol']+'\n'+r['policy']+
            (f" / 기준 {money(r['breakeven'])}" if r.get('breakeven') else '') for r in s.get('protection_rows',[]) if r['symbol'] in policy_names))
        counts,stamp=candidate_display(s)
        for key,label in CANDIDATE_FIELDS.items(): self.stats[key].setText(label+'  '+str(counts[key]))
        self.summary_stamp.setText(stamp)
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
        self.logs.setPlainText('\n'.join(json.dumps(r,ensure_ascii=False) for r in s.get('log_rows',[])))
        info=s.get('halt_info',{})
        recover='기존 엔진의 조건부 I/O 복구 대상 (GUI에서 해제하지 않음)' if info.get('classification')=='RECOVERABLE_IO' else '자동복구 불가 / 수동 확인 필요' if info.get('classification')=='MANUAL_REVIEW' else '미확인 — GUI에서 자동 해제하지 않음'
        self.errors.setPlainText('현재 HALT: '+(s.get('halt') or '없음')+'\n발생시각: '+str(info.get('occurred_at') or '기록 없음')+
            '\n자동복구: '+recover+'\n분류: '+str(info.get('classification') or '미확인')+
            '\n현재 오류: '+s['current_error']+'\n\n과거 오류 (현재 오류와 구분):\n'+s['error'])
        permission='실제매매 조건부 ON' if s['trading_on'] else '실제매매 OFF / 관찰 또는 정지'
        self.notice.setText(permission+' · '+(block if not running and block else s.get('last_decision','판단 기록 없음')))
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
            if not emergency: self.working=False
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
        real=self.status['live_mode'] and self.status['live_approved']
        if real and not self.confirm(f"승인한도 {money(self.status['cap'])} 내 실제 주문이 가능해집니다.\n계좌·연결·시장·위험 조건은 기존 엔진이 다시 검사합니다. 시작할까요?",'자동매매 시작'): return
        self.submit(lambda:self.service.start_user(confirmed=bool(real)))
    def send_stop(self,command):
        if command=='STOP_FLAT' and not self.confirm('봇 포지션 청산 후 중지를 요청합니다. 삼성전자·광전자는 자동매도하지 않습니다.','청산 후 중지'): return
        if command=='KILL' and not self.confirm('신규 주문을 차단하고 비상정지 상태로 전환합니다.\n기존 KILL 처리와 보호종목 잠금은 유지됩니다.','비상정지 실행'): return
        self.submit(lambda:self.service.stop(command),emergency=True)
    def details(self,value,title='상태 / 진단'):
        dialog=QDialog(self); dialog.setWindowTitle(title); dialog.resize(850,620); layout=QVBoxLayout(dialog)
        content=QPlainTextEdit(); content.setReadOnly(True); content.setPlainText(json.dumps(redact(value),ensure_ascii=False,indent=2)); layout.addWidget(content)
        close=QPushButton('닫기'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()
    def show_result(self,result): QMessageBox.information(self,'요청 / 검사 결과',result_text(result))
    def diagnostic(self):
        d=QDialog(self); d.setWindowTitle('상태 / 진단'); d.resize(650,360); layout=QVBoxLayout(d)
        layout.addWidget(QLabel('상태 확인은 저장파일 조회입니다. 운영시간 밖에서도 이용할 수 있습니다.\n고급 150초 시운전은 최초 0원 조회 검사이며 현재 승인을 자동 변경하지 않습니다.'))
        for label,call in [('상세 상태 보기 (읽기 전용)',lambda:self.details(self.status)),
            ('최근 요청 / 검사 결과 (기록시각 확인)',lambda:self.show_result((self.status or {}).get('recent_result',{}))),
            ('로컬 진단 (네트워크 없음)',lambda:self.submit(self.service.local_diagnostic)),
            ('고급 진단: 최초 150초 안전 시운전',self.smoke)]:
            b=QPushButton(label); b.clicked.connect(call); layout.addWidget(b)
        close=QPushButton('닫기'); close.clicked.connect(d.accept); layout.addWidget(close); d.exec()
    def smoke(self):
        if self.confirm('최초 안전 시운전 조건(0원·조회 대기·실주문 꺼짐·비용 미확인)을 먼저 검사합니다.\n현재 설정을 자동으로 바꾸지 않으며 검사 시간은 약 150초입니다.','150초 안전 시운전'):
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
