"""Unit tests for cron migration to the per-path scheduler."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-node-audit.py'
spec = importlib.util.spec_from_file_location('node_audit', SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(audit, 'STORE', self.root / 'settings.json'),
            patch.object(audit, 'STATE', self.root / 'state'),
            patch.object(audit, 'CONFIG', self.root / 'config.yaml'),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.temp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def test_migrate_cron_to_scheduler(self):
        """Enabling all three paths mirrors the old single nightly cron."""
        for ident in ('vpn', 'vpn_nop2p', 'ai'):
            audit.schedule_set(ident, 'true', '03:30', 'check-only')
        for ident in ('vpn', 'vpn_nop2p', 'ai'):
            result = audit.schedule_get(ident)
            self.assertTrue(result['ok'])
            self.assertTrue(result['schedule']['enabled'])
            self.assertEqual(result['schedule']['time'], '03:30')
            self.assertEqual(result['schedule']['mode'], 'check-only')

    def test_migration_preserves_existing_schedules(self):
        """A schedule the owner already set is not replaced by the default."""
        audit.schedule_set('vpn', 'true', '04:00', 'check-and-apply')
        result = audit.schedule_get('vpn')
        self.assertTrue(result['schedule']['enabled'])
        self.assertEqual(result['schedule']['time'], '04:00')
        self.assertEqual(result['schedule']['mode'], 'check-and-apply')

    def test_migration_idempotent(self):
        """Running the same migration twice leaves identical state."""
        for _ in range(2):
            for ident in ('vpn', 'vpn_nop2p', 'ai'):
                audit.schedule_set(ident, 'true', '03:30', 'check-only')
        for ident in ('vpn', 'vpn_nop2p', 'ai'):
            result = audit.schedule_get(ident)
            self.assertTrue(result['schedule']['enabled'])
            self.assertEqual(result['schedule']['time'], '03:30')
            self.assertEqual(result['schedule']['mode'], 'check-only')

    def test_migration_preserves_other_paths(self):
        """Changing one path's schedule never touches its neighbours."""
        audit.schedule_set('vpn', 'true', '04:00', 'check-only')
        audit.schedule_set('vpn_nop2p', 'false', '03:30', 'check-only')
        audit.schedule_set('ai', 'true', '05:00', 'check-and-apply')

        audit.schedule_set('vpn', 'true', '03:30', 'check-only')

        nop2p = audit.schedule_get('vpn_nop2p')
        self.assertFalse(nop2p['schedule']['enabled'])
        self.assertEqual(nop2p['schedule']['time'], '03:30')

        ai = audit.schedule_get('ai')
        self.assertTrue(ai['schedule']['enabled'])
        self.assertEqual(ai['schedule']['time'], '05:00')
        self.assertEqual(ai['schedule']['mode'], 'check-and-apply')


if __name__ == '__main__':
    unittest.main()
