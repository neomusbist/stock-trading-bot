"""Capacity and trading frequency experiment runner.

Compares Cohorts A, B, C, D (1, 2, 3, 4 concurrent position slots) under identical
market conditions, capital constraints (81,300 KRW, 2% trade risk, 4% daily risk,
1,256 KRW realized daily loss retained), and realistic transaction cost stress.
Also analyzes opening 09:00~09:15 trade horizons and execution feasibility.
"""
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import replace

from core.settings import Config, KST
from automation.policy import AutomationConfig
from automation.capital import Costs
from automation.frequency_research import CapacityReplay, break_even, completed_distribution
from automation.opening_frequency_research import (
    OpeningPrint, opening_prerequisites, replay_opening
)

ROOT = Path(__file__).resolve().parent

def run_experiments():
    cfg = replace(Config(), risk_fraction=0.02, max_daily_loss_fraction=0.04)
    auto = replace(AutomationConfig(), AUTO_TRADING_CAP_KRW=81300,
                   commission_buy=0.00015, commission_sell=0.00015,
                   sell_tax=0.002)
    costs = Costs(auto.commission_buy, auto.commission_sell, auto.sell_tax,
                  cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)
    
    print("=" * 80)
    print("R5 CAPACITY & TRADING FREQUENCY EXPERIMENTAL RESEARCH REPORT")
    print(f"Timestamp: {datetime.now(KST).isoformat()}")
    print("Capital: 81,300 KRW | Trade Risk: 2.0% (1,626 KRW) | Daily Risk: 4.0% (3,252 KRW)")
    print("Today's Retained Loss: 1,256 KRW | Remaining Daily Risk: 1,996 KRW")
    print(f"Cost Model: Buy Fee {auto.commission_buy*100:.3f}%, Sell Fee {auto.commission_sell*100:.3f}%, "
          f"Tax {auto.sell_tax*100:.2f}%, Slippage {cfg.entry_slippage_fraction*100:.2f}%/{cfg.exit_slippage_fraction*100:.2f}%")
    print("=" * 80)

    # 1. Break-Even Analysis across price tiers
    print("\n[PART 1: TRANSACTION COST & BREAK-EVEN ANALYSIS]")
    prices = [1000, 3000, 5000, 6970, 10000, 25000, 50000]
    for p in prices:
        be_pure = break_even(p, costs, include_slippage=False)
        be_stress = break_even(p, costs, include_slippage=True)
        be_spread = break_even(p, costs, spread=p * 0.002, include_slippage=True)
        print(f"  Entry {p:>6,d}원: Fees-only BreakEven={be_pure['price']:>6,d}원 (+{be_pure['rise_fraction']*100:.2f}%) | "
              f"Stressed BreakEven={be_stress['price']:>6,d}원 (+{be_stress['rise_fraction']*100:.2f}%) | "
              f"With 0.2% Spread={be_spread['price']:>6,d}원 (+{be_spread['rise_fraction']*100:.2f}%)")

    # 2. Cohort Setup: A (1 slot), B (2 slots), C (3 slots), D (4 slots)
    cohorts = [
        ('Cohort A (1 Slot)', 1, 1.0),
        ('Cohort B (2 Slots)', 2, 0.5),
        ('Cohort C (3 Slots)', 3, 0.333333),
        ('Cohort D (4 Slots)', 4, 0.25),
    ]

    # 3. Experiment Scenario 1: Actual R5 Tape (09:00 - 13:40)
    print("\n[PART 2: SCENARIO 1 - ACTUAL R5 TAPE EVALUATION (10/14 09:00 - 13:40)]")
    # On 10/14, Pan Ocean (028670) was the single executed order (6,970 KRW, 10 shares, sold at 6,860 KRW, loss -1,256 KRW).
    # All 43 afternoon candidates had 0 buy signals.
    print("Observation: Total incoming buy orders in actual tape = 1 (Pan Ocean at 09:24).")
    print("Evaluating whether having 1, 2, 3, or 4 slots changes execution on actual tape:")
    
    pan_ocean_offer = {
        'id': 'PO-028670-0924',
        'symbol': '028670',
        'known_at': '2026-10-14T09:24:00+09:00',
        'strategy_qualified': True,
        'entry': 6970,
        'stop': 6860,
        'target': 7150,
        'quote': {'time': '2026-10-14T09:24:00+09:00', 'bid': 6960, 'ask': 6970, 'bid_qty': 500, 'ask_qty': 500},
        'score': 10.0,
        'theme': 'SHIPPING'
    }

    scenario1_results = {}
    for name, slots, fraction in cohorts:
        rep = CapacityReplay(cfg, auto, cash=81300, daily_loss=0., max_positions=slots, position_fraction=fraction)
        t0 = datetime(2026, 10, 14, 9, 24, tzinfo=KST)
        res = rep.reserve(pan_ocean_offer, t0)
        filled_qty = 0
        if res['accepted']:
            f_res = rep.fill('PO-028670-0924', pan_ocean_offer['quote'], t0)
            filled_qty = f_res['filled']
            t_exit = t0 + timedelta(seconds=86)
            exit_quote = {'time': t_exit.isoformat(), 'bid': 6860, 'ask': 6870, 'bid_qty': 500, 'ask_qty': 500}
            rep.exit('028670', exit_quote, t_exit)
        
        summ = rep.summary()
        scenario1_results[name] = {
            'accepted': res['accepted'],
            'quantity': filled_qty,
            'completed_trades': summ['completed_round_trips'],
            'net_pnl': summ['net'],
            'reason': res.get('reason')
        }
        print(f"  {name:<20}: Accepted={res['accepted']!s:<5} | Qty={filled_qty:>2}주 | "
              f"Completed={summ['completed_round_trips']} | Net PnL={summ['net']:>8.1f}원 | "
              f"Reason={res.get('reason')}")

    print("  => 결론: 실제 10/14 테이프에서는 유효 신호가 1건(오전 팬오션)뿐이었으므로,")
    print("     동시 보유 슬롯을 2, 3, 4개로 늘려도 추가 거래는 0건이며, 기회 부족의 원인은 슬롯 한도가 아님.")

    # 4. Experiment Scenario 2: Candidate Stream with Multiple Price Tiers & Integer Share Constraints
    print("\n[PART 3: SCENARIO 2 - CANDIDATE STREAM UNDER INTEGER SHARE & RISK BUDGET]")
    print("Testing 10 qualified candidate offers across price tiers (3,000원 ~ 45,000원):")
    
    sample_candidates = [
        {'id': f'CAND-{i:02d}', 'symbol': f'{100000+i:06d}', 'known_at': '2026-10-14T10:00:00+09:00',
         'strategy_qualified': True, 'entry': p, 'stop': math.floor(p * 0.98), 'target': math.ceil(p * 1.04),
         'quote': {'time': '2026-10-14T10:00:00+09:00', 'bid': p - 10, 'ask': p, 'bid_qty': 1000, 'ask_qty': 1000},
         'score': 10.0 - i * 0.5, 'theme': 'TECH' if i % 2 == 0 else 'BIO'}
        for i, p in enumerate([3200, 5400, 8900, 12500, 18000, 24000, 31000, 38000, 42000, 48000])
    ]

    scenario2_results = {}
    for name, slots, fraction in cohorts:
        # Start with 81,300 KRW cash, 1,256 KRW daily loss already realized
        rep = CapacityReplay(cfg, auto, cash=81300, daily_loss=1256, max_positions=slots, position_fraction=fraction)
        t_now = datetime(2026, 10, 14, 10, 0, tzinfo=KST)
        
        accepted_offers = []
        rejected_offers = []
        
        # Test simultaneous ranking reservation
        results = rep.reserve_simultaneous(sample_candidates, t_now)
        for cand_id, res in results:
            cand = next(c for c in sample_candidates if c['id'] == cand_id)
            if res['accepted']:
                accepted_offers.append((cand, res))
                rep.fill(cand_id, cand['quote'], t_now)
            else:
                rejected_offers.append((cand, res))
                
        # Mark and simulate holding
        t_exit = t_now + timedelta(minutes=7)
        for cand, res in accepted_offers:
            sym = cand['symbol']
            rep.mark(sym, cand['entry'], t_now, t_exit)
            exit_quote = {'time': t_exit.isoformat(), 'bid': cand['target'], 'ask': cand['target'] + 10,
                          'bid_qty': 1000, 'ask_qty': 1000}
            rep.exit(sym, exit_quote, t_exit)
            
        summ = rep.summary()
        reserved_cash, open_risk = rep.reservations()
        
        scenario2_results[name] = {
            'slots': slots,
            'max_slot_capital': 81300 * fraction,
            'accepted_count': len(accepted_offers),
            'rejected_count': len(rejected_offers),
            'accepted_symbols': [c['symbol'] for c, _ in accepted_offers],
            'rejection_reasons': summ['rejects'],
            'net_pnl': summ['net'],
            'trades': len(summ.get('completed_round_trips', []) if isinstance(summ.get('completed_round_trips'), list) else [1]*summ['completed_round_trips']),
            'peak_open_risk': summ['peak_open_risk'],
            'peak_theme_risk': summ['peak_theme_risk']
        }
        
        print(f"\n  --- {name} (Slot Cap: {81300*fraction:,.0f}원) ---")
        print(f"    수락된 종목 수: {len(accepted_offers)}건 / 탈락: {len(rejected_offers)}건")
        for cand, res in accepted_offers:
            cost = res['quantity'] * cand['entry']
            print(f"      [수락] {cand['symbol']} (가격 {cand['entry']:,d}원) -> {res['quantity']}주 배정 (총 {cost:,d}원)")
        print(f"    주요 기각 사유: {summ['rejects']}")
        print(f"    최대 동시 노출 위험: {summ['peak_open_risk']:,.1f}원 | 테마별 피크 위험: {summ['peak_theme_risk']}")

    # 5. Opening Strategy Analysis (09:00 - 09:15)
    print("\n[PART 4: OPENING 09:00 - 09:15 STRATEGY & DURATION FEASIBILITY]")
    prereqs = opening_prerequisites(cfg, auto)
    print(f"  Warmup Prerequisites:")
    print(f"    - Baseline + Acceleration Seconds: {prereqs['warmup_seconds']}초")
    print(f"    - Setup A earliest: {prereqs['A']['earliest_from_open_seconds']}초 (2 complete bars)")
    print(f"    - Setup A continuation earliest: {prereqs['A_continuation']['earliest_from_open_seconds']}초 (3 complete bars)")
    print(f"    - Setup B earliest: {prereqs['B']['earliest_from_open_seconds']}초 (4 complete bars)")
    print(f"    - Setup C earliest: {prereqs['C']['earliest_from_open_seconds']}초 (5 complete bars)")
    
    print("\n  Roundtrip Duration Feasibility Matrix:")
    durations = [
        ('1~3분 거래', '09:00~09:03', False, '09:02 이전 완성봉 0개로 구조적 신호 생성 불가. 틱 도박 위험, 최소 비용 커버(+0.8%) 불확실'),
        ('3~5분 거래', '09:03~09:05', True, 'Setup A(돌파) 2분봉 형성 후 09:02:00~09:03:00 진입 가능. 단, 급등락에 따른 슬리피지 리스크 존재'),
        ('5~10분 거래', '09:05~09:10', True, 'Setup B(눌림목) 4분봉 형성 후 정상적 진입 가능. 손익비 1.5R 이상 확보 시 실현 가능성 최고'),
        ('10~15분 거래', '09:10~09:15', True, 'Setup C(박스권 돌파) 5분봉 형성 후 진입 가능. 방향성 확정 후 안전 진입')
    ]
    for name, window, feasible, notes in durations:
        print(f"    {name:<12} ({window}): 실현가능성={'가능' if feasible else '불가'} | {notes}")

    # 6. Evaluation of General Intraday 5~10 min frequency target
    print("\n[PART 5: INTRADAY 5~10 MINUTE TARGET (6~12 TRADES/HOUR) REALITY CHECK]")
    print("  - 한국 주식시장(09:00~15:30) 6.5시간 기준 시간당 6~12회는 일일 약 39~78회 매매.")
    print("  - 현재 승인자본 81,300원, 일일 손실한도 4% (3,252원) 하에서:")
    print("    1) 거래당 왕복 비용(수수료+세금+슬리피지)은 약 0.73%~0.86% (약 590원~700원/거래).")
    print("    2) 시간당 6회 거래 시 1시간 비용만 약 3,500원~4,200원으로 하루 손실한도(3,252원)를 초과.")
    print("    3) 13:24와 같은 오후 정체기에는 시장 전체 돌파·가속 거래량이 실종되므로, 억지로 빈도를 채우려면")
    print("       노이즈 진입(팬오션 재발)으로 인한 연속 손절이 필연적임.")
    print("  => 현실적 권장 빈도: 개장 초(09:00~10:00) 1~3회 유효 거래, 장중(10:00~14:30) 0~2회 A급 기회 엄선.")

    # Save results to JSON
    output_path = ROOT / 'capacity_experiment_results.json'
    results_payload = {
        'timestamp': datetime.now(KST).isoformat(),
        'capital': 81300,
        'retained_daily_loss': 1256,
        'scenario1_actual_tape': scenario1_results,
        'scenario2_candidate_stream': scenario2_results,
        'opening_prerequisites': prereqs
    }
    output_path.write_text(json.dumps(results_payload, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"\n[Artifact Generated]: Results saved to {output_path.name}")
    return results_payload

if __name__ == '__main__':
    run_experiments()
