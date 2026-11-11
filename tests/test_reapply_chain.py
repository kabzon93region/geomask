"""Re-applying over an already applied path must not break a later revoke.

The apply that replaces a managed generation used to record that very generation as the
source to restore. Its pool file is pruned by the same apply, so a later revoke pointed the
group at a provider file that no longer existed.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1] / 'assets'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = _load('audit_state_reapply', 'geomask-audit-state.py')
apply = _load('apply_reapply', 'geomask-apply.py')


class OriginalSourceTests(unittest.TestCase):
    def test_a_managed_source_is_replaced_by_the_original(self):
        applied = {'previous_use': ['sub2']}
        self.assertEqual(apply._original_source(applied, ['audit-vpn-g4']), ['sub2'])

    def test_a_plain_source_is_kept(self):
        self.assertEqual(apply._original_source({}, ['sub2']), ['sub2'])

    def test_an_original_source_is_not_lost_when_the_group_is_already_managed(self):
        applied = {'previous_use': ['sub2'], 'generation': 3}
        self.assertEqual(apply._original_source(applied, ['audit-vpn-g3']), ['sub2'])

    def test_without_a_recorded_original_the_current_source_is_used(self):
        self.assertEqual(apply._original_source({'previous_use': []}, ['audit-vpn-g9']),
                         ['audit-vpn-g9'])


class ReapplyChainTests(unittest.TestCase):
    """End to end through apply: the second apply keeps the subscription as the fallback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.snapshots = self.root / 'snapshots'
        self.pools = self.root / 'pools'
        self.pools.mkdir()
        self.config = self.root / 'config.yaml'

        self.memory = {'schema': 2, 'generation': 1,
                       'paths': {'vpn': {'path_revision': 'r1'}}}

        def load():
            return json.loads(json.dumps(self.memory)), None

        def save(value):
            self.memory = json.loads(json.dumps(value))

        def commit(files, **kwargs):
            # Stand in for the real transaction: write the candidate so the file-level
            # assertions below see what would have landed.
            for target, data in files.items():
                Path(target).write_bytes(data)
            return {'ok': True, 'tx_id': 't'}

        for patcher in (
            mock.patch.object(apply, 'DEFAULT_CONFIG', self.config),
            mock.patch.object(apply, 'DEFAULT_STATE_DIR', self.reports),
            mock.patch.object(apply, 'POOL_DIR', self.pools),
            mock.patch.object(apply, 'load_store', side_effect=load),
            mock.patch.object(apply, 'save_store', side_effect=save),
            mock.patch.object(apply, 'load_node_snapshot',
                              side_effect=lambda i, j: state.load_node_snapshot(i, j, self.snapshots)),
            mock.patch.object(apply, '_preview_token', return_value='pid'),
            mock.patch.object(apply, 'start_transaction', side_effect=commit),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _prepare(self, use, applied=None):
        self.config.write_text(
            'proxy-providers:\n'
            '  sub2:\n'
            '    type: http\n'
            '    url: "https://example.com/sub"\n'
            + ('  audit-vpn-g3:\n    type: file\n    path: %s/vpn-g3.yaml\n' % self.pools
               if use == ['audit-vpn-g3'] else '') +
            'proxy-groups:\n'
            '  - name: VPN\n'
            '    type: url-test\n'
            '    use:\n'
            + ''.join('      - %s\n' % p for p in use), encoding='utf-8')
        entry = {'path_revision': 'r1'}
        if applied:
            entry['applied'] = applied
        self.memory['paths']['vpn'] = entry
        fingerprint = apply._fingerprint_of({'name': 'A'}, 'sub2')
        (self.reports / 'vpn.json').write_text(json.dumps({
            'job_id': 'job1', 'status': 'complete', 'stale': False,
            'settings_revision': 'r1',
            'source_revision': apply.digest(self.config.read_bytes()),
            'nodes': [{'name': 'A', 'status': 'eligible', 'node_fingerprint': fingerprint}],
        }), encoding='utf-8')
        state.save_node_snapshot('vpn', 'job1', {'A': {
            'provider': 'sub2', 'node_fingerprint': fingerprint,
            'definition': {'name': 'A'},
        }}, self.snapshots)
        (self.pools / 'vpn-g3.yaml').write_text('proxies: []\n', encoding='utf-8')

    def test_reapply_keeps_the_subscription_as_the_restore_point(self):
        self._prepare(['audit-vpn-g3'],
                      applied={'status': 'applied', 'generation': 3,
                               'provider_id': 'audit-vpn-g3', 'previous_use': ['sub2']})
        result = apply.apply_preview('vpn', 'pid', 'r1')
        self.assertTrue(result.get('ok'))
        record = self.memory['paths']['vpn']['applied']
        self.assertEqual(record['previous_use'], ['sub2'])
        self.assertEqual(record['provider_id'], 'audit-vpn-g4')
        self.assertNotIn('audit-vpn-g3', (apply.yaml_load(self.config.read_text(encoding='utf-8'))
                                          .get('proxy-providers') or {}))
        self.assertFalse((self.pools / 'vpn-g3.yaml').exists())

    def test_a_first_apply_still_records_the_current_source(self):
        self._prepare(['sub2'])
        apply.apply_preview('vpn', 'pid', 'r1')
        record = self.memory['paths']['vpn']['applied']
        self.assertEqual(record['previous_use'], ['sub2'])
        self.assertEqual(record['provider_id'], 'audit-vpn-g1')


if __name__ == '__main__':
    unittest.main()
