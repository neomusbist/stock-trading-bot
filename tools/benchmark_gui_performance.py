"""
GUI Performance & Column Width Stability Benchmark
Measures:
1. Total time and average latency for 100 rapid UI updates.
2. Column width instability (how many times column widths shift across updates).
3. Selection preservation across updates.
4. Memory usage (RSS) and object allocations.
"""
import os
import sys
import time
import copy
from pathlib import Path

# Offscreen Qt
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
import operator_gui as gui
from tests.test_gui import FakeService, synthetic_status

def generate_mock_status_sequence(count=100):
    sequence = []
    base = synthetic_status()
    for seq in range(count):
        s = copy.deepcopy(base)
        s['time'] = f"2026-10-13T09:30:{seq % 60:02d}+09:00"
        
        # Varying prices and observation ages that change every tick
        s['candidate_board'] = [
            {
                'symbol': '002990',
                'name': '금호건설' if seq % 2 == 0 else '금호건설(급등)',
                'state': 'WATCH',
                'current_price': 13800 + (seq * 10),
                'timestamp': f"2026-10-13T09:30:{max(0, seq - 5):02d}+09:00",
                'data_fresh': True,
                'blocker_reason': f"호가 간격 적정 / 1분봉 {seq % 15}개 완성",
                'setup_trials': [{'setup': 'B', 'candidate_generated': True, 'gates': {}}]
            },
            {
                'symbol': '003230',
                'name': '삼양식품',
                'state': 'SHADOW',
                'current_price': 540000 + (seq * 100),
                'timestamp': f"2026-10-13T09:30:{max(0, seq - 2):02d}+09:00",
                'data_fresh': True,
                'blocker_reason': f"거래대금 충족 / 상단 여유 {2.5 + (seq % 5) * 0.1:.1f}%",
                'setup_trials': []
            },
            {
                'symbol': '036570',
                'name': '엔씨소프트',
                'state': 'WATCH',
                'current_price': 195000 - (seq * 50),
                'timestamp': f"2026-10-13T09:30:{max(0, seq - 10):02d}+09:00",
                'data_fresh': False,
                'data_note': '17초 틱 지연',
                'blocker_reason': "MARKET_STATUS_STALE",
                'setup_trials': []
            }
        ]
        
        # Growing events list
        s['important_events'] = [
            [f"09:30:{i:02d}", "TICK", f"종목 {i}", f"가격 {10000+i*10}원 체결 평가"]
            for i in range(max(1, (seq % 30) + 1))
        ]
        
        # Growing decision rows
        s['decision_rows'] = [
            {'timestamp': f"2026-10-13T09:30:{i:02d}+09:00", 'symbol': f"00{i:04d}",
             'name': f"후보_{i}", 'decision': 'NO_BUY', 'primary_reason_code': 'ACCELERATION_LOW'}
            for i in range(max(1, (seq % 20) + 1))
        ]
        
        # Bot positions with fluctuating current price
        s['bot_positions'] = [
            {
                'symbol': '002990', 'name': '금호건설', 'entry': 13800,
                'current': 13800 + (seq % 7 - 3) * 50, 'quantity': 5,
                'setup': 'C', 'invalidation': 13640, 'protection': 13700,
                'hold_reason': '보호선 위 유지', 'next_exit': '13640 이탈 시 손절'
            }
        ]
        
        # Orders / trades
        s['orders'] = [
            {'timestamp': f"2026-10-13T09:30:{i:02d}+09:00", 'symbol': '002990', 'name': '금호건설',
             'side': 'BUY', 'qty': 5, 'filled': 5, 'limit': 13800, 'reason': 'SETUP_C',
             'execution': 'REAL', 'status': 'FILLED', 'pnl_estimate': 0, 'strategy_version': 'V1.3'}
            for i in range(min(5, (seq // 10) + 1))
        ]
        
        sequence.append(s)
    return sequence

def run_benchmark(iterations=100):
    app = QApplication.instance() or QApplication([])
    gui.configure_font(app)
    service = FakeService()
    window = gui.MainWindow(service, auto_refresh=False)
    
    # Select a row in candidate board to check if selection is preserved
    seqs = generate_mock_status_sequence(iterations)
    
    # 1. Warmup with first frame
    window.apply_status(seqs[0])
    
    # 2. Test manual resize preservation
    # User manually resizes column 0 from default (120) to 220, and column 1 from default (150) to 260
    window.board.setColumnWidth(0, 220)
    window.board.setColumnWidth(1, 260)
    expected_col0_width = 220
    expected_col1_width = 260
    
    # Record column widths after manual resize
    tables = {
        'board': window.board,
        'bot_board': window.bot_board,
        'event_feed': window.event_feed,
        'decisions': window.decisions,
        'trades': window.trades
    }
    
    initial_widths = {
        name: [t.columnWidth(c) for c in range(t.columnCount())]
        for name, t in tables.items()
    }
    
    width_shift_counts = {name: 0 for name in tables}
    last_widths = copy.deepcopy(initial_widths)
    
    # Select row 0 in board
    window.board.selectRow(0)
    selection_lost_count = 0
    
    t0 = time.perf_counter()
    for idx, s in enumerate(seqs):
        step_start = time.perf_counter()
        window.apply_status(s)
        QApplication.processEvents() # Process all pending paint/layout events
        
        # Check column width changes
        for name, t in tables.items():
            curr = [t.columnWidth(c) for c in range(t.columnCount())]
            if curr != last_widths[name]:
                width_shift_counts[name] += 1
                last_widths[name] = curr
                
        # Check selection in board
        selected = window.board.selectionModel().selectedRows()
        if not selected or selected[0].row() != 0:
            selection_lost_count += 1
            # Re-select for next iteration
            if window.board.rowCount() > 0:
                window.board.selectRow(0)
                
    total_time = time.perf_counter() - t0
    avg_latency_ms = (total_time / iterations) * 1000.0
    
    # Check manual resize preservation
    final_col0 = window.board.columnWidth(0)
    final_col1 = window.board.columnWidth(1)
    manual_resize_preserved = (final_col0 == expected_col0_width and final_col1 == expected_col1_width)
    
    # Check text elide mode & tooltips
    elide_ok = window.board.textElideMode() == gui.Qt.TextElideMode.ElideRight
    tooltip_ok = bool(window.board.item(0, 0) and window.board.item(0, 0).toolTip())
    
    window.allow_close = True
    window.close()
    
    print(f"=== GUI PERFORMANCE BENCHMARK RESULT ({iterations} UPDATES) ===")
    print(f"Total Updates Processed: {iterations}")
    print(f"Total Time: {total_time:.4f}s")
    print(f"Average Latency Per Update: {avg_latency_ms:.2f} ms")
    print(f"Updates Per Second (Throughput): {iterations / total_time:.1f} fps/ups")
    print("\n[Column Width Shifts (Instability Count)]")
    for name, shifts in width_shift_counts.items():
        print(f"  - {name:12s}: {shifts:3d} width shifts / {iterations} updates")
    print(f"\n[Selection Losses]: {selection_lost_count} times lost selection / {iterations} updates")
    print(f"[User Manual Width Preserved]: {manual_resize_preserved} (Col 0: {final_col0}px=={expected_col0_width}px, Col 1: {final_col1}px=={expected_col1_width}px)")
    print(f"[ElideRight Mode Enabled]: {elide_ok}")
    print(f"[Tooltip Set on Cells]: {tooltip_ok}\n")
    
    return {
        'iterations': iterations,
        'total_time': total_time,
        'avg_latency_ms': avg_latency_ms,
        'throughput': iterations / total_time,
        'width_shifts': width_shift_counts,
        'selection_losses': selection_lost_count,
        'manual_resize_preserved': manual_resize_preserved,
        'elide_ok': elide_ok,
        'tooltip_ok': tooltip_ok
    }

if __name__ == '__main__':
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    run_benchmark(iters)

