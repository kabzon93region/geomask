"""One lane lock: the watchdog and the kill switch must never interleave.

Review 14-15.11.2026, point 8. `geomask-guard.sh` read `enabled` once and only then added the
console ip rule. If `off` finished between the read and the add, the rule came back after the
owner switched the lane off -- the console kept leaving through the tunnel while the panel said
"off", and nothing in the log explained it.

The lock is a directory with the owner pid inside (BusyBox on OpenWrt has no flock everywhere).
`on` and `off` take it for the whole operation; the watchdog takes it and re-reads
`enabled`/`ip` INSIDE it, so a rule can no longer be added on a state that is already gone.

Every script here runs inside one TemporaryDirectory and talks to stubs for `ip`, `logger` and
`conntrack`: no router, no `/etc`, no host `/tmp`.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
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


def guard_block(case):
    """The shipped watchdog block, with the state path pointed at the temporary tree.

    The guard has to read its state file by path. Before the fix that path is hard-coded to
    /etc/geomask/console.state, which a test must not touch, so the literal assignment is
    replaced here. After the fix the assignment is `${GM_CONSOLE_STATE:-/etc/...}`, nothing is
    replaced, and the environment variable does the same job.
    """
    start = GUARD_TEXT.index(GUARD_START)
    end = GUARD_TEXT.index(GUARD_END, start)
    block = GUARD_TEXT[start:end]
    return block.replace('CS=/etc/geomask/console.state',
                         'CS="%s/console.state"' % win(case))


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

    def test_the_watchdog_does_not_resurrect_a_rule_after_off(self):
        # Two processes. The kill switch holds the lane lock; the watchdog starts and must
        # wait. While it waits, `off` writes enabled=0 and the console rule is gone. After the
        # lock is released the watchdog has to read the state again and add nothing.
        watchdog = self.write('watchdog.sh', (
            'set -u\n'
            'CASE="%s"\n'
            'GM_CONSOLE_STATE="%s/console.state"\n'
            'GM_CONSOLE_LOCK="%s/console.lock"\n'
            'log() { logger -t geomask-guard "$*"; }\n'
            'logger() { printf "%%s\\n" "$*" >> "$CASE/logger.log"; }\n'
            'conntrack() { return 0; }\n'
            % (win(self.case), win(self.case), win(self.case))
        ) + IP_STUB + guard_block(self.case) + '\nsleep 0.2\n')

        orchestrator = self.write('run.sh', (
            'set -u\n'
            'CASE="%s"\n'
            'LOCK="$CASE/console.lock"\n'
            'BASH_BIN="%s"\n'
            'mkdir -p "$LOCK"\n'
            'printf "%%s\\n" "$$" > "$LOCK/pid"\n'
            '"$BASH_BIN" "$CASE/watchdog.sh" &\n'
            'WD=$!\n'
            'sleep 2\n'
            '# `off`: the state goes first, then the rule the watchdog would re-seat.\n'
            'printf "enabled=0\\nip=192.168.1.177\\n" > "$CASE/console.state"\n'
            ': > "$CASE/rules"\n'
            'rm -rf "$LOCK"\n'
            'wait $WD\n'
            'printf "WD_RC=%%s\\n" "$?"\n'
            % (win(self.case), win(BASH))
        ))
        result = subprocess.run([BASH, win(orchestrator)], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('WD_RC=0', result.stdout)
        adds = (self.case / 'ip-adds').read_text(encoding='utf-8')
        self.assertEqual(adds, '', 'the watchdog re-seated the rule after `off`:\n' + adds)
        self.assertNotIn('console rule', (self.case / 'logger.log').read_text(encoding='utf-8'))
        self.assertEqual(self.state.read_text(encoding='utf-8').splitlines()[0], 'enabled=0')

    def test_the_watchdog_never_leaves_the_lock_behind(self):
        # A blocked `on`/`off` would wait for a watchdog that forgot to unlock, so the block
        # must give the lane lock back on every path.
        (self.case / 'console.state').write_text('enabled=0\nip=192.168.1.177\n',
                                                 encoding='utf-8', newline='\n')
        script = self.write('watchdog.sh', (
            'set -u\n'
            'CASE="%s"\n'
            'GM_CONSOLE_STATE="%s/console.state"\n'
            'GM_CONSOLE_LOCK="%s/console.lock"\n'
            'log() { logger -t geomask-guard "$*"; }\n'
            'logger() { printf "%%s\\n" "$*" >> "$CASE/logger.log"; }\n'
            'conntrack() { return 0; }\n'
            % (win(self.case), win(self.case), win(self.case))
        ) + IP_STUB + guard_block(self.case) + '\n')
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.case / 'console.lock').exists(),
                         'the watchdog did not release the lane lock')

    def test_a_lock_left_by_a_dead_process_is_broken(self):
        # A killed `on` must not block the lane forever: a lock whose pid is gone is stale and
        # is removed on the next attempt, immediately, not after the whole wait.
        script = self.write('lock.sh', (
            'set -u\n'
            'CONSOLE_LOCK="%s/console.lock"\n'
            'CONSOLE_LOCK_TRIES=3\n'
            'sh -c "exit 0" & DEAD=$!\n'
            'wait $DEAD\n'
            'mkdir -p "$CONSOLE_LOCK"\n'
            'printf "%%s\\n" "$DEAD" > "$CONSOLE_LOCK/pid"\n'
            'kill -0 "$DEAD" 2>/dev/null && printf "DEAD_ALIVE\\n"\n'
            % win(self.case)
        ) + extract('_console_lock') + '\n' + extract('_console_unlock') + '\n' + (
            '_console_lock; printf "RC=%s\\n" "$?"\n'
            'printf "OWNER=%s\\n" "$(cat "$CONSOLE_LOCK/pid" 2>/dev/null)"\n'
            'printf "SELF=%s\\n" "$$"\n'
            '_console_unlock\n'
        ))
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('DEAD_ALIVE', result.stdout)
        self.assertIn('RC=0', result.stdout)
        owner = re.search(r'OWNER=(\S+)', result.stdout)
        itself = re.search(r'SELF=(\S+)', result.stdout)
        self.assertIsNotNone(owner)
        self.assertIsNotNone(itself)
        self.assertEqual(owner.group(1), itself.group(1))

    def test_a_live_lock_is_not_broken(self):
        # The opposite case: somebody else is working. The lane must report busy, not steal the
        # lock, and the owner's pid must survive.
        script = self.write('lock.sh', (
            'set -u\n'
            'CONSOLE_LOCK="%s/console.lock"\n'
            'CONSOLE_LOCK_TRIES=1\n'
            'mkdir -p "$CONSOLE_LOCK"\n'
            'printf "%%s\\n" "$$" > "$CONSOLE_LOCK/pid"\n'
            % win(self.case)
        ) + extract('_console_lock') + '\n' + (
            '_console_lock; printf "RC=%s\\n" "$?"\n'
            'printf "OWNER=%s\\n" "$(cat "$CONSOLE_LOCK/pid" 2>/dev/null)"\n'
            'printf "SELF=%s\\n" "$$"\n'
        ))
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(re.search(r'OWNER=(\S+)', result.stdout).group(1),
                         re.search(r'SELF=(\S+)', result.stdout).group(1))

    # ---------------------------------------------------------------- backend callers
    def lock_holder(self):
        """Start a real second process that owns the lane lock (its own pid goes inside)."""
        holder = subprocess.Popen(
            [BASH, '-c', 'printf "%s\\n" "$$" > "$1"; sleep 30', 'sh',
             win(self.case / 'console.lock' / 'pid')],
            cwd=str(self.case))
        self.addCleanup(holder.kill)
        # The directory has to exist before the holder can write the pid into it.
        return holder

    def hold_lock(self, body):
        """Run a backend body while another live process owns the lane lock."""
        (self.case / 'console.lock').mkdir()
        holder = self.lock_holder()
        pidfile = self.case / 'console.lock' / 'pid'
        for _ in range(100):
            if pidfile.exists() and pidfile.stat().st_size:
                break
            time.sleep(0.05)
        script = self.write('run.sh', (
            'set -u\n'
            'CONSOLE_LOCK="%s/console.lock"\n'
            'CONSOLE_LOCK_TRIES=1\n'
            'CONSOLE_STATE="%s/console.state"\n'
            'CONSOLE_TABLE=102\n'
            'CONSOLE_TUN_DEV=Console\n'
            'CONSOLE_SOCKS_PORT=7893\n'
            'CONSOLE_PY=/nonexistent/geomask-console.py\n'
            'PYTHON_BIN=/nonexistent/python3\n'
            'LOG="%s/geo-sync.log"\n'
            'CONSOLE_INITD="%s/hev-console"\n'
            'ip() { return 1; }\n'
            'conntrack() { return 0; }\n'
            '_console_rule_off() { return 0; }\n'
            % (win(self.case), win(self.case), win(self.case), win(self.case))
        ) + '\n'.join(extract(name) for name in
                      ('_console_get', '_console_set', '_console_lock', '_console_unlock',
                       'cmd_console')) + '\n' + body)
        result = subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        holder.kill()
        try:
            holder.wait(timeout=10)      # do not let a dying holder keep the lock dir busy
        except subprocess.TimeoutExpired:
            pass
        return result

    def test_the_kill_switch_waits_for_the_lane_lock(self):
        # The emergency path does not wait for a backup, but it is not allowed to work without
        # the lock: `off` must fail loudly instead of interleaving with a running switch.
        before = self.state.read_text(encoding='utf-8')
        result = self.hold_lock('cmd_console off 192.168.1.177; printf "\\nRC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('"ok":false', result.stdout)
        self.assertEqual(self.state.read_text(encoding='utf-8'), before)
        self.assertTrue((self.case / 'console.lock' / 'pid').exists(),
                        'the kill switch removed somebody else\'s lock')

    def test_status_does_not_take_the_lock(self):
        # `status` runs on every panel render; it must stay read-only and never wait for a
        # switch, otherwise the panel freezes while `on` is working.
        result = self.hold_lock('cmd_console status "" "" ""; printf "\\nRC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout)
        self.assertIn('"ok":true', result.stdout)
        self.assertTrue((self.case / 'console.lock' / 'pid').exists(),
                        'status took or replaced the lane lock')


if __name__ == '__main__':
    unittest.main()
