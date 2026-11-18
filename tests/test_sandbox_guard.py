"""After-the-fact write detector for six modules. This is NOT a sandbox.

Review 14-15.11.2026, point 1 (P0). Three modules reached real host paths: `test_network_safety`
drove the SMB helper into an absolute `/etc/init.d/<svc> restart`, `test_backend_safety` wrote
the AI-DNS state into `/tmp`, and `test_recovery_wiring` opened a transaction under
`/etc/geomask`. CI hid all three behind `sudo`, and a run on a developer machine meant real
writes into `C:\\...\\etc` under Git-Bash.

What this module does: it runs six named modules with `HOME`/`TMPDIR` inside a temporary
directory and compares a snapshot of a few watched places before and after. The snapshot
carries `(kind, mode, ino, size, mtime_ns, ctime_ns, uid, nlink, link target)` for regular
files, directories and symlinks, plus a SHA-256 of regular files up to 4 MiB. That makes a
deletion, `chmod`, symlink substitution, create+delete in a watched directory (its mtime
changes) and a same-size/same-mtime rewrite visible inside the watched set.

What it does NOT do (R06): it does not intercept, block, namespace, chroot or audit anything;
it sees only six modules of the suite and a short watched list, and it cannot see a
same-size/same-mtime rewrite of a file larger than 4 MiB (metadata only: inode, size, times,
mode), a create+delete in a directory it does not snapshot, or the network. A module that
skipped every test still returns 0 from unittest, so the guard treats "ran 0 tests", "all
tests skipped" and a run that executed nothing as problems as well. A clean guard run means
"no change was noticed in the watched subset", never "the suite is isolated". This is a
detector, not a sandbox. See tests/BOUNDARIES.md.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Full-content hash catches a same-size/same-mtime rewrite. The cap keeps a snapshot from
# reading an archive or a log twice per module; larger files are covered by inode/mode/size/
# mtime_ns/ctime_ns only, which is stated in the boundary above.
HASH_LIMIT = 4 * 1024 * 1024


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
        _record(seen, root)
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


def _digest(path, st):
    if not stat.S_ISREG(st.st_mode) or st.st_size > HASH_LIMIT:
        return None
    hasher = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            while True:
                chunk = handle.read(262144)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return 'unreadable'
    return hasher.hexdigest()


def _record(seen, path):
    try:
        st = path.lstat()
    except OSError:
        return
    target = None
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = 'unreadable'
    seen[str(path)] = (
        stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_ino, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns, getattr(st, 'st_uid', -1),
        getattr(st, 'st_nlink', -1), target, _digest(path, st),
    )


def added(before, after):
    return sorted(path for path in after if path not in before)


def removed(before, after):
    return sorted(path for path in before if path not in after)


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

    def test_the_guard_notices_a_deleted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            victim = watched / 'quiet.txt'
            victim.write_text('same', encoding='utf-8')
            before = snapshot([watched], [], None)
            victim.unlink()
            after = snapshot([watched], [], None)
            self.assertIn(str(victim), removed(before, after))

    def test_the_guard_notices_chmod(self):
        # Only the mode bits are modelled (Windows ACLs remain B-WIN-ACL); the read-only bit
        # is visible through st_mode on both hosts, so the detector test is cross-platform.
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            victim = watched / 'quiet.txt'
            victim.write_text('same', encoding='utf-8')
            original = stat.S_IMODE(victim.stat().st_mode)
            before = snapshot([watched], [], None)
            os.chmod(victim, 0o444)
            after = snapshot([watched], [], None)
            try:
                self.assertIn(str(victim), touched(before, after))
            finally:
                os.chmod(victim, original)

    def test_the_guard_notices_a_symlink_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            victim = watched / 'quiet.txt'
            victim.write_text('same', encoding='utf-8')
            target = Path(tmp) / 'outside.txt'
            target.write_text('outside', encoding='utf-8')
            before = snapshot([watched], [], None)
            victim.unlink()
            try:
                victim.symlink_to(target)
            except OSError as exc:
                self.skipTest('no-native-symlink: %s' % exc)
            after = snapshot([watched], [], None)
            self.assertIn(str(victim), touched(before, after))

    def test_the_guard_notices_create_and_delete_between_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            before = snapshot([watched], [], None)
            time.sleep(0.02)
            ghost = watched / 'ghost.txt'
            ghost.write_text('here and gone', encoding='utf-8')
            ghost.unlink()
            after = snapshot([watched], [], None)
            self.assertIn(str(watched), touched(before, after),
                          'the watched directory mtime did not record create+delete')

    def test_the_guard_notices_a_same_size_same_mtime_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            watched = Path(tmp) / 'watched'
            watched.mkdir()
            victim = watched / 'quiet.txt'
            victim.write_text('AAAA', encoding='utf-8')
            original = victim.stat()
            before = snapshot([watched], [], None)
            victim.write_text('BBBB', encoding='utf-8')
            os.utime(victim, ns=(original.st_atime_ns, original.st_mtime_ns))
            after = snapshot([watched], [], None)
            self.assertIn(str(victim), touched(before, after),
                          'a same-size/same-mtime rewrite was invisible to the content hash')

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
                cmd = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                       '-p', module]
                try:
                    try:
                        result = subprocess.run(
                            cmd, cwd=str(ROOT), env=environment, capture_output=True, text=True,
                            timeout=MODULE_TIMEOUT)
                    except FileNotFoundError:
                        # Windows CreateProcess fails with WinError 2 now and then under load
                        # (observed once in a full gate run, never standalone). One retry with
                        # an explicit interpreter path; only then is it a real problem.
                        other = shutil.which('python') or shutil.which('python3')
                        if not other:
                            raise
                        cmd[0] = other
                        result = subprocess.run(
                            cmd, cwd=str(ROOT), env=environment, capture_output=True, text=True,
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
                for path in removed(before, after):
                    problems.append('%s deleted %s' % (module, path))
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
