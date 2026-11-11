"""Guard tests for the apply entry points.

These do not need mihomo: every case here must be refused before a transaction starts.
The point is that all four public entry points accept a plain path string and never
raise a bare TypeError -- a shadowed name bug in this module previously made preview,
apply and status fail at runtime with exactly that symptom.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

state = _load('audit_state_apply', 'geomask-audit-state.py')


class ApplyGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'node-audit.json'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.config = self.root / 'config.yaml'
        self.config.write_text(
            'proxy-groups:\n'
            '  - name: VPN\n    type: url-test\n    use: [sub]\n'
            '  - name: VPN-noP2P\n    type: url-test\n    use: [sub]\n'
            '  - name: AI\n    type: url-test\n    use: [slot]\n',
            encoding='utf-8')

        apply = _load('apply_under_test', 'geomask-apply.py')
        self.apply = apply
        self.patches = {
            'DEFAULT_CONFIG': patch.object(apply, 'DEFAULT_CONFIG', self.config),
            'DEFAULT_STATE_DIR': patch.object(apply, 'DEFAULT_STATE_DIR', self.state),
        }
        for p in self.patches.values():
            p.start()
            self.addCleanup(p.stop)

        # Minimal v2 store with one configured path.
        data = state.create_default_v2() if hasattr(state, 'create_default_v2') else {
            'api_schema': 2, 'paths': {}}
        data.setdefault('paths', {})['vpn'] = {
            'configured': True,
            'countries_allow': ['DE'],
            'path_revision': 'a' * 64,
            'path_generation': 1,
            'schedule': {'enabled': False, 'time': '03:30', 'mode': 'check-only'},
            'applied': {'generation': 0, 'status': 'never'},
        }
        for ident in ('vpn_nop2p', 'ai'):
            data['paths'].setdefault(ident, {})
        self.store.write_text(json.dumps(data), encoding='utf-8')
        self.store_patch = patch.object(apply, 'load_store',
                                        side_effect=lambda: (json.loads(self.store.read_text()), None))
        self.save_patch = patch.object(apply, 'save_store',
                                       side_effect=lambda d: self.store.write_text(json.dumps(d)))
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.save_patch.start()
        self.addCleanup(self.save_patch.stop)

    def test_preview_rejects_unknown_path_without_type_error(self):
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.preview_apply('nope', 'job', 'a' * 64, 'b' * 64)
        self.assertNotIn('TypeError', str(ctx.exception))

    def test_apply_rejects_unknown_path_without_type_error(self):
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.apply_preview('nope', 'p' * 64, 'a' * 64)
        self.assertNotIn('TypeError', str(ctx.exception))

    def test_status_accepts_a_plain_path_and_reports_never(self):
        out = self.apply.apply_status('vpn')
        self.assertTrue(out['ok'])
        self.assertFalse(out['applied'])
        self.assertEqual(out['applied_detail']['status'], 'never')

    def test_status_rejects_unknown_path(self):
        with self.assertRaises(self.apply.ApplyError):
            self.apply.apply_status('nope')

    def test_preview_refuses_when_revision_is_stale(self):
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.preview_apply('vpn', 'job', '0' * 64, 'b' * 64)
        self.assertIn('STALE', str(ctx.exception))

    def test_apply_refuses_when_revision_is_stale(self):
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.apply_preview('vpn', 'p' * 64, '0' * 64)
        self.assertIn('STALE', str(ctx.exception))

    def test_revoke_of_nothing_is_a_no_op(self):
        out = self.apply.revoke_applied_pool('vpn')
        self.assertTrue(out['ok'])
        self.assertFalse(out['revoked'])

    def test_revoke_rejects_unknown_path(self):
        with self.assertRaises(self.apply.ApplyError):
            self.apply.revoke_applied_pool('nope')

    def test_hosts_regex_is_anchored_and_escaped(self):
        pattern = self.apply._hosts_regex(['DE.1', 'DE+2'])
        import re
        self.assertTrue(re.match(pattern, 'DE.1'))
        self.assertTrue(re.match(pattern, 'DE+2'))
        self.assertIsNone(re.match(pattern, 'DE.1-extra'))
        self.assertIsNone(re.match(pattern, 'XX'))

    def test_preview_refuses_a_report_whose_settings_moved(self):
        """Echoing the report's own revisions back must not make it look current."""
        job = {'status': 'complete', 'job_id': 'j' * 24,
               'settings_revision': 'b' * 64,      # old revision, not the store's
               'source_revision': self.apply.digest(self.config.read_bytes()),
               'counts': {'eligible': 1}, 'nodes': []}
        (self.state / 'vpn.json').write_text(json.dumps(job), encoding='utf-8')
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.preview_apply('vpn', 'j' * 24, 'a' * 64, job['source_revision'])
        self.assertIn('STALE', str(ctx.exception))

    def test_preview_refuses_when_the_live_config_moved(self):
        job = {'status': 'complete', 'job_id': 'k' * 24,
               'settings_revision': 'a' * 64,
               'source_revision': 'f' * 64,        # not the live config digest
               'counts': {'eligible': 1}, 'nodes': []}
        (self.state / 'vpn.json').write_text(json.dumps(job), encoding='utf-8')
        with self.assertRaises(self.apply.ApplyError) as ctx:
            self.apply.preview_apply('vpn', 'k' * 24, 'a' * 64, 'f' * 64)
        self.assertIn('STALE', str(ctx.exception))

    def test_expiry_marks_the_pool_without_widening_it(self):
        """An expired pool must keep its filter: expiry is not permission to widen."""
        data = json.loads(self.store.read_text())
        data['paths']['vpn']['applied'] = {
            'generation': 1, 'status': 'applied', 'path_revision': 'a' * 64,
            'applied_at': '2020-01-01T00:00:00+00:00', 'previous_filter': '',
            'pool_digest': 'd' * 64, 'node_count': 3, 'group': 'VPN',
        }
        self.store.write_text(json.dumps(data))
        widened = []
        with patch.object(self.apply, 'revoke_applied_pool',
                          side_effect=lambda i: widened.append(i)):
            out = self.apply.handle_expired_pools()
        self.assertTrue(out['expired_any'])
        self.assertFalse(out['widened'])
        self.assertEqual(widened, [], 'expiry must not revoke (which widens) the pool')
        after = json.loads(self.store.read_text())['paths']['vpn']['applied']
        self.assertEqual(after['status'], 'expired')

    def test_status_reports_filter_in_force_for_an_expired_pool(self):
        data = json.loads(self.store.read_text())
        data['paths']['vpn']['applied'] = {
            'generation': 1, 'status': 'applied', 'path_revision': 'a' * 64,
            'applied_at': '2020-01-01T00:00:00+00:00', 'node_count': 3,
        }
        self.store.write_text(json.dumps(data))
        out = self.apply.apply_status('vpn')
        self.assertFalse(out['applied'])
        self.assertTrue(out['filter_in_force'])
        self.assertGreater(out['evidence_age_hours'], 24)

    def test_status_reports_a_fresh_pool_as_applied(self):
        from datetime import datetime, timezone
        data = json.loads(self.store.read_text())
        data['paths']['vpn']['applied'] = {
            'generation': 1, 'status': 'applied', 'path_revision': 'a' * 64,
            'applied_at': datetime.now(timezone.utc).isoformat(), 'node_count': 3,
        }
        self.store.write_text(json.dumps(data))
        out = self.apply.apply_status('vpn')
        self.assertTrue(out['applied'])
        self.assertTrue(out['filter_in_force'])

    def test_expiry_is_idempotent(self):
        data = json.loads(self.store.read_text())
        data['paths']['vpn']['applied'] = {
            'generation': 1, 'status': 'expired', 'applied_at': '2020-01-01T00:00:00+00:00',
        }
        self.store.write_text(json.dumps(data))
        out = self.apply.handle_expired_pools()
        self.assertFalse(out['expired_any'])
        self.assertFalse(out['widened'])

    def test_preview_token_changes_when_the_selection_changes(self):
        report = {'job_id': 'j', 'source_revision': 's',
                  'nodes': [{'name': 'A', 'node_fingerprint': 'f1', 'status': 'eligible',
                             'admitted': True, 'countries': ['DE']},
                            {'name': 'B', 'node_fingerprint': 'f2', 'status': 'eligible',
                             'admitted': True, 'countries': ['DE']}]}
        pool = {'eligible': [{'name': 'A', 'node_fingerprint': 'f1'},
                             {'name': 'B', 'node_fingerprint': 'f2'}]}
        first = self.apply._preview_token('vpn', report, pool, ['DE'])
        pool2 = {'eligible': [{'name': 'A', 'node_fingerprint': 'f1'}]}
        second = self.apply._preview_token('vpn', report, pool2, ['DE'])
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
