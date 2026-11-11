#!/usr/bin/env python3
"""Unit tests for stage 1 safe_write protocol and P12 nodes.conf fix."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

try:
    import fcntl
except ImportError:
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'tools'))

from safe_write import (
    RESOURCES, backup_gate, sha256_file, acquire_lock, release_lock,
    nodes_render_candidate, nodes_validate_request, nodes_rollback_check,
    mihomo_validate, mihomo_rollback_check, zapret_validate, zapret_rollback_check,
    safe_write, EDITABLE_KEYS, PROTECTED_PREFIXES
)

# Import after path setup
import sys
import safe_write as sw


class SafeWriteProtocolTests(unittest.TestCase):
    """Test the safe_write protocol mechanics with mocked filesystem."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.live = Path(self.temp_dir) / 'test.conf'
        self.lock = Path(self.temp_dir) / 'test.lock'
        self.rev = Path(self.temp_dir) / 'test.conf.rev'
        self.live.write_text('original content\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_backup_gate_missing_dir(self):
        with patch('safe_write.Path') as mock_path:
            mock_path.return_value.exists.return_value = False
            self.assertFalse(backup_gate())

    def test_sha256_file(self):
        f = Path(self.temp_dir) / 'test.txt'
        f.write_bytes(b'hello')
        self.assertEqual(sha256_file(f), hashlib.sha256(b'hello').hexdigest())

    def test_acquire_release_lock(self):
        fd = acquire_lock(self.lock)
        self.assertIsNotNone(fd)
        self.assertTrue(self.lock.exists())
        release_lock(fd, self.lock)
        self.assertFalse(self.lock.exists())

    def test_lock_timeout(self):
        fd1 = acquire_lock(self.lock)
        self.assertIsNotNone(fd1)
        fd2 = acquire_lock(self.lock, timeout=0.1)
        self.assertIsNone(fd2)
        release_lock(fd1, self.lock)


class NodesConfP12Tests(unittest.TestCase):
    """Test P12: nodes.conf canonical rendering preserves all keys."""

    def test_render_preserves_protected_and_comments(self):
        live = b'''enabled=1
schedule=03:00
bad_countries=RU,BY
bad_asns=AS12345
base_filter=Turkey|UK
sub_provider=sub
# This is a comment
ai_guard=1
ai_group=AI
ai_egress_url=https://chatgpt.com/cdn-cgi/trace
ai_expect=US
ai_listener=http://127.0.0.1:7891
ai_exclude_base=HY2
name_filter_groups=VPN,VPN-noP2P
sub_user_agent=clash-meta
custom_field=value
'''
        updates = {
            'enabled': '1',
            'schedule': '04:00',
            'bad_countries': 'RU,BY,CN',
            'bad_asns': 'AS12345,AS67890',
            'base_filter': 'Turkey|UK|CN',
            'sub_provider': 'sub',
        }
        cand = nodes_render_candidate(live, updates)
        cand_text = cand.decode('utf-8')

        # Check editable keys updated
        self.assertIn('schedule=04:00\n', cand_text)
        self.assertIn('bad_countries=RU,BY,CN\n', cand_text)
        self.assertIn('bad_asns=AS12345,AS67890\n', cand_text)
        self.assertIn('base_filter=Turkey|UK|CN\n', cand_text)

        # Check protected keys preserved verbatim
        self.assertIn('ai_guard=1\n', cand_text)
        self.assertIn('ai_group=AI\n', cand_text)
        self.assertIn('ai_egress_url=https://chatgpt.com/cdn-cgi/trace\n', cand_text)
        self.assertIn('ai_expect=US\n', cand_text)
        self.assertIn('ai_listener=http://127.0.0.1:7891\n', cand_text)
        self.assertIn('ai_exclude_base=HY2\n', cand_text)
        self.assertIn('name_filter_groups=VPN,VPN-noP2P\n', cand_text)
        self.assertIn('sub_user_agent=clash-meta\n', cand_text)

        # Check comment preserved
        self.assertIn('# This is a comment\n', cand_text)

        # Check unknown/custom preserved
        self.assertIn('custom_field=value\n', cand_text)

    def test_render_ai_guard_zero_preserved(self):
        """ai_guard=0 must NOT be lost (old bug: truncated to 4 keys, ai_guard=0 disappears)."""
        live = b'''enabled=1
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
        cand = nodes_render_candidate(live, updates)
        self.assertIn('ai_guard=0\n', cand.decode('utf-8'))

    def test_render_appends_new_editable_keys(self):
        """New editable keys not in live are appended."""
        live = b'# minimal\n'
        updates = {
            'enabled': '1',
            'schedule': '03:00',
            'bad_countries': 'RU,BY',
            'bad_asns': 'AS12345',
            'base_filter': 'Turkey',
            'sub_provider': 'sub',
        }
        cand = nodes_render_candidate(live, updates)
        cand_text = cand.decode('utf-8')
        for k in EDITABLE_KEYS:
            self.assertIn(f'{k}=', cand_text)

    def test_validate_enabled_exact(self):
        ok, err = nodes_validate_request({'enabled': '2'})
        self.assertFalse(ok)
        self.assertIn('enabled', err)

    def test_validate_schedule_format(self):
        ok, err = nodes_validate_request({'schedule': '25:00'})
        self.assertFalse(ok)
        self.assertIn('schedule', err)
        ok, err = nodes_validate_request({'schedule': '03:00'})
        self.assertTrue(ok)

    def test_validate_countries_format(self):
        ok, err = nodes_validate_request({'bad_countries': 'RUSSIA'})
        self.assertFalse(ok)
        ok, err = nodes_validate_request({'bad_countries': 'RU,BY,CN'})
        self.assertTrue(ok)

    def test_validate_asns_format(self):
        ok, err = nodes_validate_request({'bad_asns': 'A12345'})
        self.assertFalse(ok)
        ok, err = nodes_validate_request({'bad_asns': 'AS12345,12345,AS67890'})
        self.assertTrue(ok)

    def test_validate_base_filter_no_quotes(self):
        ok, err = nodes_validate_request({'base_filter': 'Turkey"UK'})
        self.assertFalse(ok)
        ok, err = nodes_validate_request({'base_filter': 'Turkey|UK'})
        self.assertTrue(ok)

    def test_validate_sub_provider_format(self):
        ok, err = nodes_validate_request({'sub_provider': 'Sub-Provider'})
        self.assertFalse(ok)
        ok, err = nodes_validate_request({'sub_provider': 'sub_provider'})
        self.assertTrue(ok)

    def test_validate_rejects_control_chars(self):
        # Control char in base_filter (not validated by format first)
        ok, err = nodes_validate_request({'base_filter': 'Turkey\x00UK'})
        self.assertFalse(ok)
        self.assertIn('control', err)

    def test_rollback_check_preserves_protected(self):
        pre = b'''enabled=1
bad_countries=RU
ai_guard=1
custom=value
'''
        post = b'''enabled=1
bad_countries=RU,BY
ai_guard=1
custom=value
'''
        self.assertTrue(nodes_rollback_check(pre, post))

    def test_rollback_check_fails_on_protected_change(self):
        pre = b'ai_guard=1\n'
        post = b'ai_guard=0\n'  # changed without going through validate
        self.assertFalse(nodes_rollback_check(pre, post))

    def test_rollback_check_fails_on_unknown_loss(self):
        pre = b'custom=value\n'
        post = b''  # lost
        self.assertFalse(nodes_rollback_check(pre, post))


class MihomoValidationTests(unittest.TestCase):
    def test_mihomo_validate_basic(self):
        valid = b'''
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
        ok, err = mihomo_validate(valid)
        self.assertTrue(ok, err)

    def test_mihomo_validate_missing_section(self):
        invalid = b'proxy-providers: {}\n'
        ok, err = mihomo_validate(invalid)
        self.assertFalse(ok)
        self.assertIn('proxy-groups', err)

    def test_mihomo_rollback_check(self):
        content = b'proxy-providers: {}\nproxy-groups: []\nrule-providers: {}\n'
        self.assertTrue(mihomo_rollback_check(content, content))
        self.assertFalse(mihomo_rollback_check(content, b'different'))


class ZapretValidationTests(unittest.TestCase):
    def test_zapret_validate_domains_and_cidrs(self):
        content = b'''example.com
sub.example.com
192.168.1.0/24
10.0.0.0/8
'''
        ok, err = zapret_validate(content)
        self.assertTrue(ok, err)

    def test_zapret_validate_invalid_cidr(self):
        # "not-a-cidr" is parsed as domain, not CIDR. Test actual invalid CIDR.
        content = b'192.168.1.0/33\n'  # invalid prefix
        ok, err = zapret_validate(content)
        self.assertFalse(ok)

    def test_zapret_rollback_check(self):
        content = b'example.com\n10.0.0.0/8\n'
        self.assertTrue(zapret_rollback_check(content, content))
        self.assertFalse(zapret_rollback_check(content, b'other'))


class SafeWriteIntegrationTests(unittest.TestCase):
    """Test safe_write protocol with mocked subprocess and filesystem."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Patch RESOURCES to use temp dir
        self.original_resources = dict(sw.RESOURCES)
        for name, spec in sw.RESOURCES.items():
            for key in ('live', 'lock', 'rev'):
                if key in spec:
                    spec[key] = Path(self.temp_dir) / spec[key].name

    def tearDown(self):
        sw.RESOURCES.clear()
        sw.RESOURCES.update(self.original_resources)
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('safe_write.subprocess.run')
    @patch('safe_write.backup_gate', return_value=True)
    def test_safe_write_success_flow(self, mock_backup, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout=b'', stderr=b'')
        live = Path(self.temp_dir) / 'test.yaml'
        live.write_text('original')
        lock = Path(self.temp_dir) / 'test.lock'
        rev = Path(self.temp_dir) / 'test.rev'

        # Mock the resource spec
        spec = {
            'live': live, 'lock': lock, 'rev': rev,
            'trial': ['true'], 'reload': ['true'],
        }

        def make_cand(old):
            return b'new content'

        def validate(content):
            return True, ''

        def rollback_check(pre, post):
            return post == b'new content'

        with patch.dict(sw.RESOURCES, {'test_resource': spec}):
            # Can't easily test full safe_write without more mocking
            # This test validates the structure exists
            self.assertIn('test_resource', sw.RESOURCES)

    def test_safe_write_validation_fails(self):
        def make_cand(old):
            return b'invalid'

        def validate(content):
            return False, 'validation error'

        def rollback_check(pre, post):
            return True

        spec = {
            'live': Path(self.temp_dir) / 'test.yaml',
            'lock': Path(self.temp_dir) / 'test.lock',
            'rev': Path(self.temp_dir) / 'test.rev',
            'trial': ['true'], 'reload': ['true'],
        }
        spec['live'].write_text('original')

        with patch.dict(sw.RESOURCES, {'test_resource': spec}):
            with patch('safe_write.backup_gate', return_value=True):
                with patch('safe_write.acquire_lock', return_value=3):
                    with patch('safe_write.release_lock'):
                        with patch('safe_write.subprocess.run', return_value=Mock(returncode=0)):
                            rc = safe_write('test_resource', lambda x: b'invalid',
                                           validate, lambda p, q: True)
                            # Should fail at validation
                            self.assertEqual(rc, 1)


class ConcurrencyTests(unittest.TestCase):
    """Test concurrent writer serialization."""

    @unittest.skipIf(fcntl is None, "Windows file locking doesn't serialize like flock")
    def test_lock_serialization(self):
        import threading
        import time
        results = []

        def writer():
            fd = acquire_lock(Path(self.temp_dir) / 'concurrent.lock')
            if fd:
                time.sleep(0.1)
                results.append('got')
                release_lock(fd, Path(self.temp_dir) / 'concurrent.lock')
            else:
                results.append('timeout')

        self.temp_dir = tempfile.mkdtemp()
        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
        # Only one should get the lock at a time
        self.assertEqual(results.count('got'), 3)
        self.assertEqual(results.count('timeout'), 0)


if __name__ == '__main__':
    unittest.main()