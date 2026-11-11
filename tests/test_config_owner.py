"""Unit tests for geomask-config-owner.py."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import threading
import contextlib

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-config-owner.py'
spec = importlib.util.spec_from_file_location('config_owner', SOURCE)
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)

# No-lock context manager for tests
@contextlib.contextmanager
def _test_ownership_lock():
    yield  # No locking in tests

class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Override paths
        self.patches = [
            patch.object(owner._state, 'DEFAULT_STATE_DIR', self.root / 'state'),
            patch.object(owner, 'DEFAULT_STATE_DIR', self.root / 'state'),
            patch.object(owner, 'OWNERSHIP_FILE', self.root / 'state' / 'config-ownership.json'),
            patch.object(owner, 'ownership_lock', lambda: contextlib.nullcontext()),
        ]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def test_claim_and_check(self):
        # Claim a group
        result = owner.claim_group('VPN', 'slot', ['sub1'])
        self.assertTrue(result)
        
        # Check ownership
        owner_name = owner.get_owner('VPN')
        self.assertEqual(owner_name, 'slot')
        
        # Second claim by same owner should succeed
        result2 = owner.claim_group('VPN', 'slot', ['sub1'])
        self.assertTrue(result2)
        
        # Claim by different owner should fail
        result3 = owner.claim_group('VPN', 'scanner', ['sub2'])
        self.assertFalse(result3)

    def test_unregister(self):
        owner.claim_group('VPN', 'slot', ['sub1'])
        owner.unregister_owner('VPN', 'slot')
        self.assertIsNone(owner.get_owner('VPN'))
        
        # Unregister with wrong owner should fail
        owner.claim_group('VPN', 'slot', ['sub1'])
        with self.assertRaises(owner.OwnershipError):
            owner.unregister_owner('VPN', 'scanner')

    def test_can_modify_group(self):
        self.assertTrue(owner.can_modify_group('VPN', 'slot'))  # Unowned
        owner.claim_group('VPN', 'slot', ['sub1'])
        self.assertTrue(owner.can_modify_group('VPN', 'slot'))
        self.assertFalse(owner.can_modify_group('VPN', 'scanner'))

    def test_check_group_ownership_before_write(self):
        owner.claim_group('VPN', 'slot', ['sub1'])
        owner.check_group_ownership_before_write('VPN', 'slot')  # Should not raise
        with self.assertRaises(owner.OwnershipError):
            owner.check_group_ownership_before_write('VPN', 'scanner')

    def test_load_save_ownership(self):
        owner.claim_group('VPN', 'slot', ['sub1'])
        owner.claim_group('VPN-noP2P', 'scanner', ['sub2'])
        
        # Simulate restart by creating new module instance
        import importlib.util
        SPEC = importlib.util.spec_from_file_location('config_owner2', SOURCE)
        owner2 = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(owner2)
        
        # Patch paths for owner2
        owner2._state.DEFAULT_STATE_DIR = owner._state.DEFAULT_STATE_DIR
        owner2.OWNERSHIP_FILE = owner.OWNERSHIP_FILE
        owner2.DEFAULT_STATE_DIR = owner.DEFAULT_STATE_DIR
        
        self.assertEqual(owner2.get_owner('VPN'), 'slot')
        self.assertEqual(owner2.get_owner('VPN-noP2P'), 'scanner')

    def test_sync_ownership_with_config(self):
        config = {
            'proxy-groups': [
                {'name': 'VPN', 'use': ['sub1']},
                {'name': 'VPN-noP2P', 'use': ['sub2']},
                {'name': 'AI', 'use': ['sub3']}
            ]
        }
        owner.sync_ownership_with_config(config)

        data = owner.load_ownership()
        self.assertIn('VPN', data['groups'])
        self.assertIn('VPN-noP2P', data['groups'])
        self.assertIn('AI', data['groups'])
        # All should be 'manual' since they weren't claimed before
        self.assertEqual(data['groups']['VPN']['owner'], 'manual')

    def test_unregister_with_wrong_owner_raises(self):
        owner.claim_group('VPN', 'slot', ['sub1'])
        with self.assertRaises(owner.OwnershipError):
            owner.unregister_owner('VPN', 'scanner')

    def test_get_group_provider_ids(self):
        config = {
            'proxy-groups': [
                {'name': 'VPN', 'use': ['sub1', 'sub2']},
                {'name': 'AI', 'use': ['sub3']}
            ]
        }
        providers = owner.get_group_provider_ids(config, 'VPN')
        self.assertEqual(providers, ['sub1', 'sub2'])
        providers = owner.get_group_provider_ids(config, 'AI')
        self.assertEqual(providers, ['sub3'])
        providers = owner.get_group_provider_ids(config, 'nonexistent')
        self.assertEqual(providers, [])

    def test_claim_transfers_ownership(self):
        owner.claim_group('VPN', 'slot', ['sub1'])
        # Same owner can re-claim with different providers
        result = owner.claim_group('VPN', 'slot', ['sub3'])
        self.assertTrue(result)
        # Different owner cannot claim
        result = owner.claim_group('VPN', 'scanner', ['sub2'])
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()