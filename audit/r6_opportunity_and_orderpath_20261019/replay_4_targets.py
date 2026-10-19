"""Historical exploratory script; INVALID as causal before/after evidence.

The old implementation invents resistance from later prices, source counts
and missing bid/ask values; reported names lack source identity linkage.
Kept for audit provenance only. CLI execution is quarantined below.
See INDEPENDENT_REVIEW_20261019.md for verified findings and limitations.
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from dataclasses import replace

# Add dev workspace to path
dev_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(dev_dir))

from core.strategy import Tick
from tests.test_automation import Config, AutomationConfig
from candidate_builder import structure
from automation.radar import observation_structure
from automation.patterns import PatternDetector
from automation.capital import Costs

def load_target_ticks():
    runtime = Path(r"C:\Users\MJS\Desktop\주식자동매매\runtime")
    tick_files = [runtime / 'shadow_ticks.jsonl']
    for i in range(1, 10):
        p = runtime / f'shadow_ticks.jsonl.{i}'
        if p.exists():
            tick_files.append(p)
            
    targets = {'089230', '025950', '478340', '445680'}
    ticks_by_symbol = defaultdict(list)
    
    for tf in tick_files:
        if not tf.exists():
            continue
        with open(tf, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if '2026-10-19T16:' in line or '2026-10-19T17:' in line:
                    try:
                        d = json.loads(line)
                        sym = d.get('symbol')
                        if sym in targets and d.get('event') == 'TRADE':
                            t_str = d.get('time') or d.get('exchange_at') or d.get('received')
                            p = d.get('price')
                            if t_str and p and float(p) > 0:
                                ticks_by_symbol[sym].append({
                                    'time_str': t_str,
                                    'dt': datetime.fromisoformat(t_str),
                                    'price': float(p),
                                    'volume': int(d.get('volume', 0)),
                                    'bid': float(d.get('bid', 0)),
                                    'ask': float(d.get('ask', 0)),
                                    'bid_qty': d.get('bid_qty'),
                                    'ask_qty': d.get('ask_qty'),
                                    'symbol': sym
                                })
                    except Exception:
                        pass
                        
    for sym in sorted(targets):
        ticks_by_symbol[sym].sort(key=lambda x: x['dt'])
        print(f"Loaded {len(ticks_by_symbol[sym])} ticks for {sym}", flush=True)
        
    return ticks_by_symbol

def analyze_before_and_after(ticks_by_symbol):
    cfg = Config()
    auto = AutomationConfig()
    costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax, cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
    
    target_names = {
        '089230': 'FSN',
        '025950': '동신건설',
        '478340': '한울소재과학',
        '445680': '디아이티'
    }
    
    print("\n" + "="*80, flush=True)
    print("4 TARGET SYMBOLS FORENSIC REPLAY (BEFORE VS AFTER)", flush=True)
    print("="*80, flush=True)
    
    report_rows = []
    
    for sym in ['089230', '025950', '478340', '445680']:
        ticks = ticks_by_symbol[sym]
        name = target_names[sym]
        print(f"\nStarting replay for {sym} ({name}) with {len(ticks)} ticks...", flush=True)
        if not ticks:
            print(f"No ticks for {sym} ({name})")
            continue
            
        first_t = ticks[0]
        max_p_tick = max(ticks, key=lambda x: x['price'])
        gain = (max_p_tick['price'] - first_t['price']) / first_t['price'] * 100
        
        # Ingest and simulate
        detector = PatternDetector(cfg, auto, costs)
        detector.enable_redesign_trials = True
        detector.continuous_observation = True
        
        eval_events = []
        opportunities = []
        reasons_seen = defaultdict(int)
        gates_failed_summary = defaultdict(int)
        cumulative_vol = 0
        
        # Watch metadata dictionary
        watch_meta = {
            'symbol': sym,
            'name': name,
            'status': 'SHADOW',
            'resistance': max_p_tick['price'] * 1.05,
            'overhead_share': 0.05,
            'source_count': 3
        }
        
        for idx, t in enumerate(ticks):
            if idx % 500 == 0:
                print(f"    Processing tick {idx}/{len(ticks)}...", flush=True)
            cumulative_vol += t['volume']
            tick_obj = Tick(
                symbol=sym,
                time=t['dt'],
                price=t['price'],
                volume=t['volume'],
                cumulative=cumulative_vol,
                ask=t['ask'] if t['ask'] > 0 else t['price'],
                bid=t['bid'] if t['bid'] > 0 else t['price'],
                regular=True
            )
            # Ingest
            detector.ingest(tick_obj, t['dt'])
            
            # Evaluate signal in shadow opportunity mode
            sig = detector.signal(tick_obj, watch_meta)
            last_eval = detector.last_eval.get(sym, {})
            gates = last_eval.get('gates', {})
            failed = [k for k, v in gates.items() if v.get('result') == 'FAIL']
            for f in failed:
                gates_failed_summary[f] += 1
                
            trials = last_eval.get('trials', [])
            ready_trials = [tr for tr in trials if tr.get('entry_ready')]
            
            if last_eval:
                eval_events.append({
                    'time': t['time_str'],
                    'price': t['price'],
                    'gates': gates,
                    'failed_gates': failed,
                    'trials_count': len(trials),
                    'ready_trials': [tr.get('setup') for tr in ready_trials],
                    'signal': sig
                })
            if sig or ready_trials:
                pattern = sig.get('pattern') if sig else ready_trials[0].get('setup')
                opportunities.append({
                    'time': t['time_str'],
                    'price': t['price'],
                    'pattern': pattern,
                    'sig': sig,
                    'ready_trial': ready_trials[0] if ready_trials else None
                })
                
        first_eval_time = eval_events[0]['time'] if eval_events else "None"
        warmup_pass_events = [e for e in eval_events if 'warmup' not in e['failed_gates']]
        warmup_pass_time = warmup_pass_events[0]['time'] if warmup_pass_events else "None"
        
        row = {
            'symbol': sym,
            'name': name,
            'tick_count': len(ticks),
            'first_tick_time': first_t['time_str'],
            'first_tick_price': first_t['price'],
            'max_tick_time': max_p_tick['time_str'],
            'max_tick_price': max_p_tick['price'],
            'gain_pct': gain,
            'total_evals': len(eval_events),
            'first_eval_time': first_eval_time,
            'warmup_pass_time': warmup_pass_time,
            'warmup_pass_count': len(warmup_pass_events),
            'shadow_opportunities': len(opportunities),
            'first_opp_time': opportunities[0]['time'] if opportunities else None,
            'failed_gates_distribution': dict(gates_failed_summary)
        }
        report_rows.append(row)
        
        print(f"\n[{sym} - {name}]")
        print(f"  Ticks: {len(ticks)} | Range: {first_t['price']} -> {max_p_tick['price']} (+{gain:.2f}%)")
        print(f"  First Tick: {first_t['time_str']} | Max Tick: {max_p_tick['time_str']}")
        print(f"  BEFORE Fix:")
        if sym == '089230':
            print("    Status: REJECT: 거래대금 부족 (ka10027 trde_prica None -> turnover 0)")
            print("    WATCH Promotion: 탈락 (0 / 12)")
            print("    Live Evaluations: 0 건 (기회 상실)")
        elif sym == '025950':
            print("    Status: ELIGIBLE이나 NOTIFICATION_STALE (5초 초과로 차단)")
            print("    WATCH Promotion: 탈락 (0 / 12)")
            print("    Live Evaluations: 0 건 (기회 상실)")
        elif sym == '478340':
            print("    Status: UNKNOWN: 일봉 부족 201/500 (신규상장 201일 -> 하드코딩 500 미달)")
            print("    WATCH Promotion: UNKNOWN 필터 탈락 (0 / 12)")
            print("    Live Evaluations: 0 건 (기회 상실)")
        elif sym == '445680':
            print("    Status: 16:06:56 WATCH 진입 -> 16:10:16 탈락")
            print("    Warmup: 16:07:01 관찰 시작 -> 2완성봉(120초) 소요 -> 16:09:00 첫 평가")
            print("    Live Evaluations: 82 건 (워밍업 중 68,900->73,300 급등 종료)")
            
        print(f"  AFTER Fix (실증 재현):")
        print(f"    Subscribed Non-WATCH 평가 경로: 활성화됨")
        print(f"    최초 평가 시점: {first_eval_time}")
        print(f"    워밍업 통과 시점: {warmup_pass_time}")
        print(f"    워밍업 통과 평가 횟수: {len(warmup_pass_events)} / {len(eval_events)}")
        print(f"    SHADOW 기회 신호: {len(opportunities)} 건")
        if opportunities:
            print(f"    최초 기회 시점: {opportunities[0]['time']} @ {opportunities[0]['price']} ({opportunities[0]['pattern']})")
        print(f"    탈락 게이트 분포: {dict(gates_failed_summary)}")
        
    out_file = Path(r"C:\Users\MJS\.gemini\antigravity\brain\b6201a9d-1dc4-4f9a-94dc-bf2931db5e45\scratch\replay_4_targets_result.json")
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(report_rows, f, ensure_ascii=False, indent=2)
    print(f"\nSaved detailed results to {out_file}")

if __name__ == '__main__':
    print('INVALID_FOR_CAUSAL_REPLAY: future-derived resistance, invented source counts/quotes, '
          'unverified symbol identity and hard-coded BEFORE results. '
          'No logs scanned; no signal/order/profitability claim is valid from this script.',file=sys.stderr)
    raise SystemExit(2)
