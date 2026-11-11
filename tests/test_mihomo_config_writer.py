#!/usr/bin/env python3
"""Test mihomo config writer validation, trial, and rollback."""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

# Ensure os.chown exists for patching on Windows
if not hasattr(os, 'chown'):
    os.chown = lambda *args, **kwargs: None

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'tools'))
import safe_write as sw


class MihomoConfigWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mihomo_validate_minimal(self):
        content = b'''
proxy-providers:
  sub:
    type: http
    url: "https://example.com/sub"
proxy-groups:
  - name: VPN
rule-providers:
  test:
    type: file
'''
        ok, err = sw.mihomo_validate(content)
        self.assertTrue(ok, err)

    def test_mihomo_validate_missing_section(self):
        content = b'proxy-providers: {}\n'
        ok, err = sw.mihomo_validate(content)
        self.assertFalse(ok)
        self.assertIn('proxy-groups', err)

    def test_mihomo_rollback_check(self):
        content = b'proxy-providers: {}\nproxy-groups: []\nrule-providers: {}\n'
        self.assertTrue(sw.mihomo_rollback_check(content, content))
        self.assertFalse(sw.mihomo_rollback_check(content, b'different'))

    @patch('safe_write.subprocess.run')
    @patch('safe_write.backup_gate', return_value=True)
    @patch('safe_write.acquire_lock', return_value=3)
    @patch('safe_write.release_lock')
    def test_safe_write_mihomo_success(self, mock_release, mock_lock, mock_backup, mock_run):
        """Test successful mihomo config write with mocked trial/reload."""
        # Mock os.chown for Windows compatibility
        with patch.object(os, 'chown', Mock()):
            mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')

            live = Path(self.temp_dir) / 'config.yaml'
            live.write_bytes(b'proxy-providers: {}\nproxy-groups: []\nrule-providers: {}\n')
            lock = Path(self.temp_dir) / 'mihomo_config.lock'
            rev = Path(self.temp_dir) / 'config.yaml.rev'

            spec = sw.RESOURCES['mihomo_config'].copy()
            spec['live'] = live
            spec['lock'] = lock
            spec['rev'] = rev

            def make_cand(old):
                return old.replace(b'proxy-providers: {}', b'proxy-providers:\n  sub:\n    type: http\n    url: "https://test.com"')

            def validate(content):
                return sw.mihomo_validate(content)

            def rollback_check(pre, post):
                return hashlib.sha256(pre).hexdigest() != hashlib.sha256(post).hexdigest()

            with patch.dict(sw.RESOURCES, {'mihomo_config': spec}):
                rc = sw.safe_write('mihomo_config', make_cand, validate, rollback_check)
                # With full mocking, should succeed
                self.assertEqual(rc, 0)

    @patch('safe_write.subprocess.run')
    @patch('safe_write.backup_gate', return_value=True)
    @patch('safe_write.acquire_lock', return_value=3)
    @patch('safe_write.release_lock')
    def test_safe_write_mihomo_trial_fails(self, mock_release, mock_lock, mock_backup, mock_run):
        """Trial failure (mihomo -t) should abort before install."""
        mock_run.return_value = Mock(returncode=1, stderr=b'config test failed')

        live = Path(self.temp_dir) / 'config.yaml'
        live.write_bytes(b'proxy-providers: {}\nproxy-groups: []\nrule-providers: {}\n')
        lock = Path(self.temp_dir) / 'mihomo_config.lock'
        rev = Path(self.temp_dir) / 'config.yaml.rev'

        spec = sw.RESOURCES['mihomo_config'].copy()
        spec['live'] = live
        spec['lock'] = lock
        spec['rev'] = rev

        def make_cand(old):
            return b'invalid yaml'

        def validate(content):
            return False, 'bad'

        def rollback_check(pre, post):
            return True

        with patch.dict(sw.RESOURCES, {'mihomo_config': spec}):
            rc = sw.safe_write('mihomo_config', make_cand, validate, rollback_check)
            self.assertEqual(rc, 1)  # validation failed

    @patch('safe_write.subprocess.run')
    @patch('safe_write.backup_gate', return_value=True)
    @patch('safe_write.acquire_lock', return_value=3)
    @patch('safe_write.release_lock')
    def test_safe_write_mihomo_reload_fails_then_rollback(self, mock_release, mock_lock, mock_backup, mock_run):
        """Reload failure should trigger rollback."""
        # Mock os.chown for Windows compatibility
        with patch.object(os, 'chown', Mock()):
            # First call: trial succeeds
            # Second call: reload fails
            # Third call: rollback reload succeeds
            reload_fail = Mock(returncode=1, stdout=b'', stderr=b'reload failed')
            mock_run.side_effect = [
                Mock(returncode=0, stdout=b'', stderr=b''),   # trial
                reload_fail,   # reload fails
                Mock(returncode=0, stdout=b'', stderr=b''),   # rollback reload
            ]

        live = Path(self.temp_dir) / 'config.yaml'
        live.write_bytes(b'proxy-providers: {}\nproxy-groups: []\nrule-providers: {}\n')
        lock = Path(self.temp_dir) / 'mihomo_config.lock'
        rev = Path(self.temp_dir) / 'config.yaml.rev'

        spec = sw.RESOURCES['mihomo_config'].copy()
        spec['live'] = live
        spec['lock'] = lock
        spec['rev'] = rev

        original = live.read_bytes()

        def make_cand(old):
            return b'proxy-providers:\n  sub:\n    type: http\n    url: "https://test.com"\nproxy-groups: []\nrule-providers: {}\n'

        def validate(content):
            return True, ''

        def rollback_check(pre, post):
            return post == original

        with patch.dict(sw.RESOURCES, {'mihomo_config': spec}):
            rc = sw.safe_write('mihomo_config', make_cand, validate, rollback_check)
            # Should rollback and return 4 (install/reload failed)
            self.assertEqual(rc, 4)
            # Verify live restored
            self.assertEqual(live.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()