"""Unit tests for geomask-audit-state.py (schema v2)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-audit-state.py'
spec = importlib.util.spec_from_file_location('audit_state', SOURCE)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)

class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Store paths for passing to functions
        self.store = self.root / 'node-audit.json'
        self.events = self.root / 'events.jsonl'
        self.state_dir = self.root / 'state'
        self.pools_dir = self.root / 'pools'
        self.addCleanup(self.temp.cleanup)

    def test_create_default_v2(self):
        data = state.create_default_v2()
        self.assertEqual(data['schema'], 2)
        self.assertEqual(set(data['paths'].keys()), {'vpn', 'vpn_nop2p', 'ai'})
        for ident in state.PATHS:
            self.assertEqual(data['paths'][ident]['path_generation'], 1)
            self.assertIsNotNone(data['paths'][ident]['path_revision'])
            self.assertEqual(data['paths'][ident]['applied']['generation'], 0)
            self.assertFalse(data['paths'][ident]['configured'])

    def test_migrate_v1_to_v2_preserves_countries(self):
        v1 = {
            'schema': 1,
            'generation': 6,
            'paths': {
                'vpn': {'countries_allow': ['DE', 'FR', 'NL', 'PL']},
                'vpn_nop2p': {'countries_allow': ['DE']},
                'ai': {'countries_allow': ['US']}
            }
        }
        v2 = state.migrate_v1_to_v2(v1)
        self.assertEqual(v2['schema'], 2)
        self.assertEqual(v2['paths']['vpn']['countries_allow'], ['DE', 'FR', 'NL', 'PL'])
        self.assertEqual(v2['paths']['vpn_nop2p']['countries_allow'], ['DE'])
        self.assertEqual(v2['paths']['ai']['countries_allow'], ['US'])
        self.assertEqual(v2['paths']['vpn']['path_generation'], 1)
        self.assertIsNotNone(v2['paths']['vpn']['path_revision'])
        # Paths that existed in v1 should be configured
        self.assertTrue(v2['paths']['vpn']['configured'])
        self.assertTrue(v2['paths']['vpn_nop2p']['configured'])
        self.assertTrue(v2['paths']['ai']['configured'])

    def test_migrate_v1_empty_uses_defaults(self):
        v1 = {'schema': 1, 'generation': 0, 'paths': {}}
        v2 = state.migrate_v1_to_v2(v1)
        self.assertEqual(v2['paths']['vpn']['countries_allow'], ['DE', 'FR', 'NL', 'SE'])
        self.assertEqual(v2['paths']['vpn_nop2p']['countries_allow'], ['DE', 'PL'])
        self.assertEqual(v2['paths']['ai']['countries_allow'], ['US'])
        # Paths not in v1 should not be configured
        self.assertFalse(v2['paths']['vpn']['configured'])
        self.assertFalse(v2['paths']['vpn_nop2p']['configured'])
        self.assertFalse(v2['paths']['ai']['configured'])

    def test_migrate_idempotent_on_v2(self):
        v1 = {'schema': 1, 'generation': 1, 'paths': {'vpn': {'countries_allow': ['DE']}}}
        v2 = state.migrate_v1_to_v2(v1)
        # Run again on v2 - should not change
        v2_again = state.migrate_v1_to_v2(v2)
        self.assertEqual(v2['paths']['vpn']['path_revision'], v2_again['paths']['vpn']['path_revision'])

    def test_path_revision_changes_only_on_countries_or_binding(self):
        v2 = state.create_default_v2()
        orig_rev = v2['paths']['vpn']['path_revision']
        # Schedule change should NOT change path_revision
        v2['paths']['vpn']['schedule']['enabled'] = True
        v2['paths']['vpn']['schedule']['time'] = '04:00'
        new_rev = state.compute_path_revision(v2['paths']['vpn'])
        self.assertEqual(orig_rev, new_rev)
        # Countries change SHOULD change path_revision
        v2['paths']['vpn']['countries_allow'] = ['DE', 'FR']
        new_rev2 = state.compute_path_revision(v2['paths']['vpn'])
        self.assertNotEqual(orig_rev, new_rev2)
        # Source binding change SHOULD change path_revision
        v2['paths']['vpn']['countries_allow'] = ['DE', 'FR', 'NL', 'SE']  # back to defaults
        v2['paths']['vpn']['source_binding'] = {'provider_ids': ['sub2'], 'source_fingerprint': 'abc'}
        new_rev3 = state.compute_path_revision(v2['paths']['vpn'])
        self.assertNotEqual(orig_rev, new_rev3)

    def test_save_store_recomputes_path_revision(self):
        # Write initial v2 store
        self.store.write_bytes(state.encode(state.create_default_v2()))
        data = state.load_store(self.store)[0]
        orig_rev = data['paths']['vpn']['path_revision']
        orig_gen = data['paths']['vpn']['path_generation']
        # No-op save (same countries)
        data['paths']['vpn']['schedule']['enabled'] = True
        state.save_store(data, self.store)
        reloaded = state.load_store(self.store)[0]
        self.assertEqual(reloaded['paths']['vpn']['path_revision'], orig_rev)
        self.assertEqual(reloaded['paths']['vpn']['path_generation'], orig_gen)
        # Actual countries change
        data['paths']['vpn']['countries_allow'] = ['DE', 'FR']
        state.save_store(data, self.store)
        reloaded = state.load_store(self.store)[0]
        self.assertNotEqual(reloaded['paths']['vpn']['path_revision'], orig_rev)
        self.assertEqual(reloaded['paths']['vpn']['path_generation'], orig_gen + 1)
        # Neighbor unchanged
        self.assertEqual(reloaded['paths']['vpn_nop2p']['path_revision'], data['paths']['vpn_nop2p']['path_revision'])

    def test_events_append_and_trim(self):
        for i in range(10):
            state.append_event({'path': 'vpn', 'action': 'save', 'seq': i}, self.events)
        events = state.get_events('vpn', events=self.events)
        self.assertEqual(len(events), 10)
        # Trim per path
        for i in range(10, 35):
            state.append_event({'path': 'vpn', 'action': 'save', 'seq': i}, self.events)
        events = state.get_events('vpn', events=self.events)
        self.assertLessEqual(len(events), state.MAX_EVENTS_PER_PATH)

    def test_events_pagination(self):
        for i in range(5):
            state.append_event({'path': 'vpn', 'action': 'save', 'seq': i}, self.events)
        # before cursor
        first = state.get_events('vpn', limit=2, events=self.events)
        self.assertEqual(len(first), 2)
        cursor = first[-1]['event_id']
        second = state.get_events('vpn', before=cursor, limit=2, events=self.events)
        self.assertEqual(len(second), 2)
        self.assertNotEqual(first[0]['event_id'], second[0]['event_id'])

    def test_resolve_source_binding(self):
        config = {
            'proxy-groups': [{'name': 'VPN', 'use': ['sub2']}],
            'proxy-providers': {
                'sub2': {'url': 'https://example.com/sub.yaml', 'header': {'User-Agent': 'test'}}
            }
        }
        binding = state.resolve_source_binding(config, 'vpn')
        self.assertIsNotNone(binding)
        self.assertEqual(binding['provider_ids'], ['sub2'])
        self.assertIsNotNone(binding['source_fingerprint'])
        self.assertEqual(binding['subscription_url_digest'], state.digest(b'https://example.com/sub.yaml'))

    def test_resolve_source_binding_missing_group(self):
        config = {'proxy-groups': [], 'proxy-providers': {}}
        binding = state.resolve_source_binding(config, 'vpn')
        self.assertIsNone(binding)

    def test_pool_save_load(self):
        pool = {'nodes': ['n1', 'n2'], 'digest': 'abc123'}
        state.save_pool('vpn', 1, pool, self.pools_dir)
        loaded = state.load_pool('vpn', 1, self.pools_dir)
        self.assertEqual(loaded, pool)
        pool_file = self.pools_dir / 'vpn-gen1.json'
        self.assertTrue(pool_file.exists())
        # Check permissions (0o600) - skip on Windows where chmod is limited
        import platform
        if platform.system() != 'Windows':
            import stat
            mode = pool_file.stat().st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_pool_not_found_raises(self):
        with self.assertRaises(state.AuditError):
            state.load_pool('vpn', 999, self.pools_dir)


if __name__ == '__main__':
    unittest.main()