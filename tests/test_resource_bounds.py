"""Bounded resources: snapshot budget and the single-file transaction contract."""
import importlib.util
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


state = _load('audit_state_budget', 'geomask-audit-state.py')
tx = _load('config_tx_contract', 'geomask-config-tx.py')


class SnapshotBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_an_exhausted_budget_refuses_instead_of_deleting(self):
        first = state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {'name': 'A'}}}, self.dir)
        self.assertTrue(first.is_file())

        with mock.patch.object(state, 'MAX_SNAPSHOTS_BYTES', first.stat().st_size + 8):
            with self.assertRaises(state.AuditError) as ctx:
                state.save_node_snapshot('vpn', 'job2', {'B': {'definition': {'name': 'B'}}}, self.dir)

        self.assertIn('budget', str(ctx.exception))
        # The snapshot that was already there must survive a refused write.
        self.assertTrue(first.is_file())
        self.assertFalse((self.dir / 'vpn-job2.json').exists())

    def test_a_snapshot_within_budget_is_written(self):
        target = state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {'name': 'A'}}}, self.dir)
        self.assertTrue(target.is_file())

    def test_rewriting_the_same_job_does_not_double_count(self):
        state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {'name': 'A'}}}, self.dir)
        size = (self.dir / 'vpn-job1.json').stat().st_size
        with mock.patch.object(state, 'MAX_SNAPSHOTS_BYTES', size + 16):
            target = state.save_node_snapshot('vpn', 'job1', {'A': {'definition': {'name': 'A'}}}, self.dir)
        self.assertTrue(target.is_file())


class TransactionContractTests(unittest.TestCase):
    def test_more_than_one_file_is_refused(self):
        with self.assertRaises(tx.TxError) as ctx:
            tx.start_transaction({'/etc/a.yaml': b'x', '/etc/b.yaml': b'y'})
        self.assertIn('one file at a time', str(ctx.exception))

    def test_a_relative_path_is_still_refused(self):
        with self.assertRaises(tx.TxError):
            tx.start_transaction({'config.yaml': b'x'})


if __name__ == '__main__':
    unittest.main()
