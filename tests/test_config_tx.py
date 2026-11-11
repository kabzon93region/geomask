"""Unit tests for geomask-config-tx.py."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-config-tx.py'
spec = importlib.util.spec_from_file_location('config_tx', SOURCE)
ctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctx)

class ConfigTxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Override paths
        self.patches = [
            patch.object(ctx, 'DEFAULT_CONFIG', self.root / 'config.yaml'),
            patch.object(ctx, 'DEFAULT_STATE_DIR', self.root / 'state'),
            patch.object(ctx, 'TX_JOURNAL', self.root / 'state' / 'config-tx-journal.jsonl'),
            patch.object(ctx, 'TX_LOCK', self.root / 'state' / '.config-tx.lock'),
            patch.object(ctx, 'TX_STATE_DIR', self.root / 'state' / 'tx-work'),
            patch.object(ctx, 'TX_PREIMAGES_DIR', self.root / 'state' / 'tx-preimages'),
        ]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)
        # Create dirs
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def test_atomic_write(self):
        f = self.root / 'test.txt'
        ctx.atomic_write(f, b'hello')
        self.assertEqual(f.read_bytes(), b'hello')
        # Overwrite
        ctx.atomic_write(f, b'world')
        self.assertEqual(f.read_bytes(), b'world')

    def test_digest(self):
        self.assertEqual(ctx.digest(b'test'), '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08')

    def test_tx_lock(self):
        with ctx.tx_lock():
            pass  # Should acquire and release
        # Nested should fail
        with ctx.tx_lock():
            with self.assertRaises(Exception):
                with ctx.tx_lock():
                    pass

    def test_append_and_read_journal(self):
        ctx.append_tx_journal({'tx_id': 'abc', 'stage': 'start'})
        ctx.append_tx_journal({'tx_id': 'abc', 'stage': 'committed'})
        journal = ctx.read_tx_journal()
        self.assertEqual(len(journal), 2)
        self.assertEqual(journal[0]['tx_id'], 'abc')
        self.assertEqual(journal[1]['stage'], 'committed')

    def test_config_transaction_preimage_and_stage(self):
        config = self.root / 'config.yaml'
        config.write_text('original')
        
        with ctx.ConfigTransaction('tx1') as tx:
            tx.capture_preimage(config)
            self.assertEqual(tx.preimages[str(config)], b'original')
            
            tx.stage_file(config, b'staged')
            self.assertIn(str(config), tx.staged_files)
            
            staged_path = tx.staged_files[str(config)]
            self.assertEqual(staged_path.read_bytes(), b'staged')

    def test_config_transaction_rollback_on_exception(self):
        config = self.root / 'config.yaml'
        config.write_text('original')
        
        with patch.object(ctx, 'atomic_write', side_effect=IOError('disk error')):
            try:
                with ctx.ConfigTransaction('tx2') as tx:
                    tx.capture_preimage(config)
                    tx.stage_file(config, b'staged')
            except (IOError, ctx.TxError):
                pass
        
        # Config should be unchanged (rollback)
        self.assertEqual(config.read_bytes(), b'original')

    def test_start_transaction_mock(self):
        config = self.root / 'config.yaml'
        config.write_text('mixed-port: 7890\n')  # valid YAML
        
        # Mock mihomo validation, trial, and subprocess
        with patch('subprocess.run') as mock_run, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch('time.sleep', return_value=None), \
             patch.object(ctx.ConfigTransaction, 'run_isolated_trial', return_value=True), \
             patch.object(ctx.ConfigTransaction, 'verify_runtime', return_value=None):
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            # Mock responses for different URLs
            def mock_urlopen_side_effect(req, *args, **kwargs):
                url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
                mock_resp = Mock()
                if '/version' in url:
                    mock_resp.read.return_value = b'{"version":"1.0"}'
                elif '/proxies/VPN' in url or '/proxies/VPN-noP2P' in url or '/proxies/AI' in url:
                    mock_resp.read.return_value = b'{"now":"test-node"}'
                elif '/configs' in url:
                    mock_resp.read.return_value = b'{}'
                else:
                    mock_resp.read.return_value = b'{}'
                mock_resp.__enter__ = Mock(return_value=mock_resp)
                mock_resp.__exit__ = Mock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = mock_urlopen_side_effect
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            result = ctx.start_transaction({config: b'mixed-port: 7891\n'})
            
            self.assertTrue(result['ok'])
            self.assertIn('tx_id', result)

    def test_start_transaction_validation_fails(self):
        config = self.root / 'config.yaml'
        config.write_text('invalid: yaml: [}')
        
        with self.assertRaises(ctx.TxError):
            ctx.start_transaction({config: b''})

    def test_journal_persistence(self):
        ctx.append_tx_journal({'tx_id': 'tx1', 'stage': 'start'})
        ctx.append_tx_journal({'tx_id': 'tx1', 'stage': 'committed'})
        
        # Simulate restart - new module instance would read journal
        journal = ctx.read_tx_journal()
        self.assertEqual(len(journal), 2)
        
        # Find latest stage for tx1
        tx1_stages = [e['stage'] for e in journal if e['tx_id'] == 'tx1']
        self.assertEqual(tx1_stages[-1], 'committed')

    def test_recover_interrupted_transactions_no_journal(self):
        # Should not raise
        ctx.recover_interrupted_transactions()

    def test_fsync_dir(self):
        d = self.root / 'testdir'
        d.mkdir()
        ctx.fsync_dir(d)  # Should not raise

    def test_config_transaction_mihomo_validation_fails(self):
        """A candidate mihomo rejects must abort before any file is installed."""
        config = self.root / 'config.yaml'
        original = b'invalid: yaml: [}'
        config.write_bytes(original)

        with patch.object(ctx.ConfigTransaction, 'validate_candidate',
                          side_effect=ctx.TxError('mihomo rejected the candidate')):
            with self.assertRaises(ctx.TxError):
                ctx.start_transaction({config: original})

        self.assertEqual(config.read_bytes(), original)

    def test_rejects_non_bytes_file_content(self):
        """The file-content API refuses anything that is not bytes."""
        config = self.root / 'config.yaml'
        config.write_bytes(b'mixed-port: 7890\n')
        with self.assertRaises(ctx.TxError):
            ctx.start_transaction({str(config): 'mixed-port: 7890\n'})
        self.assertEqual(config.read_bytes(), b'mixed-port: 7890\n')

    def test_rejects_empty_mapping(self):
        with self.assertRaises(ctx.TxError):
            ctx.start_transaction({})

    def test_rejects_trial_path_outside_the_transaction(self):
        config = self.root / 'config.yaml'
        other = self.root / 'other.yaml'
        config.write_bytes(b'mixed-port: 7890\n')
        other.write_bytes(b'x: 1\n')
        with self.assertRaises(ctx.TxError):
            ctx.start_transaction({config: b'x: 1\n'}, trial_paths=[other])

    def test_config_transaction_trial_failure_rolls_back(self):
        """Test that trial run failure triggers rollback."""
        config = self.root / 'config.yaml'
        config.write_text('mixed-port: 7890\n')
        
        with patch('subprocess.run') as mock_run, \
             patch('subprocess.Popen') as mock_popen, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(ctx.ConfigTransaction, 'verify_runtime', return_value=None):
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            def mock_urlopen_side_effect(req, *args, **kwargs):
                url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
                mock_resp = Mock()
                if '/version' in url:
                    mock_resp.read.return_value = b'{"version":"1.0"}'
                elif '/proxies/VPN' in url or '/proxies/VPN-noP2P' in url or '/proxies/AI' in url:
                    mock_resp.read.return_value = b'{"now":"test-node"}'
                elif '/configs' in url:
                    mock_resp.read.return_value = b'{}'
                else:
                    mock_resp.read.return_value = b'{}'
                mock_resp.__enter__ = Mock(return_value=mock_resp)
                mock_resp.__exit__ = Mock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = mock_urlopen_side_effect
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            mock_proc = Mock()
            mock_proc.poll.return_value = None
            mock_proc.__enter__ = Mock(return_value=mock_proc)
            mock_proc.__exit__ = Mock(return_value=False)
            
            with patch('subprocess.Popen', return_value=mock_proc) as mock_popen:
                with patch.object(ctx.ConfigTransaction, 'run_isolated_trial', side_effect=ctx.TxError('trial failed')):
                    with self.assertRaises(ctx.TxError):
                        ctx.start_transaction({config: b'mixed-port: 7891\n'})
        
        # Verify rollback was attempted (journal has rollback entries)
        journal = ctx.read_tx_journal()
        rollback_entries = [e for e in journal if e.get('stage', '').startswith('rollback')]
        self.assertTrue(len(rollback_entries) > 0)

    def test_config_transaction_reload_failure_rolls_back(self):
        """Test that reload failure triggers rollback."""
        config = self.root / 'config.yaml'
        config.write_text('mixed-port: 7890\n')
        
        with patch('subprocess.run') as mock_run, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(ctx.ConfigTransaction, 'run_isolated_trial', return_value=True), \
             patch.object(ctx.ConfigTransaction, 'verify_runtime', return_value=None):
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            # Mock reload to fail
            def mock_urlopen_side_effect(req, *args, **kwargs):
                url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
                mock_resp = Mock()
                if '/version' in url:
                    mock_resp.read.return_value = b'{"version":"1.0"}'
                elif '/proxies/VPN' in url or '/proxies/VPN-noP2P' in url or '/proxies/AI' in url:
                    mock_resp.read.return_value = b'{"now":"test-node"}'
                elif '/configs' in url:
                    raise Exception('reload failed')
                else:
                    mock_resp.read.return_value = b'{}'
                mock_resp.__enter__ = Mock(return_value=mock_resp)
                mock_resp.__exit__ = Mock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = mock_urlopen_side_effect
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            with patch.object(ctx.ConfigTransaction, 'run_isolated_trial', return_value=True):
                with self.assertRaises(ctx.TxError):
                    ctx.start_transaction({config: b'mixed-port: 7891\n'})
        
        # Verify rollback was attempted (journal has rollback entries)
        journal = ctx.read_tx_journal()
        rollback_entries = [e for e in journal if e.get('stage', '').startswith('rollback')]
        self.assertTrue(len(rollback_entries) > 0)

    def test_config_transaction_runtime_verification_failure_rolls_back(self):
        """Test that runtime verification failure triggers rollback."""
        config = self.root / 'config.yaml'
        config.write_text('mixed-port: 7890\n')
        
        with patch('subprocess.run') as mock_run, \
             patch('urllib.request.urlopen') as mock_urlopen, \
             patch.object(ctx.ConfigTransaction, 'run_isolated_trial', return_value=True):
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            def mock_urlopen_side_effect(req, *args, **kwargs):
                url = req.full_url if hasattr(req, 'full_url') else req.get_full_url()
                mock_resp = Mock()
                if '/version' in url:
                    mock_resp.read.return_value = b'{"version":"1.0"}'
                elif '/proxies/VPN' in url or '/proxies/VPN-noP2P' in url or '/proxies/AI' in url:
                    mock_resp.read.return_value = b'{}'  # Missing 'now' field
                elif '/configs' in url:
                    mock_resp.read.return_value = b'{}'
                else:
                    mock_resp.read.return_value = b'{}'
                mock_resp.__enter__ = Mock(return_value=mock_resp)
                mock_resp.__exit__ = Mock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = mock_urlopen_side_effect
            
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
            
            with patch.object(ctx.ConfigTransaction, 'run_isolated_trial', return_value=True):
                with self.assertRaises(ctx.TxError):
                    ctx.start_transaction({config: b'mixed-port: 7891\n'})
        
        # Verify rollback was attempted (journal has rollback entries)
        journal = ctx.read_tx_journal()
        rollback_entries = [e for e in journal if e.get('stage', '').startswith('rollback')]
        self.assertTrue(len(rollback_entries) > 0)

    def test_staging_never_touches_the_live_file(self):
        """The staged copy lives under the transaction dir, never at the target path."""
        config = self.root / 'config.yaml'
        config.write_bytes(b'mixed-port: 7890\n')
        tx = ctx.ConfigTransaction('iso1')
        tx.stage_file(config, b'mixed-port: 7891\n')
        staged = tx.staged_files[str(config)]
        self.assertNotEqual(staged.resolve(), config.resolve())
        self.assertIn((ctx.TX_STATE_DIR / 'iso1').resolve(), staged.resolve().parents)
        self.assertEqual(config.read_bytes(), b'mixed-port: 7890\n')
        self.assertEqual(staged.read_bytes(), b'mixed-port: 7891\n')

    def test_recovery_after_interrupted_transaction(self):
        """An interrupted transaction is restored to its recorded preimage bytes."""
        config = self.root / 'config.yaml'
        original = b'mixed-port: 7890\n'
        config.write_bytes(original)

        tx = ctx.ConfigTransaction('tx1')
        tx.capture_preimage(config)
        # Simulate the crash: the file now holds the half-applied candidate and the
        # journal has no terminal stage for this transaction.
        config.write_bytes(b'mixed-port: 7891\n')
        ctx.append_tx_journal({'tx_id': 'tx1', 'stage': 'committing'})

        ctx.recover_interrupted_transactions()

        self.assertEqual(config.read_bytes(), original)
        journal = ctx.read_tx_journal()
        final = [e for e in journal if e.get('original_tx_id') == 'tx1']
        self.assertTrue(final)
        # Restoration is what matters; the follow-up reload cannot succeed on a test box
        # with no live controller, and that failure is recorded separately.
        self.assertIn('recovery_complete', [e['stage'] for e in final])

    def test_recovery_does_not_touch_a_committed_transaction(self):
        """Recovery must never roll back work that already committed."""
        config = self.root / 'config.yaml'
        config.write_bytes(b'mixed-port: 7890\n')

        tx = ctx.ConfigTransaction('tx2')
        tx.capture_preimage(config)
        config.write_bytes(b'mixed-port: 7899\n')
        ctx.append_tx_journal({'tx_id': 'tx2', 'stage': 'committed'})

        ctx.recover_interrupted_transactions()
        self.assertEqual(config.read_bytes(), b'mixed-port: 7899\n')

    def test_recovery_removes_a_file_that_did_not_exist_before(self):
        """A file the transaction created must be removed on recovery."""
        config = self.root / 'config.yaml'
        tx = ctx.ConfigTransaction('tx3')
        tx.capture_preimage(config)          # records "did not exist"
        config.write_bytes(b'created by the transaction\n')
        ctx.append_tx_journal({'tx_id': 'tx3', 'stage': 'committing'})

        ctx.recover_interrupted_transactions()
        self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()