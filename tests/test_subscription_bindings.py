"""Subscription assignment must survive both YAML styles and a managed pool.

The panel lost every assignment badge after one apply. Root cause: the assignment was parsed
from config text with a regex that expected `  - name: ...` and a double-quoted URL, while
applying a pool re-serialises the whole config with PyYAML. The other half is the managed
file provider: it has no URL, so the subscription has to come from the apply record.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import yaml

HERE = Path(__file__).resolve().parents[1] / 'assets'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


subs = _load('substatus_tests', 'geomask-subscription-status.py')

SUB_A = 'https://a.example.invalid/sub'
SUB_B = 'https://b.example.invalid/sub'
SUB_C = 'https://c.example.invalid/sub'


def config(use_vpn=('sub2',), use_nop2p=('sub',), use_ai=('slot',), extra=None):
    providers = {
        'sub2': {'type': 'http', 'url': SUB_A, 'interval': 3600},
        'sub': {'type': 'http', 'url': SUB_B, 'interval': 3600},
        'slot': {'type': 'http', 'url': SUB_C, 'interval': 3600},
    }
    providers.update(extra or {})
    return {
        'proxy-providers': providers,
        'proxy-groups': [
            {'name': 'VPN', 'type': 'url-test', 'use': list(use_vpn)},
            {'name': 'VPN-noP2P', 'type': 'url-test', 'use': list(use_nop2p)},
            {'name': 'AI', 'type': 'url-test', 'use': list(use_ai)},
        ],
    }


ROWS = [{'id': '100', 'name': 'A', 'url': SUB_A},
        {'id': '200', 'name': 'B', 'url': SUB_B},
        {'id': '300', 'name': 'C', 'url': SUB_C}]


class DirectBindingTests(unittest.TestCase):
    def test_direct_paths_resolve(self):
        bindings = subs.resolve_subscription_bindings(config(), {}, ROWS)
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertEqual(bindings['vpn_nop2p']['subscription_ids'], ['200'])
        self.assertEqual(bindings['ai']['subscription_ids'], ['300'])
        for ident in subs.PATH_IDS:
            self.assertEqual(bindings[ident]['status'], 'resolved')
            self.assertEqual(bindings[ident]['via'], 'direct')

    def test_reserialised_yaml_gives_the_same_answer(self):
        legacy = yaml.safe_load(yaml.safe_dump(config(), allow_unicode=True, sort_keys=False))
        # The dangerous form is the text, so compare against a real round trip.
        reserialised = yaml.safe_load(yaml.safe_dump(legacy, allow_unicode=True, sort_keys=False))
        self.assertEqual(json.dumps(subs.resolve_subscription_bindings(legacy, {}, ROWS), sort_keys=True),
                         json.dumps(subs.resolve_subscription_bindings(
                             yaml.safe_load(yaml.safe_dump(reserialised, sort_keys=False)), {}, ROWS),
                             sort_keys=True))

    def test_flow_style_use_list_resolves(self):
        cfg = config(use_vpn=['sub2', 'sub'])
        bindings = subs.resolve_subscription_bindings(cfg, {}, ROWS)
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100', '200'])
        self.assertEqual(bindings['vpn']['status'], 'resolved')

    def test_bare_and_single_quoted_urls_match_equally(self):
        for quote in ('', "'", '"'):
            cfg = config()
            spec = cfg['proxy-providers']['sub2']
            spec['url'] = SUB_A
            text = yaml.safe_dump(cfg, default_style=quote or None, sort_keys=False)
            parsed = yaml.safe_load(text)
            bindings = subs.resolve_subscription_bindings(parsed, {}, ROWS)
            self.assertEqual(bindings['vpn']['subscription_ids'], ['100'], 'quote=%r' % quote)

    def test_one_source_can_serve_three_paths(self):
        cfg = config(use_vpn=('sub2',), use_nop2p=('sub2',), use_ai=('sub2',))
        bindings = subs.resolve_subscription_bindings(cfg, {}, ROWS)
        for ident in subs.PATH_IDS:
            self.assertEqual(bindings[ident]['subscription_ids'], ['100'])
            self.assertEqual(bindings[ident]['status'], 'resolved')

    def test_duplicate_registry_url_lights_up_every_row(self):
        rows = ROWS + [{'id': '400', 'name': 'dup', 'url': SUB_A}]
        bindings = subs.resolve_subscription_bindings(config(), {}, rows)
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100', '400'])

    def test_source_without_registry_row_is_unregistered(self):
        bindings = subs.resolve_subscription_bindings(config(), {}, [])
        self.assertEqual(bindings['vpn']['status'], 'unregistered')
        self.assertEqual(bindings['vpn']['subscription_ids'], [])
        self.assertEqual(bindings['vpn']['reason_code'], 'registry_has_no_match')

    def test_missing_group_is_not_configured(self):
        cfg = config()
        cfg['proxy-groups'] = [g for g in cfg['proxy-groups'] if g['name'] != 'AI']
        bindings = subs.resolve_subscription_bindings(cfg, {}, ROWS)
        self.assertEqual(bindings['ai']['status'], 'not-configured')
        self.assertEqual(bindings['ai']['reason_code'], 'group_missing')

    def test_group_without_providers_is_not_configured(self):
        bindings = subs.resolve_subscription_bindings(config(use_vpn=()), {}, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'not-configured')

    def test_provider_without_url_is_unknown_not_invented(self):
        cfg = config(extra={'inline': {'type': 'file', 'path': '/tmp/x.yaml'}})
        cfg['proxy-groups'][0]['use'] = ['inline']
        bindings = subs.resolve_subscription_bindings(cfg, {}, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')
        self.assertEqual(bindings['vpn']['reason_code'], 'provider_without_url')


class ManagedBindingTests(unittest.TestCase):
    def applied(self, **overrides):
        record = {'status': 'applied', 'provider_id': 'audit-vpn-g8', 'group': 'VPN',
                  'previous_use': ['sub2']}
        record.update(overrides)
        return {'paths': {'vpn': {'applied': record}}}

    def managed_config(self):
        return config(use_vpn=('audit-vpn-g8',), extra={
            'audit-vpn-g8': {'type': 'file', 'path': '/etc/mihomo/geomask-pools/vpn-g8.yaml'}})

    def test_managed_pool_reports_the_recorded_subscription(self):
        bindings = subs.resolve_subscription_bindings(self.managed_config(), self.applied(), ROWS)
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertEqual(bindings['vpn']['source_provider_ids'], ['sub2'])
        self.assertEqual(bindings['vpn']['via'], 'managed')
        self.assertEqual(bindings['vpn']['status'], 'resolved')

    def test_sibling_paths_keep_their_own_assignment(self):
        bindings = subs.resolve_subscription_bindings(self.managed_config(), self.applied(), ROWS)
        self.assertEqual(bindings['vpn_nop2p']['subscription_ids'], ['200'])
        self.assertEqual(bindings['ai']['subscription_ids'], ['300'])

    def test_unrecorded_managed_pool_is_unknown(self):
        bindings = subs.resolve_subscription_bindings(self.managed_config(), {}, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')
        self.assertEqual(bindings['vpn']['subscription_ids'], [])
        self.assertIsNone(bindings['vpn']['via'])

    def test_record_for_another_provider_is_not_used(self):
        store = self.applied(provider_id='audit-vpn-g7')
        bindings = subs.resolve_subscription_bindings(self.managed_config(), store, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')

    def test_record_for_another_group_is_not_used(self):
        store = self.applied(group='VPN-noP2P')
        bindings = subs.resolve_subscription_bindings(self.managed_config(), store, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')

    def test_revoked_record_does_not_light_up_a_direct_path(self):
        store = self.applied(status='revoked', provider_id='audit-vpn-g8')
        bindings = subs.resolve_subscription_bindings(config(), store, ROWS)
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertEqual(bindings['vpn']['via'], 'direct')

    def test_expired_and_applying_records_still_explain_the_source(self):
        for status in ('expired', 'applying'):
            bindings = subs.resolve_subscription_bindings(self.managed_config(),
                                                          self.applied(status=status), ROWS)
            self.assertEqual(bindings['vpn']['subscription_ids'], ['100'], status)

    def test_missing_original_provider_is_unknown(self):
        store = self.applied(previous_use=['gone'])
        bindings = subs.resolve_subscription_bindings(self.managed_config(), store, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')
        self.assertEqual(bindings['vpn']['reason_code'], 'managed_source_missing')

    def test_mixed_managed_and_direct_is_not_reported_as_one_via(self):
        cfg = config(use_vpn=('audit-vpn-g8', 'sub'), extra={
            'audit-vpn-g8': {'type': 'file', 'path': '/x.yaml'}})
        bindings = subs.resolve_subscription_bindings(cfg, self.applied(), ROWS)
        self.assertEqual(bindings['vpn']['status'], 'resolved')
        self.assertIn('100', bindings['vpn']['subscription_ids'])
        self.assertIn('200', bindings['vpn']['subscription_ids'])
        self.assertIsNone(bindings['vpn']['via'])

    def test_unverified_managed_part_leaves_the_direct_part_partial(self):
        cfg = config(use_vpn=('audit-vpn-g8', 'sub'), extra={
            'audit-vpn-g8': {'type': 'file', 'path': '/x.yaml'}})
        bindings = subs.resolve_subscription_bindings(cfg, {}, ROWS)
        self.assertEqual(bindings['vpn']['status'], 'partial')
        self.assertEqual(bindings['vpn']['subscription_ids'], ['200'])

    def test_a_managed_provider_of_the_wrong_type_is_not_trusted(self):
        cfg = self.managed_config()
        cfg['proxy-providers']['audit-vpn-g8'] = {'type': 'http', 'url': SUB_A}
        bindings = subs.resolve_subscription_bindings(cfg, self.applied(), ROWS)
        self.assertEqual(bindings['vpn']['status'], 'unknown')


class CliAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.yaml'
        self.store_path = self.root / 'store.json'
        self.registry_path = self.root / 'subs.list'
        self.config_path.write_text(yaml.safe_dump(config(), sort_keys=False), encoding='utf-8')
        self.registry_path.write_text('100|A|%s\n200|B|%s\n300|C|%s\n' % (SUB_A, SUB_B, SUB_C),
                                      encoding='utf-8')

    def read(self):
        return subs.read_subscription_bindings(self.config_path, self.store_path,
                                               self.registry_path)

    def run_cli(self, mode, stdin=''):
        old_argv, old_stdin = subs.sys.argv, subs.sys.stdin
        old_paths = (subs.DEFAULT_CONFIG, subs.DEFAULT_STORE, subs.DEFAULT_REGISTRY)
        subs.sys.argv = ['prog', mode]
        subs.sys.stdin = io.StringIO(stdin)
        subs.DEFAULT_CONFIG = self.config_path
        subs.DEFAULT_STORE = self.store_path
        subs.DEFAULT_REGISTRY = self.registry_path
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = subs.main(['prog', mode])
        finally:
            subs.sys.argv, subs.sys.stdin = old_argv, old_stdin
            subs.DEFAULT_CONFIG, subs.DEFAULT_STORE, subs.DEFAULT_REGISTRY = old_paths
        return code, out.getvalue()

    def test_reading_never_creates_files(self):
        before = sorted(p.name for p in self.root.iterdir())
        self.read()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_missing_state_file_is_not_created(self):
        self.assertFalse(self.store_path.exists())
        bindings = self.read()
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertFalse(self.store_path.exists())

    def test_missing_config_is_unknown_not_a_crash(self):
        self.config_path.unlink()
        bindings = self.read()
        for ident in subs.PATH_IDS:
            self.assertEqual(bindings[ident]['status'], 'unknown')
            self.assertEqual(bindings[ident]['reason_code'], 'config_unreadable')

    def test_corrupt_config_is_unknown(self):
        self.config_path.write_text('proxy-groups: [unclosed\n', encoding='utf-8')
        bindings = self.read()
        self.assertEqual(bindings['vpn']['status'], 'unknown')

    def test_corrupt_state_is_unknown(self):
        self.store_path.write_text('{not json', encoding='utf-8')
        bindings = self.read()
        self.assertEqual(bindings['vpn']['status'], 'unknown')

    def test_changing_snapshot_retries_then_reports_unknown(self):
        calls = {'count': 0}
        original = subs._read_snapshot
        real = (self.config_path.read_bytes(), None, self.registry_path.read_bytes())

        def flapping(*args):
            calls['count'] += 1
            return real[0], None, (real[2] + b'\n' * calls['count'])

        subs._read_snapshot = flapping
        try:
            with self.assertRaises(RuntimeError) as ctx:
                subs._read_and_resolve(self.config_path, self.store_path, self.registry_path)
            self.assertEqual(str(ctx.exception), 'snapshot_changed')
        finally:
            subs._read_snapshot = original
        self.assertGreaterEqual(calls['count'], 3)

    def test_status_cli_merges_without_secret_leak(self):
        code, out = self.run_cli('status', stdin='{"vpn_node":"x","ok":true}')
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload['vpn_node'], 'x')
        self.assertEqual(payload['vpn_sub'], '100')
        self.assertEqual(payload['vpn_nop2p_sub'], '200')
        self.assertEqual(payload['ai_sub'], '300')
        self.assertEqual(payload['subscription_bindings']['vpn']['status'], 'resolved')

    def test_legacy_scalar_keeps_a_shared_single_source(self):
        # One id for three slots is unambiguous even though it is shared.
        self.config_path.write_text(yaml.safe_dump(
            config(use_vpn=('sub2',), use_nop2p=('sub2',), use_ai=('sub2',)), sort_keys=False),
            encoding='utf-8')
        code, out = self.run_cli('status', stdin='{}')
        payload = json.loads(out)
        self.assertEqual(payload['vpn_sub'], '100')
        self.assertEqual(payload['ai_sub'], '100')
        self.assertEqual(payload['subscription_bindings']['vpn']['subscription_ids'], ['100'])

    def test_legacy_scalar_is_blank_when_one_path_has_two_sources(self):
        self.config_path.write_text(yaml.safe_dump(config(use_vpn=('sub2', 'sub')),
                                                   sort_keys=False), encoding='utf-8')
        code, out = self.run_cli('status', stdin='{}')
        payload = json.loads(out)
        self.assertEqual(payload['vpn_sub'], '')
        self.assertEqual(sorted(payload['subscription_bindings']['vpn']['subscription_ids']),
                         ['100', '200'])

    def test_cli_never_prints_the_url_outside_legacy_fields(self):
        code, out = self.run_cli('status', stdin='{}')
        payload = json.loads(out)
        blob = json.dumps(payload['subscription_bindings'], ensure_ascii=False)
        self.assertNotIn(SUB_A, blob)
        self.assertNotIn(SUB_A, payload['subscription_bindings']['vpn'].get('reason_code') or '')

    def test_malformed_base_status_is_refused_without_replacement(self):
        code, out = self.run_cli('status', stdin='{"vpn_node": }')
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), '')

    def test_ai_sub_reports_the_actual_provider(self):
        code, out = self.run_cli('ai-sub')
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload['provider'], 'slot')
        self.assertEqual(payload['source_subscription_ids'], ['300'])


if __name__ == '__main__':
    unittest.main()
