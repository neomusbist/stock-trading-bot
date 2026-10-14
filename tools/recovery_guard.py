"""R6 Graceful Emergency Stop & R5 Recovery Safety Guard.
Enforces non-destructive process stopping, verifies order cancellation,
and blocks R5 automatic recovery if R6 has active holdings or UNKNOWN orders.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from automation.policy import Policy
from core.settings import load_config, now
from local_diagnostics import account_diagnostic
from main import load_credentials

def graceful_stop(root=ROOT, timeout_seconds=10):
    """Gracefully sets policy to STOP without killing process with Force,
    and polls the live broker until all pending orders are confirmed cancelled.
    """
    policy_path = root / 'runtime/automation_policy.json'
    policy = Policy(policy_path)
    policy.command('STOP')
    print(f"[STOP 1/2] Policy mode set to: {policy.mode} (New BUY blocked, active BUY cancelled)")

    print(f"[STOP 2/2] Verifying broker order cancellation...")
    load_credentials()
    cfg = load_config(root / 'config.json')
    deadline = time.monotonic() + timeout_seconds
    clean = False
    last_count = 0
    while time.monotonic() < deadline:
        diag = account_diagnostic(cfg, root, now())
        if diag.get('verdict') == 'PASS':
            last_count = int(diag.get('orders_count', 0))
            if last_count == 0:
                clean = True
                break
        time.sleep(1.0)

    if not clean:
        msg = f"STOP_INCOMPLETE: Broker still has {last_count} pending orders! HTS manual check required."
        print(f"  [ERROR] {msg}")
        raise RuntimeError(msg)

    print(f"  [CONFIRMED] Broker pending orders: 0. Graceful STOP complete.")
    return True

def check_r6_clean_state(root=ROOT):
    """Verifies that R6 has 0 active bot positions, 0 open orders, and 0 UNKNOWN orders."""
    print("[2/4] Checking R6 runtime state files...")
    state_files = list((root / 'runtime').glob('automation-*/state.json'))
    if not state_files:
        print("  No active automation state file found (Clean).")
        return True, "No active state file"
        
    for p in state_files:
        try:
            state = json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            return False, f"Failed to read state file {p}: {e}"
            
        positions = state.get('positions', {})
        if positions:
            return False, f"Active bot positions exist in {p.name}: {list(positions.keys())}"
            
        active_orders = [o for o in state.get('orders', {}).values()
                         if o.get('status') in {'INTENT', 'SUBMITTING', 'ACCEPTED'}]
        if active_orders:
            return False, f"Unresolved active orders exist in {p.name}: {len(active_orders)} orders"
            
        unknown_orders = [o for o in state.get('orders', {}).values()
                          if o.get('status') == 'UNKNOWN']
        if unknown_orders:
            return False, f"UNKNOWN status orders exist in {p.name}: {len(unknown_orders)} orders (HTS verification required!)"
            
        halt = state.get('halt', '')
        if halt:
            return False, f"Engine is latched in HALT: '{halt}'"
            
    print("  R6 runtime state: 0 positions, 0 open orders, 0 UNKNOWN orders, 0 HALT.")
    return True, "Clean runtime state"

def check_broker_orders_clean(root=ROOT):
    """Queries broker read-only API to ensure 0 pending orders on exchange."""
    print("[3/4] Querying live Kiwoom broker for pending orders...")
    load_credentials()
    cfg = load_config(root / 'config.json')
    diag = account_diagnostic(cfg, root, now())
    
    if diag.get('verdict') != 'PASS':
        return False, f"Broker diagnostic failed: {diag.get('message')}"
        
    orders_count = int(diag.get('orders_count', 0))
    if orders_count > 0:
        return False, f"Broker has {orders_count} pending open orders!"
        
    print(f"  Broker verified: 0 pending orders, Cash: {diag.get('cash', 0):,.1f} KRW.")
    return True, "Broker orders clean"

def evaluate_r5_recovery_eligibility(root=ROOT):
    """Evaluates whether switching back to R5 is safe."""
    print("[4/4] Evaluating R5 Recovery Eligibility...")
    state_ok, state_msg = check_r6_clean_state(root)
    broker_ok, broker_msg = check_broker_orders_clean(root)
    
    if not state_ok:
        print(f"\n[RECOVERY BLOCKED] R5 Recovery is STRICTLY FORBIDDEN:")
        print(f"  Reason: {state_msg}")
        print("  Action: Close or resolve active R6 positions/orders in HTS before recovering R5.")
        return False, state_msg
        
    if not broker_ok:
        print(f"\n[RECOVERY BLOCKED] R5 Recovery is STRICTLY FORBIDDEN:")
        print(f"  Reason: {broker_msg}")
        print("  Action: Cancel remaining orders at broker before recovering R5.")
        return False, broker_msg
        
    print("\n[RECOVERY ELIGIBLE] State is completely clean. R5 Recovery is safe.")
    return True, "Safe to recover R5"

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'stop':
        graceful_stop()
    eligible, reason = evaluate_r5_recovery_eligibility()
    sys.exit(0 if eligible else 1)
