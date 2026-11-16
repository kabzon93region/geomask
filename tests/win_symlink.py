"""Native symlink support for the reinstall fixture tests.

Git Bash implements `ln -s` as a plain file copy unless `MSYS=winsymlinks:nativestrict` is
set. A copy is not a symlink: `readlink`/`-L` see nothing, so an rc.d snapshot silently
becomes empty and a "restored" autostart link is a regular file. Test results would then
depend on the operator's environment instead of the product - green by default, which is
exactly the kind of hidden dependency an independent check must not have.

This module therefore:
  * gives every bash fixture subprocess an explicit environment with the native mode set;
  * probes once whether the host can really create native symlinks (Python `os.symlink` and
    Git Bash `ln -s` are both exercised), and lets the test modules skip EXPLICITLY with a
    readable reason when it cannot. A checked-in test is never "green by default" on a host
    that cannot represent the POSIX object it is supposed to test.

The probe runs in a throwaway directory and touches no live system path.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_bash():
    for candidate in (r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe', shutil.which('bash')):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def bash_env(extra=None):
    """Environment for every fixture bash run: native symlinks are mandatory, not ambient."""
    env = dict(os.environ)
    env['MSYS'] = 'winsymlinks:nativestrict'
    if extra:
        env.update(extra)
    return env


def _probe():
    if BASH is None:
        return False, 'bash is required for the fixture-based tests'
    workdir = tempfile.mkdtemp(prefix='geomask-symlink-probe-')
    try:
        target = Path(workdir) / 'target'
        target.write_text('x\n', encoding='utf-8')
        python_link = Path(workdir) / 'python-link'
        try:
            os.symlink('target', python_link)
        except (OSError, NotImplementedError) as exc:
            return False, ('native symlinks are unavailable on this host '
                           '(python os.symlink: %s)' % exc)
        if not python_link.is_symlink():
            return False, 'native symlinks are unavailable on this host (os.symlink made a copy)'
        result = subprocess.run(
            [BASH, '-c', 'ln -s target bash-link && [ -L bash-link ]'],
            cwd=workdir, env=bash_env(), capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False, ('native symlinks are unavailable in Git Bash: ln -s creates a copy '
                           'unless MSYS=winsymlinks:nativestrict is effective '
                           '(enable Developer Mode or allow the user to create symlinks)')
        return True, ''
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _probe_exec():
    """Can the host actually EXECUTE a script through a native symlink?

    Windows/Git Bash often creates the link but returns 126 (EACCES) on exec.
    Tests that only need the link for snapshot/restore/pin logic must not depend on
    this; tests that really spawn the symlinked script skip explicitly instead.
    """
    if not NATIVE_SYMLINKS:
        return False, 'native symlinks unavailable: ' + SYMLINK_REASON
    workdir = tempfile.mkdtemp(prefix='geomask-exec-probe-')
    try:
        marker = Path(workdir) / 'marker'
        target = Path(workdir) / 'target.sh'
        target.write_text('#!/bin/sh\nprintf ok > "%s"\n' % marker.as_posix(),
                          encoding='utf-8', newline='\n')
        target.chmod(0o755)
        link = Path(workdir) / 'link.sh'
        try:
            os.symlink(str(target), link)
        except (OSError, NotImplementedError) as exc:
            return False, 'host cannot create an executable native symlink: %s' % exc
        result = subprocess.run([BASH, '-c', 'exec "$1"', 'probe', str(link)],
                                env=bash_env(), capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and marker.exists():
            return True, ''
        return False, ('host cannot execute through a native symlink '
                       '(rc=%s, marker=%s)' % (result.returncode, marker.exists()))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


NATIVE_SYMLINKS, SYMLINK_REASON = _probe()
EXEC_SYMLINK, EXEC_REASON = _probe_exec()

_LAYOUT = None


def _probe_layout():
    """Can this host represent a router-style POSIX symlink layout AND does the
    product snapshot accept it (trusted root, absolute + relative links)?
    """
    if not NATIVE_SYMLINKS:
        return False, 'native symlinks unavailable: ' + SYMLINK_REASON
    try:
        import test_reinstall_phase2b as t
    except Exception as exc:  # pragma: no cover - import guard
        return False, 'cannot import the fixture for the layout probe: %s' % exc
    h = t.Phase2bTest('test_snapshot_records_absent_objects')
    h.setUp()
    try:
        mnt = h.case / 'mnt'
        mnt.mkdir(exist_ok=True)
        marker = mnt / 'layout-exec'
        target = mnt / 'layout-network'
        target.write_text('#!/bin/sh\nprintf ok > "%s"\n' % marker.as_posix(),
                          encoding='utf-8', newline='\n')
        target.chmod(0o755)
        link = h.case / 'etc/init.d/network'
        abs_link = h.case / 'etc/init.d/layout-abs'
        rel_link = h.case / 'etc/init.d/layout-rel'
        for path in (link, abs_link, rel_link):
            if path.exists() or path.is_symlink():
                path.unlink()
        try:
            link.symlink_to(str(target))
            abs_link.symlink_to(str(target))
            rel_link.symlink_to('../../mnt/layout-network')
        except (OSError, NotImplementedError) as exc:
            return False, 'host cannot create the router POSIX symlink layout: %s' % exc
        snap = h.script(h.snap())
        snap_ok = 'SNAP_RC=0' in snap.stdout
        body = ('_rs_link_ok /etc/init.d/layout-abs "%s" file; printf "ABS=%%s\\n" "$?"\n'
                '_rs_link_ok /etc/init.d/layout-rel "%s" file; printf "REL=%%s\\n" "$?"\n'
                % (t.posix(abs_link), t.posix(rel_link)))
        checks = h.script(body)
        abs_ok = 'ABS=0' in checks.stdout
        rel_ok = 'REL=0' in checks.stdout
        proc = subprocess.run([BASH, '-c', 'exec "$1"', 'probe', str(link)],
                              env=bash_env(), capture_output=True, text=True, timeout=60)
        exec_ok = proc.returncode == 0 and marker.exists()
        if snap_ok and abs_ok and rel_ok and exec_ok:
            return True, ''
        details = 'snapshot=%s abs=%s rel=%s exec=%s(rc=%s)' % (
            snap_ok, abs_ok, rel_ok, exec_ok, proc.returncode)
        tail = (snap.stdout + snap.stderr + h.logs())[-500:]
        return False, ('host cannot represent router POSIX symlink layout '
                       '(%s); Linux/router validation required: %s'
                       % (details, tail.replace('\n', ' ')))
    except Exception as exc:  # pragma: no cover - defensive probe
        return False, ('POSIX symlink layout probe failed: %s; '
                       'Linux/router validation required' % exc)
    finally:
        try:
            h.doCleanups()
        except Exception:
            pass


def layout_status():
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = _probe_layout()
    return _LAYOUT

