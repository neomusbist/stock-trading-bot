"""LIVE ka10023 early-discovery source: joins the normal LIVE rank pipeline, fail-safe."""
import time
import unittest

import automation.radar as radar
from automation.observation_settings import ObservationSettings
from automation.early_discovery import get_volume_surge_rank_spec
from core.util import code

KEYS = {'ka10029': 'exp_cntr_flu_rt_upper', 'ka10032': 'trde_prica_upper', 'ka10027': 'pred_pre_flu_rt_upper',
        'ka00198': 'item_inq_rank', 'ka10031': 'pred_trde_qty_upper'}


def row(sym_code, name, **extra):
    return {'stk_cd': sym_code, 'stk_nm': name, 'cur_prc': '+13880', 'flu_rt': '+3.1', **extra}


class FakeRest:
    def __init__(self, fail=False, custom_ka10023_rows=None):
        self.calls = []
        self.payloads = {}
        self.fail = fail
        self.custom_ka10023_rows = custom_ka10023_rows
        self.radar_observation_settings = ObservationSettings(rank_limit_multiplier=2)

    def query(self, api, payload, path=None):
        self.calls.append(api)
        self.payloads[api] = payload
        if api == 'ka10023':
            if self.fail:
                raise RuntimeError('ka10023: contract error')
            if self.custom_ka10023_rows is not None:
                return {'trde_qty_sdnin': self.custom_ka10023_rows}
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

    def test_code_sanitizes_krx_nxt_and_all_suffixes(self):
        self.assertEqual(code('009900_AL'), '009900')
        self.assertEqual(code('176750_NX'), '176750')
        self.assertEqual(code('A009900_AL'), '009900')
        self.assertEqual(code('A176750_NX'), '176750')
        self.assertEqual(code('005930'), '005930')
        self.assertEqual(code('A005930'), '005930')
        with self.assertRaises(ValueError):
            code('12345')
        with self.assertRaises(ValueError):
            code('INVALID_SYMBOL')

    def test_get_volume_surge_rank_spec_session_branching(self):
        # Regular session defaults to KRX (stex_tp='1')
        spec_reg = get_volume_surge_rank_spec('REGULAR', 'KRX')
        self.assertEqual(spec_reg[0], 'ka10023')
        self.assertEqual(spec_reg[1], 'trde_qty_sdnin')
        self.assertEqual(spec_reg[3]['stex_tp'], '1')

        # After session or SOR venue branches to unified KRX+NXT (stex_tp='3')
        spec_after = get_volume_surge_rank_spec('AFTER', 'SOR')
        self.assertEqual(spec_after[3]['stex_tp'], '3')

        spec_after_regvenue = get_volume_surge_rank_spec('AFTER', 'KRX')
        self.assertEqual(spec_after_regvenue[3]['stex_tp'], '3')

        # NXT venue branches to NXT (stex_tp='2')
        spec_nxt = get_volume_surge_rank_spec('REGULAR', 'NXT')
        self.assertEqual(spec_nxt[3]['stex_tp'], '2')

    def test_ka10023_parses_nx_and_al_symbols(self):
        custom_rows = [
            row('009900_AL', '명신산업', sdnin_rt='+150'),
            row('176750_NX', '듀켐바이오', sdnin_rt='+220'),
        ]
        rest = FakeRest(custom_ka10023_rows=custom_rows)
        items = {x['code']: x for x in radar.collect(rest)}
        self.assertIn('009900', items)
        self.assertIn('176750', items)
        self.assertEqual(items['009900']['reference_price'], 13880)
        self.assertEqual(items['176750']['reference_price'], 13880)
        self.assertTrue(rest.early_discovery_status['ok'])
        symbols = [s['code'] for s in rest.early_discovery_status['symbols']]
        self.assertIn('009900', symbols)
        self.assertIn('176750', symbols)


if __name__ == '__main__':
    unittest.main()
