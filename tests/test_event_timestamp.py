#!/usr/bin/env python3
"""Test timestamp handling in geomask-audit-state.py"""
import tempfile
import json
import importlib.util
import sys
from pathlib import Path

STATE_MODULE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-audit-state.py'
spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE)
audit_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_state)

append_event = audit_state.append_event
get_events = audit_state.get_events
DEFAULT_EVENTS = audit_state.DEFAULT_EVENTS

def test_timestamp_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        events_file = Path(tmpdir) / 'events.jsonl'
        
        # Test 1: New events get timestamp
        append_event({'path': 'vpn', 'action': 'check', 'result': 'success'}, events=events_file)
        events = get_events('vpn', events=events_file)
        assert len(events) == 1
        assert 'timestamp' in events[0]
        assert events[0]['timestamp'] is not None
        print(f'Test 1 PASSED: New event has timestamp: {events[0]["timestamp"]}')
        
        # Test 2: Legacy event without timestamp gets normalized from event_id
        # Manually write a legacy event (simulating old format)
        legacy_line = json.dumps({
            'event_id': '2026-11-09T14:16:28.149342+03:00-a5962c7b',
            'path': 'vpn',
            'action': 'check',
            'result': 'success'
        }, ensure_ascii=False)
        with open(events_file, 'a', encoding='utf-8') as f:
            f.write(legacy_line + '\n')
        
        events = get_events('vpn', events=events_file)
        assert len(events) == 2
        # The legacy event should have timestamp normalized (it's the most recent in file, so events[0])
        legacy_event = events[0]
        assert 'timestamp' in legacy_event
        assert legacy_event['timestamp'] == '2026-11-09T14:16:28.149342+03:00'
        print(f'Test 2 PASSED: Legacy event normalized: {legacy_event["timestamp"]}')
        
        # Test 3: New events get current timestamp (not preserved from input)
        append_event({'path': 'vpn', 'action': 'apply', 'result': 'success'}, events=events_file)
        events = get_events('vpn', events=events_file)
        assert len(events) == 3
        apply_event = events[0]  # Most recent
        assert 'timestamp' in apply_event
        assert apply_event['timestamp'] is not None
        print(f'Test 3 PASSED: New event gets current timestamp: {apply_event["timestamp"]}')
        
        # Test 4: get_events with path filter
        events = get_events('ai', events=events_file)
        assert len(events) == 0
        print('Test 4 PASSED: Path filter works')
        
        # Test 5: get_events with limit
        events = get_events('vpn', limit=2, events=events_file)
        assert len(events) == 2
        print('Test 5 PASSED: Limit works')
        
        # Test 6: Event with malformed event_id
        malformed_line = json.dumps({
            'event_id': 'not-a-valid-timestamp',
            'path': 'vpn',
            'action': 'check',
            'result': 'success'
        }, ensure_ascii=False)
        with open(events_file, 'a', encoding='utf-8') as f:
            f.write(malformed_line + '\n')
        events = get_events('vpn', events=events_file)
        malformed_event = events[0]
        assert 'timestamp' not in malformed_event  # Should not add invalid timestamp
        print('Test 6 PASSED: Malformed event_id does not add invalid timestamp')
        
        # Test 7: trimming keeps chronological order across paths
        with tempfile.TemporaryDirectory() as tmp2:
            events2 = Path(tmp2) / 'events.jsonl'
            # more than MAX_EVENTS_PER_PATH per path, so trimming actually runs
            for i in range(audit_state.MAX_EVENTS_PER_PATH + 5):
                for path_id in ('vpn', 'ai'):
                    audit_state.append_event({'path': path_id, 'action': 'check',
                                              'result': 'success',
                                              'note': 'g%02d' % i}, events=events2)
            lines = [json.loads(l) for l in events2.read_text(encoding='utf-8').splitlines()]
            stamps = [e['timestamp'] for e in lines]
            assert stamps == sorted(stamps), 'trimming broke chronological order'
            print('Test 7 PASSED: trimming keeps chronological order (%d events)' % len(lines))

        print('\nAll tests PASSED!')

if __name__ == '__main__':
    test_timestamp_handling()