#!/usr/bin/env python3
"""Test backup gate: fresh/expired/missing backup, MD5 mismatch."""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'tools'))
import safe_write as sw
import pathlib


class BackupGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = Path(self.temp_dir) / 'geomask-backups'
        self.backup_dir.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('safe_write.Path', side_effect=lambda p: Path(p) if 'geomask-backups' not in str(p) else Path(p))
    def test_backup_gate_missing_dir(self, mock_path):
        with patch('safe_write.Path.exists', return_value=False):
            self.assertFalse(sw.backup_gate())

    def test_backup_gate_no_files(self):
        with patch('safe_write.Path.exists', return_value=True):
            with patch('safe_write.Path.glob', return_value=[]):
                self.assertFalse(sw.backup_gate())

    def test_backup_gate_fresh(self):
        fresh = self.backup_dir / 'router-backup-20261107-120000.tar.gz'
        fresh.write_bytes(b'backup')
        fresh.touch()  # current mtime
        with patch('safe_write.Path') as mock_path_class:
            mock_dir = Mock()
            mock_dir.exists.return_value = True
            
            mock_fresh = Mock()
            mock_fresh.stat.return_value.st_mtime = time.time()
            
            mock_dir.glob.return_value = [mock_fresh]
            mock_path_class.return_value = mock_dir
            self.assertTrue(sw.backup_gate())

    def test_backup_gate_expired(self):
        old = self.backup_dir / 'router-backup-20261107-120000.tar.gz'
        old.write_bytes(b'backup')
        old.touch(time.time() - 400)  # older than 300s
        with patch('safe_write.Path') as mock_path_class:
            mock_dir = Mock()
            mock_dir.exists.return_value = True
            
            mock_old = Mock()
            mock_old.stat.return_value.st_mtime = time.time() - 400
            
            mock_dir.glob.return_value = [mock_old]
            mock_path_class.return_value = mock_dir
            self.assertFalse(sw.backup_gate())

    def test_backup_gate_picks_latest(self):
        old = self.backup_dir / 'router-backup-20261107-100000.tar.gz'
        old.write_bytes(b'old')
        old.touch(time.time() - 500)
        fresh = self.backup_dir / 'router-backup-20261107-120000.tar.gz'
        fresh.write_bytes(b'fresh')
        fresh.touch()
        with patch('safe_write.Path') as mock_path_class:
            # Mock the backup directory Path
            mock_dir = Mock()
            mock_dir.exists.return_value = True
            
            # Mock the backup files with stat()
            mock_old = Mock()
            mock_old.stat.return_value.st_mtime = time.time() - 500
            
            mock_fresh = Mock()
            mock_fresh.stat.return_value.st_mtime = time.time()
            
            mock_dir.glob.return_value = [mock_old, mock_fresh]
            mock_path_class.return_value = mock_dir
            self.assertTrue(sw.backup_gate())


class NodesConfIntegrationTests(unittest.TestCase):
    """Test full nodes.conf apply via safe_write."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.live = Path(self.temp_dir) / 'geomask-nodes.conf'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_cycle_apply(self):
        """Test complete nodes.conf update through render -> validate -> rollback_check."""
        # Initial live content
        initial = b'''enabled=1
schedule=03:00
bad_countries=RU,BY
bad_asns=AS210976
base_filter=Turkey|UK
sub_provider=sub
ai_guard=1
ai_group=AI
ai_egress_url=https://chatgpt.com/cdn-cgi/trace
ai_expect=US
ai_listener=http://127.0.0.1:7891
ai_exclude_base=HY2
name_filter_groups=VPN,VPN-noP2P
sub_user_agent=clash-meta
# comment
custom_field=value
'''
        self.live.write_bytes(initial)

        # Updates to apply
        updates = {
            'enabled': '1',
            'schedule': '04:00',
            'bad_countries': 'RU,BY,CN',
            'bad_asns': 'AS210976,AS67890',
            'base_filter': 'Turkey|UK|CN',
            'sub_provider': 'sub',
        }

        # Validate
        ok, err = sw.nodes_validate_request(updates)
        self.assertTrue(ok, err)

        # Render candidate
        cand = sw.nodes_render_candidate(initial, updates)
        self.assertIn(b'ai_guard=1', cand)
        self.assertIn(b'schedule=04:00', cand)
        self.assertIn(b'bad_countries=RU,BY,CN', cand)
        self.assertIn(b'# comment', cand)
        self.assertIn(b'custom_field=value', cand)

        # Verify rollback check would pass
        self.assertTrue(sw.nodes_rollback_check(initial, cand))

        # Simulate write
        self.live.write_bytes(cand)
        after = self.live.read_bytes()
        self.assertEqual(after, cand)

        # Rollback check against new live
        self.assertTrue(sw.nodes_rollback_check(initial, after))

    def test_ai_guard_zero_preserved_through_cycle(self):
        """Ensure ai_guard=0 survives render and rollback_check."""
        initial = b'''enabled=1
bad_countries=RU
bad_asns=
base_filter=Turkey
sub_provider=sub
ai_guard=0
'''
        updates = {
            'enabled': '1',
            'bad_countries': 'RU',
            'bad_asns': '',
            'base_filter': 'Turkey',
            'sub_provider': 'sub',
        }
        ok, err = sw.nodes_validate_request(updates)
        self.assertTrue(ok, err)

        cand = sw.nodes_render_candidate(initial, updates)
        self.assertIn(b'ai_guard=0', cand)

        self.assertTrue(sw.nodes_rollback_check(initial, cand))


if __name__ == '__main__':
    unittest.main()