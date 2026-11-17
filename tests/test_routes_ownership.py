"""Route ownership and the one-writer-per-table lock.

Defect (plan 21): the sync compared the *desired* line with the owned file and deleted by
prefix. So when our own route on a prefix changed A->B, B was skipped (it was not in
`owned` yet) and A was removed by `ip route del table N <prefix>`, which cannot tell two
entries of the same prefix apart - a foreign route on that prefix went with it.

Defect (plan 22, N01): the owned file was written with no lock. The hotplug path and the panel
can fire together, both read the same baseline and the slower one overwrote the faster one's
prefixes; a prefix left in the table without an owner can never be cleaned up again.

The helper is run as shipped against a stateful `ip` stub. The stub models the pessimistic
reading of iproute2: `ip route del <prefix>` drops every entry of that prefix, while
`ip route del <prefix> dev ... via ...` drops exactly one line. That is precisely why the
helper must never delete by prefix, so the tests assert on the recorded calls as well.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'assets' / 'geomask-routes.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()

STUBS = r'''uci() {
  case "$1" in -q) shift ;; esac
  case "$1" in
    show)
      case "$2" in
        firewall)
          printf 'firewall.@zone[0]=zone\nfirewall.@zone[0].name=lan\nfirewall.@zone[0].network=lan\n'
          return 0 ;;
      esac
      return 1 ;;
    get)
      case "$2" in
        firewall.*.name) printf 'lan\n'; return 0 ;;
        firewall.*.network) printf 'lan\n'; return 0 ;;
        network.lan.device) printf 'br-lan\n'; return 0 ;;
        network.lan.ifname) return 1 ;;
      esac
      return 1 ;;
  esac
  return 1
}

ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$1" in
    -4)                     # ip -4 addr show <dev>
      [ -f "$CASE/no-addrs" ] && return 0
      printf '    inet 192.168.1.1/24 brd 192.168.1.255 scope global %s\n' "$4"
      return 0 ;;
    link) return 1 ;;       # no Wireguard interface in this case
    route) ;;
    *) return 0 ;;
  esac
  shift
  _cmd=$1; shift
  _tbl=main
  while [ $# -gt 0 ]; do
    case "$1" in
      table) _tbl=$2; shift 2 ;;
      *) break ;;
    esac
  done
  _f="$CASE/tbl-$_tbl"
  # A second process may describe its own `main`: the shared files stay shared.
  if [ "$_tbl" = main ] && [ -n "${MAIN_FILE:-}" ]; then _f=$MAIN_FILE; fi
  case "$_cmd" in
    show)
      if [ "$_tbl" = main ] && [ -f "$CASE/no-main" ]; then return 1; fi
      if [ -f "$_f" ]; then cat "$_f"; fi
      return 0 ;;
    replace)
      _line=$*
      awk -v p="${_line%% *}" '$1 != p' "$_f" > "$_f.new" 2>/dev/null
      mv -f "$_f.new" "$_f"
      printf '%s\n' "$_line" >> "$_f"
      return 0 ;;
    del)
      _line=$*
      case "$_line" in
        *' '*)
          grep -vxF "$_line" "$_f" > "$_f.new"
          mv -f "$_f.new" "$_f"
          printf 'full: %s\n' "$_line" >> "$CASE/deleted" ;;
        *)
          awk -v p="$_line" '$1 != p' "$_f" > "$_f.new"
          mv -f "$_f.new" "$_f"
          printf 'BARE-PREFIX: %s\n' "$_line" >> "$CASE/deleted" ;;
      esac
      return 0 ;;
  esac
  return 0
}
'''

LAN_CONNECTED = '192.168.1.0/24 dev br-lan proto kernel scope link src 192.168.1.1'


@unittest.skipIf(BASH is None or not LIB.exists(), 'bash and the routes helper are required')
class _RoutesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'calls.log').write_text('', encoding='utf-8')
        (self.case / 'deleted').write_text('', encoding='utf-8')
        (self.case / 'stubs.sh').write_text(STUBS, encoding='utf-8', newline='\n')

    def write(self, name, lines):
        (self.case / name).write_text(''.join(line + '\n' for line in lines),
                                      encoding='utf-8', newline='\n')

    def read(self, name):
        path = self.case / name
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def table(self):
        return self.read('tbl-102')

    def owned(self):
        return self.read('state/owned-102')

    def calls(self):
        return self.read('calls.log')

    def state_dir(self):
        state = self.case / 'state'
        state.mkdir(exist_ok=True)
        return state

    def install(self, main, installed, owned):
        self.write('tbl-main', main)
        self.write('tbl-102', installed)
        owned_file = self.state_dir() / 'owned-102'
        if owned is None:
            owned_file.unlink(missing_ok=True)
        else:
            owned_file.write_text(''.join(line + '\n' for line in owned),
                                  encoding='utf-8', newline='\n')

    def script(self, name='run.sh', lock_wait=None, main_file=None, before=''):
        lines = [
            'set -u',
            'CASE="%s"' % self.case.as_posix(),
            'export CASE',
        ]
        if main_file is not None:
            lines.append('MAIN_FILE="%s"; export MAIN_FILE' % main_file.as_posix())
        lines += [
            '. "$CASE/stubs.sh"',
            'GM_ROUTE_STATE_DIR="$CASE/state"',
            'export GM_ROUTE_STATE_DIR',
            'GM_LOCK_DIR="$CASE/locks"',
            'export GM_LOCK_DIR',
            'GM_LOCK_LIB="%s"' % (ROOT / 'assets' / 'geomask-lock.sh').as_posix(),
            'export GM_LOCK_LIB',
            '. "$GM_LOCK_LIB"',
        ]
        if lock_wait is not None:
            lines.append('GM_ROUTE_LOCK_WAIT=%s; export GM_ROUTE_LOCK_WAIT' % lock_wait)
        if before:
            lines.append(before)
        lines += [
            '. "%s"' % LIB.as_posix(),
            'geomask_routes_sync 102',
            'printf "RC=%s\\n" "$?"',
        ]
        path = self.case / name
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        return path

    def start(self, path):
        return subprocess.Popen([BASH, path.as_posix()], cwd=str(self.case),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8')

    def run_sync(self, main, installed, owned, lock_wait=None, main_file=None, before=''):
        self.install(main, installed, owned)
        script = self.script(lock_wait=lock_wait, main_file=main_file, before=before)
        return subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=120)

    def rc(self, result):
        self.assertIn('RC=', result.stdout, result.stdout + result.stderr)
        return int(result.stdout.rsplit('RC=', 1)[1].split()[0])

    def lock_file(self):
        return self.case / 'locks' / 'route-102.lock'

    def hold_lock(self):
        """A real second process owns the table lock through the shipped helper.

        R04: the lock is the persistent kernel lock of geomask-lock.sh; nothing is written into
        the state directory and no pid is involved. On the Windows test host the helper selects
        its documented portable (O_EXCL) backend, where the file itself is the lock.
        """
        self.case.joinpath('locks').mkdir(exist_ok=True)
        script = self.case / 'hold.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire route-102 0 || exit 9\n'
            'printf held > %s\nsleep 30\n'
            % (self.case.joinpath('locks').as_posix(),
               (ROOT / 'assets' / 'geomask-lock.sh').as_posix(),
               (self.case / 'held').as_posix()),
            encoding='utf-8', newline='\n')
        holder = subprocess.Popen([BASH, script.as_posix()], cwd=str(self.case))
        self.addCleanup(holder.kill)
        for _ in range(200):
            if (self.case / 'held').exists():
                break
            time.sleep(0.05)
        self.assertTrue((self.case / 'held').exists(), 'the lock holder never started')
        return holder

    def lock_is_free(self):
        script = self.case / 'probe.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire route-102 0; printf "RC=%%s\\n" "$?"\n'
            'gm_lock_release_all\n'
            % (self.case.joinpath('locks').as_posix(),
               (ROOT / 'assets' / 'geomask-lock.sh').as_posix()),
            encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        return 'RC=0' in result.stdout


class RoutesOwnershipTests(_RoutesCase):
    def test_a_changed_own_route_replaces_itself(self):
        """A->B: B is installed first, then A goes; nothing is skipped or left behind."""
        old = '10.0.0.0/24 via 192.168.1.2 dev br-lan'
        new = '10.0.0.0/24 via 192.168.1.3 dev br-lan'
        result = self.run_sync(main=[LAN_CONNECTED, new], installed=[old], owned=[old])
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertEqual(sorted(self.table()), sorted([LAN_CONNECTED, new]))
        self.assertNotIn(old, self.table())

    def test_a_foreign_route_on_our_prefix_survives(self):
        """Our record is stale and the prefix now belongs to somebody else: do not touch it."""
        stale = '10.0.0.0/24 dev br-lan'
        foreign = '10.0.0.0/24 dev eth9'
        result = self.run_sync(main=[LAN_CONNECTED], installed=[foreign], owned=[stale])
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertIn(foreign, self.table())
        self.assertEqual(sorted(self.table()), sorted([LAN_CONNECTED, foreign]))
        self.assertFalse([line for line in self.read('deleted') if line.startswith('BARE-PREFIX')],
                         self.read('deleted'))

    def test_a_foreign_occupant_is_reported_not_overwritten(self):
        """A wanted prefix taken by a foreign route: keep it, refuse to install, rc=77 (R05).

        77 is the "mutation failed / ownership conflict" code: the queue must PARK the event
        instead of retrying - a retry would repeat the same replace against the same foreign
        occupant (R12 owns reconciliation).
        """
        wanted = '10.0.0.0/24 via 192.168.1.3 dev br-lan'
        foreign = '10.0.0.0/24 via 9.9.9.9 dev eth9'
        result = self.run_sync(main=[LAN_CONNECTED, wanted], installed=[foreign], owned=None)
        self.assertEqual(self.rc(result), 77, 'a table that does not match `desired` must be 77')
        self.assertIn(foreign, self.table())
        self.assertNotIn(wanted, self.table())

    def test_a_stale_own_route_is_removed_by_its_full_line(self):
        """The delete path still works for our own entry, and it names the whole line."""
        stale = '10.0.0.0/24 dev br-lan'
        result = self.run_sync(main=[LAN_CONNECTED], installed=[stale], owned=[stale])
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertEqual(self.table(), [LAN_CONNECTED])
        self.assertIn('full: ' + stale, self.read('deleted'))
        self.assertFalse([line for line in self.read('deleted') if line.startswith('BARE-PREFIX')],
                         self.read('deleted'))


class RoutesLockTests(_RoutesCase):
    """#22: one writer per table, and a busy table is left alone instead of being half-synced."""

    def test_a_busy_table_is_left_alone(self):
        """A live sync owns the table: this one must not touch the table or the owned file."""
        wanted = '10.0.0.0/24 via 192.168.1.3 dev br-lan'
        self.install([LAN_CONNECTED, wanted], [LAN_CONNECTED], [LAN_CONNECTED])
        self.hold_lock()
        result = self.run_sync(main=[LAN_CONNECTED, wanted], installed=[LAN_CONNECTED],
                               owned=[LAN_CONNECTED], lock_wait=1)
        self.assertEqual(self.rc(result), 75, 'a busy table must be reported as retryable (75)')
        self.assertEqual(self.table(), [LAN_CONNECTED], 'the table was changed by the loser')
        self.assertEqual(self.owned(), [LAN_CONNECTED], 'the owned file was overwritten')
        self.assertFalse([call for call in self.calls() if call.startswith('route replace')],
                         self.calls())
        self.assertTrue(self.lock_file().exists(),
                        'the lock of the other process was removed')

    def test_a_legacy_directory_lock_no_longer_blocks_the_table(self):
        """Migration: the old mkdir lock is a different path and is neither used nor deleted."""
        dead = subprocess.Popen([BASH, '-c', 'exit 0'], cwd=str(self.case))
        dead.wait()
        legacy = self.state_dir() / 'lock-102'
        legacy.mkdir()
        (legacy / 'pid').write_text('%s\n' % dead.pid, encoding='utf-8')
        result = self.run_sync(main=[LAN_CONNECTED], installed=[], owned=None, lock_wait=3)
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertIn(LAN_CONNECTED, self.table())
        self.assertTrue(legacy.exists(), 'the new code deleted a legacy lock it does not own')

    def test_the_worker_rank_sits_between_config_tx_and_the_table(self):
        """R05: routes-worker(35) nests inside config-tx(30) and outside route-<n>(40).

        No inversion: a worker may take the table lock, but a table writer may not take the
        worker lock; config-tx -> worker is the panel/transaction -> queue direction.
        """
        script = self.case / 'ranks.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\n'
            'GM_CONFIG_TX_LOCK=%s\n'
            'GM_LOCK_LIB=%s\n'
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire config-tx 0; printf "TX=%%s\\n" "$?"\n'
            'gm_lock_acquire routes-worker 0; printf "W=%%s\\n" "$?"\n'
            'gm_lock_acquire route-100 0; printf "T=%%s\\n" "$?"\n'
            'gm_lock_release_all\n'
            'gm_lock_acquire route-100 0; printf "T2=%%s\\n" "$?"\n'
            'gm_lock_acquire routes-worker 0; printf "W2=%%s\\n" "$?"\n'
            'gm_lock_release_all\n'
            % (self.case.joinpath('locks').as_posix(),
               self.case.joinpath('locks', 'tx.lock').as_posix(),
               (ROOT / 'assets' / 'geomask-lock.sh').as_posix()),
            encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertIn('TX=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('W=0', result.stdout, 'routes-worker did not nest inside config-tx')
        self.assertIn('T=0', result.stdout, 'route-100 did not nest inside routes-worker')
        self.assertIn('T2=0', result.stdout)
        self.assertIn('W2=4', result.stdout,
                      'the table writer was allowed to take the worker lock (rank inversion)')

    def test_the_lock_is_released_after_a_successful_run(self):
        result = self.run_sync(main=[LAN_CONNECTED], installed=[], owned=None)
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertTrue(self.lock_is_free(), 'the table lock is still held after a successful run')

    def test_the_lock_is_released_after_a_failed_run(self):
        """Every early exit gives the lock back; otherwise the table stays blocked for good."""
        (self.case / 'no-addrs').write_text('', encoding='utf-8')
        (self.case / 'no-main').write_text('', encoding='utf-8')
        result = self.run_sync(main=[], installed=[], owned=None)
        self.assertEqual(self.rc(result), 76,
                         'an unreadable table/state is a retryable read failure (76)')
        self.assertTrue(self.lock_is_free(), 'a failed sync kept the table lock')

    def test_two_syncs_do_not_lose_each_other_prefixes(self):
        """Two writers, one table: the owned file must end up describing the real table.

        Each process sees a different `main`, so without the lock the second one overwrites the
        owned file with its own view and the first one's prefix stays installed but unowned.
        """
        first = '10.0.0.0/24 via 192.168.1.4 dev br-lan'
        second = '10.1.0.0/24 via 192.168.1.5 dev br-lan'
        self.install([LAN_CONNECTED], [], None)
        main_first = self.case / 'main-first'
        main_second = self.case / 'main-second'
        main_first.write_text(LAN_CONNECTED + '\n' + first + '\n', encoding='utf-8', newline='\n')
        main_second.write_text(LAN_CONNECTED + '\n' + second + '\n', encoding='utf-8', newline='\n')
        a = self.start(self.script('run-a.sh', lock_wait=60, main_file=main_first))
        time.sleep(0.2)
        b = self.start(self.script('run-b.sh', lock_wait=60, main_file=main_second))
        out_a, err_a = a.communicate(timeout=180)
        out_b, err_b = b.communicate(timeout=180)
        self.assertIn('RC=0', out_a, out_a + err_a)
        self.assertIn('RC=0', out_b, out_b + err_b)
        self.assertEqual(sorted(self.owned()), sorted(self.table()),
                         'the owned file does not describe the installed table: %r vs %r'
                         % (self.owned(), self.table()))
        self.assertIn(second, self.table())


if __name__ == '__main__':
    unittest.main()
