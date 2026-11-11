"""Tests for building a managed pool from measured node definitions.

These cover the property that matters: the applied pool may only contain endpoints that
this exact check measured, and a rewritten source cannot slide a different endpoint in
under an approved name.
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

state = _load('audit_state_pool', 'geomask-audit-state.py')
apply = _load('apply_pool', 'geomask-apply.py')


def definition(name, server):
    return {'name': name, 'type': 'socks5', 'server': server, 'port': 1080}


def fingerprint(node, provider='sub'):
    """Delegate to the shipped implementation so a test cannot drift from it."""
    return apply._fingerprint_of(node, provider)


class ManagedPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshots = self.root / 'snapshots'
        self.pool_dir = self.root / 'pools'
        self.patches = {
            'snap': patch.object(state, 'DEFAULT_SNAPSHOTS_DIR', self.snapshots),
            'dir': patch.object(apply, 'POOL_DIR', self.pool_dir),
            'load': patch.object(apply, 'load_node_snapshot',
                                 side_effect=lambda i, j: state.load_node_snapshot(
                                     i, j, self.snapshots)),
        }
        for p in self.patches.values():
            p.start()
            self.addCleanup(p.stop)

    def snapshot(self, job, nodes, provider='sub'):
        entries = {}
        for node in nodes:
            entries[node['name']] = {
                'provider': provider,
                'node_fingerprint': fingerprint(node, provider),
                'definition': node,
            }
        state.save_node_snapshot('vpn', job, entries, self.snapshots)
        return entries

    def report(self, job, nodes, entries):
        return {'job_id': job,
                'nodes': [{'name': n['name'], 'node_fingerprint': entries[n['name']]['node_fingerprint'],
                           'status': 'eligible'} for n in nodes]}

    def test_snapshot_round_trip(self):
        entries = self.snapshot('j1', [definition('A', '1.1.1.1')])
        self.assertIn('A', entries)
        loaded = state.load_node_snapshot('vpn', 'j1', self.snapshots)
        self.assertEqual(loaded['A']['definition']['server'], '1.1.1.1')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX permissions are not modelled on Windows')
    def test_snapshot_file_is_root_only(self):
        self.snapshot('j1', [definition('A', '1.1.1.1')])
        mode = (self.snapshots / 'vpn-j1.json').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_snapshot_rejects_a_mislabelled_file(self):
        """A file placed under another path's name must not be trusted."""
        self.snapshot('j1', [definition('A', '1.1.1.1')])
        payload = json.loads((self.snapshots / 'vpn-j1.json').read_text(encoding='utf-8'))
        (self.snapshots / 'ai-j1.json').write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaises(state.AuditError):
            state.load_node_snapshot('ai', 'j1', self.snapshots)

    def test_missing_snapshot_is_none_not_an_error(self):
        self.assertIsNone(state.load_node_snapshot('vpn', 'nope', self.snapshots))

    def test_old_snapshots_are_pruned(self):
        for n in range(state.MAX_SNAPSHOTS_PER_PATH + 3):
            self.snapshot('j%d' % n, [definition('A', '1.1.1.1')])
        kept = list(self.snapshots.glob('vpn-*.json'))
        self.assertLessEqual(len(kept), state.MAX_SNAPSHOTS_PER_PATH)

    def test_pool_contains_only_measured_definitions(self):
        nodes = [definition('A', '1.1.1.1'), definition('B', '2.2.2.2'),
                 definition('C', '3.3.3.3')]
        entries = self.snapshot('j1', nodes)
        report = self.report('j1', nodes[:2], entries)
        proxies = apply._build_managed_proxies(
            'vpn', report, [{'name': 'A', 'node_fingerprint': entries['A']['node_fingerprint']},
                            {'name': 'B', 'node_fingerprint': entries['B']['node_fingerprint']}])
        self.assertEqual([p['name'] for p in proxies], ['A', 'B'])
        self.assertNotIn('C', [p['name'] for p in proxies])

    def test_a_rewritten_definition_is_refused(self):
        nodes = [definition('A', '1.1.1.1')]
        entries = self.snapshot('j1', nodes)
        # Same name, different endpoint, fingerprinted by the attacker to look consistent.
        tampered = dict(entries['A'], definition=definition('A', '6.6.6.6'))
        state.save_node_snapshot('vpn', 'j2', {'A': tampered}, self.snapshots)
        report = {'job_id': 'j2', 'nodes': [{'name': 'A', 'node_fingerprint': entries['A']['node_fingerprint'],
                                             'status': 'eligible'}]}
        with self.assertRaises(apply.ApplyError):
            apply._build_managed_proxies('vpn', report, [{'name': 'A',
                                                          'node_fingerprint': entries['A']['node_fingerprint']}])

    def test_a_node_absent_from_the_snapshot_is_refused(self):
        nodes = [definition('A', '1.1.1.1')]
        entries = self.snapshot('j1', nodes)
        report = self.report('j1', nodes, entries)
        with self.assertRaises(apply.ApplyError) as ctx:
            apply._build_managed_proxies('vpn', report, [{'name': 'Z'}])
        self.assertIn('was not measured', str(ctx.exception))

    def test_pool_file_is_private_and_parseable(self):
        proxies = [definition('A', '1.1.1.1')]
        target = apply._write_pool_file('vpn', 3, proxies)
        self.assertEqual(target, self.pool_dir / 'vpn-g3.yaml')
        if sys.platform != 'win32':
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        loaded = __import__('yaml').safe_load(target.read_text(encoding='utf-8'))
        self.assertEqual([p['name'] for p in loaded['proxies']], ['A'])

    def test_pruning_keeps_only_the_current_generation(self):
        for generation in (1, 2, 3):
            apply._write_pool_file('vpn', generation, [definition('A', '1.1.1.1')])
        apply._prune_pool_files('vpn', 3)
        left = sorted(p.name for p in self.pool_dir.glob('vpn-*.yaml'))
        self.assertEqual(left, ['vpn-g3.yaml'])

    def test_provider_id_is_stable_and_per_generation(self):
        self.assertEqual(apply._provider_id('vpn', 4), 'audit-vpn-g4')
        self.assertNotEqual(apply._provider_id('vpn', 4), apply._provider_id('ai', 4))


if __name__ == '__main__':
    unittest.main()
