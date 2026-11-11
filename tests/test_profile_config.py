#!/usr/bin/env python3
"""Tests for profile_config.py and deployer.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from profile_config import (
    DeployConfig, SSHConfig, BackupConfig, InstallProfile,
    PREDEFINED_PROFILES, save_deploy_config, load_deploy_config
)


class ProfileConfigTests(unittest.TestCase):
    def test_predefined_profiles_exist(self):
        self.assertIn('full', PREDEFINED_PROFILES)
        self.assertIn('vpn_only', PREDEFINED_PROFILES)
        self.assertIn('vpn_ai', PREDEFINED_PROFILES)
        self.assertIn('minimal', PREDEFINED_PROFILES)
    
    def test_full_profile_modules(self):
        profile = PREDEFINED_PROFILES['full']
        self.assertIn('core', profile.modules)
        self.assertIn('path_vpn', profile.modules)
        self.assertIn('path_vpn_nop2p', profile.modules)
        self.assertIn('path_ai', profile.modules)
        self.assertEqual(len(profile.modules), 12)
    
    def test_vpn_only_profile(self):
        profile = PREDEFINED_PROFILES['vpn_only']
        self.assertEqual(profile.modules, ['core', 'path_vpn'])
    
    def test_vpn_ai_profile(self):
        profile = PREDEFINED_PROFILES['vpn_ai']
        self.assertIn('path_vpn', profile.modules)
        self.assertIn('path_vpn_nop2p', profile.modules)
        self.assertIn('path_ai', profile.modules)
        self.assertIn('diagnostic', profile.modules)
        self.assertIn('admission_service', profile.modules)
    
    def test_minimal_profile(self):
        profile = PREDEFINED_PROFILES['minimal']
        self.assertEqual(profile.modules, ['core'])
    
    def test_deploy_config_creation(self):
        config = DeployConfig(
            profile='vpn_ai',
            router_address='192.168.1.1',
        )
        self.assertEqual(config.profile, 'vpn_ai')
        self.assertEqual(config.router_address, '192.168.1.1')
        self.assertEqual(config.get_enabled_modules(), 
            ['core', 'path_vpn', 'path_vpn_nop2p', 'path_ai', 'diagnostic', 'admission_service'])
    
    def test_custom_profile(self):
        config = DeployConfig(
            profile='custom',
            custom_modules=['core', 'path_vpn', 'youtube'],
        )
        self.assertEqual(config.profile, 'custom')
        self.assertEqual(config.get_enabled_modules(), ['core', 'path_vpn', 'youtube'])
    
    def test_deploy_config_serialization(self):
        config = DeployConfig(profile='vpn_ai')
        data = config.to_dict()
        self.assertEqual(data['profile'], 'vpn_ai')
        self.assertEqual(data['router_address'], '192.168.0.1')
        
        restored = DeployConfig.from_dict(data)
        self.assertEqual(restored.profile, 'vpn_ai')
        self.assertEqual(restored.router_address, '192.168.0.1')
    
    def test_save_load_config(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            config = DeployConfig(profile='vpn_ai', router_address='10.0.0.1')
            save_deploy_config(config, temp_path)
            
            loaded = load_deploy_config(temp_path)
            self.assertEqual(loaded.profile, 'vpn_ai')
            self.assertEqual(loaded.router_address, '10.0.0.1')
        finally:
            temp_path.unlink()
    
    def test_predefined_profiles_structure(self):
        for name, profile in PREDEFINED_PROFILES.items():
            self.assertIsInstance(profile, InstallProfile)
            self.assertEqual(profile.name, name)
            self.assertIsInstance(profile.modules, list)
            self.assertTrue(len(profile.modules) > 0)
            self.assertIn('core', profile.modules)


class SSHConfigTests(unittest.TestCase):
    def test_ssh_config_defaults(self):
        ssh = SSHConfig()
        self.assertEqual(ssh.host, '192.168.0.1')
        self.assertEqual(ssh.user, 'root')
        self.assertEqual(ssh.key_path, '~/.ssh/openwrt_final')
        self.assertEqual(ssh.port, 22)
    
    def test_ssh_config_custom(self):
        ssh = SSHConfig(host='10.0.0.1', user='admin', key_path='/custom/key', port=2222)
        self.assertEqual(ssh.host, '10.0.0.1')
        self.assertEqual(ssh.user, 'admin')
        self.assertEqual(ssh.key_path, '/custom/key')
        self.assertEqual(ssh.port, 2222)


class BackupConfigTests(unittest.TestCase):
    def test_backup_config_defaults(self):
        backup = BackupConfig()
        self.assertEqual(backup.local_dir, 'Y:/openwrt/backup')
        self.assertEqual(backup.remote_dir, '/tmp')
        self.assertEqual(backup.keep_local, 10)
        self.assertEqual(backup.keep_remote, 1)


if __name__ == '__main__':
    unittest.main()