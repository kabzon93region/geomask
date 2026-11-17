"""One lane lock: the watchdog and the kill switch must never interleave.

Review 14-15.11.2026, point 8, and R04: `geomask-guard.sh` read `enabled` once and only then
added the console ip rule. If `off` finished between the read and the add, the rule came back
after the owner switched the lane off.

The lock is the shared kernel lock of assets/geomask-lock.sh (console rank): one persistent
inode, no pid, no reclaim, no `rm -rf`. `on`/`off` take it for the whole operation and wait a
bounded time; the watchdog is a cron/hotplug background run and only TRIES it - a busy lane
means "skip this run", never "wait" and never "work without it". The state is re-read inside
whatever section is entered, so a rule cannot be re-seated on a state that is already gone.

Every script here runs inside one TemporaryDirectory and talks to stubs for `ip`, `logger` and
`conntrack`: no router, no `/etc`, no host `/tmp`.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
LOCKLIB = ROOT / 'assets' / 'geomask-lock.sh'
ROUTES = ROOT / 'assets' / 'geomask-routes.sh'
BACKEND_TEXT = BACKEND.read_text(encoding='utf-8')
GUARD_TEXT = GUARD.read_text(encoding='utf-8')

GUARD_START = '# ---------- (E) console lane watchdog ----------'
GUARD_END = '# ---------- (D) AI-DNS self-heal ----------'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def win(path):
    return str(Path(path).resolve()).replace('\\', '/')


def extract(name):
    """Whole function body from the shipped backend, brace-balanced but heredoc-aware."""
    lines = BACKEND_TEXT.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + '() {'):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    heredoc = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        opener = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", line)
        if opener is not None:
            heredoc = opener.group(1)
            continue
        if line.rstrip() == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def guard_block():
    """The shipped watchdog block verbatim, paths supplied by the environment."""
    start = GUARD_TEXT.index(GUARD_START)
    end = GUARD_TEXT.index(GUARD_END, start)
    return GUARD_TEXT[start:end]


IP_STUB = '''\
ip() {
  case "$1" in
    rule)
      case "$2" in
        show) cat "$CASE/rules" 2>/dev/null ;;
        add)
          printf '%s\\n' "$*" >> "$CASE/ip-adds"
          printf '%s\\n' "$*" >> "$CASE/rules"
          return 0 ;;
        # `while ip rule del ...` stops only on a non-zero rc: "there is no such rule".
        del) return 1 ;;
        *) return 0 ;;
      esac ;;
    route)
      case "$2" in
        show) printf 'default dev Console\\n' ;;
        *) return 0 ;;
      esac ;;
    *) return 0 ;;
  esac
}
'''


def lock_prelude(case):
    return (
        'GM_LOCK_DIR="%s/locks"\n'
        'GM_LOCK_LIB="%s"\n'
        'export GM_LOCK_DIR GM_LOCK_LIB\n'
        '. "$GM_LOCK_LIB"\n'
        % (win(case), win(LOCKLIB))
    )


def reap(proc):
    """Kill and wait: a running child with cwd inside the TemporaryDirectory blocks cleanup."""
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleWatchdogLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        (self.case / 'rules').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'ip-adds').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'logger.log').write_text('', encoding='utf-8', newline='\n')

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def lock_file(self):
        return self.case / 'locks' / 'console.lock'

    def hold_lock(self):
        """A live process owns the lane lock through the shipped helper."""
        holder = self.write('holder.sh', lock_prelude(self.case) + (
            'gm_lock_acquire console 0 || exit 9\n'
            'printf held > "%s/held"\n'
            'sleep 30\n' % win(self.case)))
        proc = subprocess.Popen([BASH, win(holder)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        marker = self.case / 'held'
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(), 'the lock holder never started')
        return proc

    def lock_is_free(self):
        probe = self.write('probe.sh', lock_prelude(self.case) + (
            'gm_lock_acquire console 0; printf "RC=%s\\n" "$?"\n'
            'gm_lock_release_all\n'))
        result = subprocess.run([BASH, win(probe)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        return 'RC=0' in result.stdout

    def hold_route(self):
        """A live sync owns table 102: the guard must not wait for it while holding `console`."""
        holder = self.write('route-holder.sh', lock_prelude(self.case) + (
            'gm_lock_acquire route-102 0 || exit 9\n'
            'printf held > "%s/route-held"\n'
            'sleep 20\n' % win(self.case)))
        proc = subprocess.Popen([BASH, win(holder)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'route-held').exists():
                return proc
            time.sleep(0.05)
        self.fail('the route lock holder never started')

    def test_the_watchdog_does_not_wait_for_the_route_table(self):
        self.hold_route()
        started = time.monotonic()
        self.run_guard()
        self.assertLess(time.monotonic() - started, 10,
                        'the guard held console while waiting for the route lock')
        log = (self.case / 'logger.log').read_text(encoding='utf-8')
        self.assertIn('not synced', log,
                      'the guard did not report the busy console table: ' + log[-200:])

    def run_guard(self, state_text=None):
        if state_text is not None:
            self.state.write_text(state_text, encoding='utf-8', newline='\n')
        # The REAL routes helper, not a /nonexistent stub: the round-2 review found that the stub
        # masked the guard waiting 20 s for a busy route-102 while it already held `console`.
        # The route lock is deliberately held by the test below; the guard must return at once.
        script = self.write('watchdog.sh', (
            'set -u\n'
            'CASE="%s"\n'
            'GM_CONSOLE_STATE="%s/console.state"\n'
            'GM_CONSOLE_LOCK="%s/locks/console.lock"\n'
            'GM_ROUTES_LIB="%s"\n'
            'GM_ROUTE_STATE_DIR="%s/state"\n'
            'log() { logger -t geomask-guard "$*"; }\n'
            'logger() { printf "%%s\\n" "$*" >> "$CASE/logger.log"; }\n'
            'conntrack() { return 0; }\n'
            % (win(self.case), win(self.case), win(self.case), win(ROUTES), win(self.case))
        ) + lock_prelude(self.case) + IP_STUB + guard_block() + '\nsleep 0.2\n')
        started = time.monotonic()
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertLess(time.monotonic() - started, 10,
                        'the watchdog waited for the route lock while holding console')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_the_watchdog_skips_a_busy_lane_and_never_resurrects_a_rule(self):
        # A switch owns the lane. The watchdog must not wait for it (hotplug/cron path), must
        # not add the rule, and must not touch the holder's lock.
        self.hold_lock()
        before = [p.name for p in (self.case / 'locks').iterdir()]
        self.run_guard()
        self.assertEqual((self.case / 'ip-adds').read_text(encoding='utf-8'), '',
                         'the watchdog re-seated the rule while the switch owned the lane')
        self.assertIn('SKIP console watchdog', (self.case / 'logger.log').read_text(encoding='utf-8'))
        self.assertEqual(sorted(p.name for p in (self.case / 'locks').iterdir()), sorted(before),
                         'the watchdog changed the holder\'s lock')
        # After the switch finishes the state says off: a later run reads it inside the lock
        # and still adds nothing.
        self.state.write_text('enabled=0\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        self.run_guard()
        self.assertEqual((self.case / 'ip-adds').read_text(encoding='utf-8'), '')
        self.assertEqual(self.state.read_text(encoding='utf-8').splitlines()[0], 'enabled=0')

    def test_the_watchdog_never_leaves_the_lock_behind(self):
        self.run_guard('enabled=0\nip=192.168.1.177\n')
        self.assertTrue(self.lock_is_free(), 'the watchdog did not release the lane lock')

    def test_a_busy_lane_is_not_stolen(self):
        # Second process tries the lane with a 1s budget: busy, and the owner keeps its lock.
        self.hold_lock()
        script = self.write('second.sh', lock_prelude(self.case) + (
            'gm_lock_acquire console 1; printf "RC=%s\\n" "$?"\n'))
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertTrue(self.lock_file().exists(), 'the refused writer deleted the lock')

    def test_a_dead_holder_is_released_without_reclaim(self):
        if os.name == 'nt':
            self.skipTest('Linux-only: the kernel releases flock(2) on SIGKILL; the portable '
                          'test backend does not and is never used on the router')
        holder = self.write('holder.sh', lock_prelude(self.case) + (
            'gm_lock_acquire console 0 || exit 9\nprintf held > "%s/held"\nsleep 60\n'
            % win(self.case)))
        proc = subprocess.Popen([BASH, win(holder)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                break
            time.sleep(0.05)
        inode = self.lock_file().stat().st_ino
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.2)
        self.assertTrue(self.lock_is_free(), 'a killed holder kept the lane locked')
        self.assertEqual(inode, self.lock_file().stat().st_ino,
                         'the lock file was recreated instead of reused')


@unittest.skipIf(BASH is None, 'bash is required')
class BackendConsoleLockTests(unittest.TestCase):
    """The kill switch and `status` against a lane that is really locked by another process."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')

    def hold_lock(self):
        script = self.case / 'holder.sh'
        script.write_text(lock_prelude(self.case) + (
            'gm_lock_acquire console 0 || exit 9\nprintf held > "%s/held"\nsleep 30\n'
            % win(self.case)), encoding='utf-8', newline='\n')
        proc = subprocess.Popen([BASH, win(script)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                break
            time.sleep(0.05)
        self.assertTrue((self.case / 'held').exists())

    def run_backend(self, body):
        script = self.case / 'run.sh'
        script.write_text((
            'set -u\n'
            'CASE="%s"\n'
            'CONSOLE_STATE="%s/console.state"\n'
            'CONSOLE_LOCK="%s/locks/console.lock"\n'
            'CONSOLE_LOCK_TRIES=1\n'
            'CONSOLE_TABLE=102\n'
            'CONSOLE_TUN_DEV=Console\n'
            'CONSOLE_SOCKS_PORT=7893\n'
            'CONSOLE_PY=/nonexistent/geomask-console.py\n'
            'PYTHON_BIN=/nonexistent/python3\n'
            'TX_PY=/nonexistent/geomask-config-tx.py\n'
            'MIHOMO_CONF="%s/config.yaml"\n'
            'LOG="%s/geo-sync.log"\n'
            'DEVICES="%s/geo-devices.list"\n'
            'CONSOLE_INITD="%s/hev-console"\n'
            'ip() { return 1; }\n'
            'conntrack() { return 0; }\n'
            '_console_rule_off() { return 0; }\n'
            % (win(self.case), win(self.case), win(self.case), win(self.case),
               win(self.case), win(self.case), win(self.case))
        ) + lock_prelude(self.case)
          + '\n'.join(extract(name) for name in
                      ('_console_get', '_console_set', '_console_lock', '_console_unlock',
                       'cmd_console'))
          + '\n' + body, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def test_the_kill_switch_waits_for_the_lane_lock(self):
        before = self.state.read_text(encoding='utf-8')
        self.hold_lock()
        result = self.run_backend('cmd_console off 192.168.1.177; printf "\\nRC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('"ok":false', result.stdout)
        self.assertEqual(self.state.read_text(encoding='utf-8'), before)
        self.assertTrue((self.case / 'locks' / 'console.lock').exists(),
                        'the kill switch removed somebody else\'s lock')

    def test_status_does_not_take_the_lock(self):
        self.hold_lock()
        result = self.run_backend('cmd_console status "" "" ""; printf "\\nRC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout)
        self.assertIn('"ok":true', result.stdout)
        self.assertTrue((self.case / 'locks' / 'console.lock').exists(),
                        'status took or replaced the lane lock')


if __name__ == '__main__':
    unittest.main()
