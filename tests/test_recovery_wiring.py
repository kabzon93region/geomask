"""Recovery must actually run, not just exist.

The recovery routine had unit tests and no caller, so a transaction interrupted mid-commit
stayed interrupted until someone noticed by hand.
"""
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


ctx = _load('config_tx_recovery', 'geomask-config-tx.py')


class RecoveryIsWiredTests(unittest.TestCase):
    def test_start_transaction_recovers_before_writing(self):
        target = Path('/etc/geomask/does-not-matter.yaml')
        calls = []

        with mock.patch.object(ctx, 'recover_interrupted_transactions',
                               side_effect=lambda: calls.append('recovered')):
            try:
                ctx.start_transaction({target: b'x'})
            except ctx.TxError:
                pass   # later stages fail off-router; the order is what is under test

        self.assertEqual(calls, ['recovered'])

    def test_a_failed_recovery_blocks_the_new_transaction(self):
        target = Path('/etc/geomask/does-not-matter.yaml')

        with mock.patch.object(ctx, 'recover_interrupted_transactions',
                               side_effect=RuntimeError('disk gone')):
            with self.assertRaises(ctx.TxError) as caught:
                ctx.start_transaction({target: b'x'})

        self.assertIn('could not be recovered', str(caught.exception))

    def test_multi_file_is_refused_before_any_recovery_side_effect(self):
        with mock.patch.object(ctx, 'recover_interrupted_transactions') as recovery:
            with self.assertRaises(ctx.TxError):
                ctx.start_transaction({'/etc/a.yaml': b'x', '/etc/b.yaml': b'y'})
        recovery.assert_not_called()


class RecoveryBehaviourTests(unittest.TestCase):
    """The routine itself, on a throwaway journal and file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'journal.jsonl'
        self.preimages = self.root / 'preimages'
        self.preimages.mkdir()
        self.config = self.root / 'config.yaml'
        for patcher in (
            mock.patch.object(ctx, 'TX_JOURNAL', self.journal),
            mock.patch.object(ctx, 'TX_PREIMAGES_DIR', self.preimages),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _preimage(self, tx_id, data):
        stored = self.preimages / ('%s-preimage' % tx_id)
        stored.write_bytes(data)
        return stored

    def test_an_interrupted_commit_is_rolled_back(self):
        original = b'mixed-port: 7890\n'
        self.config.write_bytes(b'mixed-port: 7891\n')
        stored = self._preimage('tx1', original)
        ctx.append_tx_journal({'tx_id': 'tx1', 'stage': 'preimage', 'path': str(self.config),
                               'existed': True, 'preimage_file': str(stored),
                               'digest': ctx.digest(original), 'timestamp': ctx.stamp()})
        ctx.append_tx_journal({'tx_id': 'tx1', 'stage': 'committing', 'timestamp': ctx.stamp()})

        with mock.patch.object(ctx.ConfigTransaction, 'reload_mihomo', return_value=None):
            ctx.recover_interrupted_transactions()

        self.assertEqual(self.config.read_bytes(), original)

    def test_a_committed_transaction_is_left_alone(self):
        self.config.write_bytes(b'mixed-port: 7899\n')
        stored = self._preimage('tx2', b'mixed-port: 7890\n')
        ctx.append_tx_journal({'tx_id': 'tx2', 'stage': 'preimage', 'path': str(self.config),
                               'existed': True, 'preimage_file': str(stored),
                               'digest': ctx.digest(b'mixed-port: 7890\n'),
                               'timestamp': ctx.stamp()})
        ctx.append_tx_journal({'tx_id': 'tx2', 'stage': 'committed', 'timestamp': ctx.stamp()})

        ctx.recover_interrupted_transactions()
        self.assertEqual(self.config.read_bytes(), b'mixed-port: 7899\n')

    def test_a_corrupted_preimage_is_refused_not_applied(self):
        self.config.write_bytes(b'candidate\n')
        stored = self._preimage('tx3', b'original\n')
        ctx.append_tx_journal({'tx_id': 'tx3', 'stage': 'preimage', 'path': str(self.config),
                               'existed': True, 'preimage_file': str(stored),
                               'digest': ctx.digest(b'a-different-original\n'),
                               'timestamp': ctx.stamp()})
        ctx.append_tx_journal({'tx_id': 'tx3', 'stage': 'committing', 'timestamp': ctx.stamp()})

        with mock.patch.object(ctx.ConfigTransaction, 'reload_mihomo', return_value=None):
            ctx.recover_interrupted_transactions()

        self.assertEqual(self.config.read_bytes(), b'candidate\n')
        stages = [e['stage'] for e in ctx.read_tx_journal()]
        self.assertIn('recovery_failed', stages)


if __name__ == '__main__':
    unittest.main()
