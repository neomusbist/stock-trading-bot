"""LIVE ka10023 early-discovery source: joins the normal LIVE rank pipeline, fail-safe."""
import time
import unittest

import automation.radar as radar
from automation.observation_settings import ObservationSettings

KEYS = {'ka10029': 'exp_cntr_flu_rt_upper', 'ka10032': 'trde_prica_upper', 'ka10027': 'pred_pre_flu_rt_upper',
        'ka00198': 'item_inq_rank', 'ka10031': 'pred_trde_qty_upper'}


def row(code, name, **extra):
    return {'stk_cd': code, 'stk_nm': name, 'cur_prc': '+13880', 'flu_rt': '+3.1', **extra}


class FakeRest:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.radar_observation_settings = ObservationSettings(rank_limit_multiplier=2)

    def query(self, api, payload, path=None):
        self.calls.append(api)
        if api == 'ka10023':
            if self.fail:
                raise RuntimeError('ka10023: contract error')
            return {'trde_qty_sdnin': [row('144960', '뉴파워프라즈마', sdnin_rt='+250')]}
        return {KEYS[api]: [row('005930', '삼성전자')]}

    def pages(self, api, payload, key, path=None, limit=None):
        data = self.query(api, payload, path=path)
        return (data.get(key, [])[:limit], {})


class LiveEarlyDiscoveryTests(unittest.TestCase):
    def test_ka10023_joins_live_pipeline_as_ordinary_source(self):
        rest = FakeRest()
        items = {x['code']: x for x in radar.collect(rest)}
        self.assertEqual(rest.calls[-1], 'ka10023')
        self.assertEqual(len(rest.calls), 6)
        self.assertIn('ka10023', items['144960']['source_details'])
        self.assertEqual(items['144960']['reference_price'], 13880)
        self.assertTrue(rest.early_discovery_status['ok'])
        self.assertEqual(rest.early_discovery_status['symbols'][0]['code'], '144960')
        self.assertNotIn('SHADOW_ONLY', items['144960'])

    def test_failure_never_blocks_verified_ranks_and_backs_off(self):
        rest = FakeRest(fail=True)
        items = radar.collect(rest)
        self.assertEqual({x['code'] for x in items}, {'005930'})
        self.assertFalse(rest.early_discovery_status['ok'])
        for _ in range(5):
            radar.collect(rest)
        self.assertEqual(rest.early_discovery_status['skipped'], 'BACKOFF_AFTER_REPEATED_FAILURE')
        self.assertEqual(rest.calls.count('ka10023'), 3)

    def test_switch_off_restores_original_five_ranks(self):
        rest = FakeRest()
        rest.radar_observation_settings = ObservationSettings(live_early_discovery_enabled=False)
        radar.collect(rest)
        self.assertNotIn('ka10023', rest.calls)
        self.assertEqual(len(rest.calls), 5)


if __name__ == '__main__':
    unittest.main()
