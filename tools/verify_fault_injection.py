"""
Fault Injection Proof Script
Verifies that the test suite immediately detects deliberate sabotage of critical safety gates:
1. Fault 1: HALT check bypassed in AutonomousEngine.enter() -> Detected by safety tests.
2. Fault 2: Cash check bypassed in AutonomousEngine.auto_available_capital() -> Detected by capital tests.
3. Fault 3: Missing stop-loss / invalidation allowed -> Detected by validation tests.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
from io import StringIO

def run_test_case(test_class, method_name):
    suite = unittest.TestSuite()
    suite.addTest(test_class(method_name))
    stream = StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful(), stream.getvalue()

def main():
    print("=== FAULT INJECTION VERIFICATION ===")
    
    # Import target classes
    from automation.engine import AutonomousEngine
    from tests.test_capital_retirement import CapitalRetirementTests
    from tests.test_policy_update import PolicyUpdateTests
    
    # -------------------------------------------------------------
    # 1. Fault 1: HALT Bypass
    # -------------------------------------------------------------
    print("\n--- Test 1: HALT Bypass Fault Injection ---")
    # Baseline check
    passed, log = run_test_case(CapitalRetirementTests, "test_foreign_intent_and_unknown_and_halt_isolate_and_block_new_entries")
    print(f"Baseline (Normal Code): {'PASS' if passed else 'FAIL'}")
    assert passed, f"Baseline failed: {log}"
    
    # Inject Fault: Monkeypatch enter() to clear halt before execution
    original_enter = AutonomousEngine.enter
    def bad_enter(self, signal, tick):
        if self.s.get('halt'):
            # Deliberately clear halt to simulate a dangerous bypass bug
            self.s['halt'] = ''
        return original_enter(self, signal, tick)
            
    with patch.object(AutonomousEngine, 'enter', bad_enter):
        passed_fault, log_fault = run_test_case(CapitalRetirementTests, "test_foreign_intent_and_unknown_and_halt_isolate_and_block_new_entries")
        print(f"With Fault Injected (HALT bypass): {'PASS (UNDETECTED!)' if passed_fault else 'FAIL (DETECTED!)'}")
        if not passed_fault:
            print(f"-> Test successfully caught the flaw!\n{log_fault.strip().splitlines()[-1]}")
        assert not passed_fault, "Safety suite failed to catch HALT bypass!"

    # -------------------------------------------------------------
    # 2. Fault 2: Zero Cash / Capital Cap Bypass
    # -------------------------------------------------------------
    print("\n--- Test 2: Zero Cash / Capital Cap Bypass ---")
    # Baseline check
    passed, log = run_test_case(CapitalRetirementTests, "test_capital_cap_and_cash_boundaries")
    print(f"Baseline (Normal Code): {'PASS' if passed else 'FAIL'}")
    assert passed, f"Baseline failed: {log}"
    
    # Inject Fault: Monkeypatch auto_available_capital to always return 999999999
    with patch.object(AutonomousEngine, 'auto_available_capital', return_value=999999999):
        passed_fault, log_fault = run_test_case(CapitalRetirementTests, "test_capital_cap_and_cash_boundaries")
        print(f"With Fault Injected (Cash cap bypass): {'PASS (UNDETECTED!)' if passed_fault else 'FAIL (DETECTED!)'}")
        if not passed_fault:
            print(f"-> Test successfully caught the flaw!\n{log_fault.strip().splitlines()[-1]}")
        assert not passed_fault, "Safety suite failed to catch Capital Cap bypass!"

    # -------------------------------------------------------------
    # 3. Fault 3: Missing Invalidation / Stop Loss Bypass
    # -------------------------------------------------------------
    print("\n--- Test 3: Invalid Stop Loss / Invalidation Bypass ---")
    passed, log = run_test_case(PolicyUpdateTests, "test_invalid_inputs_and_bad_symbol_rejected_before_intent")
    print(f"Baseline (Normal Code): {'PASS' if passed else 'FAIL'}")
    assert passed, f"Baseline failed: {log}"
    
    # Inject Fault: Allow signal even if stop is None or invalid
    def bad_signal_validator(self, signal, tick):
        return {'status': 'ACCEPTED', 'local': 'mock'}
    with patch.object(AutonomousEngine, 'enter', bad_signal_validator):
        passed_fault, log_fault = run_test_case(PolicyUpdateTests, "test_invalid_inputs_and_bad_symbol_rejected_before_intent")
        print(f"With Fault Injected (Stop loss bypass): {'PASS (UNDETECTED!)' if passed_fault else 'FAIL (DETECTED!)'}")
        if not passed_fault:
            print(f"-> Test successfully caught the flaw!\n{log_fault.strip().splitlines()[-1]}")
        assert not passed_fault, "Safety suite failed to catch Stop Loss bypass!"

    print("\n=== ALL 3 FAULT INJECTIONS WERE PROMPTLY DETECTED AND BLOCKED BY TESTS ===")

if __name__ == '__main__':
    main()
