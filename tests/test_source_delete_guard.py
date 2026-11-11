"""A subscription a path depends on must not be deleted out from under it.

Deleting a registry entry while a group still uses the provider that reads it would leave
the group pointing at a source that no longer exists.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1] / 'assets'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load('node_audit_source_use', 'geomask-node-audit.py')

CONFIG = {
    'proxy-providers': {
        'sub2': {'type': 'http', 'url': 'https://example.com/a'},
        'sub': {'type': 'http', 'url': 'https://example.com/b'},
        'orphan': {'type': 'http', 'url': 'https://example.com/c'},
    },
    'proxy-groups': [
        {'name': 'VPN', 'use': ['sub2']},
        {'name': 'VPN-noP2P', 'use': ['sub']},
        {'name': 'AI', 'use': ['slot']},
    ],
}


class SourceInUseTests(unittest.TestCase):
    def setUp(self):
        patched = mock.patch.object(audit, 'yaml_load', return_value=CONFIG)
        self.addCleanup(patched.stop)
        patched.start()
        bytes_patch = mock.patch.object(audit.CONFIG.__class__, 'read_bytes', return_value=b'fake')
        bytes_patch.start()
        self.addCleanup(bytes_patch.stop)
        return
        patched.start()
        self.addCleanup(patched.stop)

    def test_a_used_source_is_reported(self):
        result = audit.source_in_use('https://example.com/a')
        self.assertTrue(result['in_use'])
        self.assertEqual(result['providers'], ['sub2'])

    def test_an_unreferenced_source_is_not_blocked(self):
        result = audit.source_in_use('https://example.com/c')
        self.assertFalse(result['in_use'])
        self.assertEqual(result['providers'], [])

    def test_a_url_nobody_declares_is_not_blocked(self):
        self.assertFalse(audit.source_in_use('https://example.com/zzz')['in_use'])

    def test_an_empty_url_is_refused(self):
        with self.assertRaises(audit.AuditError):
            audit.source_in_use('')

    def test_each_used_provider_is_found(self):
        self.assertEqual(audit.source_in_use('https://example.com/b')['providers'], ['sub'])


class BackendGuardTests(unittest.TestCase):
    """The shell guard must exist and must call the check before deleting."""

    def test_the_guard_runs_before_the_delete(self):
        text = (HERE / 'geo-backend.sh').read_text(encoding='utf-8')
        block = text[text.index('cmd_sub_del()'):]
        block = block[:block.index('\n}\n')]
        self.assertIn('source-in-use', block)
        guard = block.index('source-in-use')
        removal = block.index('grep -v "^$ID|"')
        self.assertLess(guard, removal, 'the reference check must run before the delete')

    def test_the_guard_refuses_with_a_reason(self):
        text = (HERE / 'geo-backend.sh').read_text(encoding='utf-8')
        self.assertIn('switch the path to another subscription', text)


if __name__ == '__main__':
    unittest.main()
