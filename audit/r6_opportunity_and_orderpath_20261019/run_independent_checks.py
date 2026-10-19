"""Offline targeted checks; no production runtime or broker connection.

python audit/r6_opportunity_and_orderpath_20261019/run_independent_checks.py --output <private-directory>
An optional baseline root executes the SAME new counterexamples against an
existing baseline checkout without changing any file in that checkout.
"""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
MODULES=[
    'tests.test_r6_execution_integrity','tests.test_dynamic_watch_order_path',
    'tests.test_lunit_exit_20261019','tests.test_fast_momentum','tests.test_r6_fast_repair',
    'tests.test_fast_time_guard','tests.test_market_time_guard','tests.test_mk_exit_authority',
    'tests.test_data_exit_recovery','tests.test_exit_plan','tests.test_v13_risk2_consistency',
    'tests.test_watch_subscription_incident','tests.test_release_integrity',
    'tests.test_v15_feed_catchup','tests.test_midday_focus_evidence',
    'tests.test_fast_entry_context','tests.test_order_ready_synchronization',
    'tests.test_shadow_observation','tests.test_r6_opportunity_bottleneck',
    'tests.test_r6_shadow_runtime','tests.test_automation',
    'tests.test_structural_a_exit','tests.test_structural_pullback',
]
BASELINE_CASES=[
    'OrderBoundaryTests.test_one_tick_tolerance_cannot_authorize_a_different_unsent_limit',
    'OrderBoundaryTests.test_fresh_quote_does_not_revive_expired_abc_trigger',
    'OrderBoundaryTests.test_receipt_counter_keeps_source_expired_after_wall_clock_correction',
    'OrderBoundaryTests.test_source_counter_is_bound_before_intent_and_not_renewed_at_http',
    'FocusQuarantineTests.test_shadow_redesign_cannot_raise_live_signal_or_geometry_tier',
    'ReceiverPromotionTests.test_real_receiver_queue_reaches_existing_fast_and_small_account_order',
    'ReceiverPromotionTests.test_fast_receipt_timestamp_can_win_full_watch_priority',
    'ReceiverPromotionTests.test_bounded_future_exchange_tick_does_not_erase_fast_readiness',
    'FastPolicyWithoutGlobalFlagTests.test_mk_hold_is_not_overridden_by_single_quote_at_micro_line',
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--baseline-root',type=Path)
    args=parser.parse_args()
    code_root=(args.baseline_root or ROOT).resolve()
    sys.path.insert(0,str(code_root))
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    name='BASELINE' if args.baseline_root else 'TARGETED'
    if args.baseline_root:
        spec=importlib.util.spec_from_file_location('independent_counterexamples',ROOT/'tests/test_r6_execution_integrity.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        names=['independent_counterexamples.'+case for case in BASELINE_CASES]
    else:names=MODULES
    attempts=[]
    def denied(*args,**kwargs):
        attempts.append('NETWORK_ATTEMPT')
        raise RuntimeError('OFFLINE_TARGETED_TEST_NO_NETWORK')
    started=time.perf_counter()
    with (output/(name+'.log')).open('w',encoding='utf-8') as log:
        with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log), \
                patch.object(socket.socket,'connect',denied),patch('socket.create_connection',denied):
            suite=unittest.defaultTestLoader.loadTestsFromNames(names)
            result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    head=subprocess.check_output(['git','-C',str(code_root),'rev-parse','HEAD'],text=True).strip()
    report={'baseline':bool(args.baseline_root),'base_or_head_commit':head,
            'source_worktree':code_root.name,'tests':result.testsRun,
            'pass':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
            'fail':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
            'network_attempts':len(attempts),'real_orders':0,'full_regression_run':False,
            'seconds':round(time.perf_counter()-started,3),
            'failed_tests':[str(case) for case,_ in result.failures],
            'error_tests':[str(case) for case,_ in result.errors], 'modules_or_cases':names}
    (output/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__=='__main__':raise SystemExit(main())
