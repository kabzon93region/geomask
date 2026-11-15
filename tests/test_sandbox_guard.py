"""No test may write outside its own sandbox.

Review 14-15.11.2026, point 1 (P0). Three modules reached real host paths: `test_network_safety`
drove the SMB helper into an absolute `/etc/init.d/<svc> restart`, `test_backend_safety` wrote
the AI-DNS state into `/tmp`, and `test_recovery_wiring` opened a transaction under
`/etc/geomask`. CI hid all three behind `sudo`, and a run on a developer machine meant real
writes into `C:\\...\\etc` under Git-Bash.

This guard does not read the code: it runs the modules with `HOME`/`TMPDIR` inside a temporary
directory and compares a snapshot of the watched places before and after. Anything new or
touched there is reported with the module name and the path, so "the test passed" cannot hide a
write to the host.
"""
import os
from pathlib import Path
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
# run here; the slow suite-wide modules stay in CI's own per-module loop.
MODULES = ['test_backend_safety.py', 'test_network_safety.py', 'test_recovery_wiring.py',
           'test_console_diag.py', 'test_console_rules.py', 'test_dnsguard.py']


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
                result = subprocess.run(
                    [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                     '-p', module],
                    cwd=str(ROOT), env=environment, capture_output=True, text=True,
                    timeout=900)
                after = snapshot(HOST_DIRS, TMP_ROOTS, ROOT)
                for path in added(before, after):
                    problems.append('%s created %s' % (module, path))
                for path in touched(before, after):
                    problems.append('%s changed %s' % (module, path))
                before = after
                # A module that fails on its own is its own business; this test is about the
                # filesystem. Report the module name so a stray write and a failing module are
                # not confused with each other.
                if result.returncode != 0:
                    problems.append('%s did not pass (%s)' % (
                        module, (result.stderr or result.stdout).strip().splitlines()[-1:]))
            self.assertEqual([], problems, '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()
