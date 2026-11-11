"""Journal retention must bound growth without losing an unfinished transaction.

Two failure modes this pins down: the journal growing forever on a small overlay, and a
recovery that reports success while having nothing to restore from.
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


ctx = _load('config_tx_journal', 'geomask-config-tx.py')


class JournalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'journal.jsonl'
        self.preimages = self.root / 'preimages'
        self.preimages.mkdir()
        for patcher in (mock.patch.object(ctx, 'TX_JOURNAL', self.journal),
                        mock.patch.object(ctx, 'TX_PREIMAGES_DIR', self.preimages)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_journal_is_bounded(self):
        for index in range(ctx.MAX_TX_JOURNAL_ENTRIES + 60):
            ctx.append_tx_journal({'tx_id': 'tx%d' % index, 'stage': 'committed'})
        lines = self.journal.read_text(encoding='utf-8').splitlines()
        self.assertLessEqual(len(lines), ctx.MAX_TX_JOURNAL_ENTRIES + 5)
        self.assertLessEqual(self.journal.stat().st_size, ctx.MAX_TX_JOURNAL_BYTES)

    def test_an_unfinished_transaction_is_never_dropped(self):
        ctx.append_tx_journal({'tx_id': 'stuck', 'stage': 'preimage',
                               'path': '/etc/x.yaml', 'existed': False})
        ctx.append_tx_journal({'tx_id': 'stuck', 'stage': 'committing'})
        for index in range(ctx.MAX_TX_JOURNAL_ENTRIES + 40):
            ctx.append_tx_journal({'tx_id': 'done%d' % index, 'stage': 'committed'})
        remaining = [json.loads(line) for line in
                     self.journal.read_text(encoding='utf-8').splitlines()]
        stuck = [entry for entry in remaining if entry.get('tx_id') == 'stuck']
        self.assertEqual([entry['stage'] for entry in stuck], ['preimage', 'committing'])

    def test_a_finished_transaction_may_be_dropped(self):
        ctx.append_tx_journal({'tx_id': 'old', 'stage': 'preimage', 'path': '/etc/x.yaml'})
        ctx.append_tx_journal({'tx_id': 'old', 'stage': 'committed'})
        for index in range(ctx.MAX_TX_JOURNAL_ENTRIES + 20):
            ctx.append_tx_journal({'tx_id': 'done%d' % index, 'stage': 'committed'})
        remaining = [json.loads(line) for line in
                     self.journal.read_text(encoding='utf-8').splitlines()]
        self.assertFalse([entry for entry in remaining if entry.get('tx_id') == 'old'])


class RecoveryHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / 'journal.jsonl'
        self.preimages = self.root / 'preimages'
        self.preimages.mkdir()
        self.config = self.root / 'config.yaml'
        for patcher in (mock.patch.object(ctx, 'TX_JOURNAL', self.journal),
                        mock.patch.object(ctx, 'TX_PREIMAGES_DIR', self.preimages)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_recovery_without_preimages_reports_failure(self):
        self.config.write_bytes(b'candidate\n')
        # A transaction that reached committing but whose preimage record is gone.
        ctx.append_tx_journal({'tx_id': 'tx9', 'stage': 'committing'})

        with mock.patch.object(ctx.ConfigTransaction, 'reload_mihomo', return_value=None):
            ctx.recover_interrupted_transactions()

        stages = [json.loads(line)['stage'] for line in
                  self.journal.read_text(encoding='utf-8').splitlines()]
        self.assertIn('recovery_failed', stages)
        self.assertNotIn('recovery_complete', stages)
        self.assertEqual(self.config.read_bytes(), b'candidate\n')

    def test_recovery_with_a_preimage_still_restores(self):
        original = b'original\n'
        self.config.write_bytes(b'candidate\n')
        stored = self.preimages / 'tx10-preimage'
        stored.write_bytes(original)
        ctx.append_tx_journal({'tx_id': 'tx10', 'stage': 'preimage', 'path': str(self.config),
                               'existed': True, 'preimage_file': str(stored),
                               'digest': ctx.digest(original)})
        ctx.append_tx_journal({'tx_id': 'tx10', 'stage': 'committing'})

        with mock.patch.object(ctx.ConfigTransaction, 'reload_mihomo', return_value=None):
            ctx.recover_interrupted_transactions()

        self.assertEqual(self.config.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
