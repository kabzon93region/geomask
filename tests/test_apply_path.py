"""Full apply path with the transaction stubbed out.

Exists because a real run failed with TxError: the apply passed the YAML as `str` while
the transaction API only accepts bytes. A static check would not have caught it, so this
test drives apply_preview end to end and inspects the payload it hands to the transaction.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

state = _load('audit_state_applypath', 'geomask-audit-state.py')
apply = _load('apply_applypath', 'geomask-apply.py')

GROUP_CONFIG = """\
secret: "x"
proxy-providers:
  sub2:
    type: http
    url: "https://example.com/sub"
proxy-groups:
  - name: VPN
    type: url-test
    use: [sub2]
  - name: VPN-noP2P
    type: url-test
    use: [sub2]
  - name: AI
    type: url-test
    use: [sub2]
rules:
  - MATCH,VPN
"""


def definition(name, server):
    return {'name': name, 'type': 'socks5', 'server': server, 'port': 1080}


class ApplyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text(GROUP_CONFIG, encoding='utf-8')
        self.state_dir = self.root / 'state'
        self.state_dir.mkdir()
        self.snapshots = self.root / 'snapshots'
        self.pool_dir = self.root / 'pools'
        self.store = self.root / 'node-audit.json'

        data = {'api_schema': 2, 'paths': {
            'vpn': {'configured': True, 'countries_allow': ['DE'],
                    'path_revision': 'a' * 64, 'path_generation': 1,
                    'schedule': {'enabled': False, 'time': '03:30', 'mode': 'check-only'},
                    'applied': {'generation': 0, 'status': 'never'}},
            'vpn_nop2p': {}, 'ai': {}}}
        self.store.write_text(json.dumps(data), encoding='utf-8')

        self.nodes = [definition('A', '1.1.1.1'), definition('B', '2.2.2.2')]
        entries = {}
        for node in self.nodes:
            entries[node['name']] = {
                'provider': 'sub2',
                'node_fingerprint': apply._fingerprint_of(node, 'sub2'),
                'definition': node,
            }
        self.job = 'j' * 24
        state.save_node_snapshot('vpn', self.job, entries, self.snapshots)
        self.entries = entries

        self.report = {
            'job_id': self.job,
            'status': 'complete',
            'stale': False,
            'settings_revision': 'a' * 64,
            'source_revision': state.digest(self.config.read_bytes()),
            'nodes': [{'name': n['name'], 'status': 'eligible',
                       'node_fingerprint': entries[n['name']]['node_fingerprint']}
                      for n in self.nodes],
        }
        (self.state_dir / 'vpn.json').write_text(json.dumps(self.report), encoding='utf-8')

        self.patchers = [
            patch.object(apply, 'DEFAULT_CONFIG', self.config),
            patch.object(apply, 'DEFAULT_STATE_DIR', self.state_dir),
            patch.object(apply, 'POOL_DIR', self.pool_dir),
            patch.object(apply, 'load_node_snapshot',
                         side_effect=lambda i, j: state.load_node_snapshot(i, j, self.snapshots)),
            patch.object(apply, 'load_store',
                         side_effect=lambda: (json.loads(self.store.read_text()), None)),
            patch.object(apply, 'save_store',
                         side_effect=lambda d: self.store.write_text(json.dumps(d))),
            patch.object(apply, 'append_event', side_effect=lambda e: None),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

        self.tx_calls = []
        tx_patch = patch.object(apply, 'start_transaction',
                                side_effect=self._fake_tx)
        tx_patch.start()
        self.addCleanup(tx_patch.stop)

    def _fake_tx(self, files, **kwargs):
        self.tx_calls.append((files, kwargs))
        return {'ok': True, 'tx_id': 'deadbeef', 'config_digest': 'f' * 64}

    def preview(self):
        pool = apply._compile_eligible_pool(
            'vpn', self.report, ['DE'])
        return apply._preview_token('vpn', self.report, pool, ['DE'])

    def test_apply_hands_bytes_to_the_transaction(self):
        token = self.preview()
        out = apply.apply_preview('vpn', token, 'a' * 64)
        self.assertTrue(out['ok'])
        self.assertEqual(len(self.tx_calls), 1)
        files, kwargs = self.tx_calls[0]
        for path, content in files.items():
            self.assertIsInstance(content, bytes,
                                  'transaction payload for %s must be bytes' % path)
        # Pooled nodes get a suffix: the same names also exist in the source subscription,
        # and mihomo keys proxies by name process-wide. A duplicate name made the dashboard
        # resolve the group child to the source copy and render no cards at all.
        self.assertEqual(kwargs.get('require_exactly'), {'VPN': ['A · g1', 'B · g1']})

    def test_pool_names_are_unique_and_recorded(self):
        apply.apply_preview('vpn', self.preview(), 'a' * 64)
        pool = __import__('yaml').safe_load(
            (self.pool_dir / 'vpn-g1.yaml').read_text(encoding='utf-8'))
        names = [node['name'] for node in pool['proxies']]
        self.assertEqual(sorted(names), ['A · g1', 'B · g1'])
        store = apply.load_store()[0]
        renames = store['paths']['vpn']['applied']['renames']
        self.assertEqual(renames, {'A': 'A · g1', 'B': 'B · g1'})
        # The original definitions stay available for the next audit; only the pool is renamed.
        self.assertTrue(list(self.snapshots.glob('vpn-*.json')), 'snapshot kept for the next audit')

    def test_apply_writes_a_private_pool_and_switches_the_group(self):
        apply.apply_preview('vpn', self.preview(), 'a' * 64)
        pool_files = list(self.pool_dir.glob('vpn-g1.yaml'))
        self.assertEqual(len(pool_files), 1, 'exactly one managed pool file per generation')
        if sys.platform != 'win32':
            self.assertEqual(pool_files[0].stat().st_mode & 0o777, 0o600)

        files, _ = self.tx_calls[0]
        self.assertIn(str(self.config), files)
        config = __import__('yaml').safe_load(files[str(self.config)].decode('utf-8'))
        group = [g for g in config['proxy-groups'] if g['name'] == 'VPN'][0]
        self.assertEqual(group['use'], ['audit-vpn-g1'])
        self.assertNotIn('filter', group)
        self.assertIn('audit-vpn-g1', config['proxy-providers'])
        self.assertEqual(config['proxy-providers']['audit-vpn-g1']['type'], 'file')

        # Untouched neighbours.
        other = [g for g in config['proxy-groups'] if g['name'] == 'AI'][0]
        self.assertEqual(other['use'], ['sub2'])

    def test_refused_transaction_leaves_no_pool_file(self):
        with patch.object(apply, 'start_transaction',
                          side_effect=apply._tx.TxError('nope')):
            with self.assertRaises(Exception):
                apply.apply_preview('vpn', self.preview(), 'a' * 64)
        self.assertEqual(list(self.pool_dir.glob('*.yaml')), [])

    def test_revoke_hands_bytes_and_restores_the_original_source(self):
        apply.apply_preview('vpn', self.preview(), 'a' * 64)
        self.tx_calls.clear()
        out = apply.revoke_applied_pool('vpn')
        self.assertTrue(out['ok'])
        files, _ = self.tx_calls[0]
        self.assertIsInstance(files[str(self.config)], bytes)
        self.assertIn(str(self.config), files)
        config = __import__('yaml').safe_load(files[str(self.config)].decode('utf-8'))
        group = [g for g in config['proxy-groups'] if g['name'] == 'VPN'][0]
        self.assertEqual(group['use'], ['sub2'])
        self.assertNotIn('audit-vpn-g1', config['proxy-providers'])
        self.assertEqual(list(self.pool_dir.glob('*.yaml')), [])

    def test_apply_suspends_legacy_exclude_filter_and_restores_it(self):
        """A legacy exclude-filter silently narrowed the applied pool on a real run."""
        legacy = 'Россия|Финляндия|Нидерланды'
        config = GROUP_CONFIG.replace(
            '  - name: VPN\n    type: url-test\n    use: [sub2]\n',
            "  - name: VPN\n    type: url-test\n    use: [sub2]\n"
            "    exclude-filter: '%s'\n" % legacy, 1)
        self.config.write_text(config, encoding='utf-8')
        self.report['source_revision'] = state.digest(self.config.read_bytes())
        (self.state_dir / 'vpn.json').write_text(json.dumps(self.report), encoding='utf-8')

        apply.apply_preview('vpn', self.preview(), 'a' * 64)
        files, _ = self.tx_calls[0]
        applied_cfg = __import__('yaml').safe_load(files[str(self.config)].decode('utf-8'))
        group = [g for g in applied_cfg['proxy-groups'] if g['name'] == 'VPN'][0]
        self.assertNotIn('exclude-filter', group)
        self.assertEqual(group['use'], ['audit-vpn-g1'])

        self.tx_calls.clear()
        apply.revoke_applied_pool('vpn')
        files, _ = self.tx_calls[0]
        restored = __import__('yaml').safe_load(files[str(self.config)].decode('utf-8'))
        group = [g for g in restored['proxy-groups'] if g['name'] == 'VPN'][0]
        self.assertEqual(group['exclude-filter'], legacy)
        self.assertEqual(group['use'], ['sub2'])

    def test_apply_refuses_a_non_provider_group(self):
        config = GROUP_CONFIG.replace('    use: [sub2]\n', '    proxies: [A]\n', 1)
        self.config.write_text(config, encoding='utf-8')
        self.report['source_revision'] = state.digest(self.config.read_bytes())
        (self.state_dir / 'vpn.json').write_text(json.dumps(self.report), encoding='utf-8')
        with self.assertRaises(apply.ApplyError) as ctx:
            apply.apply_preview('vpn', self.preview(), 'a' * 64)
        self.assertIn('not provider-backed', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
