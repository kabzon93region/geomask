#!/usr/bin/env python3
"""Timestamps in geomask-audit-state.py: new events, legacy normalisation, trimming.

This used to be a script with a single `test_*` function and no TestCase, so
`unittest discover` imported it and ran nothing - the seven checks below never executed in
CI. They are real tests now.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

STATE_MODULE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-audit-state.py'
spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE)
audit_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_state)


class EventTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = Path(self.tmp.name) / 'events.jsonl'

    def append_raw(self, payload):
        with open(self.events, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def test_new_event_gets_a_timestamp(self):
        audit_state.append_event({'path': 'vpn', 'action': 'check', 'result': 'success'},
                                 events=self.events)
        events = audit_state.get_events('vpn', events=self.events)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].get('timestamp'))

    def test_legacy_event_is_normalised_from_its_id(self):
        self.append_raw({'event_id': '2026-11-09T14:16:28.149342+03:00-a5962c7b',
                         'path': 'vpn', 'action': 'check', 'result': 'success'})
        events = audit_state.get_events('vpn', events=self.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['timestamp'], '2026-11-09T14:16:28.149342+03:00')

    def test_a_new_event_gets_the_current_time_not_the_input(self):
        audit_state.append_event({'path': 'vpn', 'action': 'check', 'result': 'success',
                                  'timestamp': '2000-01-01T00:00:00+00:00'},
                                 events=self.events)
        events = audit_state.get_events('vpn', events=self.events)
        self.assertNotEqual(events[0]['timestamp'], '2000-01-01T00:00:00+00:00')

    def test_path_filter(self):
        audit_state.append_event({'path': 'vpn', 'action': 'check', 'result': 'success'},
                                 events=self.events)
        self.assertEqual(audit_state.get_events('ai', events=self.events), [])

    def test_limit(self):
        for _ in range(4):
            audit_state.append_event({'path': 'vpn', 'action': 'check', 'result': 'success'},
                                     events=self.events)
        self.assertEqual(len(audit_state.get_events('vpn', limit=2, events=self.events)), 2)

    def test_malformed_event_id_adds_no_timestamp(self):
        self.append_raw({'event_id': 'not-a-valid-timestamp', 'path': 'vpn',
                         'action': 'check', 'result': 'success'})
        events = audit_state.get_events('vpn', events=self.events)
        self.assertNotIn('timestamp', events[0])

    def test_trimming_keeps_chronological_order(self):
        for index in range(audit_state.MAX_EVENTS_PER_PATH + 5):
            for path_id in ('vpn', 'ai'):
                audit_state.append_event({'path': path_id, 'action': 'check',
                                          'result': 'success', 'note': 'g%02d' % index},
                                         events=self.events)
        lines = [json.loads(line) for line in
                 self.events.read_text(encoding='utf-8').splitlines()]
        stamps = [event['timestamp'] for event in lines]
        self.assertEqual(stamps, sorted(stamps), 'trimming broke chronological order')


if __name__ == '__main__':
    unittest.main()
