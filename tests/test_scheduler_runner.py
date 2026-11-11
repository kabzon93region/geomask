"""Unit tests for the per-path audit scheduler (due-time logic, no router)."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

sched = _load('audit_scheduler', 'geomask-audit-scheduler.py')

def at(hour, minute):
    return time.struct_time((2026, 11, 8, hour, minute, 0, 6, 312, 0))

class DueTests(unittest.TestCase):
    def entry(self, enabled=True, hhmm='03:30', mode='check-only', last=None):
        return {'id': 'vpn',
                'schedule': {'enabled': enabled, 'time': hhmm, 'mode': mode},
                '_last_run': last}

    def test_disabled_never_fires(self):
        self.assertFalse(sched.due(self.entry(enabled=False), at(3, 30)))

    def test_not_yet_due(self):
        self.assertFalse(sched.due(self.entry(hhmm='03:30'), at(3, 0)))

    def test_exact_minute_fires(self):
        self.assertTrue(sched.due(self.entry(hhmm='03:30'), at(3, 30)))

    def test_late_within_tolerance_fires(self):
        self.assertTrue(sched.due(self.entry(hhmm='03:30'), at(5, 0)))

    def test_beyond_tolerance_does_not_fire(self):
        self.assertFalse(sched.due(self.entry(hhmm='03:30'), at(12, 0)))

    def test_once_per_day(self):
        self.assertFalse(sched.due(self.entry(hhmm='03:30', last='2026-11-08'), at(4, 0)))

    def test_previous_day_allows_a_new_run(self):
        self.assertTrue(sched.due(self.entry(hhmm='03:30', last='2026-11-07'), at(3, 30)))

    def test_invalid_time_never_fires(self):
        self.assertFalse(sched.due(self.entry(hhmm='oops'), at(3, 30)))
        self.assertFalse(sched.due(self.entry(hhmm='99:99'), at(3, 30)))

    def test_missing_schedule_is_inert(self):
        self.assertFalse(sched.due({'id': 'vpn'}, at(3, 30)))

class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.patches = [
            patch.object(sched, 'STATE', self.state),
            patch.object(sched, 'RUNS', self.state / 'runs.json'),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_check_only_never_applies(self):
        with patch.object(sched.audit, 'start', return_value={'ok': True, 'job_id': 'a' * 24}), \
             patch.object(sched.audit, 'audit_job_get',
                          return_value={'job': {'status': 'complete', 'counts': {'eligible': 3}}}), \
             patch.object(sched.apply, 'apply_preview') as applied:
            out = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-only'}})
        self.assertTrue(out['ok'])
        self.assertFalse(out['applied'])
        applied.assert_not_called()

    def test_check_and_apply_goes_through_preview(self):
        report = {'status': 'complete', 'job_id': 'b' * 24,
                  'settings_revision': 'r' * 64, 'counts': {'eligible': 2}}
        with patch.object(sched.audit, 'start', return_value={'ok': True, 'job_id': 'b' * 24}), \
             patch.object(sched.audit, 'audit_job_get', return_value={'job': report}), \
             patch.object(sched, '_source_revision', return_value='s' * 64), \
             patch.object(sched.apply, 'preview_apply',
                          return_value={'preview_id': 'p' * 64}) as preview, \
             patch.object(sched.apply, 'apply_preview',
                          return_value={'node_count': 2, 'tx_id': 't'}) as applied:
            out = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})
        self.assertTrue(out['ok'])
        self.assertTrue(out['applied'])
        preview.assert_called_once()
        applied.assert_called_once()

    def test_failed_check_is_not_reported_applied(self):
        with patch.object(sched.audit, 'start', return_value={'ok': True, 'job_id': 'c' * 24}), \
             patch.object(sched.audit, 'audit_job_get',
                          return_value={'job': {'status': 'error', 'error': 'probe failed'}}), \
             patch.object(sched.apply, 'apply_preview') as applied:
            out = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})
        self.assertFalse(out['ok'])
        self.assertFalse(out.get('applied'))
        applied.assert_not_called()

    def test_start_rejection_propagates(self):
        with patch.object(sched.audit, 'start', return_value={'ok': False, 'error': 'busy'}):
            out = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-only'}})
        self.assertFalse(out['ok'])
        self.assertEqual(out['error'], 'busy')

    def test_apply_error_is_reported_not_swallowed(self):
        report = {'status': 'complete', 'job_id': 'd' * 24,
                  'settings_revision': 'r' * 64, 'counts': {'eligible': 1}}
        with patch.object(sched.audit, 'start', return_value={'ok': True, 'job_id': 'd' * 24}), \
             patch.object(sched.audit, 'audit_job_get', return_value={'job': report}), \
             patch.object(sched, '_source_revision', return_value='s' * 64), \
             patch.object(sched.apply, 'preview_apply',
                          side_effect=sched.apply.ApplyError('No eligible nodes')):
            out = sched.run_one({'id': 'vpn', 'schedule': {'mode': 'check-and-apply'}})
        self.assertFalse(out['ok'])
        self.assertIn('No eligible nodes', out['error'])

if __name__ == '__main__':
    unittest.main()
