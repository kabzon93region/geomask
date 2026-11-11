"""Reports carry their method, and retention never deletes referenced material."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = _load('audit_state_retention', 'geomask-audit-state.py')
audit = _load('node_audit_method', 'geomask-node-audit.py')


class ProbeMethodTests(unittest.TestCase):
    def test_the_probe_version_is_declared(self):
        self.assertIsInstance(audit.PROBE_VERSION, str)
        self.assertTrue(audit.PROBE_VERSION)

    def test_ai_required_and_informational_services_do_not_overlap(self):
        self.assertFalse(set(audit.AI_REQUIRED_SERVICES) & set(audit.AI_INFORMATIONAL_SERVICES))

    def test_google_is_not_in_the_admission_set(self):
        self.assertNotIn('google', audit.AI_REQUIRED_SERVICES)


class SnapshotRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _make(self, job):
        return state.save_node_snapshot('vpn', job, {'A': {'definition': {'name': 'A'}}}, self.dir)

    def test_old_snapshots_beyond_the_limit_are_dropped(self):
        for index in range(state.MAX_SNAPSHOTS_PER_PATH + 2):
            self._make('job%d' % index)
        kept = sorted(p.name for p in self.dir.glob('vpn-*.json'))
        self.assertLessEqual(len(kept), state.MAX_SNAPSHOTS_PER_PATH)

    def test_a_protected_snapshot_survives_retention(self):
        oldest = self._make('job0')
        for index in range(1, state.MAX_SNAPSHOTS_PER_PATH + 3):
            self._make('job%d' % index)
        self.assertFalse(oldest.exists(), 'the unprotected oldest snapshot should be gone')

        protected = self._make('keepme')
        for index in range(100, 100 + state.MAX_SNAPSHOTS_PER_PATH + 2):
            self._make('job%d' % index)
        self.assertFalse(protected.exists(), 'this one was not protected yet')

        protected = self._make('keepme2')
        for index in range(200, 200 + state.MAX_SNAPSHOTS_PER_PATH + 2):
            state.save_node_snapshot('vpn', 'job%d' % index,
                                     {'A': {'definition': {'name': 'A'}}}, self.dir,
                                     protect=['keepme2'])
        self.assertTrue(protected.exists(), 'a protected snapshot must not be pruned')

    def test_protection_does_not_block_writing(self):
        target = state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {}}}, self.dir,
                                          protect=['job1'])
        self.assertTrue(target.is_file())
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['job_id'], 'job1')

    def test_unknown_protected_ids_are_ignored(self):
        target = state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {}}}, self.dir,
                                          protect=['never-existed'])
        self.assertTrue(target.is_file())


if __name__ == '__main__':
    unittest.main()
