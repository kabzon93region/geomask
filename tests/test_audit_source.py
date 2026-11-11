"""An applied path must stay auditable.

An apply replaces the group's source with a managed file provider. Resolving the audit
source naively then points the next check at our own frozen pool, so the path could never
be checked again and the nightly run would fail on it forever.
"""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

audit = _load('node_audit_source', 'geomask-node-audit.py')

CONFIG = {
    'proxy-providers': {
        'sub2': {'type': 'http', 'url': 'https://example.com/sub'},
        'slot': {'type': 'file', 'path': '/etc/mihomo/slot.yaml'},
        'audit-vpn-g4': {'type': 'file', 'path': '/etc/mihomo/geomask-pools/vpn-g4.yaml'},
    },
    'proxy-groups': [
        {'name': 'VPN', 'type': 'url-test', 'use': ['audit-vpn-g4']},
        {'name': 'VPN-noP2P', 'type': 'url-test', 'use': ['sub2']},
        {'name': 'AI', 'type': 'url-test', 'use': ['slot']},
    ],
}


class AuditSourceTests(unittest.TestCase):
    def test_plain_group_resolves_normally(self):
        _, providers = audit.resolve_audit_source(CONFIG, 'vpn_nop2p')
        self.assertEqual(providers, ['sub2'])

    def test_applied_group_falls_back_to_the_recorded_source(self):
        path_data = {'applied': {'status': 'applied', 'previous_use': ['sub2']}}
        _, providers = audit.resolve_audit_source(CONFIG, 'vpn', path_data)
        self.assertEqual(providers, ['sub2'])

    def test_recorded_source_must_still_exist(self):
        path_data = {'applied': {'status': 'applied', 'previous_use': ['gone']}}
        with self.assertRaises(audit.AuditError) as ctx:
            audit.resolve_audit_source(CONFIG, 'vpn', path_data)
        self.assertIn('original source is not recorded', str(ctx.exception))

    def test_previous_report_is_used_when_the_store_is_silent(self):
        report = {'providers': ['sub2', 'audit-vpn-g4']}
        _, providers = audit.resolve_audit_source(CONFIG, 'vpn', {}, report)
        self.assertEqual(providers, ['sub2'])

    def test_a_dangling_managed_provider_without_history_is_an_error(self):
        with self.assertRaises(audit.AuditError):
            audit.resolve_audit_source(CONFIG, 'vpn', {})

    def test_binding_can_be_pinned_to_the_original_providers(self):
        binding = audit._resolve_source_binding(CONFIG, 'vpn', ['sub2'])
        self.assertEqual(binding['provider_ids'], ['sub2'])
        managed = audit._resolve_source_binding(CONFIG, 'vpn')
        self.assertEqual(managed['provider_ids'], ['audit-vpn-g4'])
        self.assertNotEqual(binding['source_fingerprint'], managed['source_fingerprint'])


if __name__ == '__main__':
    unittest.main()
