"""Unit tests for scheduler functionality."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-node-audit.py'
spec = importlib.util.spec_from_file_location('node_audit', SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(audit, 'STORE', self.root / 'settings.json'),
            patch.object(audit, 'STATE', self.root / 'state'),
            patch.object(audit, 'CONFIG', self.root / 'config.yaml'),
        ]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches: self.addCleanup(p.stop)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def test_schedule_get_default(self):
        """Test getting default schedule for a path."""
        result = audit.schedule_get('vpn')
        self.assertTrue(result['ok'])
        self.assertFalse(result['schedule']['enabled'])
        self.assertEqual(result['schedule']['time'], '03:30')
        self.assertEqual(result['schedule']['mode'], 'check-only')

    def test_schedule_set_enable(self):
        """Test enabling schedule for a path."""
        result = audit.schedule_set('vpn', 'true', '04:00', 'check-only')
        self.assertTrue(result['ok'])
        self.assertTrue(result['schedule']['enabled'])
        self.assertEqual(result['schedule']['time'], '04:00')
        self.assertEqual(result['schedule']['mode'], 'check-only')
        
        # Verify it's saved
        get_result = audit.schedule_get('vpn')
        self.assertTrue(get_result['schedule']['enabled'])
        self.assertEqual(get_result['schedule']['time'], '04:00')

    def test_schedule_set_disable(self):
        """Test disabling schedule for a path."""
        # First enable
        audit.schedule_set('vpn', 'true', '04:00', 'check-and-apply')
        # Then disable
        result = audit.schedule_set('vpn', 'false', '04:00', 'check-and-apply')
        self.assertTrue(result['ok'])
        self.assertFalse(result['schedule']['enabled'])

    def test_schedule_set_check_and_apply_mode(self):
        """Test setting check-and-apply mode."""
        result = audit.schedule_set('vpn', 'true', '05:30', 'check-and-apply')
        self.assertTrue(result['ok'])
        self.assertEqual(result['schedule']['mode'], 'check-and-apply')

    def test_schedule_set_invalid_enabled(self):
        """Test that invalid enabled value is rejected."""
        with self.assertRaises(audit.AuditError):
            audit.schedule_set('vpn', 'yes', '03:30', 'check-only')

    def test_schedule_set_invalid_time(self):
        """Test that invalid time format is rejected."""
        with self.assertRaises(audit.AuditError):
            audit.schedule_set('vpn', 'true', '25:00', 'check-only')
        with self.assertRaises(audit.AuditError):
            audit.schedule_set('vpn', 'true', '12:60', 'check-only')

    def test_schedule_set_invalid_mode(self):
        """Test that invalid mode is rejected."""
        with self.assertRaises(audit.AuditError):
            audit.schedule_set('vpn', 'true', '03:30', 'invalid-mode')

    def test_schedule_persists_across_restart(self):
        """Test that schedule persists across module reload."""
        audit.schedule_set('vpn', 'true', '05:00', 'check-and-apply')
        
        # Simulate restart by creating new module instance
        import importlib.util
        SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-node-audit.py'
        spec = importlib.util.spec_from_file_location('node_audit2', SOURCE)
        audit2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit2)
        
        # Patch paths for the new instance
        audit2.STORE = audit.STORE
        audit2.STATE = audit.STATE
        audit2.CONFIG = audit.CONFIG
        
        result = audit2.schedule_get('vpn')
        self.assertTrue(result['schedule']['enabled'])
        self.assertEqual(result['schedule']['time'], '05:00')
        self.assertEqual(result['schedule']['mode'], 'check-and-apply')

    def test_schedule_set_invalid_path(self):
        """Test that invalid path is rejected."""
        with self.assertRaises(audit.AuditError):
            audit.schedule_set('invalid', 'true', '03:30', 'check-only')

    def test_schedule_independent_per_path(self):
        """Test that schedules are independent per path."""
        audit.schedule_set('vpn', 'true', '04:00', 'check-only')
        audit.schedule_set('vpn_nop2p', 'true', '05:00', 'check-and-apply')
        audit.schedule_set('ai', 'false', '03:30', 'check-only')
        
        vpn_sched = audit.schedule_get('vpn')['schedule']
        nop2p_sched = audit.schedule_get('vpn_nop2p')['schedule']
        ai_sched = audit.schedule_get('ai')['schedule']
        
        self.assertTrue(vpn_sched['enabled'])
        self.assertEqual(vpn_sched['time'], '04:00')
        self.assertEqual(vpn_sched['mode'], 'check-only')
        
        self.assertTrue(nop2p_sched['enabled'])
        self.assertEqual(nop2p_sched['time'], '05:00')
        self.assertEqual(nop2p_sched['mode'], 'check-and-apply')
        
        self.assertFalse(ai_sched['enabled'])

    def test_schedule_rpc_validation(self):
        """Test RPC validation for schedule methods."""
        # Valid schedule-get
        result = audit.validate_rpc('nodes_audit_schedule_get', '{"path_id": "vpn"}')
        self.assertTrue(result['ok'])
        
        # Valid schedule-set
        result = audit.validate_rpc('nodes_audit_schedule_set', '{"path_id": "vpn", "enabled": "true", "time": "04:00", "mode": "check-only"}')
        self.assertTrue(result['ok'])
        
        # Invalid enabled
        result = audit.validate_rpc('nodes_audit_schedule_set', '{"path_id": "vpn", "enabled": "yes", "time": "03:30", "mode": "check-only"}')
        self.assertFalse(result['ok'])
        
        # Invalid time
        result = audit.validate_rpc('nodes_audit_schedule_set', '{"path_id": "vpn", "enabled": "true", "time": "25:00", "mode": "check-only"}')
        self.assertFalse(result['ok'])
        
        # Invalid mode
        result = audit.validate_rpc('nodes_audit_schedule_set', '{"path_id": "vpn", "enabled": "true", "time": "03:30", "mode": "invalid"}')
        self.assertFalse(result['ok'])

    def test_schedule_get_nonexistent_path(self):
        """Test getting schedule for nonexistent path."""
        result = audit.schedule_get('invalid')
        self.assertFalse(result['ok'])
        self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()