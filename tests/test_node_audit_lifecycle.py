"""Offline lifecycle contracts for the actual audit executable (no router or network)."""
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'node_audit_lifecycle', ROOT / 'assets' / 'geomask-node-audit.py')
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        # Keep real atomic replacement on the workspace filesystem, not /etc or /tmp.
        temporary = tempfile.TemporaryDirectory(prefix='node-audit-test-', dir=str(ROOT))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (
                ('STORE', self.root / 'settings.json'),
                ('STATE', self.root / 'state'),
                ('CONFIG', self.root / 'config.yaml')):
            replacement = patch.object(audit, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        audit.STATE.mkdir()
        audit.CONFIG.write_bytes(b'original config')
        self.config = {
            'proxy-groups': [
                {'name': 'VPN', 'use': ['shared']},
                {'name': 'VPN-noP2P', 'use': ['shared']},
                {'name': 'AI', 'use': ['shared']}],
            'proxy-providers': {'shared': {'url': 'https://offline.invalid/private'}}}
        self.nodes = [{'name': 'node-' + str(i), 'server': 'offline.invalid'}
                      for i in range(3)]
        self.lock = self.enter_patch('locked', side_effect=lambda *a, **k: contextlib.nullcontext())
        self.yaml_load = self.enter_patch('yaml_load', side_effect=lambda raw: self.config)
        self.fetch = self.enter_patch('fetch_nodes', return_value=(self.nodes, 'provider-digest'))
        self.probe = Mock()
        self.probe.probe.return_value = {'status': 'ok', 'country': 'PL'}
        self.isolated = self.enter_patch('IsolatedProbe')
        self.isolated.return_value.__enter__.return_value = self.probe
        # Defense in depth: unexpected real process/network operations fail immediately.
        self.enter_patch('free_port', return_value=19001)
        self.network = patch.object(audit.urllib.request, 'urlopen',
                                    side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def enter_patch(self, name, **kwargs):
        replacement = patch.object(audit, name, **kwargs)
        value = replacement.start()
        self.addCleanup(replacement.stop)
        return value

    def stored(self, ident):
        return json.loads((audit.STATE / (ident + '.json')).read_text(encoding='utf-8'))

    def test_shared_provider_checks_keep_path_allow_lists_independent(self):
        audit.atomic(audit.STORE, audit.encode({
            'schema': 1, 'generation': 1, 'paths': {
                'vpn': {'countries_allow': ['DE']},
                'vpn_nop2p': {'countries_allow': ['PL']}}}))
        settings_before = audit.STORE.read_bytes()
        vpn = audit.check('vpn')
        nop2p = audit.check('vpn_nop2p')
        self.assertEqual(vpn['counts'], {'eligible': 0, 'excluded': 3, 'unknown': 0})
        self.assertEqual(nop2p['counts'], {'eligible': 3, 'excluded': 0, 'unknown': 0})
        for ident, report in [('vpn', vpn), ('vpn_nop2p', nop2p)]:
            self.assertEqual(report['status'], 'complete')
            self.assertTrue(report['ok'])
            self.assertFalse(report['stale'])
            self.assertFalse(report['applied'])
            self.assertEqual(report['providers'], ['shared'])
            self.assertEqual(self.stored(ident), report)
            self.assertTrue(all(n['source_digest'] == 'provider-digest' for n in report['nodes']))
        self.assertEqual(audit.STORE.read_bytes(), settings_before)
        self.assertEqual(audit.CONFIG.read_bytes(), b'original config')
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.probe.probe.call_count, 6)

    def test_config_mutation_during_check_marks_report_stale(self):
        def observe(*unused):
            audit.CONFIG.write_bytes(b'changed during observation')
            return {'status': 'ok', 'country': 'DE'}
        self.probe.probe.side_effect = observe
        report = audit.check('vpn')
        self.assertEqual(report['status'], 'stale')
        self.assertTrue(report['stale'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['source_revision'], audit.digest(b'original config'))
        self.assertEqual(self.stored('vpn'), report)
        self.isolated.return_value.__exit__.assert_called_once()

    def test_get_marks_completed_report_stale_after_config_changes(self):
        report = audit.check('vpn')
        self.assertFalse(report['stale'])
        before = (audit.STATE / 'vpn.json').read_bytes()
        audit.CONFIG.write_bytes(b'changed after completion')
        current = next(p for p in audit.get()['paths'] if p['id'] == 'vpn')
        self.assertTrue(current['report']['stale'])
        self.assertFalse(current['applied'])
        self.assertEqual((audit.STATE / 'vpn.json').read_bytes(), before)

    def test_start_validation_error_never_forks_or_queues(self):
        self.yaml_load.side_effect = audit.AuditError('invalid YAML source')
        with patch.object(audit.os, 'fork', create=True) as fork:
            with self.assertRaisesRegex(audit.AuditError, 'invalid YAML source'):
                audit.start('vpn')
        fork.assert_not_called()
        self.assertFalse((audit.STATE / 'vpn.json').exists())
        self.lock.assert_any_call('job-vpn', nonblocking=True)

    def test_check_budget_exhaustion_is_error_and_closes_probe(self):
        clock = iter([0])
        with patch.object(audit.time, 'monotonic', side_effect=lambda: next(clock, 10000)):
            report = audit.check('vpn')
        self.assertEqual(report['status'], 'error')
        self.assertFalse(report['ok'])
        self.assertIn('budget exceeded', report['error'])
        self.assertEqual(report['nodes'], [])
        self.probe.probe.assert_not_called()
        self.isolated.return_value.__exit__.assert_called_once()
        self.assertEqual(self.stored('vpn'), report)

    def test_busy_lock_prevents_fetch_and_report_writes(self):
        self.lock.side_effect = audit.AuditError('job already running')
        with self.assertRaisesRegex(audit.AuditError, 'job already running'):
            audit.check('vpn')
        self.fetch.assert_not_called()
        self.isolated.assert_not_called()
        self.assertFalse((audit.STATE / 'vpn.json').exists())
        self.lock.assert_called_once()
        self.assertTrue(self.lock.call_args.kwargs['nonblocking'])

    def test_worker_owned_lock_is_not_reacquired(self):
        report = audit.check('vpn', owns_lock=True, job_id='reserved-job')
        self.lock.assert_not_called()
        self.assertEqual(report['job_id'], 'reserved-job')
        self.assertEqual(report['status'], 'complete')


class ProbeStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='node-audit-startup-', dir=str(ROOT))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = patch.object(audit, 'STATE', self.root)
        state.start()
        self.addCleanup(state.stop)
        ports = patch.object(audit, 'free_port', return_value=19001)
        ports.start()
        self.addCleanup(ports.stop)
        # The module object is intentionally fake; PyYAML is not a test dependency.
        self.yaml = SimpleNamespace(safe_dump=Mock(return_value='offline config'))
        modules = patch.dict(sys.modules, {'yaml': self.yaml})
        modules.start()
        self.addCleanup(modules.stop)
        # LifecycleTests patches the class only within its own setUp/cleanup.
        self.instance = audit.IsolatedProbe([{'name': 'offline', 'server': 'offline.invalid'}])

    def test_candidate_validation_failure_cleans_up_without_process_or_watchdog(self):
        with patch.object(audit.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
                patch.object(audit.subprocess, 'Popen') as popen, \
                patch.object(audit.os, 'fork', create=True) as fork:
            with self.assertRaisesRegex(audit.AuditError, 'validation failed'):
                self.instance.__enter__()
        popen.assert_not_called()
        fork.assert_not_called()
        self.assertIsNone(self.instance.process)
        self.assertFalse(self.instance.work.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_serialization_failure_cleans_up_workspace_before_process_launch(self):
        # Normal regression assertion, never expectedFailure: failed startup must not leak.
        self.yaml.safe_dump.side_effect = ValueError('serialization failed')
        with patch.object(audit.subprocess, 'run') as run, \
                patch.object(audit.subprocess, 'Popen') as popen, \
                patch.object(audit.os, 'fork', create=True) as fork:
            with self.assertRaisesRegex(ValueError, 'serialization failed'):
                self.instance.__enter__()
        run.assert_not_called()
        popen.assert_not_called()
        fork.assert_not_called()
        self.assertIsNone(self.instance.process)
        self.assertEqual(list(self.root.iterdir()), [],
                         'failed __enter__ must remove its temporary workspace')


if __name__ == '__main__':
    unittest.main()
