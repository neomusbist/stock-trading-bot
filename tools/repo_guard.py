"""업로드 목록 및 실제 staged 바이트 검사. 탐지된 값은 출력하지 않는다."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT_FILES={'main.py','candidate_builder.py','control.py','supervisor.py','operator_console.py','operator_service.py',
    'operator_ui_service.py','operator_gui.py','gui_launch.py','gui_menu.cmd','GUI_GUIDE.md',
    'operator_runner.py','operator_process.py','local_diagnostics.py','run.bat','test.bat','operator_menu.cmd',
    '.gitignore','.gitattributes','.env.example','config.example.json','automation.example.json','requirements.txt',
    'README.md','AUDIT_REPORT.md','TEST_REPORT.md','NEXT_SESSION_CHECK.md','운영요약.md','사용자_시운전_가이드.txt','DEPLOYMENT_LAYOUT.md','observation.example.json','REPOSITORY_GUIDE.md','SECURITY.md','THIRD_PARTY_NOTICES.md','SOURCE_MANIFEST.json','V1.3_HANDOFF_20261007.md','V1.2_설계감사_구현명세.md','TEST_AUDIT_REPORT.md','STORAGE_AUDIT_REPORT.md','HANDOFF.md','cleanup_audit_trail.json','GUI_PERFORMANCE_REPORT.md','HANDOFF_TRADE_QUALITY_20261013.md'}
TOOLS={'repo_guard.py','live_overlay.py','setup_local.py','export_source.py','session_audit.py','offline_benchmark.py','offline_regression.py','night_profile.py','verify_fault_injection.py','benchmark_gui_performance.py','trade_quality_replay.py','trade_quality_v15_replay.py','exit_policy_replay.py'}
TOOLS.add('measure_runtime_budget.py')  # Reviewed read-only OS sampler; generated process evidence stays private.
TOOLS.update({'reproduce_89_signals.py','full_universe_backtest.py','account_level_replay.py',
              'eval_1019_theoretical.py','build_1019_bars.py','run_all_replays.py'})
CAUSAL_RESEARCH_FILES={
    'research/initial_structure.py','research/support_recovery.py','research/initial_structure_replay.py',
    'audit/r6_oct24_initial_structure/RESULTS.md','audit/r6_oct24_initial_structure/CASES.csv',
    'research/ab_feasibility.py',
    'research/session_validation.py','audit/r6_oct26_night/READINESS_AND_REPLAY.md',
    'research/causal_data.py','research/causal_account.py','research/causal_replay.py',
    'research/causal_strategy_adapters.py',
    'research/retest_strategy.py','research/evidence_collector.py',
    'audit/r6_causal_validation_20261020/RESULTS.md',
    'audit/r6_causal_validation_20261020/REPRODUCE.md',
    'audit/r6_causal_validation_20261020/HANDOFF_TO_AG.md',
    'audit/r6_causal_validation_20261020/RESULTS.json'}
R6_REPAIR_FILES={
    'HANDOFF_TO_ANTIGRAVITY.md',
    *('audit/r6_work_repair_20261014/'+name for name in (
        'FINAL_REPORT.md','BUG_FIXES.md','SYMBOL_DATA_AUDIT.csv','TRADE_LEDGER.csv',
        'SLIPPAGE_SENSITIVITY.csv','ENTRY_LATENCY.csv','READINESS_RESULTS.md',
        'SHADOW_RUNBOOK.md','TEST_RESULTS.md','REPRODUCE.md','SHADOW_REPAIR.md',
        'REPLAY_RESULTS.json','DATA_IDENTITY_RESULTS.json','BASE_COMMIT_MAP.json',
        'TEST_RESULTS.json','replay.py','symbol_audit.py'))}
REVIEWED_DEPENDENCY_FILES={
    'vendor/websocket/_utils.py',
    'licenses/websocket-client-1.9.0-LICENSE.txt',
}

def allowed(name):
    if name.replace('\\','/') in REVIEWED_DEPENDENCY_FILES:return True
    if name.replace('\\','/') in CAUSAL_RESEARCH_FILES:return True
    if name.replace('\\','/') in R6_REPAIR_FILES:return True
    p=Path(name); parts=p.parts
    if len(parts)==1: return name in ROOT_FILES
    if any(x.startswith('.') or x=='__pycache__' for x in parts): return False
    if len(parts)==2 and parts[0] in {'core','automation'}: return p.suffix=='.py'
    if len(parts)==2 and parts[0]=='tests': return p.name.startswith('test_') and p.suffix=='.py'
    if len(parts)==2 and parts[0]=='tools': return p.name in TOOLS
    return name.replace('\\','/') in {'reference/contract.json','tests/fixtures/original_052260_daily.csv','tests/fixtures/README.md'}

def inspect(name,data):
    reasons=[]
    if not allowed(name): return ['LOCAL_OR_UNREVIEWED_FILE']
    if b'\x00' in data: return ['BINARY_FILE']
    try: value=data.decode('utf-8-sig')
    except UnicodeError: return ['NON_TEXT_FILE']
    patterns={
        'PERSONAL_WINDOWS_PATH':r'C:[/\\]+Users[/\\]+(?!\{)(?!<)(?!%)[A-Za-z0-9_.-]+[/\\]',
        'GITHUB_TOKEN':r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})',
        'BEARER_LITERAL':r'Bearer\s+[A-Za-z0-9._-]{20,}',
        'JWT_LITERAL':r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}',
        'CREDENTIAL_LITERAL':r'(?i)[\"\x27]?(?:appkey|secretkey|KIWOOM_APP_KEY|KIWOOM_APP_SECRET|access_token|refresh_token|password)[\"\x27]?\s*[:=]\s*[\"\x27]?(?!YOUR_|os\.|self\.|\[REDACTED\])[A-Za-z0-9_-]{20,}',
        'ACCOUNT_NUMBER':r'(?i)[\"\x27]?(?:account_number|acct_no|acnt_no)[\"\x27]?\s*[:=]\s*[\"\x27]?[0-9-]{8,}',
    }
    for reason,pattern in patterns.items():
        if re.search(pattern,value): reasons.append(reason)
    if name in {'config.example.json','automation.example.json'}:
        try:
            cfg=json.loads(value)
            if name=='config.example.json' and cfg.get('live_mode') is not False: reasons.append('UNSAFE_EXAMPLE_LIVE')
            if name=='automation.example.json' and (cfg.get('AUTO_TRADING_CAP_KRW')!=0 or cfg.get('costs_confirmed') is not False): reasons.append('UNSAFE_EXAMPLE_CAP_OR_COSTS')
        except ValueError: reasons.append('INVALID_JSON')
    if name=='reference/contract.json':
        try:
            cfg=json.loads(value)
            if any(set(v)!={'request','response'} for v in cfg.values()): reasons.append('UNREVIEWED_API_ORIGINAL')
            for v in cfg.values():
                for section in v.values():
                    for fields in section.values():
                        if any(set(f)-{'element','required'} for f in fields): reasons.append('UNREVIEWED_API_DESCRIPTION')
        except (ValueError,TypeError,AttributeError): reasons.append('INVALID_CONTRACT')
    return sorted(set(reasons))

def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
def scan(root,staged=False,history=False):
    root=Path(root); findings=[]
    if history:
        entries=[]; seen=set()
        for commit in git(root,'rev-list','--all').decode('ascii').splitlines():
            for row in git(root,'ls-tree','-r','-z',commit).split(b'\0'):
                if not row: continue
                info,name=row.split(b'\t',1); kind,sha=info.split()[1:]
                key=(name,sha)
                if kind!=b'blob' or key in seen: continue
                seen.add(key); entries.append((name.decode('utf-8'),git(root,'show',sha.decode('ascii'))))
    elif staged:
        # working tree와 달라져도 index에 실제 들어간 바이트를 검사한다.
        names=git(root,'diff','--cached','--name-only','--diff-filter=ACMRT','-z').decode('utf-8').split('\0')
        entries=[(n,git(root,'show',':'+n)) for n in names if n]
    else:
        if (root/'.git').exists():
            names=git(root,'ls-files','--cached','--others','--exclude-standard','-z').decode('utf-8').split('\0')
            entries=[(n,(root/n).read_bytes()) for n in sorted(set(names)) if n and (root/n).is_file()]
        else:
            entries=[(p.relative_to(root).as_posix(),p.read_bytes()) for p in sorted(root.rglob('*'))
                if p.is_file() and not any(x in {'.git','__pycache__','.pytest_cache','.venv'} for x in p.relative_to(root).parts)]
    for name,data in entries:
        reasons=inspect(name,data)
        if reasons: findings.append({'file':name,'reason_codes':reasons})
    return {'passed':not findings,'files_checked':len(entries),'findings':findings,'values_disclosed':False}
def main():
    p=argparse.ArgumentParser(); p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    mode=p.add_mutually_exclusive_group(); mode.add_argument('--staged',action='store_true'); mode.add_argument('--history',action='store_true')
    args=p.parse_args(); result=scan(args.root,args.staged,args.history); print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result['passed'] else 1
if __name__=='__main__': raise SystemExit(main())
