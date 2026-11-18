#!/usr/bin/env python3
"""Make "the bytes are written, the runtime applied" an observable fact (C04/C07).

Both the MagiTrickle sync and the AI-DNS apply write files and then ask mihomo to reload
them through the controller. The old code treated that reload as a best-effort step: on
failure it printed a line, the identical next run saw "nothing changed" and therefore did
not retry, and the panel read a state file that said everything was fine. The persisted
file was not the applied state, and nothing recorded the difference.

This helper owns that gap:

* a pending marker records that a reload was needed but did not succeed;
* every later identical run retries the reload because of the marker, so the runtime
  eventually catches up without a restart;
* a success clears the marker, so the state is "applied" only after a successful reload;
* the caller gets JSON plus an exit code (0 applied, 1 pending) instead of a log line.

It only talks to the URL it is given and only touches the pending/state files it is given,
which is what makes the behaviour testable off-router.

    geomask-reload-state.py [--url URL] [--method PUT] [--body JSON] [--header 'K: V']
                            [--config FILE] [--timeout 5] [--needed] [--pending FILE]
                            [--state FILE] [--reason TEXT]

--config replaces the body's config path with a `path` field only when --body is not given;
--needed says the caller changed bytes and wants a reload even without a pending marker.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PENDING = '/tmp/geomask-reload.pending'


def read_pending(path):
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError('not a mapping')
        return {'since': str(data.get('since') or ''), 'attempts': int(data.get('attempts') or 0),
                'reason': str(data.get('reason') or '')}
    except FileNotFoundError:
        return None
    except Exception:
        # A damaged marker is still a marker: the runtime was never proven applied, and
        # guessing "no pending" is what left stale runtime behind before.
        return {'since': '', 'attempts': 0, 'reason': 'pending marker damaged'}


def write_pending(path, marker):
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(marker, handle, ensure_ascii=False)
            handle.write('\n')
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def clear_pending(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def reload_runtime(url, method, body, headers, timeout):
    data = body.encode('utf-8') if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        answer.read()
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--url', default='http://127.0.0.1:9090/configs?force=true')
    parser.add_argument('--method', default='PUT')
    parser.add_argument('--body', default='{"path": "/etc/mihomo/config.yaml"}')
    parser.add_argument('--header', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--needed', action='store_true')
    parser.add_argument('--pending', default=DEFAULT_PENDING)
    parser.add_argument('--state', default='')
    parser.add_argument('--reason', default='')
    args = parser.parse_args(argv)

    marker = read_pending(args.pending)
    result = {'attempted': False, 'ok': False, 'pending': bool(marker),
              'attempts': marker['attempts'] if marker else 0, 'reason': args.reason}
    if not args.needed and marker is None:
        # Nothing changed and nothing was left unapplied: there is no reload to make.
        if args.state:
            write_state(args.state, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    headers = {'Content-Type': 'application/json'}
    for raw in args.header:
        if ':' in raw:
            name, value = raw.split(':', 1)
            headers[name.strip()] = value.strip()
    result['attempted'] = True
    result['attempts'] = (marker['attempts'] if marker else 0) + 1
    try:
        reload_runtime(args.url, args.method, args.body, headers, args.timeout)
        result['ok'] = True
        result['pending'] = False
        result['attempts'] = 0
        clear_pending(args.pending)
        rc = 0
    except Exception as exc:
        result['ok'] = False
        result['pending'] = True
        result['error'] = str(exc)[:200]
        write_pending(args.pending, {'since': (marker or {}).get('since') or
                                     time.strftime('%F %T'),
                                     'attempts': result['attempts'],
                                     'reason': args.reason or result['error']})
        rc = 1
    if args.state:
        write_state(args.state, result)
    print(json.dumps(result, ensure_ascii=False))
    return rc


STATE_LOCK = None


def write_state(path, payload):
    """Merge into the caller's JSON state: a reload verdict must not erase its other fields."""
    data = {}
    try:
        with open(path, encoding='utf-8') as handle:
            current = json.load(handle)
        if isinstance(current, dict):
            data = current
    except Exception:
        pass
    data['reload'] = 'ok' if payload['ok'] else ('failed' if payload['attempted'] else None)
    data['pending'] = payload['pending']
    data['attempts'] = payload['attempts']
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write('\n')
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == '__main__':
    sys.exit(main())
