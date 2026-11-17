"""R04: one kernel lock helper, no reclaim, fixed order, fail-closed.

The defect: three copies of `mkdir lock; write pid; rm -rf` (route table, console lane in the
backend, console lane in the guard). Two writers that read the same dead pid both delete the
lock; the second one deletes the lock the first one has just created and both enter the
critical section. A lock with no pid yet is reclaimed too ("ownerless window"), and a slow
writer's release can delete the new owner's lock ("unlock after takeover"). A reused pid
blocks the lane forever. Node-scan locked a *different* file than the config transaction, and
the transaction ran recovery before taking its own lock.

The contract implemented by assets/geomask-lock.sh:
  * one persistent lock file per name, kernel flock(2) on it, file never deleted/recreated;
  * no pid, no owner guessing, no stale reclaim - a killed holder is released by the kernel;
  * fixed rank order enforced at run time, bounded wait, genuine no-wait mode;
  * fail-closed on Linux when the primitive is missing (no mkdir fallback).
Python uses fcntl.flock on the same paths (geomask-config-tx.py, node-scan, torrent).

What these tests can decide on the Windows host: refusal of a second writer at no-wait, the
wait budget, wrong-order and re-entrant refusal, fail-closed selection, the hotplug try-lock,
and the static contract (no reclaim code, same lock paths, recovery under the lock, no
unchecked skip switch). What they cannot: flock(2) semantics, release after SIGKILL, inode
identity across processes, Python<->shell exclusion on one inode, descriptor inheritance.
Those are marked Linux-only and skipped here - a green run on Windows is not proof of the
Linux runtime.
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
BACKEND = ASSETS / 'geo-backend.sh'
GUARD = ASSETS / 'geomask-guard.sh'
TX = ASSETS / 'geomask-config-tx.py'
NODESCAN = ASSETS / 'geomask-node-scan.sh'
HOTPLUG = ASSETS / '99-default-vpn-route'
TEMPLATE = ROOT / 'geomask.sh.in'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()

UNIX_ONLY = unittest.skipUnless(
    BASH is not None and os.name != 'nt',
    'Linux-only: kernel flock(2), SIGKILL release, /proc and inode identity are not the '
    'Windows test host (a Windows pass is not proof of the Linux runtime)')


def posix(path):
    return Path(path).as_posix()


@unittest.skipIf(BASH is None, 'bash is required')
class LockHelperTests(unittest.TestCase):
    """Behaviour of the shipped helper in a throwaway lock directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.locks = self.case / 'locks'

    def script(self, name, body):
        path = self.case / name
        path.write_text(
            'set -u\n'
            'CASE=%s\n'
            'GM_LOCK_DIR=%s\n'
            'GM_CONFIG_TX_LOCK=%s\n'
            'GM_LOCK_LIB=%s\n'
            '. "$GM_LOCK_LIB"\n' % (sh_quote(posix(self.case)), sh_quote(posix(self.locks)),
                                   sh_quote(posix(self.locks / 'tx.lock')),
                                   sh_quote(posix(HELPER))) + body,
            encoding='utf-8', newline='\n')
        return path

    def run_script(self, name, body, timeout=60):
        path = self.script(name, body)
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=timeout)

    def holder(self):
        """A live process that holds `console` for 5 s (real helper, not a simulation)."""
        path = self.script('holder.sh',
                           'gm_lock_acquire console 0 || { echo NOLOCK > "$CASE/holder.rc"; exit 9; }\n'
                           'printf held > "$CASE/held"\n'
                           'sleep 5\n'
                           'gm_lock_release console\n')
        proc = subprocess.Popen([BASH, posix(path)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        marker = self.case / 'held'
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists(), 'the holder never acquired the lock')
        return proc

    def test_a_second_writer_is_refused_at_no_wait(self):
        self.holder()
        result = self.run_script('second.sh',
                          'gm_lock_acquire console 0; printf "RC=%s\\n" "$?"\n')
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)

    def test_the_wait_budget_is_bounded_and_no_wait_is_immediate(self):
        self.holder()
        # Measure in Python: the shell `date +%s` integer-second delta is too coarse - three
        # process spawns can cross a second boundary on a loaded host, which made this test
        # flaky (false RED on slow CI/Linux). The contract is "bounded wait actually waits"
        # and "no-wait does not spend the budget", not "exactly 0 seconds".
        started = time.monotonic()
        waited = self.run_script('waiter.sh',
                                 'gm_lock_acquire console 1; printf "RC=%s\\n" "$?"\n')
        waited_for = time.monotonic() - started
        self.assertIn('RC=1', waited.stdout, waited.stdout + waited.stderr)
        self.assertGreaterEqual(waited_for, 0.9, 'the bounded wait did not wait')
        started = time.monotonic()
        nowait = self.run_script('nowait.sh',
                                 'gm_lock_acquire console 0; printf "RC=%s\\n" "$?"\n')
        no_wait_for = time.monotonic() - started
        self.assertIn('RC=1', nowait.stdout, nowait.stdout + nowait.stderr)
        # no-wait must not sit out the budget; keep a wide margin for process spawn under load
        self.assertLess(no_wait_for, 0.9,
                        'no-wait spent the whole budget (%.2fs)' % no_wait_for)

    def test_a_busy_attempt_does_not_touch_the_lock_file(self):
        # The ownerless window is impossible by construction: a failed attempt must not create,
        # rename or remove anything under the lock name of another writer.
        self.holder()
        lock = self.locks / 'console.lock'
        before = (lock.exists(), lock.stat().st_size if lock.exists() else None)
        result = self.run_script('second.sh', 'gm_lock_acquire console 0; printf "RC=%s\\n" "$?"\n')
        self.assertIn('RC=1', result.stdout)
        after = (lock.exists(), lock.stat().st_size if lock.exists() else None)
        self.assertEqual(before, after, 'a refused acquisition changed the lock file')

    def test_wrong_order_and_reentrancy_are_refused(self):
        result = self.run_script('order.sh',
                          'gm_lock_acquire route-102 0; printf "R1=%s\\n" "$?"\n'
                          'gm_lock_acquire console 0; printf "R2=%s\\n" "$?"\n'
                          'gm_lock_acquire route-102 0; printf "R3=%s\\n" "$?"\n'
                          'gm_lock_release_all\n')
        self.assertIn('R1=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('R2=4', result.stdout, 'downward lock nesting was not refused')
        self.assertIn('R3=3', result.stdout, 're-entrant acquisition was not refused')
        combined = result.stdout + result.stderr
        self.assertIn('lock order violation', combined)
        self.assertIn('already holds', combined)

    def test_routes_worker_nests_between_config_tx_and_the_table(self):
        """R05: the queue worker is rank 35 - inside config-tx(30), outside route-<n>(40)."""
        result = self.run_script('ranks.sh',
                          'gm_lock_acquire config-tx 0; printf "A=%s\\n" "$?"\n'
                          'gm_lock_acquire routes-worker 0; printf "B=%s\\n" "$?"\n'
                          'gm_lock_acquire route-100 0; printf "C=%s\\n" "$?"\n'
                          'gm_lock_release_all\n'
                          'gm_lock_acquire route-100 0; printf "D=%s\\n" "$?"\n'
                          'gm_lock_acquire routes-worker 0; printf "E=%s\\n" "$?"\n'
                          'gm_lock_release_all\n')
        self.assertIn('A=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('B=0', result.stdout, 'routes-worker did not nest inside config-tx')
        self.assertIn('C=0', result.stdout, 'route-100 did not nest inside routes-worker')
        self.assertIn('D=0', result.stdout)
        self.assertIn('E=4', result.stdout, 'a table writer took the queue-worker lock')

    def test_a_missing_primitive_fails_closed_on_linux(self):
        # A stripped PATH whose `uname` says Linux and which has no `flock`: every acquisition
        # must return 2 and nothing may be created. There is no mkdir fallback to fall back to.
        stub = self.case / 'stub'
        stub.mkdir()
        (stub / 'uname').write_text('#!/bin/sh\nprintf "Linux\\n"\n', encoding='utf-8',
                                    newline='\n')
        (stub / 'uname').chmod(0o755)
        path = self.script('missing.sh',
                           'PATH=%s\n'
                           'export PATH\n'
                           'gm_lock_acquire console 0; printf "RC=%%s\\n" "$?"\n'
                           'printf "FILES=%%s\\n" "$(ls -A "$GM_LOCK_DIR" 2>/dev/null | tr "\\n" " ")"\n'
                           % sh_quote(posix(stub)))
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertIn('RC=2', result.stdout, result.stdout + result.stderr)
        self.assertIn('FILES=', result.stdout)
        files = result.stdout.rsplit('FILES=', 1)[1].strip()
        self.assertEqual('', files, 'fail-closed still created a lock artefact: ' + files)

    def test_acquire_and_release_do_not_delete_the_lock_file(self):
        result = self.run_script('lifecycle.sh',
                          'gm_lock_acquire console 0; printf "A1=%s\\n" "$?"\n'
                          'printf "EXISTS=%s\\n" "$([ -f "$GM_LOCK_DIR/console.lock" ] && echo 1 || echo 0)"\n'
                          'gm_lock_release console\n'
                          'printf "EXISTS_AFTER=%s\\n" "$([ -f "$GM_LOCK_DIR/console.lock" ] && echo 1 || echo 0)"\n'
                          'gm_lock_acquire console 0; printf "A2=%s\\n" "$?"\n'
                          'gm_lock_release_all\n')
        self.assertIn('A1=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('A2=0', result.stdout)
        # On the portable (non-Linux test) backend the file is the lock itself and is removed on
        # release; the persistent-inode contract is a flock(2) property and is asserted on Linux.
        if os.name == 'nt':
            return
        self.assertIn('EXISTS_AFTER=1', result.stdout,
                      'the lock file was deleted on release - the inode must stay')


@unittest.skipIf(BASH is None, 'bash is required')
class HotplugNoWaitTests(unittest.TestCase):
    """R05: the ifup entrypoint accepts the event and returns at once, with no `ip` of its own.

    The old test extracted only the `sync_table` block and drove it directly; that stayed green
    while the top-level script exited 0 after losing the event (R05 D10). This test runs the
    REAL `/etc/hotplug.d/iface/99-default-vpn-route` against a busy route-100.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'state').mkdir()
        (self.case / 'locks').mkdir()
        (self.case / 'calls.log').write_text('', encoding='utf-8')
        (self.case / 'tbl-100').write_text('', encoding='utf-8')
        (self.case / 'stubs.sh').write_text(r'''
uci() {
  case "$1" in -q) shift ;; esac
  case "$*" in
    'show firewall') printf 'firewall.@zone[0]=zone\nfirewall.@zone[0].name=lan\nfirewall.@zone[0].network=lan\n' ;;
    'get firewall.@zone[0].name') printf lan ;;
    'get firewall.@zone[0].network') printf lan ;;
    'get network.lan.device') printf br-lan ;;
    'get network.lan.ifname') return 1 ;;
    *) return 1 ;;
  esac
}
ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$*" in
    "link show Mihomo") return 0 ;;
    "link show "*) return 1 ;;
    "route show table main") printf '192.168.1.0/24 dev br-lan scope link\n' ;;
    "route show table 100") return 0 ;;
    "-4 addr show br-lan") printf '    inet 192.168.1.1/24 scope global br-lan\n' ;;
  esac
  return 0
}
logger() { :; }
''', encoding='utf-8', newline='\n')
        (self.case / 'mihomo.yaml').write_text('tun:\n  device: Mihomo\n', encoding='utf-8',
                                               newline='\n')
        (self.case / 'devices.list').write_text('', encoding='utf-8')
        (self.case / 'console.state').write_text('enabled=0\n', encoding='utf-8')

    def hold(self):
        script = self.case / 'hold.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire route-100 0 || exit 9\n'
            'printf held > %s\nsleep 20\n'
            % (sh_quote(posix(self.case / 'locks')), sh_quote(posix(HELPER)),
               sh_quote(posix(self.case / 'held'))),
            encoding='utf-8', newline='\n')
        proc = subprocess.Popen([BASH, posix(script)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return
            time.sleep(0.05)
        self.fail('the route-100 holder never started')

    def test_the_entrypoint_returns_at_once_and_keeps_the_event(self):
        self.hold()
        script = self.case / 'run.sh'
        script.write_text(
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
            'mkdir() { case "$*" in *"/tmp/geomask-routes"*) return 1 ;; esac; command mkdir "$@"; }\n'
            'ACTION=ifup\n'
            '. %s\n'
            % (sh_quote(posix(self.case)), sh_quote(posix(HELPER)), sh_quote(posix(ROUTES)),
               sh_quote(posix(HOTPLUG))),
            encoding='utf-8', newline='\n')
        started = time.monotonic()
        result = subprocess.run([BASH, posix(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, 1.5, 'the ifup path waited for the route lock (%.2fs)' % elapsed)
        self.assertTrue((self.case / 'state' / 'pending-100').exists(),
                        'the busy event was not queued')
        calls = (self.case / 'calls.log').read_text(encoding='utf-8')
        self.assertNotIn('route replace', calls)
        self.assertNotIn('rule add', calls)


class StaticContractTests(unittest.TestCase):
    """The writers must go through the one helper; the unsafe code must be gone."""

    @classmethod
    def setUpClass(cls):
        cls.helper = HELPER.read_text(encoding='utf-8')
        cls.routes = ROUTES.read_text(encoding='utf-8')
        cls.backend = BACKEND.read_text(encoding='utf-8')
        cls.guard = GUARD.read_text(encoding='utf-8')
        cls.tx = TX.read_text(encoding='utf-8')
        cls.nodescan = NODESCAN.read_text(encoding='utf-8')
        cls.template = TEMPLATE.read_text(encoding='utf-8')

    def test_no_reclaim_code_survives(self):
        for name, text in (('helper', self.helper), ('routes', self.routes),
                           ('backend', self.backend), ('guard', self.guard)):
            self.assertNotIn('rm -rf "$CONSOLE_LOCK"', text, name)
            self.assertNotIn('rm -rf "$_gl"', text, name)
            self.assertNotIn('rm -rf "$CLOCK"', text, name)
            self.assertNotRegex(text, r'kill -0\s+"\$_(go|owner|cop|uo|p)"', name)
        # The helper itself may not remove the lock it locked: on flock that would split the
        # inode, on the portable backend only the owner's release removes its own file.
        self.assertNotIn('rm -rf', self.helper)
        self.assertNotIn('unlink', self.helper)

    def test_route_lock_uses_the_helper(self):
        self.assertIn('gm_lock_acquire "route-$1" "$GM_ROUTE_LOCK_WAIT"', self.routes)
        self.assertIn('gm_lock_release "route-$1"', self.routes)
        self.assertNotIn('lock-$1"', self.routes)

    def test_the_rank_table_has_the_route_worker_between_config_tx_and_route(self):
        body = self.helper[self.helper.index('gm_lock_rank()'):]
        body = body[:body.index('\n}\n')]
        ranks = dict(re.findall(r"^\s{4}([a-z*()-]+)\)\s+printf '(\d+)'", body, re.M))
        self.assertEqual(ranks.get('maintenance'), '10')
        self.assertEqual(ranks.get('console'), '20')
        self.assertEqual(ranks.get('cron'), '25')
        self.assertEqual(ranks.get('config-tx'), '30')
        self.assertEqual(ranks.get('routes-worker'), '35')
        self.assertEqual(ranks.get('route-*'), '40')
        self.assertEqual(ranks.get('sync'), '45')
        self.assertEqual(ranks.get('devices'), '60')
        self.assertEqual(ranks.get('subs'), '65')
        ordered = ['maintenance', 'console', 'cron', 'config-tx', 'routes-worker', 'route-*',
                   'sync', 'devices', 'subs']
        self.assertEqual([int(ranks[name]) for name in ordered],
                         sorted(int(ranks[name]) for name in ordered),
                         'the rank table is not strictly increasing')

    def test_console_lock_is_shared_by_backend_and_guard(self):
        self.assertIn('gm_lock_acquire console', self.backend)
        self.assertIn('gm_lock_release console', self.backend)
        self.assertIn('gm_lock_acquire console 0', self.guard,
                      'the guard watchdog must be a no-wait try-lock')
        self.assertIn('gm_lock_release console', self.guard)
        # one lock name, one default path: the backend and the guard must agree
        backend_path = re.search(r'CONSOLE_LOCK=\$\{GM_CONSOLE_LOCK:-([^}]+)\}', self.backend)
        guard_path = re.search(r'CLOCK=\$\{GM_CONSOLE_LOCK:-([^}]+)\}', self.guard)
        self.assertIsNotNone(backend_path)
        self.assertIsNotNone(guard_path)
        self.assertEqual(backend_path.group(1), guard_path.group(1))

    def test_cron_and_devices_writers_take_a_lock(self):
        self.assertIn('gm_lock_acquire cron 20', self.backend)
        self.assertIn('gm_lock_acquire devices 20', self.backend)
        self.assertIn('gm_lock_acquire subs 20', self.backend)
        self.assertIn("$DEVICES.tmp.$$", self.backend)
        self.assertNotIn('> "$DEVICES.tmp"', self.backend)
        self.assertNotIn('> "$SUBS_FILE.tmp"', self.backend)

    def test_updater_is_serialised_by_the_lock_not_a_pid(self):
        self.assertIn('gm_lock_acquire updater 0', self.backend)
        self.assertNotIn('echo $! > "$UPD_PIDF"', self.backend)
        self.assertIsNone(re.search(r'kill -0 "\$_p"', self.backend))

    def test_installer_is_single_instance_and_installs_the_helper(self):
        self.assertIn('gm_lock_acquire maintenance 0', self.template)
        self.assertIn('save_stream /usr/lib/geomask/geomask-lock.sh 644', self.template)
        self.assertIn('/usr/lib/geomask/geomask-lock.sh|regular-or-absent', self.template)
        self.assertIn('@@ASSET:geomask-lock.sh@@', self.template)
        # The very same helper source is injected into the installer (one implementation).
        self.assertGreaterEqual(self.template.count('@@ASSET:geomask-lock.sh@@'), 2)

    def test_no_unchecked_lock_skip_switch(self):
        for path in (HELPER, ROUTES, BACKEND, GUARD, TX, NODESCAN, TEMPLATE):
            text = path.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'GM_SKIP_(LOCK|FLOCK)', path.name)
            self.assertNotRegex(text, r'SKIP_LOCK', path.name)

    def test_transaction_and_node_scan_share_one_lock_path(self):
        self.assertIn("DEFAULT_STATE_DIR / '.config-tx.lock'", self.tx)
        state = (ASSETS / 'geomask-audit-state.py').read_text(encoding='utf-8')
        self.assertIn("'/etc/geomask/node-audit/state'", state)
        self.assertIn('DEFAULT_STATE_DIR = Path(os.environ.get(', state)
        scan = re.search(r"TX_LOCK=_E\.get\('GM_CONFIG_TX_LOCK','([^']+)'\)", self.nodescan)
        self.assertIsNotNone(scan, 'node-scan lock path is not a single default')
        self.assertEqual('/etc/geomask/node-audit/state/.config-tx.lock', scan.group(1))

    def test_recovery_runs_inside_the_transaction_lock(self):
        body = self.tx[self.tx.index('def start_transaction'):]
        lock_at = body.index('with tx_lock()')
        recovery_at = body.index('recover_interrupted_transactions()', lock_at)
        self.assertGreater(recovery_at, lock_at,
                           'recovery is still executed before the transaction lock')
        # and the CLI --recover path takes the lock too
        cli = self.tx[self.tx.index('def main('):]
        self.assertIn('with tx_lock():', cli)

    def test_expected_digest_is_checked_in_the_transaction(self):
        self.assertIn('expect_sha256', self.tx)
        self.assertIn('config changed since it was read (STALE)', self.tx)
        self.assertIn('--expect-sha256', self.tx)
        console = self.backend
        self.assertIn('--expect-sha256', console)
        for name in ('geomask-sync-youtube.py', 'geomask-lane-torrent.py', 'geomask-apply.py'):
            text = (ASSETS / name).read_text(encoding='utf-8')
            self.assertIn('expect_sha256', text, name)

    def test_direct_config_writers_use_the_transaction_lock(self):
        torrent = (ASSETS / 'geomask-torrent.sh').read_text(encoding='utf-8')
        lane = (ASSETS / 'geomask-lane.sh').read_text(encoding='utf-8')
        self.assertIn('/etc/geomask/node-audit/state/.config-tx.lock', torrent)
        self.assertIn('fcntl.flock', torrent)
        self.assertNotIn("open(CFG,'w',encoding='utf-8',newline='').write(new)", torrent)
        self.assertIn('gm_lock_acquire config-tx', lane)
        self.assertIn('GM_LOCK_LIB', lane)

    def test_sync_temp_files_are_per_process(self):
        sync = (ASSETS / 'sync_mtrickle_mihomo.sh').read_text(encoding='utf-8')
        self.assertNotIn("path + '.tmp'", sync)
        self.assertNotIn("ZEXCL + '.tmp'", sync)
        self.assertNotIn("ZFORCE + '.tmp'", sync)
        self.assertIn("os.getpid()", sync)


class KernelSemanticsTests(unittest.TestCase):
    """Linux-only: a green run on the Windows host says nothing about these."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.locks = self.case / 'locks'

    @UNIX_ONLY
    def test_lock_is_released_when_the_holder_is_killed(self):
        script = self.case / 'holder.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\n. %s\ngm_lock_acquire console 0 || exit 9\n'
            'printf held > %s\nsleep 60\n'
            % (sh_quote(posix(self.locks)), sh_quote(posix(HELPER)),
               sh_quote(posix(self.case / 'held'))),
            encoding='utf-8', newline='\n')
        holder = subprocess.Popen([BASH, posix(script)], cwd=str(self.case))
        self.addCleanup(reap, holder)
        marker = self.case / 'held'
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        inode = (self.locks / 'console.lock').stat().st_ino
        holder.kill()
        holder.wait(timeout=10)
        time.sleep(0.2)
        probe = self.case / 'probe.sh'
        probe.write_text(
            'GM_LOCK_DIR=%s\n. %s\ngm_lock_acquire console 0; printf "RC=%%s INODE=%%s\\n" '
            '"$?" "$(stat -c %%i "$GM_LOCK_DIR/console.lock")"\n'
            % (sh_quote(posix(self.locks)), sh_quote(posix(HELPER))),
            encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(probe)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(inode, int(re.search(r'INODE=(\d+)', result.stdout).group(1)),
                         'the lock file was recreated instead of reused')

    @UNIX_ONLY
    def test_python_and_shell_exclude_each_other_on_the_same_file(self):
        import fcntl
        import importlib.util
        spec = importlib.util.spec_from_file_location('tx', TX)
        tx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tx)
        tx.TX_LOCK = self.locks / 'tx.lock'
        self.locks.mkdir()
        holder = self.case / 'holder.sh'
        holder.write_text('sleep 30\n', encoding='utf-8', newline='\n')
        proc = subprocess.Popen([BASH, posix(holder)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        fd = os.open(tx.TX_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            rc = subprocess.run(
                [BASH, '-c',
                 'GM_LOCK_TX_FILE=%s\n. %s\n'
                 'gm_lock_acquire config-tx 0; printf "RC=%%s\\n" "$?"\n'
                 % (sh_quote(posix(tx.TX_LOCK)), sh_quote(posix(HELPER)))],
                capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertIn('RC=1', rc.stdout, rc.stdout + rc.stderr)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def sh_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def reap(proc):
    """Kill and wait: a live child with cwd inside the TemporaryDirectory blocks cleanup."""
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


if __name__ == '__main__':
    unittest.main()
