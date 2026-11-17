"""Honest per-module gate for the geomask unittest suite (R06 M1/M4/M5).

This module does not isolate anything. It runs one unittest module per child process,
with its own HOME/TMPDIR, a hard timeout, and it records what actually happened:

  * tests run / passed / failed / skipped (with the reason unittest itself reported)
  * timeout (a module killed by the gate is a timeout, not a silent pass)
  * a module where every test was skipped is NOT "ok" - it is "skip" and needs an
    allowed reason from ALLOWED_SKIPS; an unknown skip reason fails the run
  * a module that ran zero tests fails the run
  * a module that ran fewer tests than tests/expected-counts.json fails the run, and so
    does a whole run in which no test actually executed (every test was skipped)
  * a module named in tests/expected-counts.json but absent from the run fails the run
    (deleting or renaming a module must not look green)

The allowlist is deliberately code, not a free-form log: each reason maps to the
boundary ID documented in tests/BOUNDARIES.md. Adding a new skip reason is a review
decision, not something a test can do silently.

Exit status: 0 ok/allowed skips, 1 failures/timeouts/empty modules/below manifest,
2 unknown skip.

Usage:
  python -B tests/testgate.py run --tests-dir tests --log-dir test-logs [--timeout 600]
  python -B tests/testgate.py run --tests-dir <dir> --log-dir <dir> --timeout 2
  python -B tests/testgate.py run --tests-dir <dir> --log-dir <dir> \
      --expected-counts <path/to/expected-counts.json>
"""

import argparse
import glob
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

# Reason code -> boundary ID in tests/BOUNDARIES.md. Only these may explain a skip.
ALLOWED_SKIPS = {
    'no-bash': 'B-WIN-NOBASH',
    'no-native-symlink': 'B-WIN-NOSYMLINK',
    'no-python': 'B-ENV-PYTHON',
    'no-pyyaml': 'B-ENV-PYYAML',
    'posix-only': 'B-LINUX-POSIX',
    'windows-acl': 'B-WIN-ACL',
    'docs-absent': 'B-DOCS',
    'needs-root': 'B-LINUX-ROOT',
    'router-only': 'B-ROUTER',
    'linux-only': 'B-LINUX',
}

_JSON_MARKER = 'GM_GATE_JSON:'
DEFAULT_EXPECTED = Path(__file__).resolve().with_name('expected-counts.json')


def load_expected(path):
    """Load module -> minimum test count. A missing/invalid manifest is fail-closed."""
    if not path:
        return {}, 'expected-counts.json path is empty'
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except OSError as exc:
        return {}, 'cannot read expected counts %s: %s' % (path, exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, 'expected counts are not valid JSON: %s' % exc
    if not isinstance(data, dict):
        return {}, 'expected counts must be a JSON object'
    expected = {}
    for key, value in data.items():
        if key.startswith('_'):
            continue
        if not isinstance(key, str) or type(value) is not int or value < 1:
            return {}, 'expected count for %r must be a positive integer' % key
        expected[key] = value
    return expected, ''



def classify_reason(reason):
    """Map a unittest skip string to an allowlisted code, or None if unknown."""
    text = ' '.join(str(reason or '').split()).lower()
    if not text:
        return None
    if 'symlink' in text:
        return 'no-native-symlink'
    if 'bash' in text:
        return 'no-bash'
    if 'documentation' in text or 'ownership map' in text or 'docs' in text:
        return 'docs-absent'
    if ('fcntl' in text or 'flock' in text or 'fifo' in text
            or 'posix' in text or 'privacy mode' in text or 'file locking' in text):
        return 'posix-only'
    if 'windows' in text and ('permission' in text or 'acl' in text or 'lock' in text):
        return 'windows-acl'
    if 'root' in text:
        return 'needs-root'
    if 'router' in text:
        return 'router-only'
    if 'linux' in text:
        return 'linux-only'
    if 'yaml' in text:
        return 'no-pyyaml'
    if 'python' in text:
        return 'no-python'
    return None


def _run_one(tests_dir, module):
    """Child process: run exactly one module and print a machine-readable result."""
    tests_dir = os.path.abspath(tests_dir)
    # Running this file puts tests/ on sys.path[0]. That is the right script dir, but for a
    # gate pointed at another directory it can shadow the module under test with the repo's
    # copy (observed as unittest's "incorrectly imported from" ImportError).
    gate_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != gate_dir]
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    started = time.time()
    try:
        loader = unittest.TestLoader()
        suite = loader.discover(tests_dir, pattern=module, top_level_dir=tests_dir)
        stream = io.StringIO()
        runner = unittest.TextTestRunner(stream=stream, verbosity=0)
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stdout, redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            result = runner.run(suite)
        payload = {
            'module': module,
            'testsRun': result.testsRun,
            'failures': [str(test) for test, _ in result.failures],
            'errors': [str(test) for test, _ in result.errors],
            'skipped': [{'test': str(test), 'reason': str(reason)}
                        for test, reason in result.skipped],
            'expectedFailures': [str(test) for test, _ in result.expectedFailures],
            'unexpectedSuccesses': [str(test) for test in result.unexpectedSuccesses],
            'runnerOutput': stream.getvalue() + out.getvalue() + err.getvalue(),
            'duration': round(time.time() - started, 3),
        }
        payload['rc'] = 0 if result.wasSuccessful() and result.testsRun > 0 else 1
    except BaseException as exc:  # discovery/import/runner failure must not pass
        payload = {
            'module': module, 'testsRun': 0, 'failures': [], 'errors': [repr(exc)],
            'skipped': [], 'expectedFailures': [], 'unexpectedSuccesses': [],
            'runnerOutput': '', 'duration': round(time.time() - started, 3),
            'loadError': repr(exc), 'rc': 1,
        }
    print(_JSON_MARKER + json.dumps(payload, ensure_ascii=False))
    return payload['rc']


def _parse_child(stdout_text, stderr_text):
    payload = None
    for line in (stdout_text or '').splitlines():
        if line.startswith(_JSON_MARKER):
            try:
                payload = json.loads(line[len(_JSON_MARKER):])
            except ValueError:
                payload = None
    return payload


def _as_text(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _module_env(log_dir, module, extra_paths):
    stem = module[:-3] if module.endswith('.py') else module
    home = Path(log_dir) / 'tmp' / stem
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        'HOME': str(home), 'TMPDIR': str(home), 'TEMP': str(home), 'TMP': str(home),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
        'GM_TEST_GATE': '1',
    })
    if extra_paths:
        parts = list(extra_paths)
        if env.get('PYTHONPATH'):
            parts.append(env['PYTHONPATH'])
        env['PYTHONPATH'] = os.pathsep.join(parts)
    return env


def run_modules(tests_dir, log_dir, timeout, python=None, expected_path=None):
    tests_dir = os.path.abspath(tests_dir)
    log_dir = os.path.abspath(log_dir)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    expected_path = os.path.abspath(expected_path or str(DEFAULT_EXPECTED))
    expected, problem = load_expected(expected_path)
    if problem:
        return summarize([], tests_dir, log_dir, timeout, expected, expected_path, problem)
    modules = sorted(os.path.basename(p) for p in glob.glob(os.path.join(tests_dir, 'test_*.py')))
    python = python or sys.executable

    results = []
    for module in modules:
        stem = module[:-3]
        log_path = Path(log_dir) / (stem + '.log')
        started = time.time()
        env = _module_env(log_dir, module, [tests_dir])
        cmd = [python, '-B', os.path.abspath(__file__), '_run-one', tests_dir, module]
        status = 'ok'
        raw = ''
        try:
            completed = subprocess.run(
                cmd, cwd=os.path.dirname(tests_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding='utf-8', errors='replace', timeout=timeout)
            raw = (completed.stdout or '') + (completed.stderr or '')
            payload = _parse_child(completed.stdout, completed.stderr)
            elapsed = time.time() - started
            if payload is None:
                status = 'fail'
                payload = {
                    'module': module, 'testsRun': 0, 'failures': [], 'errors': [],
                    'skipped': [], 'rc': completed.returncode,
                    'runnerOutput': raw[-4000:], 'duration': round(elapsed, 3),
                    'loadError': 'child did not report a result (crash before JSON)',
                }
        except subprocess.TimeoutExpired as exc:
            elapsed = time.time() - started
            raw = _as_text(exc.stdout) + _as_text(exc.stderr)
            payload = {
                'module': module, 'testsRun': 0, 'failures': [], 'errors': [],
                'skipped': [], 'rc': 124, 'runnerOutput': raw[-4000:],
                'duration': round(elapsed, 3), 'timeout': True,
            }
            status = 'timeout'

        log_path.write_text(raw, encoding='utf-8', errors='replace')
        reason_codes = []
        unknown_reasons = []
        for item in payload.get('skipped', []):
            code = classify_reason(item.get('reason'))
            if code is None:
                unknown_reasons.append('%s: %s' % (item.get('test'), item.get('reason')))
            else:
                reason_codes.append(code)

        tests_run = int(payload.get('testsRun') or 0)
        failures = len(payload.get('failures') or [])
        errors = len(payload.get('errors') or [])
        skipped = len(payload.get('skipped') or [])
        passed = max(0, tests_run - failures - errors - skipped)
        rc = int(payload.get('rc', 1))

        if status != 'timeout':
            if payload.get('loadError'):
                status = 'fail'
            elif tests_run == 0:
                status = 'fail'
            elif rc != 0:
                status = 'fail'
            elif skipped == tests_run:
                if unknown_reasons:
                    status = 'unknown-skip'
                else:
                    status = 'skip'
            elif unknown_reasons:
                status = 'unknown-skip'
            else:
                status = 'ok'

        results.append({
            'module': module, 'status': status, 'tests': tests_run, 'pass': passed,
            'fail': failures + errors, 'skip': skipped, 'timeout': status == 'timeout',
            'rc': rc, 'duration': round(payload.get('duration', time.time() - started), 3),
            'reasons': sorted(set(reason_codes)), 'unknown_reasons': unknown_reasons,
            'log': str(log_path),
        })

    return summarize(results, tests_dir, log_dir, timeout, expected, expected_path)


def summarize(results, tests_dir, log_dir, timeout, expected=None, expected_path=None,
              problem=None):
    expected = expected or {}
    totals = {
        'modules': len(results),
        'tests': sum(r['tests'] for r in results),
        'pass': sum(r['pass'] for r in results),
        'fail': sum(r['fail'] for r in results),
        'skip': sum(r['skip'] for r in results),
        'timeout': sum(1 for r in results if r['timeout']),
        'unknown_skip': sum(len(r['unknown_reasons']) for r in results),
        'empty': sum(1 for r in results if r['status'] == 'fail' and r['tests'] == 0),
    }
    totals['executed'] = totals['tests'] - totals['skip']
    below_manifest = []
    not_in_manifest = []
    found_modules = {result['module'] for result in results}
    missing_modules = sorted(set(expected) - found_modules)
    for result in results:
        minimum = expected.get(result['module'])
        if minimum is None:
            minimum = 1
            not_in_manifest.append(result['module'])
        result['expected'] = minimum
        result['executed'] = max(0, result['tests'] - result['skip'])
        if result['tests'] < minimum:
            below_manifest.append('%s ran %d test(s), expected at least %d'
                                  % (result['module'], result['tests'], minimum))
    totals['below_manifest'] = len(below_manifest)
    totals['not_in_manifest'] = len(not_in_manifest)
    totals['missing_modules'] = len(missing_modules)
    failed_modules = [r['module'] for r in results
                      if r['status'] in ('fail', 'unknown-skip', 'timeout')]
    failed_modules.extend(item.split(' ', 1)[0] for item in below_manifest)
    failed_modules.extend(missing_modules)
    failed_modules = sorted(set(failed_modules))
    if (problem or totals['modules'] == 0 or totals['tests'] == 0
            or totals['executed'] == 0 or missing_modules):
        rc = 1
    elif any(r['status'] == 'fail' or r['status'] == 'timeout' for r in results):
        rc = 1
    elif below_manifest:
        rc = 1
    elif totals['unknown_skip']:
        rc = 2
    else:
        rc = 0

    lines = []
    lines.append('== GM TEST GATE SUMMARY ==')
    lines.append('tests dir: %s' % tests_dir)
    lines.append('log dir: %s' % log_dir)
    lines.append('per-module timeout: %ss' % timeout)
    preflight = os.environ.get('GM_PREFLIGHT_FILE')
    if preflight and os.path.isfile(preflight):
        lines.append('environment (from run_tests.sh preflight):')
        for line in Path(preflight).read_text(encoding='utf-8', errors='replace').splitlines():
            lines.append('  ' + line)
    lines.append('modules run: %d' % totals['modules'])
    lines.append('tests run: %d' % totals['tests'])
    lines.append('tests actually executed: %d (skipped excluded)' % totals['executed'])
    lines.append('pass: %d' % totals['pass'])
    lines.append('fail: %d' % totals['fail'])
    lines.append('skip: %d' % totals['skip'])
    lines.append('timeout: %d' % totals['timeout'])
    lines.append('unknown skip: %d' % totals['unknown_skip'])
    lines.append('expected counts: %s (%d module(s))'
                 % (expected_path or 'none', len(expected)))
    if problem:
        lines.append('error: %s' % problem)
    if totals['tests'] == 0:
        lines.append('error: no module ran a test')
    elif totals['executed'] == 0:
        lines.append('error: no test actually executed (every test was skipped)')
    if below_manifest:
        lines.append('below manifest: %s' % '; '.join(below_manifest))
    if missing_modules:
        lines.append('missing modules: %s (in expected-counts.json, absent from the run)'
                     % ', '.join(missing_modules))
    if not_in_manifest:
        lines.append('note: not listed in expected-counts.json (minimum 1 enforced): %s'
                     % ', '.join(not_in_manifest))
    lines.append('module results:')
    lines.append('  %-12s %-34s %5s %5s %5s %5s %4s %8s  %s'
                 % ('STATUS', 'MODULE', 'TESTS', 'PASS', 'FAIL', 'SKIP', 'RC', 'TIME', 'REASON'))
    for r in results:
        reason = ','.join(r['reasons'])
        if r['unknown_reasons']:
            reason = (reason + '; ' if reason else '') + 'unknown: ' + '; '.join(r['unknown_reasons'])
        if r['timeout']:
            reason = 'module timeout after %ss' % timeout
        if r['tests'] < r.get('expected', 1):
            reason = ((reason + '; ') if reason else '') + 'below manifest %d' % r['expected']
        if r['status'] == 'fail' and r['tests'] == 0 and not reason:
            reason = 'ran 0 tests / load error'
        lines.append('  %-12s %-34s %5d %5d %5d %5d %4d %7.1fs  %s'
                     % (r['status'], r['module'], r['tests'], r['pass'], r['fail'],
                        r['skip'], r['rc'], r['duration'], reason))
    lines.append('allowed skip reasons: %s' % ', '.join(sorted(ALLOWED_SKIPS)))
    lines.append('failed modules: %s' % (', '.join(failed_modules) if failed_modules else 'none'))
    if totals['modules'] == 0 and not problem:
        lines.append('error: no test_*.py modules found in %s' % tests_dir)
    lines.append('verdict: %s' % ('FAIL' if rc else 'PASS'))
    text = '\n'.join(lines) + '\n'

    Path(log_dir, 'summary.txt').write_text(text, encoding='utf-8')
    Path(log_dir, 'summary.json').write_text(json.dumps({
        'totals': totals, 'results': results, 'rc': rc,
        'allowed_skips': ALLOWED_SKIPS,
        'expected_counts': expected, 'expected_path': expected_path,
        'below_manifest': below_manifest, 'missing_modules': missing_modules,
        'manifest_problem': problem,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(text, end='')
    print('summary: %s' % Path(log_dir, 'summary.txt'))
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser(description='Honest per-module unittest gate')
    sub = parser.add_subparsers(dest='command')
    run = sub.add_parser('run', help='run every test_*.py in a directory')
    run.add_argument('--tests-dir', required=True)
    run.add_argument('--log-dir', required=True)
    run.add_argument('--timeout', type=float, default=600)
    run.add_argument('--python', default=None)
    run.add_argument('--expected-counts', default=str(DEFAULT_EXPECTED),
                     help='module -> minimum test count (default: %(default)s)')
    one = sub.add_parser('_run-one', help=argparse.SUPPRESS)
    one.add_argument('tests_dir')
    one.add_argument('module')
    args = parser.parse_args(argv)

    if args.command == '_run-one':
        return _run_one(args.tests_dir, args.module)
    if args.command == 'run':
        if args.timeout <= 0:
            parser.error('--timeout must be positive')
        return run_modules(args.tests_dir, args.log_dir, args.timeout, args.python,
                           args.expected_counts)
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
