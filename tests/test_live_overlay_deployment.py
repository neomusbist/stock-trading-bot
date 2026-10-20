"""Deployment/rollback transactions against wholly synthetic temporary trees."""
import hashlib,importlib.util,json,platform,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('live_overlay',Path(__file__).resolve().parents[1]/'tools/live_overlay.py')
overlay=importlib.util.module_from_spec(spec);spec.loader.exec_module(overlay)


class OverlayDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.target=self.root/'target';self.bundle=self.root/'bundle'
        self.backups=self.root/'backups';self.files={}
        source=sorted(overlay.ALLOWED-{'runtime/RELEASE_IDENTITY.json'})
        for rel in source:
            self.write(self.target/rel,b'# synthetic before\nmarker=0\n')
            self.write(self.bundle/'payload'/rel,b'# synthetic after\nmarker=1\n')
            self.files[rel]={'before':overlay.digest(self.target/rel),'after':overlay.digest(self.bundle/'payload'/rel)}
        for tree,key in ((self.target,'before'),(self.bundle/'payload','after')):
            self.json(tree/'runtime/RELEASE_IDENTITY.json',{'commit':'SYNTHETIC_'+key,
                'build_id':'SYNTHETIC_TEST_ONLY','files':{p:r[key] for p,r in self.files.items()}})
        rel='runtime/RELEASE_IDENTITY.json'
        self.files[rel]={'before':overlay.digest(self.target/rel),'after':overlay.digest(self.bundle/'payload'/rel)}
        self.json(self.target/'runtime/automation_policy.json',{'mode':'STOP','live_authorized':False})
        self.json(self.target/'runtime/automation-production/state.json',{'positions':{},'orders':{},'halt':'','trades':[{'synthetic':True}]})
        self.json(self.target/'runtime/operator_user_authorization.json',{'trading_day':'SYNTHETIC_NOT_APPROVAL'})
        self.json(self.target/'config.json',{'risk_fraction':.02,'max_daily_loss_fraction':.04})
        self.json(self.target/'automation.json',{'AUTO_TRADING_CAP_KRW':81300})
        self.protected={p:overlay.digest(self.target/p) for p in ('config.json','automation.json',
            'runtime/automation_policy.json','runtime/automation-production/state.json','runtime/operator_user_authorization.json')}
        self.json(self.bundle/'manifest.json',{'target':str(self.target),'machine':platform.node(),
            'files':self.files,'protected_hashes':self.protected,'release_id':'SYNTHETIC_TEST_ONLY','validation_status':'PASS'})

    def write(self,path,data):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    def json(self,path,data):self.write(path,json.dumps(data).encode('utf-8'))
    def assert_original(self):
        self.assertTrue(all(overlay.digest(self.target/p)==r['before'] for p,r in self.files.items()))
        self.assert_protected()
    def assert_protected(self):self.assertTrue(all(overlay.digest(self.target/p)==h for p,h in self.protected.items()))

    def test_check_is_read_only(self):
        before={p.relative_to(self.target):p.read_bytes() for p in self.target.rglob('*') if p.is_file()}
        overlay.check(self.bundle,self.target)
        self.assertEqual(before,{p.relative_to(self.target):p.read_bytes() for p in self.target.rglob('*') if p.is_file()})

    def test_apply_requires_explicit_approval(self):
        with self.assertRaises(PermissionError):overlay.apply(self.bundle,self.target,self.backups)
        self.assert_original();self.assertFalse(self.backups.exists())

    def test_apply_then_rollback_changes_only_five_allowed_files(self):
        result=overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assertEqual(result['status'],'APPLIED_STOPPED');self.assertFalse(result['trading_started'])
        self.assertTrue(all(overlay.digest(self.target/p)==r['after'] for p,r in self.files.items()))
        self.assert_protected()
        restored=overlay.rollback(Path(result['backup']),self.target,approved=True)
        self.assertEqual(restored['status'],'ROLLED_BACK_STOPPED');self.assertFalse(restored['ledger_restored'])
        self.assert_original()

    def test_live_mode_prevents_apply(self):
        self.json(self.target/'runtime/automation_policy.json',{'mode':'LIVE'})
        with self.assertRaisesRegex(RuntimeError,'STOP_REQUIRED'):overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assertTrue(all(overlay.digest(self.target/p)==r['before'] for p,r in self.files.items()))

    def test_changed_source_is_not_overwritten(self):
        changed=self.target/'automation/runner.py';changed.write_text('# independently changed',encoding='utf-8')
        with self.assertRaises(RuntimeError):overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assertEqual(changed.read_text(encoding='utf-8'),'# independently changed')

    def test_injected_partial_write_failure_restores_source_and_identity(self):
        original=overlay.atomic_bytes;calls=[]
        def faulty(path,data):
            calls.append(str(path))
            if len(calls)==4:raise OSError('SYNTHETIC_REPLACE_FAILURE')
            return original(path,data)
        with patch.object(overlay,'atomic_bytes',side_effect=faulty):
            with self.assertRaisesRegex(OSError,'SYNTHETIC_REPLACE_FAILURE'):
                overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assert_original()
        receipts=list(self.backups.glob('*/receipt.json'));self.assertEqual(len(receipts),1)
        self.assertEqual(overlay.read(receipts[0])['status'],'FAILED_ROLLED_BACK')

    def test_rollback_preserves_later_independent_source_change(self):
        result=overlay.apply(self.bundle,self.target,self.backups,approved=True)
        changed=self.target/'automation/runner.py';changed.write_text('# later change',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'TARGET_CHANGED_SINCE_DEPLOY'):
            overlay.rollback(Path(result['backup']),self.target,approved=True)
        self.assertEqual(changed.read_text(encoding='utf-8'),'# later change');self.assert_protected()

    def test_running_process_prevents_apply(self):
        with patch.object(overlay,'running_processes',return_value=[{'ProcessId':42,'Name':'SYNTHETIC'}]):
            with self.assertRaisesRegex(RuntimeError,'TARGET_PROCESS_RUNNING'):
                overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assert_original()

    def test_existing_runtime_lock_prevents_apply(self):
        with overlay.runtime_locks(self.target):
            with self.assertRaisesRegex(RuntimeError,'RUNTIME_LOCK_BUSY'):
                overlay.apply(self.bundle,self.target,self.backups,approved=True)
        self.assert_original()


if __name__=='__main__':unittest.main()
