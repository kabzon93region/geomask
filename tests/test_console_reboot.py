"""The console lane after a reboot: the ifup event is queued, the guard fills table 102.

Review 14-15.11.2026, point 9. The policy rule sends the console device into table 102, and
after a reboot that table starts empty. The guard put back the rule and *a* default, but never
mirrored the LAN prefixes into the table, and its check was `grep '^default'`: the WAN default
netifd restores for every table (via 192.168.1.1, dev br-lan) passed it, so the console kept
leaving with the real address and lost the local network at the same time.

R05 moved the ifup work into the route queue: the hotplug script only records
`pending-102` when the panel says the lane is on, and the worker/drain fills the table. The
guard's own console block still repairs the rule and the default every 5 minutes.

Every command is a stub inside the test's temporary directory - no router, no `/etc`, no host
`/tmp`.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
HOTPLUG = ROOT / 'assets' / '99-default-vpn-route'
LOCKLIB = ROOT / 'assets' / 'geomask-lock.sh'
ROUTES = ROOT / 'assets' / 'geomask-routes.sh'
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


def q(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def guard_block():
    start = GUARD_TEXT.index(GUARD_START)
    end = GUARD_TEXT.index(GUARD_END, start)
    return GUARD_TEXT[start:end]


IP_STUB = r'''ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$1 $2" in
    'link show')
      case "$3" in Wireguard|AI|pppoe-wan) return 1 ;; *) return 0 ;; esac ;;
    'rule show')
      cat "$CASE/rules" 2>/dev/null; return 0 ;;
    'rule add')
      printf '%s\n' "$*" >> "$CASE/rules"; return 0 ;;
    'rule del')
      return 1 ;;
    'route show')
      _tbl=main
      [ "${3:-}" = table ] && _tbl=$4
      cat "$CASE/tbl-$_tbl" 2>/dev/null
      return 0 ;;
    'route replace')
      shift 2
      _tbl=main
      if [ "${1:-}" = table ]; then _tbl=$2; shift 2; fi
      _line="$*"
      _pfx=${_line%% *}
      if [ -f "$CASE/tbl-$_tbl" ]; then
        awk -v p="$_pfx" '$1 != p' "$CASE/tbl-$_tbl" > "$CASE/tbl-$_tbl.new"
        mv -f "$CASE/tbl-$_tbl.new" "$CASE/tbl-$_tbl"
      fi
      printf '%s\n' "$_line" >> "$CASE/tbl-$_tbl"
      return 0 ;;
    *) return 0 ;;
  esac
}
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
'''

# The shared route helper, replaced by a recorder: these guard tests are about *whether* the
# table is synced, not about the helper's ownership rules (test_routes_ownership.py owns those).
ROUTES_STUB = r'''geomask_routes_sync() {
  printf '%s\n' "$1" >> "$CASE/syncs"
  return 0
}
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleRebootTests(unittest.TestCase):
    """The guard block: rule re-seat and a default that is really ours."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        for name in ('calls.log', 'rules', 'syncs', 'logger.log'):
            (self.case / name).write_text('', encoding='utf-8', newline='\n')
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        self.lib = self.case / 'routes-stub.sh'
        self.lib.write_text(ROUTES_STUB, encoding='utf-8', newline='\n')

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def environment(self):
        return (
            'CASE="%s"\n'
            'GM_CONSOLE_STATE="%s"\n'
            'GM_CONSOLE_LOCK="%s/console.lock"\n'
            'GM_ROUTES_LIB="%s"\n'
            'CROUTES="$GM_ROUTES_LIB"\n'
            'GM_CONSOLE_TUN="Console"\n'
            'GM_LOCK_DIR="%s/locks"\n'
            'GM_LOCK_LIB="%s"\n'
            'export CASE GM_CONSOLE_STATE GM_CONSOLE_LOCK GM_ROUTES_LIB GM_CONSOLE_TUN GM_LOCK_DIR GM_LOCK_LIB\n'
            'logger() { printf "%%s\n" "$*" >> "$CASE/logger.log"; }\n'
            'conntrack() { return 0; }\n'
            % (win(self.case), win(self.state), win(self.case), win(self.lib),
               win(self.case), win(LOCKLIB))
        )

    def run_script(self, body, prefix=''):
        script = self.write('run.sh', 'set -u\n' + self.environment() + IP_STUB
                            + prefix + '\n' + body + '\n')
        return subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=120)

    def calls(self):
        return [line for line in (self.case / 'calls.log').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def syncs(self):
        return [line for line in (self.case / 'syncs').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def table(self, number):
        path = self.case / ('tbl-%s' % number)
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_the_guard_restores_the_lan_prefixes_in_table_102(self):
        self.write('tbl-102', 'default dev Console')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('102', self.syncs(),
                      'the watchdog re-seated the rule but left the table without the LAN')

    def test_the_guard_replaces_a_wan_default_in_table_102(self):
        """`grep '^default'` accepted the WAN default netifd restores; it is not our tunnel."""
        self.write('tbl-102', 'default via 192.168.1.1 dev br-lan')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.table(102), ['default dev Console'],
                         'the WAN default survived: the console leaves with the real address')
        self.assertIn('console table 102 default restored',
                      (self.case / 'logger.log').read_text(encoding='utf-8'))

    def test_the_guard_keeps_a_correct_default_and_never_replaces_it(self):
        self.write('tbl-102', 'default dev Console')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('route replace table 102', '\n'.join(self.calls()),
                         'the correct default was rewritten on every watchdog run')


@unittest.skipIf(BASH is None, 'bash is required')
class HotplugQueueTests(unittest.TestCase):
    """R05: ifup only queues the console table; it does not run ip itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'state').mkdir()
        (self.case / 'locks').mkdir()
        for name in ('calls.log', 'rules'):
            (self.case / name).write_text('', encoding='utf-8', newline='\n')
        (self.case / 'tbl-100').write_text('', encoding='utf-8')
        (self.case / 'tbl-102').write_text('', encoding='utf-8')
        (self.case / 'mihomo.yaml').write_text('tun:\n  device: Mihomo\n', encoding='utf-8')
        (self.case / 'devices.list').write_text('', encoding='utf-8')
        (self.case / 'console.state').write_text('enabled=1\nip=192.168.1.177\n',
                                                 encoding='utf-8')
        (self.case / 'stubs.sh').write_text(IP_STUB, encoding='utf-8', newline='\n')

    def hold_worker(self):
        script = self.case / 'hold.sh'
        script.write_text(
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire routes-worker 0 || exit 9\n'
            'printf held > %s\nsleep 20\n'
            % (q(win(self.case / 'locks')), q(win(LOCKLIB)), q(win(self.case / 'held'))),
            encoding='utf-8', newline='\n')
        proc = subprocess.Popen([BASH, win(script)], cwd=str(self.case))
        self.addCleanup(reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return
            time.sleep(0.05)
        self.fail('the worker-lock holder never started')

    def run_hotplug(self, enabled=True):
        if not enabled:
            (self.case / 'console.state').write_text('enabled=0\n', encoding='utf-8')
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
            % (q(win(self.case)), q(win(LOCKLIB)), q(win(ROUTES)), q(win(HOTPLUG))),
            encoding='utf-8', newline='\n')
        return subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def pending(self):
        return sorted(p.name for p in (self.case / 'state').glob('pending-*'))

    def test_ifup_queues_the_console_table_when_the_lane_is_on(self):
        self.hold_worker()
        result = self.run_hotplug(enabled=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('pending-102', self.pending(),
                      'the console event was not queued: %r' % (self.pending(),))
        calls = '\n'.join((self.case / 'calls.log').read_text(encoding='utf-8').splitlines())
        self.assertNotIn('route replace', calls, 'ifup mutated the table itself')

    def test_ifup_leaves_the_console_table_alone_while_the_lane_is_off(self):
        self.hold_worker()
        result = self.run_hotplug(enabled=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('pending-102', self.pending(),
                         'a switched-off lane still queued its table')
        self.assertNotIn('table 102', '\n'.join(
            (self.case / 'calls.log').read_text(encoding='utf-8').splitlines()))


def reap(proc):
    try:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.2)
    except Exception:
        pass


if __name__ == '__main__':
    unittest.main()
