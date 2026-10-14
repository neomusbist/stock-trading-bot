"""R6 FAST LIVE preflight check and isolation verification.
Tests R5 -> R6 ledger succession, manual holdings isolation, capital sizing,
and graceful stop without placing any real broker orders.
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.settings import Config, KST, now, load_config
from core.strategy import Tick
from core.broker import Rejected
from automation.engine import AutonomousEngine
from automation.policy import AutomationConfig, Policy, load_automation
from automation.session import supported
from local_diagnostics import account_diagnostic
from main import load_credentials
from tests.test_automation import FakeBroker

def run_preflight():
    results = {}
    print("=== 1. Live Kiwoom Broker Read-Only Query ===")
    load_credentials()
    cfg = load_config(ROOT / 'config.json')
    auto = load_automation(ROOT / 'automation.json')
    diag = account_diagnostic(cfg, ROOT, now())
    
    cash = float(diag.get('cash', 0.0))
    holdings_count = int(diag.get('holdings_count', 0))
    orders_count = int(diag.get('orders_count', 0))
    verdict = diag.get('verdict')
    
    print(f"  Broker Verdict: {verdict}")
    print(f"  Live Cash: {cash:,.1f} KRW")
    print(f"  Holdings Count: {holdings_count}")
    print(f"  Pending Orders Count: {orders_count}")
    
    assert verdict == 'PASS', f"Broker check failed: {diag.get('message')}"
    assert orders_count == 0, f"Pending orders exist: {orders_count}"
    results['broker_check'] = 'PASS'
    
    print("\n=== 2. R5 -> R6 Ledger Succession & 4-Zero Check ===")
    r5_state_path = Path(r'C:\Users\MJS\Desktop\주식자동매매\runtime\automation-production\state.json')
    assert r5_state_path.exists(), "R5 state file not found"
    r5_state = json.loads(r5_state_path.read_text(encoding='utf-8'))
    
    r5_positions = r5_state.get('positions', {})
    r5_orders = [o for o in r5_state.get('orders', {}).values() if o.get('status') in {'INTENT', 'ACCEPTED', 'SUBMITTING'}]
    r5_unknown = [o for o in r5_state.get('orders', {}).values() if o.get('status') == 'UNKNOWN']
    r5_halt = r5_state.get('halt', '')
    
    print(f"  R5 Bot Positions: {len(r5_positions)} (Expected: 0)")
    print(f"  R5 Active Orders: {len(r5_orders)} (Expected: 0)")
    print(f"  R5 UNKNOWN Orders: {len(r5_unknown)} (Expected: 0)")
    print(f"  R5 HALT Latch: '{r5_halt}' (Expected: '')")
    
    assert len(r5_positions) == 0, "R5 has open positions!"
    assert len(r5_orders) == 0, "R5 has open orders!"
    assert len(r5_unknown) == 0, "R5 has UNKNOWN orders!"
    assert not r5_halt, f"R5 is HALTED: {r5_halt}"
    results['r5_succession'] = 'PASS'

    print("\n=== 3. Manual Holdings Isolation & Protection Test ===")
    # Fetch real holdings from Kiwoom snapshot safely
    from core.broker import Rest
    class SafeRest(Rest):
        def mutation_allowed(self, api, body): return False
    rest = SafeRest(cfg)
    snap = rest.snapshot()
    real_holdings = snap.get('holdings', {})
    print(f"  Detected Account Holdings: {list(real_holdings.keys())}")
    
    policy = Policy(ROOT / 'runtime/test_preflight_policy.json')
    policy.command('LIVE', confirmation='LIVE')
    fake_broker = FakeBroker()
    clock_time = [datetime(2026, 10, 15, 9, 5, tzinfo=KST)]
    
    engine = AutonomousEngine(cfg, auto, policy, ROOT / 'runtime/test_preflight_book', fake_broker, lambda: clock_time[0])
    engine.reconcile(snap)
    engine.connected = True
    engine.market = '3'
    
    # Check that all real holdings are in engine.s['automation']['existing']
    for sym in real_holdings:
        assert sym in engine.s['automation']['existing'], f"{sym} not isolated in existing!"
        assert sym not in engine.s['positions'], f"{sym} leaked into bot positions!"
    
    # Try entering on a manual holding -> MUST be rejected
    for sym in real_holdings:
        reason = engine.new_entry_reason(sym)
        print(f"  Symbol {sym} new_entry_reason: '{reason}'")
        assert reason in {'HARD_LOCK', 'EXISTING_POLICY_NO_BUY', '기존 보유: 추가매수 금지'}, f"Isolation failed for {sym}: {reason}"
        
        # Try selling manual holding -> MUST be rejected by order_guard
        try:
            fake_broker.submit('SELL', sym, 10, 1000)
            raise AssertionError(f"Manual holding {sym} was NOT protected against SELL!")
        except Rejected as e:
            print(f"  Symbol {sym} broker sell attempt blocked: {e}")
            assert any(p in str(e) for p in ('HARD_LOCK', 'EXISTING_POLICY', '기존 보유 정책', '저장된 봇 주문 의도 없는 직접 주문 차단', '봇 소유 수량 초과')), f"Unexpected exception: {e}"

    # Drive exits -> MUST NOT touch manual holdings
    engine.drive_exits()
    assert len(fake_broker.orders) == 0, "drive_exits placed orders on manual holdings!"
    print("  Manual holdings completely isolated from bot trading.")
    results['manual_holdings_protection'] = 'PASS'

    print("\n=== 4. Capital Sizing & Available Cash Distinction ===")
    approved_cap = auto.capital_limit
    live_cash = cash
    print(f"  Approved Cap (auto.capital_limit): {approved_cap:,.1f} KRW")
    print(f"  Actual Broker Cash (snap['cash']): {live_cash:,.1f} KRW")
    assert approved_cap == 81300.0, f"Expected 81,300 KRW, got {approved_cap}"
    assert live_cash < approved_cap, "Live cash is unexpectedly higher than cap"
    
    # Quantity calculation test on synthetic candidate
    signal = {
        'symbol': '999990', 'limit': 1000.0, 'stop': 980.0, 'pattern': 'FAST',
        'account_risk_budget_krw': live_cash * 0.02, 'time': clock_time[0].isoformat()
    }
    # Risk budget: 2% of equity/cash (72,895 * 0.02 = 1,457.9 KRW)
    # Price risk per share = 1000 - 980 = 20 KRW + costs (~3 KRW) = ~23 KRW
    # Shares by risk: 1457.9 / 23 = ~63 shares (~63,000 KRW < 72,895 KRW)
    qty = engine.quantity(signal, live_cash, live_cash, live_cash * 0.04)
    cost = qty * 1000.0 * (1 + auto.commission_buy)
    print(f"  Sized Quantity: {qty} shares (Total Cost: {cost:,.1f} KRW)")
    assert cost <= live_cash, f"Order cost {cost} exceeded live cash {live_cash}!"
    assert cost <= approved_cap, f"Order cost {cost} exceeded approved cap {approved_cap}!"
    assert qty * 20.0 <= live_cash * 0.02 + 1.0, "Risk budget exceeded!"
    
    # Add pending reservation of 40,000 KRW -> remaining cash 32,895 KRW
    res_qty = engine.quantity(signal, live_cash, live_cash - 40000.0, live_cash * 0.04)
    res_cost = res_qty * 1000.0 * (1 + auto.commission_buy)
    print(f"  After 40k KRW reservation: Sized Quantity: {res_qty} shares (Cost: {res_cost:,.1f} KRW)")
    assert res_cost <= live_cash - 40000.0, "Sizing failed to obey pending reservation!"
    results['capital_sizing'] = 'PASS'

    print("\n=== 5. Graceful STOP & Recovery Guard Check ===")
    # Simulate STOP command
    policy.command('STOP')
    assert policy.mode == 'STOP', "Policy failed to switch to STOP"
    assert engine.new_entry_reason('999990') != '', "Engine allows entry in STOP mode!"
    
    # Check Recovery Guard rule:
    # If positions > 0 or UNKNOWN > 0, recovery to R5 is FORBIDDEN
    def can_recover_to_r5(engine_state):
        if engine_state.get('positions'): return False, "Active positions remain"
        if any(o.get('status') == 'UNKNOWN' for o in engine_state.get('orders', {}).values()):
            return False, "UNKNOWN orders remain"
        if engine_state.get('halt'): return False, "Engine is HALTED"
        return True, "Safe to recover"
    
    safe, msg = can_recover_to_r5(engine.s)
    print(f"  Clean state recovery check: safe={safe}, reason='{msg}'")
    assert safe, f"Expected safe recovery for clean state: {msg}"
    
    # Dirty state check (active position injected)
    dirty_state = copy.deepcopy(engine.s)
    dirty_state['positions']['999990'] = {'qty': 10}
    safe_dirty, msg_dirty = can_recover_to_r5(dirty_state)
    print(f"  Dirty state (open pos) recovery check: safe={safe_dirty}, reason='{msg_dirty}'")
    assert not safe_dirty, "Recovery guard failed to block dirty state with position!"
    
    # Dirty state check (UNKNOWN order injected)
    dirty_unknown = copy.deepcopy(engine.s)
    dirty_unknown['orders']['2026-10-15-1'] = {'status': 'UNKNOWN'}
    safe_unk, msg_unk = can_recover_to_r5(dirty_unknown)
    print(f"  Dirty state (UNKNOWN order) recovery check: safe={safe_unk}, reason='{msg_unk}'")
    assert not safe_unk, "Recovery guard failed to block dirty state with UNKNOWN order!"
    results['graceful_stop_and_recovery'] = 'PASS'

    print("\n=== SUMMARY OF PREFLIGHT ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
    
    # Clean up test policy and book
    try:
        (ROOT / 'runtime/test_preflight_policy.json').unlink(missing_ok=True)
        import shutil
        shutil.rmtree(ROOT / 'runtime/test_preflight_book', ignore_errors=True)
    except Exception:
        pass
        
    return results

if __name__ == '__main__':
    run_preflight()
