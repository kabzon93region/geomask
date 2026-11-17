"""R05: the console lane must not wait 20 s for a route table while it holds `console`.

Defect (plan R05, D6/D7): `_console_rule_on` called `_gm_sync_routes 102` with the default
20-second route-lock budget while `cmd_console on` already held the `console` lock. The kill
switch has a 10-second budget, so `console on` and the guard could be answered with "console is
busy" for 20 s. `_ai_tun_ensure` ignored the rc of `_gm_sync_routes 101`: with table 101 busy it
still installed `default dev AI` and then added per-device rules into a table without the LAN
prefixes (a black hole).

Contract: a console switch uses a short route budget (~2 s), classifies a busy table as a
retryable error, and never blocks `off` on the route lock. A busy table 101 aborts the AI route
install before the default and propagates a failure out of `_apply_routing`.

Windows can decide the rc/timing logic against stubs and the real lock/routes helpers. Real
`ip`, netns and the RPC transport are Linux/router-only and skipped.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
LOCKLIB = ROOT / 'assets' / 'geomask-lock.sh'
ROUTES = ROOT / 'assets' / 'geomask-routes.sh'
BACKEND_TEXT = BACKEND.read_text(encoding='utf-8')


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


def q(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


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


STUBS = r'''uci() {
  case "$1" in -q) shift ;; esac
  case "$*" in
    'show firewall')
      printf 'firewall.@zone[0]=zone\nfirewall.@zone[0].name=lan\nfirewall.@zone[0].network=lan\n'
      return 0 ;;
    'get firewall.@zone[0].name') printf lan ;;
    'get firewall.@zone[0].network') printf lan ;;
    'get network.lan.device') printf br-lan ;;
    'get network.lan.ifname') return 1 ;;
  esac
  return 1
}

ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$1" in
    link) return 1 ;;
    rule)
      case "$2" in
        show) return 0 ;;
        add) return 0 ;;
        del) return 1 ;;
      esac
      return 0 ;;
    -4)
      [ -f "$CASE/no-addrs" ] && return 0
      printf '    inet 192.168.1.1/24 brd 192.168.1.255 scope global br-lan\n'
      return 0 ;;
    route)
      case "$2" in
        show) return 0 ;;
        replace) return 0 ;;
        del) return 0 ;;
      esac
      return 0 ;;
    *) return 0 ;;
  esac
}

logger() { printf '%s\n' "$*" >> "$CASE/logger.log"; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleRouteBudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'calls.log').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'logger.log').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'locks').mkdir()
        (self.case / 'state').mkdir()
        (self.case / 'tbl-main').write_text(
            '192.168.1.0/24 dev br-lan proto kernel scope link src 192.168.1.1\n',
            encoding='utf-8', newline='\n')
        for table in (100, 101, 102):
            (self.case / ('tbl-%d' % table)).write_text('', encoding='utf-8', newline='\n')
        (self.case / 'stubs.sh').write_text(STUBS, encoding='utf-8', newline='\n')
        (self.case / 'stubs-src.sh').write_text('', encoding='utf-8', newline='\n')

    def write(self, name, text, mode=None):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        if mode is not None:
            path.chmod(mode)
        return path

    def read(self, name):
        path = self.case / name
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def env_prelude(self, extra=''):
        return (
            'set -u\n'
            'CASE=%s\n'
            'export CASE\n'
            '. "$CASE/stubs.sh"\n'
            'CONSOLE_TABLE=102\n'
            'CONSOLE_TUN_DEV=Console\n'
            'CONSOLE_RULE_PREF=19903\n'
            'CONSOLE_ROUTE_WAIT=2\n'
            'LOG="$CASE/geo-sync.log"\n'
            'TMPDIR="$CASE"\n'
            'GM_ROUTE_STATE_DIR="$CASE/state"\n'
            'GM_LOCK_DIR="$CASE/locks"\n'
            'GM_LOCK_LIB=%s\n'
            'GM_ROUTES_LIB=%s\n'
            'export CONSOLE_TABLE CONSOLE_TUN_DEV CONSOLE_RULE_PREF CONSOLE_ROUTE_WAIT LOG TMPDIR\n'
            'export GM_ROUTE_STATE_DIR GM_LOCK_DIR GM_LOCK_LIB GM_ROUTES_LIB\n'
            '. "$GM_LOCK_LIB"\n'
            % (q(posix(self.case)), q(posix(LOCKLIB)), q(posix(ROUTES)))
        ) + extra

    def hold(self, name):
        script = self.write('hold-%s.sh' % name,
                            self.env_prelude() + (
            'cd / 2>/dev/null\n'
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire %s 0 || exit 9\n'
            'printf held > "$CASE/held"\n'
            '_i=0\n'
            'while [ ! -f "$CASE/release" ] && [ "$_i" -lt 600 ]; do sleep 0.1; _i=$((_i+1)); done\n'
            'gm_lock_release %s\n' % (name, name)))
        proc = subprocess.Popen([BASH, posix(script)], cwd=str(self.case),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8')
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return proc
            time.sleep(0.05)
        self.fail('the lock holder for %s never started' % name)

    def release(self):
        self.write('release', '')
        time.sleep(0.3)

    def run_script(self, body, timeout=60):
        script = self.write('run.sh', self.env_prelude() + body)
        return subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=timeout)


class ConsoleRuleBudgetTests(ConsoleRouteBudgetCase):
    def rule_on_body(self):
        return '\n'.join(extract(name) for name in ('_gm_sync_routes', '_console_rule_on')) + (
            '\n_console_rule_on 192.168.1.177\nprintf "RC=%s\\n" "$?"\n')

    def test_busy_table_102_returns_in_under_three_seconds(self):
        self.hold('route-102')
        started = time.monotonic()
        result = self.run_script(self.rule_on_body())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0,
                        'console on waited %.2fs for a busy route table' % elapsed)
        self.assertIn('RC=75', result.stdout,
                      'a busy route table was not reported as retryable/busy: %s'
                      % (result.stdout + result.stderr))

    def test_short_budget_is_used_even_when_the_caller_exported_none(self):
        self.hold('route-102')
        started = time.monotonic()
        self.run_script(self.rule_on_body())
        self.assertLess(time.monotonic() - started, 3.0)

    def test_rule_on_does_not_install_a_default_without_the_table(self):
        self.hold('route-102')
        self.run_script(self.rule_on_body())
        calls = '\n'.join(self.read('calls.log'))
        self.assertNotIn('route replace table 102 default', calls,
                         'the console default was installed without the LAN prefixes')


class AiRouteRcTests(ConsoleRouteBudgetCase):
    def test_busy_101_does_not_install_the_ai_default(self):
        body = '\n'.join(extract(name) for name in ('_gm_sync_routes', '_ai_routes_ensure')) + (
            '\n_ai_routes_ensure\nprintf "RC=%s\\n" "$?"\n')
        self.hold('route-101')
        result = self.run_script(body)
        self.assertIn('RC=75', result.stdout,
                      'busy table 101 was not propagated: %s' % (result.stdout + result.stderr))
        calls = '\n'.join(self.read('calls.log'))
        self.assertNotIn('route replace table 101 default', calls,
                         'the AI default was installed into a table without LAN prefixes')

    def test_apply_routing_skips_the_ai_rule_when_the_tunnel_is_not_ready(self):
        devices = self.write('devices.list', '192.168.1.50 ai phone\n')
        body = (
            'LOG="$CASE/geo-sync.log"\n'
            'DEVICES=%s\n'
            'export LOG DEVICES\n'
            '_ai_tun_ensure() { return 75; }\n'
            '_hotplug_sync() { :; }\n'
            '_aidns_sync() { :; }\n' % q(posix(devices))
        ) + extract('_apply_routing') + (
            '\n_apply_routing\nprintf "RC=%s\\n" "$?"\n')
        result = self.run_script(body)
        self.assertNotIn('RC=0', result.stdout,
                         '_apply_routing reported success while the AI tunnel was not ready')
        adds = [line for line in self.read('calls.log') if line.startswith('rule add')]
        self.assertFalse([line for line in adds if 'from 192.168.1.50 lookup 101' in line],
                         'a rule into a not-ready table 101 was installed anyway: %r' % (adds,))


class ConsoleRpcBudgetTests(ConsoleRouteBudgetCase):
    """The RPC answer for `console on` must say "retryable", not "installed"."""

    def python_posix(self):
        return posix(sys.executable)

    def console_on_body(self):
        state = self.write('console.state', 'enabled=0\nip=192.168.1.177\n')
        conf = self.write('config.yaml', 'mixed-port: 7890\n')
        dev = self.write('devices.list', '')
        console_py = self.write('console.py', '#!/bin/sh\nprintf \'{"ok": true}\\n\'\n', 0o755)
        tx_py = self.write('tx.py', 'import sys\nsys.exit(0)\n', 0o755)
        initd = self.write('hev-console', '#!/bin/sh\nexit 0\n', 0o755)
        prelude = (
            'CONSOLE_STATE=%s\n'
            'CONSOLE_LOCK="$CASE/locks/console.lock"\n'
            'CONSOLE_LOCK_TRIES=1\n'
            'CONSOLE_SOCKS_PORT=7893\n'
            'CONSOLE_INITD=%s\n'
            'CONSOLE_PY=%s\n'
            'TX_PY=%s\n'
            'PYTHON_BIN=%s\n'
            'MIHOMO_CONF=%s\n'
            'MIHOMO_API=http://127.0.0.1:9090\n'
            'DEVICES=%s\n'
            'LOG="$CASE/geo-sync.log"\n'
            'export CONSOLE_STATE CONSOLE_LOCK CONSOLE_LOCK_TRIES CONSOLE_SOCKS_PORT '
            'CONSOLE_INITD CONSOLE_PY TX_PY PYTHON_BIN MIHOMO_CONF MIHOMO_API DEVICES LOG\n'
            '_require_backup() { return 0; }\n'
            '_ipv4_ok() { return 0; }\n'
            '_console_tun_ensure() { return 0; }\n'
            '_console_fw_ensure() { return 0; }\n'
            '_console_rule_on() { return 75; }\n'
            'mihomo() { return 0; }\n'
            'controller_curl() { printf "200"; return 0; }\n'
            'netstat() { printf "tcp 0 0 0.0.0.0:7893 0.0.0.0:* LISTEN\\n"; return 0; }\n'
            % (q(posix(state)), q(posix(initd)), q(posix(console_py)), q(posix(tx_py)),
               q(self.python_posix()), q(posix(conf)), q(posix(dev)))
        )
        funcs = '\n'.join(extract(name) for name in
                          ('_console_get', '_console_set', '_console_lock', '_console_unlock',
                           '_console_cfg_restore', '_console_on_fail', 'cmd_console'))
        return prelude + funcs + (
            '\ncmd_console on 192.168.1.177 "" ""\nprintf "\\nRC=%s\\n" "$?"\n')

    def test_busy_route_table_is_reported_as_retryable(self):
        started = time.monotonic()
        result = self.run_script(self.console_on_body())
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn('"ok":false', result.stdout, result.stdout + result.stderr)
        self.assertIn('"retryable":true', result.stdout,
                      'a busy route table was not marked retryable: ' + result.stdout)
        self.assertIn('"step":"rule"', result.stdout)

    def test_console_off_does_not_touch_the_route_lock(self):
        state = self.write('console.state', 'enabled=1\nip=192.168.1.177\n')
        initd = self.write('hev-console', '#!/bin/sh\nexit 0\n', 0o755)
        body = (
            'CONSOLE_STATE=%s\n'
            'CONSOLE_LOCK="$CASE/locks/console.lock"\n'
            'CONSOLE_LOCK_TRIES=1\n'
            'CONSOLE_TABLE=102\n'
            'CONSOLE_INITD=%s\n'
            'LOG="$CASE/geo-sync.log"\n'
            'export CONSOLE_STATE CONSOLE_LOCK CONSOLE_LOCK_TRIES CONSOLE_TABLE CONSOLE_INITD LOG\n'
            '_console_rule_off() { return 0; }\n'
            % (q(posix(state)), q(posix(initd)))
        ) + '\n'.join(extract(name) for name in
                      ('_console_get', '_console_set', '_console_lock', '_console_unlock',
                       'cmd_console')) + (
            '\ncmd_console off 192.168.1.177\nprintf "\\nRC=%s\\n" "$?"\n')
        self.hold('route-102')
        started = time.monotonic()
        result = self.run_script(body)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, 'console off waited %.2fs for the route lock' % elapsed)
        self.assertIn('"ok":true', result.stdout, result.stdout + result.stderr)


class StaticConsoleContractTests(unittest.TestCase):
    def test_the_console_switch_has_a_short_route_budget(self):
        text = BACKEND_TEXT
        self.assertTrue(re.search(r'CONSOLE_ROUTE_WAIT=\$\{[^}]*:-[0-3]\}', text),
                        'the console switch has no short route-lock budget')
        self.assertIn('retryable', text)
        self.assertIn('_ai_routes_ensure', text)
        ai = text[text.index('_ai_tun_ensure() {'):text.index('_hotplug_sync() {')]
        self.assertNotRegex(
            ai, r'_gm_sync_routes 101\n\s*ip route replace table 101 default',
            'the AI default is still installed without checking the sync rc')


def reap(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.2)
    except Exception:
        pass


if __name__ == '__main__':
    unittest.main()
