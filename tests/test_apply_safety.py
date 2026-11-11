"""Safety records around an apply: the in-flight intent, snapshot file names, and identity.

Three gaps this pins down:

* the applied record used to be written only after the transaction committed, so a crash in
  between left a group that might be pinned with no record saying so;
* a job id went straight into a snapshot file name;
* a node fingerprint covered a hand-picked subset of keys, so a changed credential under an
  unchanged name, server and port kept the approved fingerprint.
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


state = _load('audit_state_safety', 'geomask-audit-state.py')
apply = _load('apply_safety', 'geomask-apply.py')


class SnapshotNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_traversal_is_refused_on_write(self):
        for bad in ('../../etc/passwd', 'a/b', '..', 'x' * 100, 'job:1'):
            with self.subTest(job=bad):
                with self.assertRaises(state.AuditError):
                    state.save_node_snapshot('vpn', bad, {'A': {'definition': {}}}, self.dir)

    def test_traversal_is_refused_on_read(self):
        with self.assertRaises(state.AuditError):
            state.load_node_snapshot('vpn', '../../../etc/passwd', self.dir)

    def test_a_normal_job_id_still_works(self):
        target = state.save_node_snapshot('vpn', 'abc123def456', {'A': {'definition': {}}}, self.dir)
        self.assertTrue(target.is_file())
        self.assertEqual(target.name, 'vpn-abc123def456.json')
        self.assertEqual(sorted(state.load_node_snapshot('vpn', 'abc123def456', self.dir)), ['A'])


class FingerprintCoverageTests(unittest.TestCase):
    base = {'name': 'A', 'type': 'vless', 'server': '1.1.1.1', 'port': 443, 'uuid': 'one'}

    def test_same_transport_is_not_enough(self):
        changed = dict(self.base, uuid='two')
        self.assertNotEqual(apply._fingerprint_of(self.base, 'sub'),
                            apply._fingerprint_of(changed, 'sub'))

    def test_provider_is_part_of_the_identity(self):
        self.assertNotEqual(apply._fingerprint_of(self.base, 'sub'),
                            apply._fingerprint_of(self.base, 'slot'))

    def test_same_definition_is_stable(self):
        self.assertEqual(apply._fingerprint_of(dict(self.base), 'sub'),
                         apply._fingerprint_of(dict(self.base), 'sub'))


class ApplyIntentTests(unittest.TestCase):
    """The applying record must exist before the transaction runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reports = self.root / 'reports'
        self.reports.mkdir()
        self.snapshots = self.root / 'snapshots'
        self.pools = self.root / 'pools'
        self.config = self.root / 'config.yaml'
        self.config.write_text(
            'proxy-providers:\n'
            '  sub:\n'
            '    type: http\n'
            '    url: "https://example.com/sub"\n'
            'proxy-groups:\n'
            '  - name: VPN\n'
            '    type: url-test\n'
            '    use:\n'
            '      - sub\n', encoding='utf-8')

        # A plain in-memory store: the point of this test is the order of writes, not the
        # store's own revision bookkeeping.
        self.memory = {'schema': 2, 'generation': 1,
                       'paths': {'vpn': {'path_revision': 'r1'}}}

        def load():
            return json.loads(json.dumps(self.memory)), None

        def save(value):
            self.memory = json.loads(json.dumps(value))

        self.patchers = [
            mock.patch.object(apply, 'DEFAULT_CONFIG', self.config),
            mock.patch.object(apply, 'DEFAULT_STATE_DIR', self.reports),
            mock.patch.object(apply, 'POOL_DIR', self.pools),
            mock.patch.object(apply, 'load_store', side_effect=load),
            mock.patch.object(apply, 'save_store', side_effect=save),
            # apply holds its own copy of the state module, so its snapshot lookup needs
            # the directory passed explicitly rather than patched globally.
            mock.patch.object(apply, 'load_node_snapshot',
                              side_effect=lambda i, j: state.load_node_snapshot(i, j, self.snapshots)),
            mock.patch.object(apply, '_preview_token', return_value='pid'),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        (self.reports / 'vpn.json').write_text(json.dumps({
            'job_id': 'job1', 'status': 'complete', 'stale': False,
            'settings_revision': 'r1',
            'source_revision': apply.digest(self.config.read_bytes()),
            'nodes': [{'name': 'A', 'status': 'eligible',
                       'node_fingerprint': apply._fingerprint_of({'name': 'A'}, 'sub')}],
        }), encoding='utf-8')
        state.save_node_snapshot('vpn', 'job1', {'A': {
            'provider': 'sub',
            'node_fingerprint': apply._fingerprint_of({'name': 'A'}, 'sub'),
            'definition': {'name': 'A'},
        }}, self.snapshots)

    def _record(self):
        return self.memory['paths']['vpn'].get('applied')

    def test_a_refused_transaction_leaves_the_old_record(self):
        previous = {'status': 'revoked', 'generation': 3}
        self.memory['paths']['vpn']['applied'] = dict(previous)

        with mock.patch.object(apply, 'start_transaction', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                apply.apply_preview('vpn', 'pid', 'r1')

        self.assertEqual(self._record(), previous)

    def test_the_record_is_written_before_the_transaction(self):
        seen = {}

        def fake_transaction(files, **kwargs):
            seen['record'] = self._record()
            return {'ok': False}

        with mock.patch.object(apply, 'start_transaction', side_effect=fake_transaction):
            with self.assertRaises(apply.ApplyError):
                apply.apply_preview('vpn', 'pid', 'r1')

        self.assertIsNotNone(seen.get('record'), 'no record existed while the transaction ran')
        self.assertEqual(seen['record'].get('status'), 'applying')

    def test_preview_refuses_a_snapshot_that_apply_would_reject(self):
        # A report whose fingerprint no longer matches its snapshot must fail at preview,
        # not promise a pool that apply will refuse.
        report_path = self.reports / 'vpn.json'
        payload = json.loads(report_path.read_text(encoding='utf-8'))
        payload['nodes'][0]['node_fingerprint'] = 'stale-from-an-older-build'
        report_path.write_text(json.dumps(payload), encoding='utf-8')

        with self.assertRaises(apply.ApplyError) as ctx:
            apply.preview_apply('vpn', 'job1', 'r1', apply.digest(self.config.read_bytes()))
        self.assertIn('новая проверка', str(ctx.exception))

    def test_an_applying_record_never_claims_an_unrestricted_path(self):
        self.memory['paths']['vpn']['applied'] = {'status': 'applying', 'generation': 2}
        result = apply.apply_status('vpn')
        self.assertFalse(result['applied'])
        self.assertTrue(result['filter_in_force'])
        self.assertIn('warning', result['applied_detail'])


if __name__ == '__main__':
    unittest.main()
