"""The scheduled apply must pass the real guards, not a stubbed preview.

The night run takes the same route as the panel: check, then preview, then apply. The
existing scheduler tests replace preview_apply and apply_preview with mocks, so a mismatch
between the preview token the scheduler builds and the one apply expects would only show up
at 03:30. This test drives the real functions with a real report, snapshot and config.
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


sched = _load('audit_scheduler_chain', 'geomask-audit-scheduler.py')
state = sched.audit._state
apply = sched.apply


class ScheduledApplyChainTests(unittest.TestCase):
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
            '  sub2:\n'
            '    type: http\n'
            '    url: "https://example.com/sub"\n'
            'proxy-groups:\n'
            '  - name: VPN\n'
            '    type: url-test\n'
            '    use:\n'
            '      - sub2\n', encoding='utf-8')

        self.memory = {'schema': 2, 'generation': 1,
                       'paths': {'vpn': {'path_revision': 'r1'}}}

        def load():
            return json.loads(json.dumps(self.memory)), None

        def save(value):
            self.memory = json.loads(json.dumps(value))

        def commit(files, **kwargs):
            for target, data in files.items():
                Path(target).write_bytes(data)
            return {'ok': True, 'tx_id': 'tx-night'}

        self.patches = [
            mock.patch.object(sched, 'STATE', self.root),
            mock.patch.object(sched, 'RUNS', self.root / 'runs.json'),
            mock.patch.object(apply, 'DEFAULT_CONFIG', self.config),
            mock.patch.object(apply, 'DEFAULT_STATE_DIR', self.reports),
            mock.patch.object(apply, 'POOL_DIR', self.pools),
            mock.patch.object(apply, 'load_store', side_effect=load),
            mock.patch.object(apply, 'save_store', side_effect=save),
            mock.patch.object(apply, 'load_node_snapshot',
                              side_effect=lambda i, j: state.load_node_snapshot(i, j, self.snapshots)),
            mock.patch.object(apply, 'start_transaction', side_effect=commit),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        fingerprint = apply._fingerprint_of({'name': 'A'}, 'sub2')
        self.report = {
            'job_id': 'n' * 24, 'path_id': 'vpn', 'group': 'VPN', 'providers': ['sub2'],
            'status': 'complete', 'stale': False, 'settings_revision': 'r1',
            'source_revision': apply.digest(self.config.read_bytes()),
            'countries_allow': ['DE'],
            'counts': {'eligible': 1, 'excluded': 0, 'unknown': 0},
            'nodes': [{'name': 'A', 'provider': 'sub2', 'status': 'eligible',
                       'node_fingerprint': fingerprint,
                       'probes': {'cloudflare': {'status': 'ok', 'country': 'DE'}}}],
        }
        # The real check writes this file; the night route reads it back in preview_apply.
        (self.reports / 'vpn.json').write_text(json.dumps(self.report), encoding='utf-8')
        state.save_node_snapshot('vpn', 'n' * 24, {'A': {
            'provider': 'sub2', 'node_fingerprint': fingerprint,
            'definition': {'name': 'A'},
        }}, self.snapshots)

    def test_the_night_run_applies_through_the_real_preview(self):
        with mock.patch.object(sched.audit, 'start',
                               return_value={'ok': True, 'job_id': 'n' * 24}), \
             mock.patch.object(sched.audit, 'audit_job_get', return_value={'job': self.report}), \
             mock.patch.object(sched, '_source_revision',
                               return_value=self.report['source_revision']):
            outcome = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})

        self.assertTrue(outcome.get('ok'), outcome)
        self.assertTrue(outcome.get('applied'), outcome)
        self.assertEqual(outcome.get('node_count'), 1)
        record = self.memory['paths']['vpn']['applied']
        self.assertEqual(record['status'], 'applied')
        self.assertEqual(record['previous_use'], ['sub2'])
        self.assertTrue(record['acting_countries'])

    def test_a_stale_source_revision_stops_the_night_apply(self):
        with mock.patch.object(sched.audit, 'start',
                               return_value={'ok': True, 'job_id': 'n' * 24}), \
             mock.patch.object(sched.audit, 'audit_job_get', return_value={'job': self.report}), \
             mock.patch.object(sched, '_source_revision', return_value='0' * 64):
            outcome = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})

        self.assertFalse(outcome.get('ok'))
        self.assertFalse(outcome.get('applied'))
        self.assertNotIn('applied', self.memory['paths']['vpn'])

    def test_an_empty_pool_is_not_applied_at_night(self):
        empty = dict(self.report, nodes=[],
                     counts={'eligible': 0, 'excluded': 0, 'unknown': 0})
        (self.reports / 'vpn.json').write_text(json.dumps(empty), encoding='utf-8')
        with mock.patch.object(sched.audit, 'start',
                               return_value={'ok': True, 'job_id': 'n' * 24}), \
             mock.patch.object(sched.audit, 'audit_job_get', return_value={'job': empty}), \
             mock.patch.object(sched, '_source_revision',
                               return_value=self.report['source_revision']):
            outcome = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})

        self.assertFalse(outcome.get('ok'))
        self.assertFalse(outcome.get('applied'))


if __name__ == '__main__':
    unittest.main()
