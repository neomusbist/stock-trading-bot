"""Run candidate tests offline. Never imports a production runtime/credential file.

Use --full for complete unittest discovery, otherwise targeted operational tests.
The separate sitecustomize guard also applies to child Python interpreters.
"""
import argparse
import ast
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import time
import unittest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--full',action='store_true')
    parser.add_argument('--operational',action='store_true',help='Full applicable installed-runtime regression; explicitly list undeployed research exclusions')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(root))
    args.output.mkdir(parents=True,exist_ok=True)
    name='OPERATIONAL' if args.operational else 'FULL' if args.full else 'TARGETED'
    exclusions=[]
    attempts=[]
    def denied(*a,**k):
        attempts.append('NETWORK_ATTEMPT')
        raise RuntimeError('OFFLINE_CANDIDATE_TEST_NO_NETWORK')
    socket.socket.connect=denied;socket.create_connection=denied
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    started=time.perf_counter()
    with (args.output/(name+'.log')).open('w',encoding='utf-8') as log, \
            contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        if args.operational:
            not_installed={
                'test_ab_order_path_integration':'Unapproved redesign strategies are absent from the installed source and intentionally not ported',
                'test_r6_redesign_20261019':'Unapproved redesign strategies are absent from the installed source and intentionally not ported',
                'test_fast_horizon_research':'New horizon research module is not installed or deployed',
                'test_reclaim_research':'New reclaim research module is not installed or deployed',
                'test_opportunity_audit':'Offline audit module is not installed or deployed',
                'test_opportunity_replay':'Offline replay CLI is not installed or deployed',
                'test_frequency_timing_replay':'New offline OpeningPrint research API is not part of the unchanged LIVE module',
            }
            names=[]
            for p in sorted((root/'tests').glob('test_*.py')):
                if p.stem in not_installed:
                    tree=ast.parse(p.read_text(encoding='utf-8-sig'))
                    count=sum(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') for n in ast.walk(tree))
                    exclusions.append({'module':p.stem,'reason':not_installed[p.stem],'declared_test_methods_not_run':count})
                else:names.append('tests.'+p.stem)
            suite=unittest.defaultTestLoader.loadTestsFromNames(names)
        elif args.full:
            suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test_*.py')
        else:
            spec=importlib.util.spec_from_file_location('candidate_checks',root/'audit/r6_opportunity_and_orderpath_20261019/run_independent_checks.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            names=module.MODULES+['tests.test_r6_live_candidate']
            suite=unittest.defaultTestLoader.loadTestsFromNames(names)
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    failed_ids={getattr(case,'test_case',case).id() for case,_ in result.failures}
    error_ids={getattr(case,'test_case',case).id() for case,_ in result.errors}
    report={'root':str(root),'test_scope':'FULL_APPLICABLE_OPERATIONAL' if args.operational else 'FULL_DISCOVERY' if args.full else 'TARGETED_OPERATIONAL',
        'tests':result.testsRun,'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        'passed_test_cases':result.testsRun-len(failed_ids|error_ids)-len(result.skipped),
        'failed_test_cases':len(failed_ids),'error_test_cases':len(error_ids),
        'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'seconds':round(time.perf_counter()-started,3),'network_attempts':attempts,
        'failed_tests':[{'test':case.id(),'traceback':error} for case,error in result.failures],
        'error_tests':[{'test':case.id(),'traceback':error} for case,error in result.errors],
        'skipped_tests':[{'test':case.id(),'reason':reason} for case,reason in result.skipped],
        'out_of_scope_not_run':exclusions,
        'real_broker_connected':False,'real_orders':0,'live_files_changed':False}
    (args.output/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('failed_tests','error_tests','skipped_tests')},ensure_ascii=False))
    return 0 if result.wasSuccessful() and not attempts else 1


if __name__=='__main__':raise SystemExit(main())
