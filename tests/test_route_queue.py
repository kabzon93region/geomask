"""R05: the ifup path must accept a route event, never wait inside netifd, and never lose it.

Defect (plan R05, analysis 17.11.2026): when the route-table lock was busy, `apply_routes` in
`/etc/hotplug.d/iface/99-default-vpn-route` returned 1 but the script still ended with `exit 0`.
Table 100's default and `ip rule 32760` were never installed, and nobody retried: the hotplug
script was the only writer of those objects in the repository. The late-TUN path waited for the
link only and then made a single attempt. The hotplug path also ran `ip`, `uci` and three
`sync_table` calls synchronously inside netifd's ifup.

The contract implemented by assets/99-default-vpn-route + assets/geomask-routes.sh:
  * hotplug only records `pending-<table>` (temp file + atomic rename) and spawns ONE detached
    worker; rc 0 = event accepted, 1 = pending not written, 2 = no lock primitive. No `ip`, no
    `uci`, no `sleep`, no `apply_routes`/`sync_table` in the netifd path;
  * the worker is single-flight on the `routes-worker` lock (rank 35), takes `route-<table>`
    (rank 40), and NEVER waits for a lock while holding either: each attempt uses a 2 s budget;
  * rc contract of `geomask_routes_sync`: 0 ok, 75 busy lock, 76 retryable read before any
    mutation, 77 failed mutation, 2 no lock primitive, 4 lock-order inversion. Only 75/76 are
    retried (<=5 attempts, backoff 1/2/4/8, <=3 activations, ~30 s cap); 77 is never retried
    (R12 owns ownership reconciliation), it parks the event;
  * `pending -> work` is an atomic rename and `work` is removed only after success. A new
    pending that appeared during the sync gets another pass (generation recheck);
  * `gm_route_drain` applies what is already queued, inline, without spawning a worker: the
    guard/cron backstop and the installer (under `maintenance`) use it.

What a Windows host can decide: the queue protocol, the rc contract, the retry/backoff/park
bounds, the top entrypoint's timing and exit code, the static shape of the hotplug path. What
it cannot: flock(2) release after SIGKILL, descriptor inheritance, real `ip`/netns/procd.
Those are skipped with a reason (see BOUNDARIES.md).
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
ASSETS = ROOT / 'assets'
HELPER = ASSETS / 'geomask-lock.sh'
ROUTES = ASSETS / 'geomask-routes.sh'
HOTPLUG = ASSETS / '99-default-vpn-route'
GUARD = ASSETS / 'geomask-guard.sh'
TEMPLATE = ROOT / 'geomask.sh.in'


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


STUBS = r'''uci() {
  case "$1" in -q) shift ;; esac
  case "$*" in
    'show firewall')
      printf 'firewall.@zone[0]=zone\nfirewall.@zone[0].name=lan\n'
      printf 'firewall.@zone[0].network=lan\n'
      return 0 ;;
    'get firewall.@zone[0].name') printf 'lan'; return 0 ;;
    'get firewall.@zone[0].network') printf 'lan'; return 0 ;;
    'get network.lan.device') printf 'br-lan'; return 0 ;;
    'get network.lan.ifname') return 1 ;;
  esac
  return 1
}

ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$1" in
    link)
      case "${3:-}" in
        Mihomo|AI|Console|Wireguard)
          [ -f "$CASE/tun-${3}" ] && return 0 || return 1 ;;
      esac
      return 1 ;;
    rule)
      case "$2" in
        show) [ -f "$CASE/rules" ] && cat "$CASE/rules"; return 0 ;;
        add)
          _line="$*"
          _pref=0
          _prev=""
          for _tok in $_line; do
            case "$_prev" in priority|pref) _pref=$_tok ;; esac
            _prev=$_tok
          done
          printf '%s: %s\n' "$_pref" "$_line" >> "$CASE/rules"
          return 0 ;;
        del) return 1 ;;
      esac
      return 0 ;;
    -4)
      [ -f "$CASE/no-addrs" ] && return 0
      printf '    inet 192.168.1.1/24 brd 192.168.1.255 scope global br-lan\n'
      return 0 ;;
    route) ;;
    *) return 0 ;;
  esac
  shift
  _cmd=$1; shift
  _tbl=main
  if [ "${1:-}" = table ]; then _tbl=$2; shift 2; fi
  _f="$CASE/tbl-$_tbl"
  case "$_cmd" in
    show)
      [ -f "$_f" ] && cat "$_f"
      return 0 ;;
    replace)
      [ -f "$CASE/fail-replace" ] && return 1
      _line=$*
      awk -v p="${_line%% *}" '$1 != p' "$_f" > "$_f.new" 2>/dev/null
      mv -f "$_f.new" "$_f" 2>/dev/null
      printf '%s\n' "$_line" >> "$_f"
      return 0 ;;
    del)
      _line=$*
      grep -vxF "$_line" "$_f" > "$_f.new" 2>/dev/null
      mv -f "$_f.new" "$_f" 2>/dev/null
      return 0 ;;
  esac
  return 0
}

logger() { printf '%s\n' "$*" >> "$CASE/logger.log"; }
'''

LAN = '192.168.1.0/24 dev br-lan proto kernel scope link src 192.168.1.1'


@unittest.skipIf(BASH is None, 'bash is required')
class RouteQueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        for name in ('calls.log', 'rules', 'logger.log'):
            (self.case / name).write_text('', encoding='utf-8', newline='\n')
        (self.case / 'state').mkdir()
        (self.case / 'locks').mkdir()
        (self.case / 'tbl-main').write_text(LAN + '\n', encoding='utf-8', newline='\n')
        (self.case / 'tbl-100').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'tbl-101').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'tbl-102').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'tun-Mihomo').write_text('', encoding='utf-8')
        (self.case / 'devices.list').write_text('', encoding='utf-8', newline='\n')
        (self.case / 'console.state').write_text('enabled=0\nip=192.168.1.177\n',
                                                 encoding='utf-8', newline='\n')
        (self.case / 'mihomo.yaml').write_text(
            'tun:\n  enable: true\n  device: Mihomo\n', encoding='utf-8', newline='\n')
        (self.case / 'stubs.sh').write_text(STUBS, encoding='utf-8', newline='\n')
        self._procs = []

    # ------------------------------------------------------------------ helpers
    def read(self, name):
        path = self.case / name
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def env_prelude(self, extra=''):
        return (
            'set -u\n'
            'CASE=%s\n'
            'export CASE\n'
            '. "$CASE/stubs.sh"\n'
            'GM_ROUTE_STATE_DIR="$CASE/state"\n'
            'GM_LOCK_DIR="$CASE/locks"\n'
            'GM_LOCK_LIB=%s\n'
            'GM_ROUTES_LIB=%s\n'
            'MIHOMO_CONF="$CASE/mihomo.yaml"\n'
            'DEVICES="$CASE/devices.list"\n'
            'GM_CONSOLE_STATE="$CASE/console.state"\n'
            'export GM_ROUTE_STATE_DIR GM_LOCK_DIR GM_LOCK_LIB GM_ROUTES_LIB\n'
            'export MIHOMO_CONF DEVICES GM_CONSOLE_STATE\n'
            % (q(posix(self.case)), q(posix(HELPER)), q(posix(ROUTES)))
        ) + extra

    def hold(self, name, marker='held'):
        script = self.write('hold-%s.sh' % name.replace('/', '-'),
                            self.env_prelude() + (
            'cd / 2>/dev/null\n'
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire %s 0 || exit 9\n'
            'printf held > "$CASE/%s"\n'
            '_i=0\n'
            'while [ ! -f "$CASE/release" ] && [ "$_i" -lt 600 ]; do sleep 0.1; _i=$((_i+1)); done\n'
            'gm_lock_release %s\n' % (name, marker, name)))
        proc = subprocess.Popen([BASH, posix(script)], cwd=str(self.case),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8')
        self.addCleanup(reap, proc)
        self._procs.append(proc)
        for _ in range(200):
            if (self.case / marker).exists():
                return proc
            time.sleep(0.05)
        self.fail('the lock holder for %s never started' % name)

    def release_holders(self):
        self.write('release', '')
        for proc in list(getattr(self, '_procs', [])):
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

    def hotplug_script(self, extra_env=''):
        # The real entrypoint is sourced so its `exit 0` is the exit code under test; the
        # wrapper only refuses to create the historical hardcoded host /tmp path (a R06
        # live-write guard in the harness, not a change of the script under test).
        return self.env_prelude(extra_env) + (
            'mkdir() { case "$*" in *"/tmp/geomask-routes"*) return 1 ;; esac; command mkdir "$@"; }\n'
            'ACTION=ifup\n'
            'export ACTION\n'
            '. %s\n' % q(posix(HOTPLUG)))

    def run_hotplug(self, extra_env='', timeout=30):
        script = self.write('run-hotplug.sh', self.hotplug_script(extra_env))
        started = time.monotonic()
        result = subprocess.run([BASH, posix(script)], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=timeout)
        return result, time.monotonic() - started

    def drain(self):
        body = ('GM_ROUTE_STATE_DIR="$CASE/state"\n'
                'export GM_ROUTE_STATE_DIR\n'
                '. "$GM_ROUTES_LIB"\n'
                'command -v gm_route_drain >/dev/null 2>&1 || { printf "NODRAIN\\n"; exit 127; }\n'
                'gm_route_drain\n'
                'printf "RC=%s\\n" "$?"\n')
        script = self.write('drain.sh', self.env_prelude() + body)
        return subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def sync_rc(self, extra='', timeout=60):
        body = ('. "$GM_ROUTES_LIB"\n'
                'geomask_routes_sync 100\n'
                'printf "RC=%s\\n" "$?"\n')
        script = self.write('sync.sh', self.env_prelude(extra) + body)
        return subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=timeout)

    def worker(self, extra='', mode='worker', override='', timeout=60):
        body = ('GM_ROUTE_STATE_DIR="$CASE/state"\n'
                'export GM_ROUTE_STATE_DIR\n'
                '. "$GM_ROUTES_LIB"\n'
                + override
                + ('gm_route_drain' if mode == 'drain' else 'gm_route_worker') + '\n'
                + 'printf "RC=%s\\n" "$?"\n')
        script = self.write('worker.sh', self.env_prelude(extra) + body)
        return subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=timeout)

    def pending(self):
        return sorted(p.name for p in (self.case / 'state').glob('pending-*'))

    def parked(self):
        return sorted(p.name for p in (self.case / 'state').glob('parked-*'))

    def queue(self, table=100, generation='1'):
        self.write('state/pending-%d' % table, generation + '\n')

    def attempts(self):
        path = self.case / 'attempts'
        return path.read_text(encoding='utf-8').count('x') if path.exists() else 0


def reap(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.2)          # a killed shell's `sleep` child releases the cwd a moment later
    except Exception:
        pass


class HotplugEntrypointTests(RouteQueueCase):
    def test_busy_lock_accepts_the_event_in_under_a_second(self):
        """R05 D1/D2: the event must survive a busy route table; rc must mean "accepted"."""
        self.hold('route-100')
        self.hold('routes-worker')          # a worker is already active: the spawn must be a no-op
        result, elapsed = self.run_hotplug()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, 1.0,
                        'the ifup path spent %.2fs - it waited for the route lock' % elapsed)
        self.assertIn('pending-100', self.pending(),
                      'the busy event was dropped: no pending-100 (%r)' % (self.pending(),))
        calls = '\n'.join(self.read('calls.log'))
        self.assertNotIn('route replace', calls, 'the hotplug path mutated the table itself')
        self.assertNotIn('rule add', calls, 'the hotplug path mutated the rules itself')
        self.assertEqual(self.read('tbl-100'), [], 'table 100 was changed by the ifup path')

    def test_release_then_drain_installs_table_100_and_rule_32760(self):
        """R05 D1: after the lock is released the queued event must be applied, not lost."""
        self.hold('route-100')
        self.hold('routes-worker')
        result, _ = self.run_hotplug()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pending-100', self.pending())
        # Let the detached worker (spawned by the hotplug) actually run and exit on the busy
        # worker lock before the test releases it; otherwise that worker would race the drain.
        time.sleep(0.8)
        self.release_holders()
        # wait for the portable/flock holder processes to really let go
        time.sleep(0.4)
        drained = self.drain()
        self.assertNotIn('NODRAIN', drained.stdout, 'gm_route_drain is not implemented')
        self.assertIn('RC=0', drained.stdout, drained.stdout + drained.stderr)
        # The drain (or the worker that was already queued) must converge on the full policy.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if 'default dev Mihomo' in self.read('tbl-100') and '32760' in '\n'.join(self.read('rules')):
                break
            time.sleep(0.1)
        self.assertIn('default dev Mihomo', self.read('tbl-100'))
        rules = '\n'.join(self.read('rules'))
        self.assertIn('32760', rules, 'ip rule 32760 was not installed: %r' % (self.read('rules'),))
        self.assertEqual(self.pending(), [], 'the applied event stayed in the queue')
        self.assertFalse((self.case / 'state' / 'error-100').exists(),
                         'a successful drain left an error marker')

    def test_drain_applies_an_explicit_pending_event(self):
        """The drain itself is the backstop: no hotplug, no spawned worker, just the queue."""
        self.write('state/pending-100', '1\n')
        drained = self.drain()
        self.assertIn('RC=0', drained.stdout, drained.stdout + drained.stderr)
        self.assertIn('default dev Mihomo', self.read('tbl-100'))
        self.assertIn('32760', '\n'.join(self.read('rules')))
        self.assertEqual(self.pending(), [])
        self.assertEqual(sorted(p.name for p in (self.case / 'state').glob('work-*')), [])

    def test_drain_leaves_a_busy_event_pending_and_does_not_park_it(self):
        """A transient busy table during a guard drain must not stop the automation."""
        self.hold('route-100')
        self.write('state/pending-100', '1\n')
        drained = self.drain()
        self.assertIn('RC=0', drained.stdout, drained.stdout + drained.stderr)
        self.assertEqual(self.pending(), ['pending-100'],
                         'the drain consumed or parked a busy event')
        self.assertEqual(self.parked(), [], 'a transient busy table was parked by a drain')
        self.assertFalse((self.case / 'state' / 'error-100').exists(),
                         'a transient busy table produced a permanent error marker')

    def test_late_tun_is_applied_even_when_the_lock_was_busy(self):
        """R05 D3: waiting for the TUN belongs to the worker, and the worker retries."""
        (self.case / 'tun-Mihomo').unlink()
        self.hold('route-100')
        result, _ = self.run_hotplug('GM_ROUTE_BACKOFF="1 1 1 1"; export GM_ROUTE_BACKOFF\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (self.case / 'tun-Mihomo').exists():
            time.sleep(0.05)
        self.write('tun-Mihomo', '')
        # The current code applies once, about 1 s after it sees the link (busy at that moment).
        time.sleep(3.5)
        self.write('release', '')
        applied = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if 'default dev Mihomo' in self.read('tbl-100'):
                applied = True
                break
            time.sleep(0.2)
        self.assertTrue(applied, 'the late-TUN event was never retried: %r' % (self.read('tbl-100'),))

    def test_enqueue_failure_returns_nonzero(self):
        blocker = self.write('blocker', 'not a directory\n')
        result, _ = self.run_hotplug(extra_env='GM_ROUTE_STATE_DIR="$CASE/blocker/state"\n'
                                               'export GM_ROUTE_STATE_DIR\n')
        self.assertNotEqual(result.returncode, 0,
                            'a failed pending write was reported as success')

    def test_partial_enqueue_failure_still_spawns_for_the_written_tables(self):
        """101 cannot be written, 100 and 102 can: the worker must still run for them."""
        self.write('devices.list', '192.168.1.50 ai phone\n')
        self.write('console.state', 'enabled=1\nip=192.168.1.177\n')
        fake = self.write('fake-routes.sh', (
            'gm_route_enqueue() {\n'
            '  case "$1" in\n'
            '    101) return 1 ;;\n'
            '    *) : > "$GM_ROUTE_STATE_DIR/pending-$1"; return 0 ;;\n'
            '  esac\n'
            '}\n'
            'gm_lock_close_inherited() { :; }\n'
            'gm_route_worker() { printf spawned >> "$CASE/spawns"; }\n'))
        result, _ = self.run_hotplug(extra_env='GM_ROUTES_LIB=%s\n'
                                               'export GM_ROUTES_LIB\n' % q(posix(fake)))
        self.assertEqual(result.returncode, 1,
                         'the worst enqueue failure was not reported: %s'
                         % (result.stdout + result.stderr))
        self.assertIn('pending-100', self.pending())
        self.assertIn('pending-102', self.pending())
        self.assertNotIn('pending-101', self.pending())
        marker = self.case / 'spawns'
        for _ in range(50):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(),
                        'a partial enqueue failure cancelled the worker for written tables')

    def test_ifup_storm_leaves_one_pending_file(self):
        self.hold('routes-worker')
        procs = []
        for index in range(4):
            script = self.write('storm-%d.sh' % index, self.hotplug_script())
            procs.append(subprocess.Popen([BASH, posix(script)], cwd=str(self.case),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, encoding='utf-8'))
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(self.pending(), ['pending-100'],
                         'the storm left %r (one event per table is the contract)' % (self.pending(),))
        self.assertEqual(sorted(p.name for p in (self.case / 'state').glob('pending-100.*')), [],
                         'a per-process temp file survived in the queue')

    def test_a_busy_worker_lock_leaves_the_pending_event_alone(self):
        self.write('tbl-100', '')
        self.drain()  # nothing pending: must be a cheap no-op on any implementation
        self.hold('routes-worker')
        self.write('state/pending-100', '1\n')
        result = self.worker()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.pending(), ['pending-100'],
                         'the second worker consumed an event it does not own')

    def test_no_ip_or_uci_or_sleep_in_the_netifd_path(self):
        text = HOTPLUG.read_text(encoding='utf-8')
        code = '\n'.join(line for line in text.splitlines()
                         if not line.lstrip().startswith('#'))
        self.assertNotRegex(code, r'(?m)^\s*ip\s', 'the hotplug path still runs ip itself')
        self.assertNotRegex(code, r'\$\(\s*ip\s', 'the hotplug path runs ip in $()')
        self.assertNotRegex(code, r'`\s*ip\s', 'the hotplug path runs ip in backticks')
        self.assertNotRegex(code, r'(?:^|[;&|()])\s*(?:command|env)\s+ip\s',
                            'the hotplug path runs ip through command/env')
        self.assertNotRegex(code, r'(?m)^\s*uci\s', 'the hotplug path still runs uci itself')
        self.assertNotRegex(code, r'\$\(\s*uci\s', 'the hotplug path runs uci in $()')
        self.assertNotRegex(code, r'\bsleep\b', 'the hotplug path still sleeps')
        self.assertNotIn('apply_routes', code)
        self.assertNotIn('sync_table', code)
        self.assertIn('gm_route_enqueue', code)
        self.assertIn('gm_route_worker', code)
        self.assertIn('gm_lock_close_inherited', code)
        self.assertIn('if [ "$_enq_any" = 1 ]', code,
                      'a partial enqueue failure cancels the worker for the written tables')
        self.assertNotIn('exit 2 ;;', code,
                         'the enqueue loop exits before trying the remaining lanes')
        self.assertIn(') >/dev/null 2>&1 &', code,
                      'the worker is not spawned detached (netifd must not wait for it)')
        for name in ('GM_ROUTE_STATE_DIR', 'MIHOMO_CONF', 'DEVICES'):
            self.assertIn(name, code, 'the path %s is not injectable' % name)


class RouteRcContractTests(RouteQueueCase):
    def test_busy_lock_is_75_not_a_generic_failure(self):
        self.hold('route-100')
        result = self.sync_rc('GM_ROUTE_LOCK_WAIT=0; export GM_ROUTE_LOCK_WAIT\n')
        self.assertIn('RC=75', result.stdout, result.stdout + result.stderr)

    def test_unreadable_state_is_76(self):
        (self.case / 'tbl-main').unlink()
        self.write('no-addrs', '')
        result = self.sync_rc()
        self.assertIn('RC=76', result.stdout, result.stdout + result.stderr)

    def test_failed_mutation_is_77(self):
        self.write('fail-replace', '')
        result = self.sync_rc()
        self.assertIn('RC=77', result.stdout, result.stdout + result.stderr)

    def test_missing_lock_primitive_is_2(self):
        if os.name != 'nt':
            self.skipTest('Linux-only: a real flock(2) exists and cannot be removed from PATH')
        stub = self.case / 'stub'
        stub.mkdir()
        (stub / 'uname').write_text('#!/bin/sh\nprintf "Linux\\n"\n', encoding='utf-8',
                                    newline='\n')
        (stub / 'uname').chmod(0o755)
        result = self.sync_rc('PATH=%s:$PATH\n'
                              'export PATH\n' % q(posix(stub)))
        self.assertIn('RC=2', result.stdout, result.stdout + result.stderr)

    def test_an_unusable_lock_directory_is_retryable_76(self):
        """rc=2 from the helper has two meanings: missing primitive (2) vs ENOSPC/EACCES (76).

        The backend exists here; the lock directory cannot be created because a regular file
        is in the way. That is transient for the caller (retry), not a fail-closed 2 and not a
        park.
        """
        self.write('blocker', 'not a directory\n')
        result = self.sync_rc('GM_LOCK_DIR="$CASE/blocker/locks"\n'
                              'export GM_LOCK_DIR\n')
        self.assertIn('RC=76', result.stdout, result.stdout + result.stderr)


class WorkerRetryTests(RouteQueueCase):
    def test_77_is_never_retried_and_parks(self):
        self.queue()
        override = ('geomask_routes_sync() { printf x >> "$CASE/attempts"; return 77; }\n')
        result = self.worker(mode='drain', override=override)
        self.assertIn('RC=77', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 1,
                         'a 77 (failed mutation) was retried: %d' % self.attempts())
        self.assertEqual(self.parked(), ['parked-100'])
        error = (self.case / 'state' / 'error-100').read_text(encoding='utf-8')
        self.assertIn('77', error)
        self.assertEqual(self.pending(), [])

    def test_bounded_retry_parks_after_three_activations(self):
        self.queue()
        override = ('geomask_routes_sync() { printf x >> "$CASE/attempts"; return 75; }\n')
        result = self.worker('GM_ROUTE_BACKOFF="0 0 0 0"; export GM_ROUTE_BACKOFF\n',
                             mode='worker', override=override)
        self.assertIn('RC=75', result.stdout, result.stdout + result.stderr)
        attempts = self.attempts()
        self.assertEqual(attempts, 15, 'expected 5 attempts x 3 activations, got %d' % attempts)
        self.assertEqual(self.parked(), ['parked-100'])
        error = (self.case / 'state' / 'error-100').read_text(encoding='utf-8')
        self.assertIn('attempts=%d' % attempts, error)

    def test_75_then_success_clears_work_and_error(self):
        self.queue()
        override = (
            'geomask_routes_sync() {\n'
            '  printf x >> "$CASE/attempts"\n'
            '  _n=$(wc -c < "$CASE/attempts")\n'
            '  [ "$_n" -ge 2 ] && return 0\n'
            '  return 75\n'
            '}\n')
        result = self.worker('GM_ROUTE_BACKOFF="0 0 0 0"; export GM_ROUTE_BACKOFF\n',
                             mode='worker', override=override)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 2)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.parked(), [])
        self.assertFalse((self.case / 'state' / 'work-100').exists(),
                         'work-100 was left behind after success')


class DrainFairnessTests(RouteQueueCase):
    """R05 review: one drain call must serve EVERY pending table, not just the first."""

    def all_lane_tuns(self):
        self.write('tun-AI', '')
        self.write('tun-Console', '')

    def test_one_drain_applies_tables_100_101_and_102(self):
        self.all_lane_tuns()
        self.write('devices.list', '192.168.1.50 ai phone\n')
        for table in (100, 101, 102):
            self.write('state/pending-%d' % table, 'gen-%d\n' % table)
        result = self.drain()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('default dev Mihomo', self.read('tbl-100'))
        self.assertIn('default dev AI', self.read('tbl-101'))
        self.assertIn('default dev Console', self.read('tbl-102'))
        rules = '\n'.join(self.read('rules'))
        self.assertIn('32760', rules)
        self.assertIn('from 192.168.1.50 lookup 101', rules)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.parked(), [])
        self.assertEqual(sorted(p.name for p in (self.case / 'state').glob('work-*')), [])

    def test_a_parked_table_does_not_block_the_other_tables(self):
        self.all_lane_tuns()
        for table in (100, 101, 102):
            self.write('state/pending-%d' % table, 'gen-%d\n' % table)
        override = (
            'geomask_routes_sync() {\n'
            '  case "$1" in\n'
            '    101) printf x >> "$CASE/attempts"; return 77 ;;\n'
            '    *) return 0 ;;\n'
            '  esac\n'
            '}\n')
        result = self.worker(mode='drain', override=override)
        self.assertIn('RC=77', result.stdout, result.stdout + result.stderr)
        self.assertIn('default dev Mihomo', self.read('tbl-100'))
        self.assertIn('default dev Console', self.read('tbl-102'))
        self.assertEqual(self.parked(), ['parked-101'],
                         'the hard-failed table was not parked: %r' % (self.parked(),))
        self.assertTrue((self.case / 'state' / 'error-101').exists())
        self.assertEqual(self.pending(), [], 'the other tables were left pending')
        self.assertEqual(self.attempts(), 1, 'a 77 was retried inside a drain')

    def test_generation_recheck_processes_a_new_event_for_the_same_table(self):
        self.queue(100, 'gen-1')
        override = (
            'geomask_routes_sync() {\n'
            '  printf x >> "$CASE/attempts"\n'
            '  _n=$(wc -c < "$CASE/attempts")\n'
            '  [ "$_n" = 1 ] && gm_route_enqueue 100\n'
            '  return 0\n'
            '}\n')
        result = self.worker('GM_ROUTE_BACKOFF="0 0 0 0"; export GM_ROUTE_BACKOFF\n',
                             mode='worker', override=override)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.attempts(), 2,
                         'a pending event written during the sync was not processed')
        self.assertEqual(self.pending(), [])

    def test_drain_prunes_stale_per_process_temps_only(self):
        now = time.time()
        stale_pending = self.write('state/pending-100.999', 'old\n')
        stale_want = self.write('state/want-100.999', 'old\n')
        for path in (stale_pending, stale_want):
            os.utime(path, (now - 120, now - 120))
        live_temp = self.write('state/pending-100.777', 'live\n')
        result = self.drain()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertFalse(stale_pending.exists(), 'a stale pending temp survived the drain')
        self.assertFalse(stale_want.exists(), 'a stale sync temp survived the drain')
        self.assertTrue(live_temp.exists(), 'a fresh temp from a live writer was deleted')


class DrainWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = GUARD.read_text(encoding='utf-8')
        cls.template = TEMPLATE.read_text(encoding='utf-8')

    def test_guard_drains_outside_the_console_lock(self):
        self.assertIn('gm_route_drain', self.guard,
                      'the guard/cron path never drains the queue')
        drain_at = self.guard.index('gm_route_drain')
        console_at = self.guard.index('if console_lock; then')
        self.assertLess(drain_at, console_at,
                        'the guard drains while it may already hold the console lock')

    def test_installer_drains_inline_under_maintenance(self):
        self.assertIn('gm_route_drain', self.template)
        acquire = self.template.index('gm_lock_acquire maintenance 0')
        steps = self.template.index('if ! _reinstall_steps "$_vpn"; then', acquire)
        drain = self.template.index('drain_route_queue', steps)
        # The success path releases maintenance after removing the snapshot; the earlier
        # releases belong to failure branches that must not run the drain.
        success = self.template.index('rm -rf "$_snap"', steps)
        release = self.template.index('gm_lock_release maintenance', success)
        self.assertLess(drain, release,
                        'the installer drain is not inside the maintenance section')
        # Inline means inline: the installer must not spawn a worker that would outlive the
        # maintenance section (and a detached `&` would do exactly that).
        helper = self.template[self.template.index('drain_route_queue() {'):]
        helper = helper[:helper.index('\n}\n')]
        self.assertNotIn('gm_route_worker', helper)
        self.assertNotRegex(helper, r'(?m)\s&\s*$',
                            'the installer drain runs a detached background job')
        # The installer keeps reading the error marker the worker writes.
        self.assertIn('error-100', self.template)


class KernelDescriptorTests(RouteQueueCase):
    def test_worker_does_not_keep_the_console_lock_of_its_parent(self):
        """Linux-only: flock is held by the inherited descriptor, not by the shell."""
        if os.name == 'nt':
            self.skipTest('Linux-only: flock(2) descriptor inheritance; the portable test '
                          'backend has no kernel lock and is never used on the router')
        self.write('state/pending-100', '1\n')
        script = self.write('fd-holder.sh', self.env_prelude() + (
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire console 0 || exit 9\n'
            '. "$GM_ROUTES_LIB"\n'
            'command -v gm_route_worker >/dev/null 2>&1 || exit 8\n'
            '_gm_route_activate() { sleep 5; return 0; }\n'
            'gm_route_worker >/dev/null 2>&1 &\n'
            'printf spawned > "$CASE/spawned"\n'
            'exit 0\n'))
        result = subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.case / 'spawned').exists())
        time.sleep(0.5)
        probe = self.write('probe.sh', self.env_prelude() + (
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire console 0; printf "RC=%s\\n" "$?"\n'
            'gm_lock_release_all\n'))
        result = subprocess.run([BASH, posix(probe)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertIn('RC=0', result.stdout,
                      'the detached worker kept its parent console descriptor')


if __name__ == '__main__':
    unittest.main()
