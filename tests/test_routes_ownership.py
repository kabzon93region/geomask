"""Route ownership, the one-writer-per-table lock, and the R12 ownership reconcile.

Defect (plan 21): the sync compared the *desired* line with the owned file and deleted by
prefix. So when our own route on a prefix changed A->B, B was skipped (it was not in
`owned` yet) and A was removed by `ip route del table N <prefix>`, which cannot tell two
entries of the same prefix apart - a foreign route on that prefix went with it.

Defect (plan 22, N01): the owned file was written with no lock. The hotplug path and the panel
can fire together, both read the same baseline and the slower one overwrote the faster one's
prefixes; a prefix left in the table without an owner can never be cleaned up again.

Defect (plan R12): `_new` was filled only by successful `replace` calls and `cp -f _new owned`
overwrote the record on any failure. A refused replace therefore dropped a live route from the
record; the next sync declared the prefix foreign and never retried. A refused delete left a
"silent orphan": rc 0, no delete, no record. A refused `cp` truncated the record. And a
foreign occupant of our prefix made our own line drop out of the record for good.

The helper is run as shipped against a stateful `ip` stub. The stub models the pessimistic
reading of iproute2: `ip route del <prefix>` drops every entry of that prefix, while
`ip route del <prefix> dev ... via ...` drops exactly one line. That is precisely why the
helper must never delete by prefix, so the tests assert on the recorded calls as well.

R12 specifically: the stub can refuse `replace` (every call, or one prefix), `del`, the read of
the installed table (before and after a mutation) and the write of the record. A real `cp -f`
truncates the destination before it can fail and a rename either replaces the destination or
leaves it alone; both shapes are injectable, and the record must survive either.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'assets' / 'geomask-routes.sh'
LOCK = ROOT / 'assets' / 'geomask-lock.sh'


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
    link)
      # `ip link show <dev>`: the queue/worker path waits for a TUN device, so a case may
      # create one. Everything else - Wireguard included - is absent here.
      [ -n "${3:-}" ] && [ -f "$CASE/tun-$3" ] && return 0
      return 1 ;;
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
      if [ "$_tbl" != main ]; then
        # R12: the read of the installed table is injectable before and after a mutation.
        [ -f "$CASE/fail-show" ] && return 1
        [ -f "$CASE/fail-show-after-mutation" ] && [ -f "$CASE/mutated" ] && return 1
        # A successful but EMPTY read-back after a mutation (the kernel lost our routes).
        [ -f "$CASE/empty-after" ] && [ -f "$CASE/mutated" ] && return 0
      fi
      if [ -f "$_f" ]; then cat "$_f"; fi
      return 0 ;;
    replace)
      [ -f "$CASE/fail-replace" ] && return 1
      _line=$*
      [ -f "$CASE/fail-prefix" ] && grep -qxF "${_line%% *}" "$CASE/fail-prefix" && return 1
      awk -v p="${_line%% *}" '$1 != p' "$_f" > "$_f.new" 2>/dev/null
      mv -f "$_f.new" "$_f"
      if [ -f "$CASE/normalize" ]; then
        # The kernel adds decoration (proto/metric) to the installed form.
        printf '%s proto static metric 10\n' "$_line" >> "$_f"
      else
        printf '%s\n' "$_line" >> "$_f"
      fi
      # R12 D1: another writer adds a route on OUR prefix after the replace but before the
      # read-back. One-shot: the first replace consumes the marker.
      if [ -f "$CASE/race-foreign" ]; then
        cat "$CASE/race-foreign" >> "$_f"
        rm -f "$CASE/race-foreign"
      fi
      : > "$CASE/mutated"
      return 0 ;;
    del)
      [ -f "$CASE/fail-del" ] && return 1
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
      : > "$CASE/mutated"
      return 0 ;;
  esac
  return 0
}

# The write of the ownership record. `cp -f` opens/truncates the destination before it can
# fail; a rename either replaces the destination or leaves it untouched. Both shapes must be
# refused by the marker, because the record must survive a refused write in either
# implementation (R12: the old code was the truncating one).
cp() {
  printf 'write: cp %s\n' "$*" >> "$CASE/calls.log"
  [ -f "$CASE/fail-cp" ] || { command cp "$@"; return $?; }
  _last=
  for _a in "$@"; do _last=$_a; done
  : > "$_last" 2>/dev/null
  return 1
}

mv() {
  printf 'write: mv %s\n' "$*" >> "$CASE/calls.log"
  if [ -f "$CASE/fail-cp" ]; then
    case "$*" in
      *"/owned-"*) return 1 ;;
    esac
  fi
  if [ -f "$CASE/fail-retry-mv" ]; then
    case "$*" in
      *"/retry-"*) return 1 ;;
    esac
  fi
  command mv "$@"
}
'''

LAN_CONNECTED = '192.168.1.0/24 dev br-lan proto kernel scope link src 192.168.1.1'
A = '10.0.0.0/24 via 192.168.1.2 dev br-lan'
A2 = '10.0.0.0/24 via 192.168.1.3 dev br-lan'
B = '10.1.0.0/24 via 192.168.1.4 dev br-lan'
STALE = '10.9.9.0/24 via 192.168.1.5 dev br-lan'
FOREIGN = '10.0.0.0/24 via 9.9.9.9 dev eth9'


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

    def set_main(self, main):
        """Change what `main` describes without touching the table or the record."""
        self.write('tbl-main', main)

    def read(self, name):
        path = self.case / name
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def table(self, table=102):
        return self.read('tbl-%d' % table)

    def owned(self, table=102):
        return self.read('state/owned-%d' % table)

    def owned_bytes(self, table=102):
        return (self.state_dir() / ('owned-%d' % table)).read_bytes()

    def calls(self):
        return self.read('calls.log')

    def reset_calls(self):
        (self.case / 'calls.log').write_text('', encoding='utf-8', newline='\n')

    def ip_calls(self, verb):
        return [line for line in self.calls() if line.startswith('route %s' % verb)]

    def state_dir(self):
        state = self.case / 'state'
        state.mkdir(exist_ok=True)
        return state

    def state_names(self):
        state = self.case / 'state'
        return sorted(path.name for path in state.iterdir()) if state.exists() else []

    def marker(self, name, text=''):
        (self.case / name).write_text(text, encoding='utf-8', newline='\n')

    def unmark(self, name):
        (self.case / name).unlink(missing_ok=True)

    def install(self, main, installed, owned, table=102):
        self.write('tbl-main', main)
        if installed is not None:
            self.write('tbl-%d' % table, installed)
        owned_file = self.state_dir() / ('owned-%d' % table)
        if owned is None:
            owned_file.unlink(missing_ok=True)
        else:
            owned_file.write_text(''.join(line + '\n' for line in owned),
                                  encoding='utf-8', newline='\n')

    def script(self, name='run.sh', lock_wait=None, main_file=None, before='', table=102):
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
            'GM_LOCK_LIB="%s"' % LOCK.as_posix(),
            'export GM_LOCK_LIB',
            '. "$GM_LOCK_LIB"',
        ]
        if lock_wait is not None:
            lines.append('GM_ROUTE_LOCK_WAIT=%s; export GM_ROUTE_LOCK_WAIT' % lock_wait)
        if before:
            lines.append(before)
        lines += [
            '. "%s"' % LIB.as_posix(),
            'geomask_routes_sync %d' % table,
            'printf "RC=%s\\n" "$?"',
        ]
        path = self.case / name
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        return path

    def start(self, path):
        return subprocess.Popen([BASH, path.as_posix()], cwd=str(self.case),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding='utf-8')

    def sync_now(self, table=102, lock_wait=None, main_file=None, before=''):
        """Run the sync against the files as they are now: no fixture is reinstalled."""
        script = self.script(name='run-%d.sh' % table, lock_wait=lock_wait,
                            main_file=main_file, before=before, table=table)
        return subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=120)

    def run_sync(self, main, installed, owned, lock_wait=None, main_file=None, before='',
                 table=102):
        self.install(main, installed, owned, table=table)
        return self.sync_now(table=table, lock_wait=lock_wait, main_file=main_file,
                             before=before)

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
               LOCK.as_posix(),
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
            % (self.case.joinpath('locks').as_posix(), LOCK.as_posix()),
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
               LOCK.as_posix()),
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


class RoutesReconcileTests(_RoutesCase):
    """R12: a refused mutation must never delete the record of a route that stayed installed."""

    def test_a_refused_replace_keeps_the_record_and_the_next_run_repairs_the_table(self):
        """A wanted route whose `replace` is refused stays ours, so a later run can retry it.

        Before R12 the record was rebuilt from the successful replaces only: the refused prefix
        left the record, the next sync called the (still installed) route foreign and the table
        could never be repaired.
        """
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('fail-replace')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertIn(A, self.owned(),
                      'a refused replace dropped the record of our own live route: %r'
                      % (self.owned(),))
        self.assertIn(LAN_CONNECTED, self.owned())
        self.assertEqual(sorted(self.table()), sorted([A, LAN_CONNECTED]),
                         'the old route was removed although the replace failed')
        self.reset_calls()
        self.unmark('fail-replace')
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertTrue(self.ip_calls('replace'),
                        'the second run did not retry the replace: %r' % (self.calls(),))
        self.assertEqual(sorted(self.owned()), sorted(self.table()),
                         'the record and the table disagree after the repair')
        self.assertEqual(sorted(self.table()), sorted([A, LAN_CONNECTED]))

    def test_a_refused_delete_keeps_the_record_and_the_next_run_deletes(self):
        """A refused delete must not become a silent orphan (rc 0, no delete, no record)."""
        self.install([LAN_CONNECTED], [LAN_CONNECTED, STALE], [LAN_CONNECTED, STALE])
        self.marker('fail-del')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertIn(STALE, self.owned(),
                      'a refused delete dropped the record: the route would never be removed')
        self.assertIn(STALE, self.table(), 'the refused delete changed the table anyway')
        self.reset_calls()
        self.unmark('fail-del')
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertNotIn(STALE, self.table(), 'the retried delete did not remove the route')
        self.assertEqual(sorted(self.owned()), [LAN_CONNECTED])
        self.assertIn('full: ' + STALE, self.read('deleted'))

    def test_a_partial_success_keeps_every_prefix_owned_exactly_once(self):
        """One refused prefix must not cost the record of the prefixes that were written."""
        self.install([LAN_CONNECTED, A, B], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('fail-prefix', '10.0.0.0/24\n')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertIn(B, self.table(), 'the successful part of the run was not installed')
        self.assertIn(A, self.table(), 'the old route of the refused prefix was removed')
        self.assertEqual(sorted(self.owned()), sorted(self.table()),
                         'a prefix installed by this run is missing from the record: %r vs %r'
                         % (self.owned(), self.table()))
        self.assertIn(B, self.owned())
        self.reset_calls()
        self.unmark('fail-prefix')
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertEqual(sorted(self.table()), sorted([LAN_CONNECTED, A, B]))
        self.assertEqual(sorted(self.owned()), sorted(self.table()))

    def test_a_refused_record_write_leaves_the_old_file_and_the_next_run_rebuilds(self):
        """A failed write of the record must not truncate it (the old `cp -f` did)."""
        self.install([LAN_CONNECTED, A2], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        before = self.owned_bytes()
        self.marker('fail-cp')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertEqual(self.owned_bytes(), before,
                         'the refused write truncated the record instead of leaving it alone')
        self.assertIn(A2, self.table(), 'the successful replace was not installed')
        self.reset_calls()
        self.unmark('fail-cp')
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertEqual(sorted(self.owned()), sorted(self.table()),
                         'the record was not rebuilt after the failed write: %r vs %r'
                         % (self.owned(), self.table()))
        self.assertIn(A2, self.owned())

    def test_a_foreign_occupant_is_never_adopted_and_only_our_line_is_deleted(self):
        """A foreign route on our prefix is not ours; our own line keeps its record.

        Then `desired` drops the prefix: exactly our line is deleted, the foreign route stays
        and does not enter the record.
        """
        self.install([LAN_CONNECTED, A], [A, FOREIGN, LAN_CONNECTED], [A, LAN_CONNECTED])
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertNotIn(FOREIGN, self.owned(), 'a foreign route was taken into the record')
        self.assertIn(A, self.owned(), 'our own line lost its record because of a foreign occupant')
        self.assertIn(FOREIGN, self.table(), 'the foreign occupant was overwritten')
        self.assertFalse([call for call in self.ip_calls('replace') if ' 10.0.0.0/24 ' in call],
                         'the helper replaced a prefix occupied by a foreign route')
        self.reset_calls()
        self.set_main([LAN_CONNECTED])
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertIn(FOREIGN, self.table(), 'the foreign route was deleted with ours')
        self.assertNotIn(A, self.table(), 'our withdrawn route was left in the table')
        self.assertEqual(self.read('deleted'), ['full: ' + A],
                         'the delete did not name exactly our own line: %r' % (self.read('deleted'),))
        self.assertEqual(sorted(self.owned()), [LAN_CONNECTED],
                         'the record still claims a route that is gone or foreign')

    def test_a_route_removed_outside_is_dropped_from_the_record(self):
        """The record is reconciled with the table, not trusted blindly."""
        self.install([LAN_CONNECTED, A], [LAN_CONNECTED, A], [LAN_CONNECTED, A, STALE])
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertNotIn(STALE, self.owned(),
                         'the record still claims a route that somebody removed externally')
        self.assertEqual(sorted(self.owned()), sorted(self.table()))

    def test_a_refused_table_read_before_any_mutation_is_76_and_keeps_the_record(self):
        self.install([LAN_CONNECTED, A2], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        before = self.owned_bytes()
        self.marker('fail-show')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 76,
                         'an unreadable table before any mutation is the retryable 76')
        self.assertEqual(self.owned_bytes(), before)
        self.assertFalse(self.ip_calls('replace'),
                         'a mutation was attempted without a snapshot of the table')

    def test_a_refused_table_read_after_a_mutation_is_77_and_keeps_the_conservative_record(self):
        self.install([LAN_CONNECTED, A2], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        before = self.owned_bytes()
        self.marker('fail-show-after-mutation')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 77,
                         'a read failure after the mutation must not be retried as 76')
        self.assertEqual(self.owned_bytes(), before,
                         'the record was rewritten although the installed table could not be read')
        self.assertIn(A2, self.table())

    def test_a_failure_in_one_table_does_not_change_another_tables_record(self):
        other = '10.5.0.0/24 via 192.168.1.7 dev br-lan'
        self.write('tbl-100', [LAN_CONNECTED, other])
        other_owned = self.state_dir() / 'owned-100'
        other_owned.write_text(LAN_CONNECTED + '\n' + other + '\n',
                               encoding='utf-8', newline='\n')
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('fail-replace')
        result = self.sync_now(table=102)
        self.assertEqual(self.rc(result), 77, result.stdout + result.stderr)
        self.assertEqual(self.owned(100), [LAN_CONNECTED, other],
                         'the record of table 100 was changed by a table-102 failure')
        self.assertEqual(self.table(100), [LAN_CONNECTED, other])
        self.assertFalse([call for call in self.calls() if 'table 100' in call],
                         'a table-102 sync touched table 100: %r' % (self.calls(),))

    def test_a_truncated_record_line_is_dropped_without_deleting_anything(self):
        """A half-written record line is not a route: it must not be trusted or acted upon."""
        garbage = '10.0.0.0/24 via 192.168'
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [LAN_CONNECTED, A, garbage])
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertNotIn(garbage, self.owned(),
                         'a truncated record line survived the reconcile')
        self.assertEqual(sorted(self.owned()), sorted(self.table()))
        self.assertFalse([line for line in self.read('deleted') if '192.168' in line],
                         'the helper tried to delete a route it never installed')

    def test_a_wiped_record_is_rebuilt_from_the_installed_table(self):
        """`/tmp` is tmpfs: after it is wiped the table still holds our routes.

        A line that is exactly what `desired` wants is the installed form of our own route
        (nothing else could install that exact line), so the sync may re-record it instead of
        treating the whole prefix as foreign forever.
        """
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], None)
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertEqual(sorted(self.owned()), sorted(self.table()),
                         'a wiped record was not rebuilt: %r vs %r'
                         % (self.owned(), self.table()))
        self.assertIn(A, self.owned())

    def test_a_foreign_line_that_appears_after_replace_is_not_recorded(self):
        """R12 D1: a same-prefix race must not put a foreign route into our record.

        The other writer installs `10.0.0.0/24 via 9.9.9.9 dev eth9` between our successful
        `replace` and the read-back. The record may only claim lines that are structurally our
        `desired` route, so the foreign line stays out; later it is not deleted with ours.

        `desired` is ordered A first so the race is injected by A's replace; LAN's replace then
        rewrites only its own prefix and leaves the foreign line in place.
        """
        self.install([A, LAN_CONNECTED], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('race-foreign', FOREIGN + '\n')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 0, first.stdout + first.stderr)
        self.assertIn(FOREIGN, self.table(), 'the foreign route that raced us is not installed')
        self.assertNotIn(FOREIGN, self.owned(),
                         'a foreign route added between replace and read-back entered the record')
        self.assertEqual(sorted(self.owned()), sorted([A, LAN_CONNECTED]))
        self.set_main([LAN_CONNECTED])
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertIn(FOREIGN, self.table(), 'the foreign route was deleted with our record')
        self.assertNotIn(A, self.table(), 'our withdrawn route was left in the table')
        self.assertEqual(sorted(self.owned()), [LAN_CONNECTED])

    def test_a_lost_record_is_rebuilt_even_when_the_prefix_has_a_foreign_route(self):
        """R12 D2: our byte-identical desired line returns to the record without a replace.

        The record is gone, our desired line and a foreign route share the prefix, so Phase 1
        refuses the replace. Phase 4 must still claim our line (it is structurally `desired`)
        and must not claim the foreign one.
        """
        self.install([LAN_CONNECTED, A], [A, FOREIGN, LAN_CONNECTED], None)
        result = self.sync_now()
        self.assertEqual(self.rc(result), 77,
                         'a foreign occupant still blocks the replace (77)')
        self.assertIn(A, self.owned(),
                      'our byte-identical desired line was not returned to the record')
        self.assertNotIn(FOREIGN, self.owned(), 'the foreign route was claimed')
        self.assertIn(A, self.table())
        self.assertIn(FOREIGN, self.table())

    def test_an_empty_read_back_does_not_erase_the_record(self):
        """R12 D3: a successful but empty `ip route show` after our mutations is not truth."""
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        before = self.owned_bytes()
        self.marker('empty-after')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 77,
                         'an empty read-back after a mutation must not be recorded as success')
        self.assertEqual(self.owned_bytes(), before,
                         'the record was erased by an empty read-back')
        self.assertIn(A, self.table(), 'the mutations of the run were reverted')

    def test_a_kernel_normalised_installed_line_stays_owned(self):
        """A decoration (`proto static metric 10`) is not a different route (structural match)."""
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('normalize')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        decorated = A + ' proto static metric 10'
        self.assertIn(decorated, self.table())
        self.assertIn(decorated, self.owned(),
                      'the normalised installed form of our own route was not recorded')
        self.assertEqual(sorted(self.owned()), sorted(self.table()))

    def test_a_successful_sync_clears_a_retry_marker(self):
        """R12 D5: the console/panel calls the sync directly, not through the worker."""
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        retry = self.state_dir() / 'retry-102'
        retry.write_text('1.1\n', encoding='utf-8', newline='\n')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertFalse(retry.exists(),
                         'a successful direct sync left a retry marker behind')
        retry.write_text('1.1\n', encoding='utf-8', newline='\n')
        self.marker('fail-replace')
        bad = self.sync_now()
        self.assertEqual(self.rc(bad), 77, bad.stdout + bad.stderr)
        self.assertTrue(retry.exists(),
                        'a failed sync consumed the retry marker of the next attempt')

    def test_a_decorated_stale_route_is_deleted_by_the_installed_line(self):
        """A kernel-normalised record line must still be found, and deleted as installed.

        `owned` holds the raw line, the kernel prints `proto static metric 10`. A refused delete
        keeps the record; the next sync deletes the ACTUAL installed line, so no orphan stays.
        """
        decorated = STALE + ' proto static metric 10'
        self.install([LAN_CONNECTED], [LAN_CONNECTED, decorated], [LAN_CONNECTED, STALE])
        self.marker('fail-del')
        first = self.sync_now()
        self.assertEqual(self.rc(first), 77, first.stdout + first.stderr)
        self.assertTrue([line for line in self.owned() if '10.9.9.0/24' in line],
                        'a refused delete lost the record of the decorated line: %r'
                        % (self.owned(),))
        self.assertIn(decorated, self.table())
        self.reset_calls()
        self.unmark('fail-del')
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertNotIn(decorated, self.table(),
                         'the normalised installed line was not deleted (silent orphan)')
        self.assertIn('full: ' + decorated, self.read('deleted'),
                      'the delete did not name the installed line: %r'
                      % (self.read('deleted'),))
        self.assertEqual(sorted(self.owned()), [LAN_CONNECTED])

    def test_a_changed_route_repairs_a_decorated_old_form(self):
        """A kernel-normalised old form is still ours: the changed desired line replaces it."""
        decorated = A + ' proto static metric 10'
        self.install([LAN_CONNECTED, A2], [decorated, LAN_CONNECTED], [A, LAN_CONNECTED])
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertIn(A2, self.table(), 'the changed route was never installed')
        self.assertNotIn(decorated, self.table(), 'the decorated old form survived the replace')
        self.assertEqual(sorted(self.owned()), sorted(self.table()))
        self.assertIn(A2, self.owned())

    def test_a_foreign_line_with_a_foreign_src_is_not_recorded(self):
        """`src` is part of the identity: a route with another source address is not ours."""
        src_foreign = '10.0.0.0/24 via 192.168.1.2 dev br-lan src 9.9.9.9'
        self.install([A, LAN_CONNECTED], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('race-foreign', src_foreign + '\n')
        result = self.sync_now()
        self.assertEqual(self.rc(result), 0, result.stdout + result.stderr)
        self.assertIn(src_foreign, self.table(), 'the raced line is not installed')
        self.assertNotIn(src_foreign, self.owned(),
                         'a foreign line with a different src was recorded as ours')
        self.assertEqual(sorted(self.owned()), sorted([A, LAN_CONNECTED]))
        self.set_main([LAN_CONNECTED])
        second = self.sync_now()
        self.assertEqual(self.rc(second), 0, second.stdout + second.stderr)
        self.assertIn(src_foreign, self.table(),
                      'the foreign line was deleted together with our record')
        self.assertEqual(sorted(self.owned()), [LAN_CONNECTED])

    def test_no_owned_temp_survives_success_or_failure(self):
        self.install([LAN_CONNECTED, A2], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        ok = self.sync_now()
        self.assertEqual(self.rc(ok), 0, ok.stdout + ok.stderr)
        self.assertEqual(self.state_names(), ['owned-102'],
                         'a sync temp survived a successful run: %r' % (self.state_names(),))
        self.install([LAN_CONNECTED, A], [A, LAN_CONNECTED], [A, LAN_CONNECTED])
        self.marker('fail-replace')
        bad = self.sync_now()
        self.assertEqual(self.rc(bad), 77, bad.stdout + bad.stderr)
        self.assertEqual(self.state_names(), ['owned-102'],
                         'a sync temp survived a failed run: %r' % (self.state_names(),))


class RouteRetryMarkerTests(_RoutesCase):
    """R12: a parked 77 leaves a marker that ONE later drain turns into a pending event.

    The marker must not look like queue work (`_gm_route_has_queue`), or an ifup would spawn a
    worker for it; and it must survive until a drain, because a stable link sends no new ifup.
    """

    def test_a_parked_77_leaves_a_retry_marker_that_a_drain_converts_once(self):
        script = self.case / 'retry.sh'
        body = r'''set -u
CASE="@CASE@"
export CASE
. "$CASE/stubs.sh"
GM_ROUTE_STATE_DIR="$CASE/state"
GM_LOCK_DIR="$CASE/locks"
GM_LOCK_LIB="@LOCK@"
export GM_ROUTE_STATE_DIR GM_LOCK_DIR GM_LOCK_LIB
mkdir -p "$CASE/state" "$CASE/locks"
. "$GM_LOCK_LIB"
. "@LIB@"
GM_ROUTE_ATTEMPTS=1
GM_ROUTE_ACTIVATIONS=1
GM_ROUTE_LOCK_WAIT=0
export GM_ROUTE_ATTEMPTS GM_ROUTE_ACTIVATIONS GM_ROUTE_LOCK_WAIT
printf 'gen-1\n' > "$CASE/state/pending-102"
# Table 102's activation waits for its console TUN: provide it, so the retried event can
# actually succeed in the drain instead of being deferred as 79.
: > "$CASE/tun-Console"
geomask_routes_sync() { return 77; }
gm_route_worker
printf 'WORKER_RC=%s\n' "$?"
_gm_route_has_queue; printf 'HAS_QUEUE=%s\n' "$?"
[ -f "$CASE/state/retry-102" ] && printf 'RETRY1=yes\n' || printf 'RETRY1=no\n'
geomask_routes_sync() { return 0; }
gm_route_drain
printf 'DRAIN_RC=%s\n' "$?"
[ -f "$CASE/state/retry-102" ] && printf 'RETRY2=yes\n' || printf 'RETRY2=no\n'
[ -f "$CASE/state/pending-102" ] && printf 'PENDING2=yes\n' || printf 'PENDING2=no\n'
[ -f "$CASE/state/parked-102" ] && printf 'PARKED2=yes\n' || printf 'PARKED2=no\n'
'''
        script.write_text(body.replace('@CASE@', self.case.as_posix())
                              .replace('@LOCK@', LOCK.as_posix())
                              .replace('@LIB@', LIB.as_posix()),
                          encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=120)
        out = result.stdout + result.stderr
        self.assertIn('WORKER_RC=77', result.stdout, out)
        self.assertIn('RETRY1=yes', result.stdout,
                      'a parked 77 left no retry marker: a stable link would never reconcile')
        self.assertIn('HAS_QUEUE=1', result.stdout,
                      'the retry marker was reported as queue work (hotplug would spawn a worker)')
        self.assertIn('DRAIN_RC=0', result.stdout, out)
        self.assertIn('RETRY2=no', result.stdout, 'the drain did not consume the retry marker')
        self.assertIn('PENDING2=no', result.stdout, 'an extra pending event was left behind')
        self.assertIn('PARKED2=no', result.stdout, 'the parked marker survived the re-attempt')


    def test_a_refused_retry_conversion_keeps_the_marker(self):
        """R12 D4: `mv` refusals (EACCES/ENOSPC) must not lose the retry.

        The rename is refused for every `retry-` path: the park must still leave the marker
        (in-place fallback), the drain must keep it when the conversion is refused, and the
        next drain must convert it once the filesystem recovers.
        """
        script = self.case / 'retry-mv.sh'
        body = r'''set -u
CASE="@CASE@"
export CASE
. "$CASE/stubs.sh"
GM_ROUTE_STATE_DIR="$CASE/state"
GM_LOCK_DIR="$CASE/locks"
GM_LOCK_LIB="@LOCK@"
export GM_ROUTE_STATE_DIR GM_LOCK_DIR GM_LOCK_LIB
mkdir -p "$CASE/state" "$CASE/locks"
. "$GM_LOCK_LIB"
. "@LIB@"
GM_ROUTE_ATTEMPTS=1
GM_ROUTE_ACTIVATIONS=1
GM_ROUTE_LOCK_WAIT=0
export GM_ROUTE_ATTEMPTS GM_ROUTE_ACTIVATIONS GM_ROUTE_LOCK_WAIT
printf 'gen-1\n' > "$CASE/state/pending-102"
: > "$CASE/tun-Console"
# Every atomic rename of a retry marker is refused from here on.
: > "$CASE/fail-retry-mv"
geomask_routes_sync() { return 77; }
gm_route_worker
printf 'WORKER_RC=%s\n' "$?"
[ -f "$CASE/state/retry-102" ] && printf 'RETRY1=yes\n' || printf 'RETRY1=no\n'
geomask_routes_sync() { return 0; }
gm_route_drain
printf 'DRAIN1_RC=%s\n' "$?"
[ -f "$CASE/state/retry-102" ] && printf 'RETRY2=yes\n' || printf 'RETRY2=no\n'
[ -f "$CASE/state/pending-102" ] && printf 'PENDING2=yes\n' || printf 'PENDING2=no\n'
rm -f "$CASE/fail-retry-mv"
gm_route_drain
printf 'DRAIN2_RC=%s\n' "$?"
[ -f "$CASE/state/retry-102" ] && printf 'RETRY3=yes\n' || printf 'RETRY3=no\n'
'''
        script.write_text(body.replace('@CASE@', self.case.as_posix())
                              .replace('@LOCK@', LOCK.as_posix())
                              .replace('@LIB@', LIB.as_posix()),
                          encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, script.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=120)
        out = result.stdout + result.stderr
        self.assertIn('RETRY1=yes', result.stdout, out)
        self.assertIn('DRAIN1_RC=0', result.stdout, out)
        self.assertIn('RETRY2=yes', result.stdout,
                      'a refused retry->pending conversion deleted the retry marker')
        self.assertIn('PENDING2=no', result.stdout,
                      'the refused conversion left a half-written pending event')
        self.assertIn('DRAIN2_RC=0', result.stdout, out)
        self.assertIn('RETRY3=no', result.stdout,
                      'the marker was not converted after the filesystem recovered')


if __name__ == '__main__':
    unittest.main()
