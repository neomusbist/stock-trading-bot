"""Rehearse the exact candidate overlay/rollback in a temporary directory only.

Arguments: candidate root, immutable LIVE-source snapshot, original manifest,
result JSON. This utility has no actual-deployment or process-start operation.
"""
import hashlib,json,shutil,sys,tempfile
from pathlib import Path

CHANGED=('automation/decisions.py','automation/engine.py','automation/focus_selection.py',
    'automation/patterns.py','automation/position_health.py','automation/release_integrity.py','automation/runner.py')
INSTALL_PATHS=CHANGED+('runtime/RELEASE_IDENTITY.json',)

def main():
    candidate,baseline,original_manifest,output=map(lambda p:Path(p).resolve(),sys.argv[1:])
    sys.path.insert(0,str(candidate))
    from automation.release_integrity import check_runtime_integrity
    manifest=json.loads((candidate/'runtime/RELEASE_IDENTITY.json').read_text(encoding='utf-8'))
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='r6-overlay-proof-') as temp:
        target=Path(temp)/'target';backup=Path(temp)/'backup';target.mkdir();backup.mkdir()
        for rel in manifest['files']:
            dst=target/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(baseline/rel,dst)
        (target/'runtime').mkdir(exist_ok=True)
        shutil.copyfile(original_manifest,target/'runtime/RELEASE_IDENTITY.json')
        # Synthetic preservation sentinels, never actual credentials/ledger.
        sentinels={'config.json':b'{"risk_fraction":0.02,"max_daily_loss_fraction":0.04}',
            'automation.json':b'{"AUTO_TRADING_CAP_KRW":81300}',
            'runtime/automation_policy.json':b'{"mode":"STOP","live_authorized":false}',
            'runtime/automation-production/state.json':b'{"daily_loss":128,"positions":{},"orders":{}}',
            'runtime/operator_user_authorization.json':b'{"test_sentinel":"NEVER_EDIT"}'}
        for rel,value in sentinels.items():
            p=target/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value)
        before={rel:sha(target/rel) for rel in manifest['files']}
        original={rel:sha(target/rel) for rel in INSTALL_PATHS}
        for rel in INSTALL_PATHS:
            p=backup/rel;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(target/rel,p)
            shutil.copyfile(candidate/rel,target/rel)
        integrity=check_runtime_integrity(target)
        assert integrity['valid'] and integrity['manifest_verified'],integrity['reasons']
        assert all((target/p).read_bytes()==value for p,value in sentinels.items())
        overlay={rel:sha(target/rel) for rel in INSTALL_PATHS}
        for rel in INSTALL_PATHS:shutil.copyfile(backup/rel,target/rel)
        restored=all(sha(target/rel)==h for rel,h in original.items())
        all_baseline=all(sha(target/rel)==h for rel,h in before.items())
        assert restored and all_baseline
        assert all((target/p).read_bytes()==value for p,value in sentinels.items())
        result={'temporary_target_only':True,'install_paths':list(INSTALL_PATHS),
            'installed_hashes':overlay,'post_overlay_integrity':True,'manifest_files_checked':len(manifest['files']),
            'all_original_source_hashes_restored':all_baseline,'all_eight_original_paths_restored':restored,
            'ledger_risk_settings_authorization_sentinels_unchanged':True,
            'legacy_deploy_release_used':False,'live_deployed':False,'real_orders':0}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='installed_hashes'},ensure_ascii=False))

if __name__=='__main__':main()
