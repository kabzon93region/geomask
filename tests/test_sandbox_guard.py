"""After-the-fact write detector for six modules. This is NOT a sandbox.

Review 14-15.11.2026, point 1 (P0). Three modules reached real host paths: `test_network_safety`
drove the SMB helper into an absolute `/etc/init.d/<svc> restart`, `test_backend_safety` wrote
the AI-DNS state into `/tmp`, and `test_recovery_wiring` opened a transaction under
`/etc/geomask`. CI hid all three behind `sudo`, and a run on a developer machine meant real
writes into `C:\\...\\etc` under Git-Bash.

What this module does: it runs six named modules with `HOME`/`TMPDIR` inside a temporary
directory and compares a `(mtime_ns, size)` snapshot of a few watched places before and after.
Anything new or touched there is reported with the module name and the path, so "the test
passed" cannot hide an obvious write to the host.

What it does NOT do (R06): it does not intercept, block, namespace, chroot or audit anything;
it sees only six modules of the suite, only regular files, and it misses deletion, `chmod`,
symlink substitution and same-size/same-mtime rewrites. It cannot see the network. A module
that skipped every test still returns 0 from unittest, so the guard now treats "ran 0 tests"
and "skipped every test" as problems as well. A clean guard run means "no change was noticed
in the watched subset", never "the suite is isolated". See tests/BOUNDARIES.md.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def bash_tmp():
    """Where a script's literal `/tmp` lands: Git-Bash's own tmp on Windows, `/tmp` elsewhere.

    The AI-DNS defect wrote exactly there: `/tmp/geomask-aidns.state` from a shell script is
    `C:\\Program Files\\Git\\tmp\\...` on a developer machine, which a `Path('/tmp')` check
    would never see.
    """
    bash = find_bash()
    if bash is None:
        return Path('/tmp')
    try:
        answer = subprocess.run([bash, '-c', 'cd /tmp && pwd -W 2>/dev/null || pwd'],
                                capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return Path('/tmp')
    return Path(answer) if answer else Path('/tmp')


# What the shipped code may touch on a router. They are drive-relative on Windows (`Y:\etc`),
# which is harmless: the directories do not exist and the snapshot stays empty. The literal
# `/tmp` of a shell script is added separately, because on Windows it is Git-Bash's own tmp.
HOST_DIRS = [Path('/etc/geomask'), Path('/etc/config'), Path('/etc/mihomo'),
             Path('/etc/init.d'), Path('/root'), Path('/usr/share/nftables.d/ruleset-post')]
# Temporary directories: only our own names are watched, because the machine's tmp is shared
# with everything else that runs while this test runs.
TMP_ROOTS = [Path('/tmp'), bash_tmp()]
# Inside the temporary directory only our own names matter: everything else belongs to whatever
# else runs on the machine, and a busy /tmp must not make this test flaky.
TMP_GLOBS = ['geomask-*', 'geo-rpc.*']
# Inside the checkout nothing may appear or change either.
REPO_SKIP = {'.git', '__pycache__', 'audit-temp', 'sandbox', 'node_modules', '.dsh'}
REPO_DEPTH = 2

# Modules that used to write out (or that drive helpers which did). Each one is fast enough to
# run here; the slow suite-wide modules stay in CI's own per-module loop. Six of the 72 modules
# at HEAD: this list is a detector target, not a claim of suite-wide coverage.
MODULES = ['test_backend_safety.py', 'test_network_safety.py', 'test_recovery_wiring.py',
           'test_console_diag.py', 'test_console_rules.py', 'test_dnsguard.py']

# A killed module is a timeout, not a pass. The old 900 s was larger than CI's own 180 s, so
# it never fired there; R06 M5 makes the bound explicit and configurable.
MODULE_TIMEOUT = int(os.environ.get('GM_GUARD_MODULE_TIMEOUT', '600'))
_RAN = re.compile(r'^Ran (\d+) tests?', re.M)
_SKIPPED = re.compile(r'skipped=(\d+)')


def snapshot(host_dirs=None, tmp_roots=None, repo_root=None):
    seen = {}
    for root in (host_dirs or []):
        if not root.is_dir():
            continue
        for path in root.rglob('*'):
            _record(seen, path)
    for root in (tmp_roots or []):
        if not root.is_dir():
            continue
        for pattern in TMP_GLOBS:
            for path in root.glob(pattern):
                _record(seen, path)
    if repo_root is not None:
        for path in repo_root.rglob('*'):
            relative = path.relative_to(repo_root)
            if len(relative.parts) > REPO_DEPTH + 1:
                continue
            if REPO_SKIP & set(relative.parts):
                continue
            _record(seen, path)
    return seen


def _record(seen, path):
    try:
        if not path.is_file():
            return
        stat = path.stat()
    except OSError:
        return
    seen[str(path)] = (stat.st_mtime_ns, stat.st_size)


def added(before, after):
    return sorted(path for path in after if path not in before)


def touched(before, after):
    return sorted(path for path, value in after.items()
                  if path in before and before[path] != value)


class SandboxGuardTests(unittest.TestCase):
    def test_the_guard_notices_a_write_outside_the_sandbox(self):
        """The guard itself: a new file and a touched file must both be visible."""
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            (watched / 'quiet.txt').write_text('same', encoding='utf-8')
            before = snapshot([watched], [Path(tmp) / 'spool'], None)
            (watched / 'geomask-aidns.state').write_text('error', encoding='utf-8')
            (watched / 'quiet.txt').write_text('changed', encoding='utf-8')
            after = snapshot([watched], [Path(tmp) / 'spool'], None)
            self.assertIn(str(watched / 'geomask-aidns.state'), added(before, after))
            self.assertIn(str(watched / 'quiet.txt'), touched(before, after))

    def test_no_module_writes_outside_its_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            before = snapshot(HOST_DIRS, TMP_ROOTS, ROOT)
            problems = []
            for module in MODULES:
                environment = dict(os.environ)
                environment.update({
                    'HOME': str(home), 'TMPDIR': str(home),
                    'TEMP': str(home), 'TMP': str(home),
                    'PYTHONDONTWRITEBYTECODE': '1',
                })
                timed_out = False
                try:
                    result = subprocess.run(
                        [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                         '-p', module],
                        cwd=str(ROOT), env=environment, capture_output=True, text=True,
                        timeout=MODULE_TIMEOUT)
                    rc = result.returncode
                    output = (result.stdout or '') + (result.stderr or '')
                except subprocess.TimeoutExpired:
                    timed_out = True
                    rc = 124
                    output = ''
                after = snapshot(HOST_DIRS, TMP_ROOTS, ROOT)
                for path in added(before, after):
                    problems.append('%s created %s' % (module, path))
                for path in touched(before, after):
                    problems.append('%s changed %s' % (module, path))
                before = after
                # A module that fails on its own is its own business; this test is about the
                # filesystem. Report the module name so a stray write and a failing module are
                # not confused with each other. A module that skipped everything or ran zero
                # tests is NOT a pass: unittest returns 0 for both.
                if timed_out:
                    problems.append('%s timed out after %ss' % (module, MODULE_TIMEOUT))
                else:
                    ran_match = _RAN.search(output)
                    skipped_match = _SKIPPED.search(output)
                    ran = int(ran_match.group(1)) if ran_match else -1
                    skipped = int(skipped_match.group(1)) if skipped_match else 0
                    if ran == 0:
                        problems.append('%s ran 0 tests (green module that checked nothing)'
                                        % module)
                    elif ran < 0:
                        problems.append('%s produced no unittest summary (rc=%s)' % (module, rc))
                    elif skipped == ran:
                        problems.append('%s skipped all %d test(s)' % (module, ran))
                    if rc != 0:
                        tail = (output.strip().splitlines()[-1:] or ['no output'])[0]
                        problems.append('%s did not pass rc=%s (%s)' % (module, rc, tail))
            self.assertEqual([], problems, '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()
