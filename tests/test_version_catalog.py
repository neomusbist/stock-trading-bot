import copy
import json
from pathlib import Path
import unittest
from tools.validate_versions import validate,ordered_versions


class VersionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((Path(__file__).resolve().parents[1]/'VERSION_INDEX.json').read_text(encoding='utf-8'))
    def test_catalog_uses_confirmed_current_release(self):
        self.assertEqual(validate(self.data),[])
    def test_long_build_is_not_a_user_version(self):
        self.data['versions'][1]['version']='v1.1-fix-20261001'
        self.assertIn('VERSION_NUMBER_STRING_ONLY',validate(self.data))
    def test_numeric_decimal_is_not_a_version_identifier(self):
        self.data['versions'][1]['version']=1.1
        self.assertIn('VERSION_NUMBER_STRING_ONLY',validate(self.data))
    def test_same_code_cannot_get_another_version_even_with_another_commit(self):
        self.data['versions'][2]['runtime_source_fingerprint']=self.data['versions'][1]['runtime_source_fingerprint']
        self.assertIn('SAME_CODE_MULTIPLE_VERSIONS',validate(self.data))
    def test_missing_v10_must_not_be_given_a_commit(self):
        self.data['versions'][0]['sha']='f'*40
        self.assertIn('UNKNOWN_MUST_NOT_INVENT_RELEASE_SHA',validate(self.data))
    def test_historical_prerelease_cannot_be_latest(self):
        self.data['current_release']='v1.4'
        self.assertIn('LATEST_MUST_BE_CONFIRMED_RELEASE',validate(self.data))
    def test_order_comes_from_explicit_registry_even_when_input_is_reversed(self):
        self.data['versions'].reverse()
        self.assertEqual([r['version'] for r in ordered_versions(self.data)],
            ['v1.0','v1.1','v1.2','v1.3','v1.4','v1.5','v1.6','v1.7'])


if __name__=='__main__':unittest.main()
