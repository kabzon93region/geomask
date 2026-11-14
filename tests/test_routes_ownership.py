"""Route ownership: only entries recorded in `owned` may be replaced or removed.

Defect (plan 21): the sync compared the *desired* line with the owned file and deleted by
prefix. So when our own route on a prefix changed A->B, B was skipped (it was not in
`owned` yet) and A was removed by `ip route del table N <prefix>`, which cannot tell two
entries of the same prefix apart - a foreign route on that prefix went with it.

The helper is run as shipped against a stateful `ip` stub. The stub models the pessimistic
reading of iproute2: `ip route del <prefix>` drops every entry of that prefix, while
`ip route del <prefix> dev ... via ...` drops exactly one line. That is precisely why the
helper must never delete by prefix, so the tests assert on the recorded calls as well.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
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
      printf '    inet 192.168.1.1/24 brd 192.168.1.255 scope global %s\n' "$4"
      return 0 ;;
    link) return 1 ;;       # no Wireguard interface in this case
    route) ;;
    *) return 0 ;;
  esac
  shift
  _cmd=$1; shift
  _tbl=main; _pfx=''
  while [ $# -gt 0 ]; do
    case "$1" in
      table) _tbl=$2; shift 2 ;;
      exact) _pfx=$2; shift 2 ;;
      *) break ;;
    esac
  done
  _f="$CASE/tbl-$_tbl"
  case "$_cmd" in
    show)
      if [ -n "$_pfx" ]; then awk -v p="$_pfx" '$1 == p' "$_f"
      else cat "$_f"; fi
      return 0 ;;
    replace)
      _line=$*
      awk -v p="${_line%% *}" '$1 != p' "$_f" > "$_f.new"
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
class RoutesOwnershipTests(unittest.TestCase):
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
        return [line for line in (self.case / name).read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def run_sync(self, main, installed, owned):
        self.write('tbl-main', main)
        self.write('tbl-102', installed)
        state = self.case / 'state'
        state.mkdir(exist_ok=True)
        owned_file = state / 'owned-102'
        if owned is None:
            owned_file.unlink(missing_ok=True)
        else:
            owned_file.write_text(''.join(line + '\n' for line in owned),
                                  encoding='utf-8', newline='\n')
        script = '\n'.join([
            'set -u',
            'CASE="%s"' % self.case.as_posix(),
            'export CASE',
            '. "$CASE/stubs.sh"',
            'GM_ROUTE_STATE_DIR="$CASE/state"',
            'export GM_ROUTE_STATE_DIR',
            '. "%s"' % LIB.as_posix(),
            'geomask_routes_sync 102',
            'printf "RC=%s\\n" "$?"',
        ]) + '\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, path.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        return result

    def rc(self, result):
        self.assertIn('RC=', result.stdout, result.stdout + result.stderr)
        return int(result.stdout.rsplit('RC=', 1)[1].split()[0])

    def table(self):
        return self.read('tbl-102')

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
        """A wanted prefix taken by a foreign route: keep it, refuse to install, report rc=1."""
        wanted = '10.0.0.0/24 via 192.168.1.3 dev br-lan'
        foreign = '10.0.0.0/24 via 9.9.9.9 dev eth9'
        result = self.run_sync(main=[LAN_CONNECTED, wanted], installed=[foreign], owned=None)
        self.assertEqual(self.rc(result), 1, 'a table that does not match `desired` is a failure')
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


if __name__ == '__main__':
    unittest.main()
